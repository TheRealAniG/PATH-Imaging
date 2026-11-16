"""BOTH cameras: find a camera, set its format, stream from it. Names ending in _pi or _jetson run only there.

Jetson vs Pi: the Jetson needs no media-ctl wiring, and fills the low 6 bits of each pixel (the Pi leaves zeros).
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import subprocess
import numpy as np
from .shared_v4l2_capture import V4L2Capture

try:
    IS_JETSON = b"nvidia,tegra" in Path("/proc/device-tree/compatible").read_bytes()
except OSError:
    IS_JETSON = False
PI_CAPTURE_ENTITY = "rp1-cfe-csi2_ch0"  # Pi 5 capture node, same name for both cameras
COPY_THREADS = 4  # unpack() copies a frame in this many bands at once
_copiers = ThreadPoolExecutor(2 * COPY_THREADS)  # Both cameras at once


def _run(*args):
    return subprocess.run(args, capture_output=True, text=True)


def find_camera(pattern):
    """(media device, sensor entity, sub-device, video node) of the first sensor matching the regex, or None.
    Found by name because /dev numbers change between boots."""
    for media in sorted(Path("/dev").glob("media*")):
        topology = _run("media-ctl", "-d", media, "-p").stdout
        match = re.search(r"entity \d+: (" + pattern + ")", topology)
        if not match:
            continue
        sensor = match.group(1)
        if IS_JETSON:
            capture = "vi-output, " + sensor
        else:
            capture = PI_CAPTURE_ENTITY
        subdev = _run("media-ctl", "-d", media, "-e", sensor).stdout.strip()
        video = _run("media-ctl", "-d", media, "-e", capture).stdout.strip()
        return str(media), sensor, subdev, video
    return None


def wire_pi(media, entity, mbus, width, height, rate=""):
    """Turn on the capture link (off after boot) and set the size on sensor -> csi2 -> capture node.
    rate: extra text for the sensor format (the TEVS takes its frame rate there)."""
    fmt = f"fmt:{mbus}/{width}x{height} field:none"
    sensor_fmt = f"fmt:{mbus}/{width}x{height}{rate} field:none"
    commands = [
        ["-l", f'"csi2":4 -> "{PI_CAPTURE_ENTITY}":0 [1]'],
        ["-V", f'"{entity}":0 [{sensor_fmt}]'],
        ["-V", f'"csi2":0 [{fmt}]'],
        ["-V", f'"csi2":4 [{fmt}]'],
    ]
    for args in commands:
        subprocess.run(["media-ctl", "-d", media] + args)


def set_format_jetson(video, width, height, pixfmt):
    """Ask for a size; return what the driver gave: (width, height, bytes per line). IMX412C: full frame only."""
    _run("v4l2-ctl", "-d", video, "-c", "bypass_mode=0", f"--set-fmt-video=width={width},height={height},pixelformat={pixfmt}")
    out = _run("v4l2-ctl", "-d", video, "--get-fmt-video").stdout
    size = re.search(r"Width/Height\s*:\s*(\d+)/(\d+)", out)
    stride = re.search(r"Bytes per Line\s*:\s*(\d+)", out)
    return int(size.group(1)), int(size.group(2)), int(stride.group(1))


def window(data, fmt, width, height, left=None, top=None):
    """A (height, width) uint16 view into one driver frame. fmt = (width, height, bytes per line) of the frame.
    Centred by default, on even offsets so the Bayer order is kept."""
    full_width, full_height, stride = fmt
    if left is None:
        left = (full_width - width) // 2 & ~1
    if top is None:
        top = (full_height - height) // 2 & ~1
    rows = np.frombuffer(data, np.uint16, full_height * stride // 2).reshape(full_height, stride // 2)
    return rows[top:top + height, left:left + width]


def unpack(data, fmt, width, height):
    """Copy the centre of a driver frame, low 6 bits cleared, so the buffer can go back to the driver.
    Copied in bands on COPY_THREADS threads: one thread takes 24 ms per IMX412C frame, too slow for 40 fps."""
    view = window(data, fmt, width, height)
    out = np.empty((height, width), np.uint16)

    def copy_band(i):
        rows = slice(i * height // COPY_THREADS, (i + 1) * height // COPY_THREADS)
        np.bitwise_and(view[rows], 0xFFC0, out=out[rows])

    list(_copiers.map(copy_band, range(COPY_THREADS)))
    return out


def set_controls(device, **controls):  # Driver names, e.g. set_controls(subdev, exposure=4000)
    args = ["v4l2-ctl", "-d", device]
    for name, value in controls.items():
        args += ["-c", f"{name}={value}"]
    result = _run(*args)
    if result.returncode:
        print(device + ": " + result.stderr.strip())


def get_control(device, name):
    out = _run("v4l2-ctl", "-d", device, "-C", name).stdout  # e.g. "exposure: 4000"
    if ":" not in out:
        return None
    return int(out.split(":")[1])


def fourcc(text):  # 'RG16' -> V4L2's number for it
    return int.from_bytes(text.encode(), "little")


def open_capture(video, fmt=None, buffers=4):
    """Start streaming; the caller reads cap.buffers in place. fmt = (width, height, fourcc text), Pi only (on the
    Jetson set_format_jetson did it). buffers: frames the driver can hold; a frame with none free is skipped."""
    cap = V4L2Capture(video)
    cap.open()
    if fmt:
        cap.set_format(fmt[0], fmt[1], fourcc(fmt[2]))
    cap.request_buffers(buffers)
    for index in range(len(cap.buffers)):
        cap.queue_buffer(index)
    cap.stream_on()
    return cap


def stretch(x, dtype):  # Preview brightness: 99.5th percentile to white, then gamma 2.2
    bright = max(np.percentile(x[::4, ::4], 99.5), 1)
    scaled = np.clip(x / bright, 0, 1) ** (1 / 2.2)
    return (scaled * np.iinfo(dtype).max).astype(dtype)
