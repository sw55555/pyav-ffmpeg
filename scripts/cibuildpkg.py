# Utilities for building native library inside cibuildwheel

import contextlib
import os
import platform
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field, replace


def log_print(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def run(cmd: list[str], env: dict[str, str] | None = None) -> None:
    log_print(f"- Running: {cmd}")
    subprocess.run(cmd, check=True, env=env)


def fetch(url: str, path: str) -> None:
    run(["curl", "-L", "-o", path, url])


def get_platform() -> str:
    """
    Get the current platform tag.
    """
    system = platform.system()
    machine = platform.machine()
    if system == "Linux":
        return f"manylinux_{machine}"
    elif system == "Darwin":
        # cibuildwheel sets ARCHFLAGS:
        # https://github.com/pypa/cibuildwheel/blob/5255155bc57eb6224354356df648dc42e31a0028/cibuildwheel/macos.py#L207-L220
        machine = os.environ["ARCHFLAGS"].split()[1]
        return f"macosx_{machine}"
    elif system == "Windows":
        if struct.calcsize("P") * 8 == 64:
            return "win_amd64"
        else:
            return "win32"
    else:
        raise Exception(f"Unsupported system {system}")


@contextlib.contextmanager
def chdir(path: str) -> Iterator[None]:
    """
    Changes to a directory and returns to the original directory at exit.
    """
    cwd = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(cwd)


@contextlib.contextmanager
def log_group(title: str) -> Iterator[None]:
    """
    Starts a log group and ends it at exit.
    """
    start_time = time.time()
    success = False
    log_print(f"::group::{title}")
    try:
        yield
        success = True
    finally:
        duration = time.time() - start_time
        outcome = "ok" if success else "failed"
        start_color = "\033[32m" if success else "\033[31m"
        end_color = "\033[0m"
        log_print("::endgroup::")
        log_print(f"{start_color}{outcome}{end_color} {duration:.2f}s".rjust(78))


def make_args(*, parallel: bool) -> list[str]:
    """
    Arguments for GNU make.
    """
    args = []

    # do not parallelize build when running in qemu
    if parallel and platform.machine() not in ("aarch64", "ppc64le", "s390x"):
        args.append("-j")

    return args


def prepend_env(env: dict[str, str], name: str, new: str, separator: str = " ") -> None:
    old = env.get(name)
    if old:
        env[name] = new + separator + old
    else:
        env[name] = new


@dataclass(slots=True)
class Package:
    name: str
    source_url: str
    build_system: str = "autoconf"
    build_arguments: list[str] = field(default_factory=list)
    build_dir: str = "build"
    build_parallel: bool = True
    requires: list[str] = field(default_factory=list)  # for docs only
    fflags: str = ""
    source_dir: str = ""
    source_filename: str = ""
    source_strip_components: int = 1
    gpl: bool = False  # As opposed to the LGPL or other license.


class Builder:
    def __init__(self, dest_dir: str):
        self._builder_dest_dir = dest_dir + ".builder"
        self._target_dest_dir = dest_dir

        self.build_dir = os.path.abspath("build")
        self.patch_dir = os.path.abspath("patches")
        self.source_dir = os.path.abspath("source")

    def build(self, package: Package, *, for_builder: bool = False) -> None:
        # if the package is already installed, do nothing
        installed_dir = os.path.join(
            self._prefix(for_builder=for_builder), "var", "lib", "cibuildpkg"
        )
        installed_file = os.path.join(installed_dir, package.name)
        if os.path.exists(installed_file):
            return

        with log_group(f"build {package.name}"):
            if package.name == "x265":
                self._build_x265(package, for_builder=for_builder)
            elif package.build_system == "cmake":
                self._build_with_cmake(package, for_builder=for_builder)
            elif package.build_system == "meson":
                self._build_with_meson(package, for_builder=for_builder)
            else:
                self._build_with_autoconf(package, for_builder=for_builder)

        # mark package as installed
        os.makedirs(installed_dir, exist_ok=True)
        with open(installed_file, "w") as fp:
            fp.write("installed\n")

    def create_directories(self) -> None:
        # print debugging information
        if platform.system() == "Darwin":
            log_print("Environment variables")
            for var in ("ARCHFLAGS", "MACOSX_DEPLOYMENT_TARGET"):
                log_print(f" - {var}: {os.environ[var]}")

        # delete build directory
        if os.path.exists(self.build_dir):
            shutil.rmtree(self.build_dir)

        # create directories
        for d in (self.build_dir, self.source_dir):
            os.makedirs(d, exist_ok=True)

        # add tools to PATH
        prepend_env(
            os.environ,
            "PATH",
            os.path.join(self._builder_dest_dir, "bin"),
            separator=";" if platform.system() == "Windows" else ":",
        )

    def _build_with_autoconf(self, package: Package, for_builder: bool) -> None:
        assert package.build_system == "autoconf"
        package_path = os.path.join(self.build_dir, package.name)
        package_source_path = os.path.join(package_path, package.source_dir)
        package_build_path = os.path.join(package_path, package.build_dir)

        # update config.guess and config.sub
        config_files = ("config.guess", "config.sub")
        for root, dirs, files in os.walk(package_path):
            for name in filter(lambda x: x in config_files, files):
                script_path = os.path.join(root, name)
                cache_path = os.path.join(self.source_dir, name)
                if not os.path.exists(cache_path):
                    fetch(
                        "https://git.savannah.gnu.org/cgit/config.git/plain/" + name,
                        cache_path,
                    )
                shutil.copy(cache_path, script_path)
                os.chmod(script_path, 0o755)

        # determine configure arguments
        env = self._environment(for_builder=for_builder)
        prefix = self._prefix(for_builder=for_builder)
        configure_args = [
            "--disable-static",
            "--enable-shared",
            "--libdir=" + self._mangle_path(os.path.join(prefix, "lib")),
            "--prefix=" + self._mangle_path(prefix),
        ]
        darwin_arm64_cross = (
            platform.system() == "Darwin"
            and not for_builder
            and os.environ["ARCHFLAGS"] == "-arch arm64"
        )

        if package.name == "vpx":
            if darwin_arm64_cross:
                # darwin20 is the first darwin that supports arm64 macs
                configure_args += ["--target=arm64-darwin20-gcc"]
            elif platform.system() == "Darwin":
                # darwin13 matches the macos 10.9 target used by cibuildwheel:
                # https://cibuildwheel.readthedocs.io/en/stable/cpp_standards/#macos-and-deployment-target-versions
                configure_args += ["--target=x86_64-darwin13-gcc"]
            elif platform.system() == "Windows":
                configure_args += ["--target=x86_64-win64-gcc"]
        elif darwin_arm64_cross:
            # AC_FUNC_MALLOC and AC_FUNC_REALLOC fail when cross-compiling
            env["ac_cv_func_malloc_0_nonnull"] = "yes"
            env["ac_cv_func_realloc_0_nonnull"] = "yes"

            if package.name == "ffmpeg":
                configure_args += ["--arch=arm64", "--enable-cross-compile"]
            else:
                configure_args += [
                    "--build=x86_64-apple-darwin",
                    "--host=aarch64-apple-darwin",
                ]

        # build package
        os.makedirs(package_build_path, exist_ok=True)
        with chdir(package_build_path):
            run(
                [
                    "sh",
                    self._mangle_path(os.path.join(package_source_path, "configure")),
                ]
                + configure_args
                + package.build_arguments,
                env=env,
            )
            run(
                ["make"] + make_args(parallel=package.build_parallel) + ["V=1"], env=env
            )
            run(["make", "install"], env=env)

    def _build_with_cmake(self, package: Package, for_builder: bool) -> None:
        assert package.build_system == "cmake"
        package_path = os.path.join(self.build_dir, package.name)
        package_source_path = os.path.join(package_path, package.source_dir)
        package_build_path = os.path.join(package_path, package.build_dir)

        # determine cmake arguments
        env = self._environment(for_builder=for_builder)
        prefix = self._prefix(for_builder=for_builder)
        cmake_args = [
            "-GUnix Makefiles",
            "-DBUILD_SHARED_LIBS=1",
            "-DCMAKE_INSTALL_LIBDIR=lib",
            "-DCMAKE_INSTALL_PREFIX=" + prefix,
        ]
        if platform.system() == "Darwin":
            cmake_args.append("-DCMAKE_INSTALL_NAME_DIR=" + os.path.join(prefix, "lib"))
            if not for_builder and os.environ["ARCHFLAGS"] == "-arch arm64":
                cmake_args += [
                    "-DCMAKE_OSX_ARCHITECTURES=arm64",
                    "-DCMAKE_SYSTEM_NAME=Darwin",
                    "-DCMAKE_SYSTEM_PROCESSOR=arm64",
                ]

        # build package
        os.makedirs(package_build_path, exist_ok=True)
        with chdir(package_build_path):
            run(
                ["cmake", package_source_path] + cmake_args + package.build_arguments,
                env=env,
            )
            run(
                ["cmake", "--build", ".", "--verbose"]
                + make_args(parallel=package.build_parallel),
                env=env,
            )
            run(["cmake", "--install", "."], env=env)

    def _build_with_meson(self, package: Package, for_builder: bool) -> None:
        assert package.build_system == "meson"
        package_path = os.path.join(self.build_dir, package.name)
        package_source_path = os.path.join(package_path, package.source_dir)
        package_build_path = os.path.join(package_path, package.build_dir)

        # determine meson arguments
        env = self._environment(for_builder=for_builder)
        prefix = self._prefix(for_builder=for_builder)
        meson_args = ["--libdir=lib", "--prefix=" + prefix]
        if (
            platform.system() == "Darwin"
            and not for_builder
            and os.environ["ARCHFLAGS"] == "-arch arm64"
        ):
            cross_file = os.path.join(package_path, "meson.cross")
            with open(cross_file, "w") as fp:
                fp.write(
                    """[binaries]
c = 'cc'
cpp = 'c++'

[host_machine]
system = 'darwin'
cpu_family = 'aarch64'
cpu = 'aarch64'
endian = 'little'
"""
                )
            meson_args.append("--cross-file=" + cross_file)

        # build package
        os.makedirs(package_build_path, exist_ok=True)
        with chdir(package_build_path):
            run(
                ["meson", package_source_path] + meson_args + package.build_arguments,
                env=env,
            )
            run(["ninja", "--verbose"], env=env)
            run(["ninja", "install"], env=env)

    def _build_x265(self, package: Package, for_builder: bool) -> None:
        assert package.name == "x265"
        assert len(package.build_arguments) == 0

        # Build x265 three times:
        #  1: Build 12 bits static library version
        #  2: Build 10 bits static library version
        #  3: Build 8 bits shared library, linking also 10 and 12 bits
        # This last version will support 8, 10 and 12 bits pixel formats
        package_path = os.path.join(self.build_dir, package.name)

        # self._build_with_cmake always install, install intermediate
        # builds in dummy directory
        dummy_install_path = os.path.join(package_path, "dummy_install_path")

        # For 10/12 bits version, only x86_64 has assembly instructions available
        flags_high_bits = []
        platform = get_platform()
        if not ("x86_64" in platform or "amd64" in platform):
            flags_high_bits.append("-DENABLE_ASSEMBLY=0")
            flags_high_bits.append("-DENABLE_ALTIVEC=0")

        x265_12bits = replace(
            package,
            build_dir="x265-12bits",
            build_arguments=[
                "-DHIGH_BIT_DEPTH=1",
                "-DMAIN12=1",
                "-DEXPORT_C_API=0",
                "-DENABLE_CLI=0",
                "-DENABLE_SHARED=0",
                "-DCMAKE_INSTALL_PREFIX=" + dummy_install_path,
                *flags_high_bits,
            ],
        )
        self._build_with_cmake(package=x265_12bits, for_builder=for_builder)

        x265_10bits = replace(
            package,
            build_dir="x265-10bits",
            build_arguments=[
                "-DHIGH_BIT_DEPTH=1",
                "-DEXPORT_C_API=0",
                "-DENABLE_CLI=0",
                "-DENABLE_SHARED=0",
                "-DCMAKE_INSTALL_PREFIX=" + dummy_install_path,
                *flags_high_bits,
            ],
        )
        self._build_with_cmake(package=x265_10bits, for_builder=for_builder)

        package_path = os.path.join(self.build_dir, package.name)
        with chdir(os.path.join(package_path, x265_12bits.build_dir)):
            os.rename("libx265.a", "libx265-12bits.a")
        with chdir(os.path.join(package_path, x265_10bits.build_dir)):
            os.rename("libx265.a", "libx265-10bits.a")

        package.build_arguments = [
            "-DEXTRA_LIB=x265-10bits.a;x265-12bits.a",
            "-DLINKED_10BIT=1",
            "-DLINKED_12BIT=1",
            "-DEXTRA_LINK_FLAGS=-L../x265-10bits -L../x265-12bits",
        ]
        self._build_with_cmake(package=package, for_builder=for_builder)

    def extract(self, package: Package, for_builder: bool = False) -> None:
        # ifthe package is already installed, do nothing
        installed_dir = os.path.join(
            self._prefix(for_builder=for_builder), "var", "lib", "cibuildpkg"
        )
        installed_file = os.path.join(installed_dir, package.name)
        if os.path.exists(installed_file):
            return


        assert package.source_strip_components in (
            0,
            1,
        ), "source_strip_components must be 0 or 1"
        path = os.path.join(self.build_dir, package.name)
        patch = os.path.join(self.patch_dir, package.name + ".patch")
        tarball = os.path.join(
            self.source_dir,
            package.source_filename or package.source_url.split("/")[-1],
        )

        # download tarball
        if not os.path.exists(tarball):
            fetch(package.source_url, tarball)

        with tarfile.open(tarball) as tar:
            # determine common prefix to strip
            if package.source_strip_components:
                prefixes = set()
                for name in tar.getnames():
                    prefixes.add(name.split("/")[0])
                assert (
                    len(prefixes) == 1
                ), "cannot strip path components, multiple prefixes found"
                prefix = list(prefixes)[0]
            else:
                prefix = ""

            # extract archive
            with tempfile.TemporaryDirectory(dir=self.build_dir) as temp_dir:
                tar.extractall(temp_dir)
                temp_subdir = os.path.join(temp_dir, prefix)
                shutil.move(temp_subdir, path)

        # apply patch
        if os.path.exists(patch):
            run(["patch", "-d", path, "-i", patch, "-p1"])

    def _environment(self, *, for_builder: bool) -> dict[str, str]:
        env = os.environ.copy()

        prefix = self._prefix(for_builder=for_builder)
        prepend_env(
            env, "CPPFLAGS", "-I" + self._mangle_path(os.path.join(prefix, "include"))
        )
        prepend_env(
            env, "LDFLAGS", "-L" + self._mangle_path(os.path.join(prefix, "lib"))
        )
        prepend_env(
            env,
            "PKG_CONFIG_PATH",
            self._mangle_path(os.path.join(prefix, "lib", "pkgconfig")),
            separator=":",
        )

        if platform.system() == "Darwin" and not for_builder:
            arch_flags = os.environ["ARCHFLAGS"]
            if arch_flags == "-arch arm64":
                prepend_env(env, "ASFLAGS", arch_flags)
            for var in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
                prepend_env(env, var, arch_flags)

        return env

    def _mangle_path(self, path: str) -> str:
        if platform.system() == "Windows":
            return (
                path.replace(os.path.sep, "/").replace("C:", "/c").replace("D:", "/d")
            )
        else:
            return path

    def _prefix(self, *, for_builder: bool) -> str:
        if for_builder:
            return self._builder_dest_dir
        else:
            return self._target_dest_dir
