import argparse
import os
import platform
import shutil
import subprocess
import sys
from glob import glob

from cibuildpkg import Builder, Package, get_platform, log_group, run

parser = argparse.ArgumentParser("build-ffmpeg")
parser.add_argument("destination")
parser.add_argument("--disable-gpl", action="store_true")
args = parser.parse_args()

dest_dir = args.destination
output_dir = os.path.abspath("output")

plat = platform.system()

if plat == "Linux" and os.environ.get("CIBUILDWHEEL") == "1":
    output_dir = "/output"
output_tarball = os.path.join(output_dir, f"ffmpeg-{get_platform()}.tar.gz")

# FFmpeg has native TLS backends for macOS and Windows
use_gnutls = plat == "Linux"

if not os.path.exists(output_tarball):
    builder = Builder(dest_dir=dest_dir)
    builder.create_directories()

    all_packages = []
    available_tools = set()

    if plat == "Linux" and os.environ.get("CIBUILDWHEEL") == "1":
        with log_group("install packages"):
            run(
                [
                    "yum",
                    "-y",
                    "install",
                    "gperf",
                    "libuuid-devel",
                    "libxcb-devel",
                    "zlib-devel",
                ]
            )
        available_tools.update(["gperf"])
    elif plat == "Windows":
        available_tools.update(["gperf", "nasm"])

        # print tool locations
        for tool in ["gcc", "g++", "curl", "ld", "nasm", "pkg-config"]:
            run(["where", tool])

    with log_group("install python packages"):
        run(["pip", "install", "cmake", "meson", "ninja"])

    if "gperf" not in available_tools:
        all_packages.append(
            Package(
                name="gperf",
                source_url="http://ftp.gnu.org/pub/gnu/gperf/gperf-3.1.tar.gz",
            ),
        )

    if "nasm" not in available_tools:
        all_packages.append(
            Package(
                name="nasm",
                source_url="https://www.nasm.us/pub/nasm/releasebuilds/2.14.02/nasm-2.14.02.tar.bz2",
            ),
        )

    # build packages
    package_groups = [
        Package(
            name="png",
            source_url="http://deb.debian.org/debian/pool/main/libp/libpng1.6/libpng1.6_1.6.37.orig.tar.gz",
            # avoid an assembler error on Windows
            build_arguments=["PNG_COPTS=-fno-asynchronous-unwind-tables"],
        ),
        # Package(
        #     name="freetype",
        #     requires=["png"],
        #     source_url="https://download.savannah.gnu.org/releases/freetype/freetype-2.10.1.tar.gz",
        #     # At this point we have not built our own harfbuzz and we do NOT want to
        #     # pick up the system's harfbuzz.
        #     build_arguments=["--with-harfbuzz=no"],
        #     fflags="--enable-libfreetype",
        # ),
        Package(
            name="xz",
            source_url="https://github.com/tukaani-project/xz/releases/download/v5.6.0/xz-5.6.0.tar.xz",
            build_arguments=[
                "--disable-doc",
                "--disable-lzma-links",
                "--disable-lzmadec",
                "--disable-lzmainfo",
                "--disable-nls",
                "--disable-scripts",
                "--disable-xz",
                "--disable-xzdec",
            ],
        ),
        # Package(
        #     name="xml2",
        #     requires=["xz"],
        #     source_url="https://download.gnome.org/sources/libxml2/2.9/libxml2-2.9.13.tar.xz",
        #     build_arguments=["--without-python"],
        #     fflags="--enable-libxml2",
        # ),
        # Package(
        #     name="fontconfig",
        #     requires=["freetype", "xml2"],
        #     source_url="https://www.freedesktop.org/software/fontconfig/release/fontconfig-2.14.2.tar.xz",
        #     build_arguments=["--disable-nls", "--enable-libxml2"],
        #     fflags="--enable-fontconfig",
        # ),
        # Package(
        #     name="fribidi",
        #     source_url="https://github.com/fribidi/fribidi/releases/download/v1.0.13/fribidi-1.0.13.tar.xz"
        # ),
        # Package(
        #     name="harfbuzz",
        #     requires=["freetype"],
        #     source_url="https://github.com/harfbuzz/harfbuzz/releases/download/4.1.0/harfbuzz-4.1.0.tar.xz",
        #     build_arguments=[
        #         "--with-cairo=no",
        #         "--with-chafa=no",
        #         "--with-freetype=yes",
        #         "--with-glib=no",
        #     ],
        #     # parallel build fails on Windows
        #     build_parallel=plat != "Windows",
        # ),
    ]

    if use_gnutls:
        package_groups += [
            Package(
                name="unistring",
                source_url="https://ftp.gnu.org/gnu/libunistring/libunistring-1.2.tar.gz",
            ),
            Package(
                name="gmp",
                source_url="https://ftp.gnu.org/gnu/gmp/gmp-6.3.0.tar.xz",
                # out-of-tree builds fail on Windows
                build_dir=".",
                fflags="--enable-gmp",
            ),
            Package(
                name="nettle",
                requires=["gmp"],
                source_url="https://ftp.gnu.org/gnu/nettle/nettle-3.9.1.tar.gz",
                build_arguments=["--disable-documentation"],
                # build randomly fails with "*** missing separator.  Stop."
                build_parallel=False,
            ),
            Package(
                name="gnutls",
                requires=["nettle", "unistring"],
                source_url="https://www.gnupg.org/ftp/gcrypt/gnutls/v3.8/gnutls-3.8.1.tar.xz",
                build_arguments=[
                    "--disable-cxx",
                    "--disable-doc",
                    "--disable-guile",
                    "--disable-libdane",
                    "--disable-nls",
                    "--disable-tests",
                    "--disable-tools",
                    "--with-included-libtasn1",
                    "--without-p11-kit",
                ],
            ),
        ]

    package_groups += [
        # codecs
        # Package(
        #     name="aom",
        #     requires=["cmake"],
        #     source_url="https://storage.googleapis.com/aom-releases/libaom-3.2.0.tar.gz",
        #     source_strip_components=0,
        #     build_system="cmake",
        #     build_arguments=[
        #         "-DENABLE_DOCS=0",
        #         "-DENABLE_EXAMPLES=0",
        #         "-DENABLE_TESTS=0",
        #         "-DENABLE_TOOLS=0",
        #     ],
        #     build_parallel=False,
        #     fflags="--enable-libaom",
        # ),
        # Package(
        #     name="bluray",
        #     requires=["fontconfig"],
        #     source_url="https://download.videolan.org/pub/videolan/libbluray/1.3.4/libbluray-1.3.4.tar.bz2",
        #     build_arguments=["--disable-bdjava-jar"],
        #     fflags="--enable-libbluray",
        # ),
        # Package(
        #     name="ass",
        #     requires=["fontconfig", "freetype", "fribidi", "harfbuzz", "nasm", "png"],
        #     source_url="https://github.com/libass/libass/releases/download/0.17.1/libass-0.17.1.tar.gz",
        #     fflags="--enable-libass",
        # ),
        # Package(
        #     name="dav1d",
        #     requires=["meson", "nasm", "ninja"],
        #     source_url="https://code.videolan.org/videolan/dav1d/-/archive/1.4.0/dav1d-1.4.0.tar.bz2",
        #     build_system="meson",
        #     fflags="--enable-libdav1d",
        # ),
        Package(
            name="lame",
            source_url="http://deb.debian.org/debian/pool/main/l/lame/lame_3.100.orig.tar.gz",
            fflags="--enable-libmp3lame",
        ),
        # Package(
        #     name="ogg",
        #     source_url="http://downloads.xiph.org/releases/ogg/libogg-1.3.5.tar.gz",
        # ),
        # Package(
        #     name="opencore-amr",
        #     source_url="http://deb.debian.org/debian/pool/main/o/opencore-amr/opencore-amr_0.1.5.orig.tar.gz",
        #     build_parallel=plat != "Windows",
        # ),
        Package(
            name="openjpeg",
            requires=["cmake"],
            source_filename="openjpeg-2.5.2.tar.gz",
            source_url="https://github.com/uclouvain/openjpeg/archive/v2.5.2.tar.gz",
            build_system="cmake",
            fflags="--enable-libopenjpeg",
        ),
        # Package(
        #     name="opus",
        #     source_url="https://github.com/xiph/opus/releases/download/v1.4/opus-1.4.tar.gz",
        #     build_arguments=["--disable-doc", "--disable-extra-programs"],
        #     fflags="--enable-libopus",
        # ),
        # Package(
        #     name="speex",
        #     source_url="http://downloads.xiph.org/releases/speex/speex-1.2.1.tar.gz",
        #     build_arguments=["--disable-binaries"],
        #     fflags="--enable-libspeex",
        # ),
        # Package(
        #     name="twolame",
        #     source_url="http://deb.debian.org/debian/pool/main/t/twolame/twolame_0.4.0.orig.tar.gz",
        #     build_arguments=["--disable-sndfile"],
        #     fflags="--enable-libtwolame",
        # ),
        # Package(
        #     name="vorbis",
        #     requires=["ogg"],
        #     source_url="http://downloads.xiph.org/releases/vorbis/libvorbis-1.3.7.tar.gz",
        #     fflags="--enable-libvorbis",
        # ),
        # Package(
        #     name="vpx",
        #     source_filename="vpx-1.14.0.tar.gz",
        #     source_url="https://github.com/webmproject/libvpx/archive/v1.14.0.tar.gz",
        #     build_arguments=[
        #         "--disable-examples",
        #         "--disable-tools",
        #         "--disable-unit-tests",
        #     ],
        #     fflags="--enable-libvpx",
        # ),
        Package(
            name="x264",
            source_url="https://code.videolan.org/videolan/x264/-/archive/master/x264-master.tar.bz2",
            # parallel build runs out of memory on Windows
            build_parallel=plat != "Windows",
            fflags="--enable-libx264",
            gpl=True,
        ),
        # Package(
        #     name="x265",
        #     requires=["cmake"],
        #     source_url="https://bitbucket.org/multicoreware/x265_git/downloads/x265_3.5.tar.gz",
        #     build_system="cmake",
        #     source_dir="source",
        #     fflags="--enable-libx265",
        #     gpl=True,
        # ),
        # Package(
        #     name="xvid",
        #     requires=["nasm"],
        #     source_url="https://downloads.xvid.com/downloads/xvidcore-1.3.7.tar.gz",
        #     source_dir="build/generic",
        #     build_dir="build/generic",
        #     fflags="--enable-libxvid",
        #     gpl=True,
        # ),
    ]

    # openh264 = Package(
    #     name="openh264",
    #     requires=["meson", "nasm", "ninja"],
    #     source_filename="openh264-2.4.1.tar.gz",
    #     source_url="https://github.com/cisco/openh264/archive/refs/tags/v2.4.1.tar.gz",
    #     build_system="meson",
    # )

    ffmpeg_build_args = [
        "--disable-alsa",
        "--disable-doc",
        "--disable-mediafoundation",
        "--disable-videotoolbox",
        "--disable-libtheora",
        "--enable-gnutls" if use_gnutls else "--disable-gnutls",
        "--enable-libxcb" if plat == "Linux" else "--disable-libxcb",
        "--enable-lzma",
        "--enable-zlib",
        "--enable-version3",
        "--enable-nonfree",
        "--disable-programs",
        "--enable-hardcoded-tables",
        "--disable-decoders",
        "--enable-decoder=h264,hevc,mpeg4,png,mjpeg,image2",
        "--disable-encoders",
        "--enable-encoder=libx264,png,mjpeg,image2",
        "--disable-demuxers",
        "--enable-demuxer=h264,hevc,mov,image2",
        "--disable-muxers",
        "--enable-muxer=h264,mov,mp4,image2",
        "--disable-hwaccels",
        "--disable-indevs",
        "--disable-outdevs",
        "--disable-devices",
        "--enable-libx264",
        "--disable-openssl" if plat == "Linux" else "--enable-openssl",
    ]

    if args.disable_gpl:
        ffmpeg_build_args.extend(["--enable-libopenh264", "--disable-libx264"])
    else:
        ffmpeg_build_args.extend(["--disable-libopenh264", "--enable-gpl"])

    for package in package_groups:
        if package.name == "x264" and args.disable_gpl:
            # all_packages.append(openh264)
            pass
        elif not (package.gpl and args.disable_gpl):
            all_packages.append(package)
            if package.fflags != "":
                ffmpeg_build_args.append(package.fflags)

    all_packages.append(
        Package(
            name="ffmpeg",
            source_url="https://ffmpeg.org/releases/ffmpeg-6.1.1.tar.xz",
            build_arguments=ffmpeg_build_args,
        )
    )

    for package in all_packages:
        builder.extract(package)

    for package in all_packages:
        builder.build(package, for_builder=package.name in ("nasm", "gperf"))

    if plat == "Windows":
        # fix .lib files being installed in the wrong directory
        for name in (
            "avcodec",
            "avdevice",
            "avfilter",
            "avformat",
            "avutil",
            # "postproc",
            "swresample",
            "swscale",
        ):
            shutil.move(
                os.path.join(dest_dir, "bin", name + ".lib"),
                os.path.join(dest_dir, "lib"),
            )

        # copy some libraries provided by mingw
        mingw_bindir = os.path.dirname(
            subprocess.run(["where", "gcc"], check=True, stdout=subprocess.PIPE)
            .stdout.decode()
            .splitlines()[0]
            .strip()
        )
        for name in (
            "libgcc_s_seh-1.dll",
            "libiconv-2.dll",
            "libstdc++-6.dll",
            "libwinpthread-1.dll",
            "zlib1.dll",
        ):
            shutil.copy(os.path.join(mingw_bindir, name), os.path.join(dest_dir, "bin"))

    # find libraries
    if plat == "Darwin":
        libraries = glob(os.path.join(dest_dir, "lib", "*.dylib"))
    elif plat == "Linux":
        libraries = glob(os.path.join(dest_dir, "lib", "*.so"))
    elif plat == "Windows":
        libraries = glob(os.path.join(dest_dir, "bin", "*.dll"))

    # strip libraries
    if plat == "Darwin":
        run(["strip", "-S"] + libraries)
        run(["otool", "-L"] + libraries)
    else:
        run(["strip", "-s"] + libraries)

    os.makedirs(output_dir, exist_ok=True)
    run(["tar", "czvf", output_tarball, "-C", dest_dir, "bin", "include", "lib"])
