#!/usr/bin/env bash
# Build only: no sudo, installation, boot edits, or reboot.
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
release=$(uname -r)
if [[ "$(uname -m)" != aarch64 ]]; then
    echo "This helper requires ARM64." >&2
    exit 1
fi
case "$release" in
    6.12.*) branch=tn_rpi_kernel-6.12; revision=6611bfe782c3606ef960cae74c1220ad09f4dbce ;;
    6.18.*) branch=tn_rpi_kernel-6.18; revision=c681dd09c0e815d2ace2065eff867f02702c32a8 ;;
    *) echo "Unsupported kernel: $release. Supported families: 6.12 and 6.18." >&2; exit 1 ;;
esac
headers="/lib/modules/$release/build"
includes="/usr/src/linux-headers-${release%%+*}+rpt-common-rpi/include"
if [[ ! -d "$headers" || ! -f "$includes/dt-bindings/gpio/gpio.h" ]]; then
    echo "Install kernel headers matching $release before building." >&2
    exit 1
fi
build="$repo_root/.camera-driver"
mkdir -p "$build"
if [[ ! -d "$build/source/.git" ]]; then
    git clone --no-checkout https://github.com/TechNexion-Vision/tn-rpi-camera-driver.git "$build/source"
fi
if ! git -C "$build/source" cat-file -e "$revision^{commit}" 2>/dev/null; then
    git -C "$build/source" fetch origin "$branch"
fi
git -C "$build/source" checkout --force --detach "$revision"
# TechNexion's RAW patch (SGRBG8/10/12/16 -> PREVIEW_FORMAT 0x80/0x90/0xA0/0xB0); UYVY stays the default.
git -C "$build/source" apply "$repo_root/patches/tevs-raw.patch"
make -C "$headers" M="$build/source/drivers/media/i2c/tevs" CONFIG_VIDEO_TEVS=m modules -j2
cpp -nostdinc -undef -D__DTS__ -x assembler-with-cpp -I "$includes" \
    "$build/source/arch/arm64/boot/dts/overlays/tevs-rpi22-overlay.dts" > "$build/tevs-rpi22.dts"
dtc -@ -I dts -O dtb -o "$build/tevs-rpi22.dtbo" "$build/tevs-rpi22.dts"
cp "$build/source/drivers/media/i2c/tevs/tevs.ko" "$build/tevs.ko"
printf '%s\n' "$release" > "$build/kernel-release"
echo "Built driver and overlay in $build for $release. See README.md to install."
