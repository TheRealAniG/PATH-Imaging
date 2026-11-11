#!/usr/bin/env bash
# Undo jetson/install.sh: remove the boot entry (the previous default comes back), the overlay and the modules.
#   sudo bash jetson/uninstall.sh        Then reboot.
# --root DIR works on DIR instead of / (matches install.sh --root).
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
release=$(uname -r)
root=
[[ ${1:-} == --root ]] && root=${2:-}
[[ -n $root || $EUID -eq 0 ]] || { echo "run: sudo bash jetson/uninstall.sh" >&2; exit 1; }

conf="$root/boot/extlinux/extlinux.conf"
python3 "$repo_root/jetson/extlinux.py" remove "$conf"
rm -f "$root/boot/tegra234-p3767-camera-p3768-path-imaging.dtbo"

i2c="$root/lib/modules/$release/updates/drivers/media/i2c"
for module in technexion/tevs/tevs.ko technexion/maxim-gmsl/max_serdes_all_tn.ko vc_mipi_core.ko vc_mipi_camera.ko; do
    if [[ -e $i2c/$module.before-path ]]; then
        mv "$i2c/$module.before-path" "$i2c/$module"  # What was there before install.sh
    else
        rm -f "$i2c/$module"
    fi
done
rmdir -p --ignore-fail-on-non-empty "$i2c/technexion/tevs" "$i2c/technexion/maxim-gmsl" 2>/dev/null || true
depmod ${root:+-b "$root"} -a "$release"
echo "Modules and overlay removed for kernel $release. Reboot to finish."
