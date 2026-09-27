#!/bin/sh
# Configure the TEVS camera for 8-bit mono (Y8/GREY) and print its video node.
# Usage: sh setup_y8.sh [width] [height]   (default 1920 1080)
set -e
width=${1:-1920}
height=${2:-1080}

# Find the media device that contains the tevs sensor.
for media in /dev/media*; do
    sensor=$(media-ctl -d "$media" -p 2>/dev/null |
        sed -n 's/.*entity [0-9]*: \(tevs [0-9a-f-]*\) .*/\1/p' | head -n 1)
    [ -n "$sensor" ] && break
done
if [ -z "$sensor" ]; then
    echo "No tevs sensor found. Check the driver and dtoverlay." >&2
    exit 1
fi

if video=$(media-ctl -d "$media" -e rp1-cfe-csi2_ch0 2>/dev/null); then
    # Raspberry Pi 5: link the CSI-2 receiver, then set the sensor pad.
    media-ctl -d "$media" -l "'csi2':4 -> 'rp1-cfe-csi2_ch0':0 [1]"
    media-ctl -d "$media" -V "'$sensor':0 [fmt:Y8_1X8/${width}x${height} field:none]"
else
    # Raspberry Pi 4: unicam passes the video format through to the sensor.
    video=$(media-ctl -d "$media" -e unicam-image)
fi
v4l2-ctl -d "$video" --set-fmt-video=width="$width",height="$height",pixelformat=GREY

media-ctl -d "$media" --get-v4l2 "'$sensor':0"
v4l2-ctl -d "$video" --get-fmt-video
echo "$video"
