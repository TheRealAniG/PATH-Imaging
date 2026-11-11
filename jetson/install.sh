#!/usr/bin/env bash
# Install what jetson/build.sh built: four kernel modules, the camera overlay, and a boot entry that uses it.
#
#   sudo bash jetson/install.sh                    IMX412C on CAM0, TEVS on CAM1 (how the board is wired now)
#   sudo bash jetson/install.sh --imx412-on cam1   IMX412C on CAM1 (4 lanes), TEVS on CAM0
#
# Then reboot. The existing "primary" boot entry is not touched: if the new entry does not boot, pick
# "primary" in the boot menu (3 s, on the monitor or serial console) and run jetson/uninstall.sh.
# --root DIR installs into DIR instead of / and needs no sudo (for trying it out).
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
here="$repo_root/jetson"
out="$repo_root/.jetson-build/out"
release=$(uname -r)
wiring=cam0 root=
while [[ $# -gt 0 ]]; do
    case $1 in
        --imx412-on) wiring=${2:-}; shift 2 ;;
        --root) root=${2:-}; shift 2 ;;
        *) sed -n '2,10p' "${BASH_SOURCE[0]}" >&2; exit 2 ;;
    esac
done
die() { echo "install.sh: $*" >&2; exit 1; }
[[ $wiring == cam0 || $wiring == cam1 ]] || die "--imx412-on takes cam0 or cam1"
[[ -n $root || $EUID -eq 0 ]] || die "run: sudo bash jetson/install.sh"
[[ -f $out/kernel-release && $(cat "$out/kernel-release") == "$release" ]] || die "build for the running kernel first: bash jetson/build.sh"

modules_dir="$root/lib/modules/$release"
conf="$root/boot/extlinux/extlinux.conf"
overlay=/boot/tegra234-p3767-camera-p3768-path-imaging.dtbo
[[ -d $modules_dir && -f $conf ]] || die "$modules_dir or $conf is missing"
[[ -w $conf ]] || die "$conf is not writable"

# The device tree this board boots with: the one in /boot/dtb whose board ID is ours.
read -r -d '' board < /proc/device-tree/compatible || true  # First string of the list: the board ID
names() { fdtget -t s "$1" / compatible 2>/dev/null || true; }  # A device tree's board IDs, space separated
fdt=
for dtb in "$root"/boot/dtb/*.dtb; do
    [[ -f $dtb && " $(names "$dtb") " == *" $board "* ]] && fdt=/boot/dtb/$(basename "$dtb")
done
[[ -n $fdt ]] || die "no device tree for $board in $root/boot/dtb"
[[ " $(names "$out/path-imx412-$wiring.dtbo") " == *" $board "* ]] || die "the overlay is not for this board ($board)"

# Kernel modules, in the directories the vendors' own installers use. Anything already there is kept as *.before-path.
place() {  # place <built file> <destination>
    if [[ -f $2 ]] && ! cmp -s "$1" "$2" && [[ ! -e $2.before-path ]]; then
        cp -p "$2" "$2.before-path"
    fi
    install -D -m 644 "$1" "$2"
}
i2c="$modules_dir/updates/drivers/media/i2c"
for module in tevs.ko max_serdes_all_tn.ko vc_mipi_core.ko vc_mipi_camera.ko; do
    [[ $(modinfo -F vermagic "$out/$module" | cut -d' ' -f1) == "$release" ]] || die "$module is not for kernel $release: bash jetson/build.sh"
done
place "$out/tevs.ko" "$i2c/technexion/tevs/tevs.ko"
place "$out/max_serdes_all_tn.ko" "$i2c/technexion/maxim-gmsl/max_serdes_all_tn.ko"
place "$out/vc_mipi_core.ko" "$i2c/vc_mipi_core.ko"
place "$out/vc_mipi_camera.ko" "$i2c/vc_mipi_camera.ko"
depmod ${root:+-b "$root"} -a "$release"

# Overlay and boot entry.
install -m 644 "$out/path-imx412-$wiring.dtbo" "$root$overlay"
[[ -e $conf.before-path-cameras ]] || cp -p "$conf" "$conf.before-path-cameras"
if [[ $wiring == cam0 ]]; then menu="PATH cameras (IMX412C on CAM0, TEVS on CAM1)"; else menu="PATH cameras (IMX412C on CAM1, TEVS on CAM0)"; fi
python3 "$here/extlinux.py" add "$conf" "$fdt" "$overlay" "$menu"

cat <<EOF

Installed for kernel $release:
  modules   $i2c/{technexion/tevs/tevs.ko,technexion/maxim-gmsl/max_serdes_all_tn.ko,vc_mipi_core.ko,vc_mipi_camera.ko}
  overlay   $root$overlay  (IMX412C on ${wiring^^}, TEVS on $([[ $wiring == cam0 ]] && echo CAM1 || echo CAM0))
  boot      $conf: new default entry 'path-cameras' = 'primary' + FDT $fdt + the overlay
            (the file as it was: $conf.before-path-cameras)

Reboot, then follow "Check after the reboot" in docs/jetson-orin-nano-setup.md.
If it does not boot: choose 'primary' in the boot menu, then  sudo bash jetson/uninstall.sh
EOF
