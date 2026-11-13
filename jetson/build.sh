#!/usr/bin/env bash
# Build both camera drivers and the device-tree overlay on a Jetson Orin Nano Developer Kit (L4T 39.2).
# Build only: no sudo, nothing installed, no boot changes. Everything lands in .jetson-build/.
# See docs/jetson-orin-nano-setup.md. Rerun after every kernel update, then rerun install.sh.
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
here="$repo_root/jetson"
build="$repo_root/.jetson-build"
release=$(uname -r)
kdir="/lib/modules/$release/build"
nv=/usr/src/nvidia/nvidia-public  # nvidia-l4t-kernel-oot-headers: NVIDIA's camera headers and symbol versions

# Vendor sources, pinned. All three are the vendors' L4T 39.2 branches.
tn_driver=(https://github.com/TechNexion-Vision/tn-jetson-camera-driver.git 2e711295244860b97dbbaf081db5b3fbf885c914)  # tn_l4t-r39.2.ga_kernel-6.8
tn_dt=(https://github.com/TechNexion-Vision/tn-jetson-device-tree.git d8493345822153a2e4915198704c049ca874a550)       # tn_l4t-r39.2.ga_kernel-6.8_t23x
vc=(https://github.com/VC-MIPI-modules/vc_mipi_nvidia.git cc93afc9285b009034c1e2da09cdf9ff90875767)                    # 322-support-for-l4t-392
# TechNexion's prebuilt modules for this L4T, used only as the reference in the ABI check below.
tn_prebuilt=https://download.technexion.com/demo_software/EVK/NVIDIA/OrinNano/pre-built-modules/latest/JP721/tn_camera_modules_jp721.tar.gz
validated_release=6.8.12-1021-tegra  # The kernel jetson/include/nvidia/conftest.h was last proven on

say() { printf '\n== %s\n' "$*"; }
die() { echo "build.sh: $*" >&2; exit 1; }

[[ $(uname -m) == aarch64 ]] || die "run this on the Jetson itself"
grep -qs '^# R39 ' /etc/nv_tegra_release || die "this targets L4T 39.2 (JetPack 7.2); found: $(head -1 /etc/nv_tegra_release 2>/dev/null || echo 'no /etc/nv_tegra_release')"
[[ -f $kdir/Makefile ]] || die "kernel headers for $release are missing (package nvidia-l4t-kernel-headers)"
[[ -f $nv/Module.symvers && -f $nv/include/media/tegracam_core.h ]] || die "$nv is missing (package nvidia-l4t-kernel-oot-headers)"
for tool in git make gcc cpp dtc objdump curl tar python3; do
    command -v "$tool" >/dev/null || die "missing tool: $tool (sudo apt install git build-essential device-tree-compiler curl)"
done

fetch() {  # fetch <dir> <url> <revision>: exactly that revision, nothing else
    local dir=$1 url=$2 rev=$3
    if [[ $(git -C "$dir" rev-parse HEAD 2>/dev/null) != "$rev" ]]; then
        rm -rf "$dir" && git init -q "$dir"
        git -C "$dir" fetch -q --depth 1 "$url" "$rev"
        git -C "$dir" checkout -q --detach FETCH_HEAD
    fi
    git -C "$dir" reset -q --hard && git -C "$dir" clean -qfdx
}
kbuild() {  # kbuild <module dir>: out-of-tree build against the running kernel and NVIDIA's camera headers
    make -s -C "$kdir" M="$1" -j"$(nproc)" KCPPFLAGS="-I$nv/include -I$here/include" KBUILD_EXTRA_SYMBOLS="$nv/Module.symvers" modules 2>&1 |
        grep -v -e 'compiler differs from the one used' -e 'The kernel was built by' -e 'You are using' || true
}
check_conftest_macros() {  # check_conftest_macros <module dir>: NVIDIA's headers must not test a conftest macro our stand-in does not settle
    local macro tested
    tested=$(find "$1" -name '.*.o.cmd' -exec cat {} + | tr ' \\' '\n\n' | grep "^$nv/include/" | sort -u |
        xargs -r grep -hE '^\s*#\s*(if|ifdef|ifndef|elif)\b' | grep -oE '\bNV_[A-Z0-9_]+' | sort -u)
    for macro in $tested; do
        grep -q "^#define $macro\$" "$here/include/nvidia/conftest.h" ||
            die "NVIDIA's camera headers now test $macro, which jetson/include/nvidia/conftest.h does not settle. See docs/jetson-orin-nano-setup.md, 'After a kernel update'."
    done
    echo "conftest macros tested by the headers $(basename "$1") includes: $(echo $tested)"
}
instructions() {  # Normalised instruction stream of a module, for comparing two builds of the same source
    objdump -d --no-show-raw-insn -j .text "$1" | sed -E 's/^\s*[0-9a-f]+:\s*//; s/<[^>]*>//g; s/\b[0-9a-f]{4,16}\b//g' | grep -vE '^\s*$|file format|Disassembly'
}

say "Fetching vendor sources"
mkdir -p "$build/src" "$build/out"
fetch "$build/src/tn-driver" "${tn_driver[@]}"
fetch "$build/src/tn-dt" "${tn_dt[@]}"
fetch "$build/src/vc" "${vc[@]}"

say "Checking the stand-in for NVIDIA's generated conftest.h (struct layouts shared with tegra-camera.ko)"
# 1. Build TechNexion's driver unmodified.
rm -rf "$build/abi" && mkdir -p "$build/abi"
cp -r "$build/src/tn-driver/tevs" "$build/src/tn-driver/maxim-gmsl" "$build/abi/"
printf 'obj-m += tevs/\nobj-m += maxim-gmsl/\n' > "$build/abi/Makefile"
kbuild "$build/abi"
[[ -f $build/abi/tevs/tevs.ko ]] || die "TechNexion's unmodified driver did not build"
# 2. The headers it pulled in must test only the conftest macros our stand-in settles.
check_conftest_macros "$build/abi"
# 3. The same source must compile to the same instructions as TechNexion's own binary, which was built with the real conftest.h.
mkdir -p "$build/prebuilt"
if [[ ! -f $build/prebuilt/tevs.ko ]]; then
    if curl -fsSL --max-time 120 "$tn_prebuilt" -o "$build/prebuilt/package.tar.gz" && tar -xzf "$build/prebuilt/package.tar.gz" -C "$build/prebuilt"; then
        find "$build/prebuilt" -name tevs.ko -path '*technexion*' -exec cp {} "$build/prebuilt/tevs.ko" \;
    fi
fi
if [[ -f $build/prebuilt/tevs.ko && $(modinfo -F vermagic "$build/prebuilt/tevs.ko" | cut -d' ' -f1) == "$release" ]]; then
    if diff <(instructions "$build/prebuilt/tevs.ko") <(instructions "$build/abi/tevs/tevs.ko") > "$build/abi/instructions.diff"; then
        echo "ABI check passed: our build of TechNexion's unmodified driver matches their prebuilt tevs.ko instruction for instruction ($(instructions "$build/abi/tevs/tevs.ko" | wc -l) instructions)."
    else
        die "our build of TechNexion's unmodified driver differs from their prebuilt tevs.ko ($build/abi/instructions.diff): jetson/include/nvidia/conftest.h is wrong for $release. Do not install."
    fi
elif [[ $release == "$validated_release" ]]; then
    echo "WARNING: no TechNexion prebuilt module for $release to compare with (download failed or it targets another kernel). conftest.h was proven on this exact kernel before, continuing."
else
    die "cannot prove jetson/include/nvidia/conftest.h for kernel $release: no TechNexion prebuilt module for it to compare with. See docs/jetson-orin-nano-setup.md, 'After a kernel update'."
fi

say "Building tevs.ko with the RAW10 patch"
rm -rf "$build/technexion" && mkdir -p "$build/technexion"
cp -r "$build/src/tn-driver/tevs" "$build/src/tn-driver/maxim-gmsl" "$build/technexion/"
printf 'obj-m += tevs/\nobj-m += maxim-gmsl/\n' > "$build/technexion/Makefile"
patch -s -p1 -d "$build/technexion" < "$here/patches/tevs-raw10.patch"
kbuild "$build/technexion"

say "Building vc_mipi_core.ko and vc_mipi_camera.ko"
rm -rf "$build/vc" && mkdir -p "$build/vc/vc_mipi"
for file in "$build"/src/vc/src/driver/*; do  # VC ships one file with CRLF line ends; the patch is LF
    sed 's/\r$//' "$file" > "$build/vc/vc_mipi/$(basename "$file")"
done
patch -s -p3 -d "$build/vc/vc_mipi" < "$here/patches/vc-mipi-stock-tegracam.patch"
cp "$here/vc_mipi.Makefile" "$build/vc/Makefile"
kbuild "$build/vc"
check_conftest_macros "$build/vc"

modules=("$build/technexion/tevs/tevs.ko" "$build/technexion/maxim-gmsl/max_serdes_all_tn.ko" "$build/vc/vc_mipi_core.ko" "$build/vc/vc_mipi_camera.ko")
for module in "${modules[@]}"; do
    [[ -f $module ]] || die "not built: $module"
    [[ $(modinfo -F vermagic "$module" | cut -d' ' -f1) == "$release" ]] || die "$module is not for kernel $release"
    cp "$module" "$build/out/"
done

say "Building and checking the device-tree overlays"
dts() {  # dts <source> <output.dtbo> [cpp flags...]
    local src=$1 out=$2; shift 2
    cpp -P -nostdinc -undef -D__DTS__ -x assembler-with-cpp -I"$kdir/include" "$@" "$src" -o "${out%.dtbo}.pre.dts"
    dtc -q -@ -I dts -O dtb -o "$out" "${out%.dtbo}.pre.dts"
}
vendor_inc=(-I"$build/src/tn-dt/include/platforms" -I"$build/src/tn-dt/include")
vc_dts="$build/src/vc/src/devicetree/NV_DevKit_OrinNano/tegra234-p3767-camera-p3768-vc_mipi-dual.dts"
rm -rf "$build/dt" && mkdir -p "$build/dt"
export IMX412_METADATA_H=${IMX412_METADATA_H:-2}  # Embedded-data lines the overlay declares for the IMX412C; check_overlay.py reads it too
dts "$here/overlay/path-cameras.dts" "$build/out/path-imx412-cam0.dtbo" -DVC_METADATA_H="\"$IMX412_METADATA_H\""
dts "$here/overlay/path-cameras.dts" "$build/out/path-imx412-cam1.dtbo" -DVC_METADATA_H="\"$IMX412_METADATA_H\"" -DVC_ON_CAM1
rm -f "$build"/out/*.pre.dts
# The vendors' own overlays, compiled from their sources, as the reference.
dts "$build/src/tn-dt/overlay/tegra234-p3767-camera-p3768-tevs-dual-overlay.dts" "$build/dt/vendor-tevs.dtbo" "${vendor_inc[@]}" -I"$build/src/tn-dt/overlay"
dts "$vc_dts" "$build/dt/vendor-vc-2lane.dtbo" "${vendor_inc[@]}" -I"$(dirname "$vc_dts")"
sed -E 's/^(#define VC_MIPI_LANES +)2 /\14 /' "$vc_dts" > "$build/dt/vc-4lane.dts"  # VC's documented switch for 4 lanes
grep -qE '^#define VC_MIPI_LANES +4 ' "$build/dt/vc-4lane.dts" || die "could not switch VC's overlay to 4 lanes"
dts "$build/dt/vc-4lane.dts" "$build/dt/vendor-vc-4lane.dtbo" "${vendor_inc[@]}" -I"$(dirname "$vc_dts")"
base=$(ls /boot/dtb/*.dtb 2>/dev/null | head -1)
python3 "$here/check_overlay.py" "$build/out/path-imx412-cam0.dtbo" vc="$build/dt/vendor-vc-2lane.dtbo" tevs="$build/dt/vendor-tevs.dtbo" ${base:+--base "$base"}
python3 "$here/check_overlay.py" "$build/out/path-imx412-cam1.dtbo" vc="$build/dt/vendor-vc-4lane.dtbo" tevs="$build/dt/vendor-tevs.dtbo" ${base:+--base "$base"}

printf '%s\n' "$release" > "$build/out/kernel-release"
say "Built for $release in $build/out:"
ls -1 "$build/out"
echo
echo "Next: sudo bash jetson/install.sh   (then reboot; see docs/jetson-orin-nano-setup.md)"
