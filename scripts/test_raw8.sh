#!/usr/bin/env bash
# Hardware test: does the TEVS firmware accept PREVIEW_FORMAT 0x80 (sensor RAW8)?
# Loads the patched module temporarily (insmod, not installed), captures
# UYVY (0x50), Y8 (0x52) and RAW8 (0x80), then restores the installed driver.
set -uo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
ko="$repo_root/.camera-driver/source/drivers/media/i2c/tevs/tevs.ko"
out="$repo_root/.camera-driver/raw8-test"
W=1280 H=720
[[ $EUID -eq 0 ]] || { echo "Run: sudo bash scripts/test_raw8.sh" >&2; exit 1; }
modinfo -p "$ko" | grep -q bypass_isp || { echo "Patched tevs.ko not built: $ko" >&2; exit 1; }
mkdir -p "$out"

# rp1-cfe holds a reference to tevs, so detach it around every module swap.
cfe=/sys/bus/platform/drivers/rp1-cfe csi=1f00110000.csi
swap() {  # args: command to load tevs
    [[ -e $cfe/$csi ]] && echo "$csi" > "$cfe/unbind"
    if lsmod | grep -q '^tevs '; then rmmod tevs || return 1; fi
    "$@" || return 1
    echo "$csi" > "$cfe/bind"
}
restore() { swap modprobe tevs && echo "Restored installed tevs driver."; }
trap restore EXIT

find_media() {
    for i in $(seq 50); do
        for m in /dev/media*; do
            s=$(media-ctl -d "$m" -p 2>/dev/null | grep -oP 'entity \d+: \Ktevs [^(]+?(?= \()')
            [[ -n $s ]] && { echo "$m|$s"; return; }
        done
        sleep 0.2
    done
    return 1
}

capture() {  # name bypass mbus pixfmt
    local name=$1 bypass=$2 mbus=$3 pix=$4
    echo "=== $name: bypass_isp=$bypass mbus=$mbus pixfmt=$pix ==="
    swap insmod "$ko" bypass_isp="$bypass" || { echo "module swap failed"; exit 1; }
    local r; r=$(find_media) || { echo "sensor did not appear"; return; }
    local media=${r%%|*} sensor=${r#*|}
    local video; video=$(media-ctl -d "$media" -e rp1-cfe-csi2_ch0)
    media-ctl -d "$media" -l '"csi2":4 -> "rp1-cfe-csi2_ch0":0 [1]'
    media-ctl -d "$media" -V "\"$sensor\":0 [fmt:$mbus/${W}x$H field:none]"
    media-ctl -d "$media" -V "\"csi2\":0 [fmt:$mbus/${W}x$H field:none]"
    media-ctl -d "$media" -V "\"csi2\":4 [fmt:$mbus/${W}x$H field:none]"
    media-ctl -d "$media" -p | grep -A1 -E "pad0: SINK|pad4: SOURCE" | grep fmt | head -2
    rm -f "$out/$name.bin"
    timeout 20 v4l2-ctl -d "$video" --set-fmt-video=width=$W,height=$H,pixelformat=$pix \
        --stream-mmap --stream-count=10 --stream-to="$out/$name.bin" 2>&1 | tail -3
    echo "exit=$?  bytes=$(stat -c %s "$out/$name.bin" 2>/dev/null || echo 0)"
    dmesg | tail -5 | grep -iE "tevs|cfe|csi" || true
}

dmesg -C
capture uyvy 0 UYVY8_1X16 UYVY
capture y8   0 Y8_1X8     GREY
capture raw8 1 Y8_1X8     GREY
dmesg > "$out/dmesg.txt"
chown -R "${SUDO_USER:-root}" "$out"
python3 "$repo_root/scripts/raw8_stats.py" "$out" "$W" "$H"
