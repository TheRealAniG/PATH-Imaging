"""Capture on the Jetson (NVIDIA tegra-video), for both cameras. The Raspberry Pi side stays in the callers.

Differences from the Pi: one video node per camera takes the format and delivers frames, with no media-ctl
wiring; controls are on the sensor's sub-device for the TEVS and on either node for the IMX412C; formats are
'RG10' / 'BA10'; and the 10-bit value sits in bits 15..6 of each word with the top six bits repeated in the
low six (measured on the Orin Nano), where the Pi leaves zeros.
"""
from pathlib import Path
import re
import subprocess

import numpy as np

try:
    IS_JETSON = b"nvidia,tegra" in Path("/proc/device-tree/compatible").read_bytes()
except OSError:
    IS_JETSON = False


def _run(*args):
    return subprocess.run(args, capture_output=True, text=True)


def find_camera(pattern):
    """(media device, sensor entity, sensor sub-device, video node) for the first sensor matching the regex, or None."""
    for media in sorted(Path("/dev").glob("media*")):
        sensor = re.search(rf"entity \d+: ({pattern})", _run("media-ctl", "-d", media, "-p").stdout)
        if sensor:
            node = lambda name: _run("media-ctl", "-d", media, "-e", name).stdout.strip()
            return str(media), sensor[1], node(sensor[1]), node(f"vi-output, {sensor[1]}")
    return None


def set_format(video, width, height, pixfmt):
    """Ask for a size and return what the driver gives: (width, height, bytes per line, bytes per frame).
    The IMX412C driver only has the full frame and answers with that; callers crop."""
    _run("v4l2-ctl", "-d", video, "-c", "bypass_mode=0", f"--set-fmt-video=width={width},height={height},pixelformat={pixfmt}")
    out = _run("v4l2-ctl", "-d", video, "--get-fmt-video").stdout
    size = re.search(r"Width/Height\s*:\s*(\d+)/(\d+)", out)
    return int(size[1]), int(size[2]), int(re.search(r"Bytes per Line\s*:\s*(\d+)", out)[1]), int(re.search(r"Size Image\s*:\s*(\d+)", out)[1])


def set_controls(device, **controls):
    args = [arg for name, value in controls.items() for arg in ("-c", f"{name}={value}")]
    result = _run("v4l2-ctl", "-d", device, *args)
    if result.returncode:
        print(f"{device}: {result.stderr.strip()}")


def get_control(device, name):
    out = _run("v4l2-ctl", "-d", device, "-C", name).stdout
    return int(out.split(":", 1)[1]) if ":" in out else None


def window(data, fmt, width, height, left=None, top=None):
    """The (height, width) window of one driver frame as a uint16 view, still value << 6 (plus repeated low bits).
    Centred unless left/top are given; offsets are kept even so the Bayer order does not change."""
    full_width, full_height, stride, _ = fmt
    left = (full_width - width) // 2 & ~1 if left is None else left
    top = (full_height - height) // 2 & ~1 if top is None else top
    rows = np.frombuffer(data, np.uint16, full_height * stride // 2).reshape(full_height, stride // 2)
    return rows[top:top + height, left:left + width]
