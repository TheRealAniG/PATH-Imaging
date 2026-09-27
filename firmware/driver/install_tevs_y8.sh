#!/bin/sh
# Rebuild TechNexion's tevs module with the AR0822 Y8 patch for the running
# Raspberry Pi kernel. Run on the Pi after TechNexion's normal driver install.
set -e

here=$(cd "$(dirname "$0")" && pwd)
kver=$(uname -r)
series=$(echo "$kver" | cut -d. -f1,2)
if [ "$series" != "6.12" ]; then
    echo "The patch targets tn_rpi_kernel-6.12; this Pi runs $kver." >&2
    exit 1
fi

sudo apt install -y git make gcc "linux-headers-$kver"

work=$(mktemp -d)
git clone --depth 1 -b tn_rpi_kernel-6.12 \
    https://github.com/TechNexion-Vision/tn-rpi-camera-driver.git "$work/tn"
git -C "$work/tn" apply "$here/tevs-ar0822-y8.patch"

make -C "/lib/modules/$kver/build" M="$work/tn/drivers/media/i2c/tevs" \
    CONFIG_VIDEO_TEVS=m modules

# updates/ takes priority over the module TechNexion's installer copied in.
sudo install -D -m 644 "$work/tn/drivers/media/i2c/tevs/tevs.ko" \
    "/lib/modules/$kver/updates/tevs.ko"
sudo depmod -a
rm -rf "$work"

echo "Installed $(modinfo -n tevs). Reboot to load it."
