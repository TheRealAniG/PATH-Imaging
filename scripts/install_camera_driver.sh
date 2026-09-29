#!/usr/bin/env bash
# Run after build_camera_driver.sh. Does not change boot configuration or reboot.
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
build="$repo_root/.camera-driver"
release=$(uname -r)
if [[ $EUID -ne 0 ]]; then
    echo "Run: sudo bash scripts/install_camera_driver.sh" >&2
    exit 1
fi
if [[ ! -f "$build/kernel-release" || "$(cat "$build/kernel-release")" != "$release" ]]; then
    echo "Build for the running kernel first: bash scripts/build_camera_driver.sh" >&2
    exit 1
fi
test -f "$build/tevs-rpi22.dtbo"
vermagic=$(modinfo -F vermagic "$build/tevs.ko")
if [[ "${vermagic%% *}" != "$release" ]]; then
    echo "Driver kernel version does not match $release." >&2
    exit 1
fi
install -D -m 644 "$build/tevs.ko" "/lib/modules/$release/extra/tevs.ko"
install -m 644 "$build/tevs-rpi22.dtbo" /boot/firmware/overlays/tevs-rpi22.dtbo
depmod -a "$release"
# rp1-cfe holds a reference to a loaded tevs; detach it so the new module replaces the old one.
cfe=/sys/bus/platform/drivers/rp1-cfe csi=1f00110000.csi
[[ -e $cfe/$csi ]] && echo "$csi" > "$cfe/unbind"
lsmod | grep -q '^tevs ' && rmmod tevs
modprobe tevs
[[ -e /sys/bus/platform/devices/$csi && ! -e $cfe/$csi ]] && echo "$csi" > "$cfe/bind"
echo "TEVS driver installed and loaded. Run: ./.venv/bin/python camera_API.py"
echo "If discovery still fails, inspect: sudo dmesg | grep -iE 'tevs|csi|i2c'"
