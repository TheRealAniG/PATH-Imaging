#!/usr/bin/env bash
# Build only: no sudo, installation, boot edits, or reboot.
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
release=$(uname -r)
if [[ "$release" != 6.12.* || "$(uname -m)" != aarch64 ]]; then
    echo "This helper targets the CM5's ARM64 6.12 kernel. See TechNexion's guide for other kernels." >&2
    exit 1
fi
headers="/lib/modules/$release/build"
includes="/usr/src/linux-headers-${release%%+*}+rpt-common-rpi/include"
if [[ ! -d "$headers" || ! -f "$includes/dt-bindings/gpio/gpio.h" ]]; then
    echo "Install kernel headers matching $release before building." >&2
    exit 1
fi
build="$repo_root/.camera-driver"
revision=6611bfe782c3606ef960cae74c1220ad09f4dbce
mkdir -p "$build"
if [[ ! -d "$build/source/.git" ]]; then
    git clone --no-checkout https://github.com/TechNexion-Vision/tn-rpi-camera-driver.git "$build/source"
fi
git -C "$build/source" checkout --detach "$revision"
make -C "$headers" M="$build/source/drivers/media/i2c/tevs" CONFIG_VIDEO_TEVS=m modules -j2
cpp -nostdinc -undef -D__DTS__ -x assembler-with-cpp -I "$includes" \
    "$build/source/arch/arm64/boot/dts/overlays/tevs-rpi22-overlay.dts" > "$build/tevs-rpi22.dts"
dtc -@ -I dts -O dtb -o "$build/tevs-rpi22.dtbo" "$build/tevs-rpi22.dts"
cp "$build/source/drivers/media/i2c/tevs/tevs.ko" "$build/tevs.ko"
printf '%s\n' "$release" > "$build/kernel-release"
echo "Built driver and overlay in $build for $release. See README.md to install."
