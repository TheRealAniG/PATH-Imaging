"""Vision Components IMX412C on the Raspberry Pi 5 / CM5: find the camera, wire the media pipeline, stream RAW10.

Follows VC's docs/cropping.md (the manual pipeline) and docs/pixelformat.md (RG16: each 10-bit value << 6 in a
16-bit word). Frames come from VC's own V4L2 class (v4l2_capture.py), settings go through VC's ioctl helpers
(vc_controls.py). Nothing here talks to the sensor directly; the VC kernel driver does.

    from vc import Camera, Mode, save, to_rgb8
    with Camera(Mode(4032, 3040), exposure=30000) as cam:   # exposure in us, gain in mdB, frame_rate in mHz (0 = max)
        frame = cam.read()                                  # (3040, 4032) uint16, true 10-bit values 0-1023, Bayer RGGB
        info = cam.info()
    save(frame, "shot", info)                               # shot.npy (exact) + shot.json (how it was taken)
"""
from dataclasses import dataclass
import json
import os
import re
import subprocess
import time
from pathlib import Path

import numpy as np

from . import v4l2_capture
from . import vc_controls as ctl

FULL_WIDTH, FULL_HEIGHT = 4032, 3040  # vc_init_ctrl_imx412 in VC's vc_mipi_modules.c; docs/binning_mode.md
MBUS_FORMAT = "SRGGB10_1X10"  # Sensor -> CSI-2 receiver: packed 10-bit Bayer ('pRAA' in docs/cropping.md)
PIXEL_FORMAT, RG16 = "RG16", int.from_bytes(b"RG16", "little")  # Receiver -> memory: V4L2_PIX_FMT_SRGGB16, value << 6
BINNING = {0: (1, 1), 1: (1, 2), 2: (2, 2), 3: (4, 2), 4: (8, 2), 5: (16, 2)}  # binning_mode -> (horizontal, vertical)
CFE_ENTITY = "rp1-cfe-csi2_ch0"  # The Pi 5 receiver's capture entity, as in VC's set_rpi5_pipeline
SETTLE_FRAMES = 4  # Discarded after stream start or an exposure change: they predate the new settings


@dataclass
class Nodes:
    media: str  # /dev/mediaN: the map of sensor -> csi2 -> capture node (media-ctl)
    entity: str  # e.g. "vc_mipi_camera 0-001a"
    subdev: str  # /dev/v4l-subdevN: controls, sensor format and crop
    video: str  # /dev/videoN: frames


def find_camera():  # docs/cropping.md step 1, over every media device because the numbering changes between boots
    for media in sorted(Path("/dev").glob("media*")):
        graph = subprocess.run(["media-ctl", "-d", media, "-p"], capture_output=True, text=True).stdout
        if sensor := re.search(r"entity \d+: (vc_mipi_camera \S+)", graph):
            def node(name):
                return subprocess.run(["media-ctl", "-d", media, "-e", name], capture_output=True, text=True).stdout.strip()
            return Nodes(str(media), sensor[1], node(sensor[1]), node(CFE_ENTITY))
    raise RuntimeError("no vc_mipi_camera on any /dev/media*: is the VC driver loaded and the camera on cam1?")


@dataclass
class Mode:
    """Output size and crop, checked against docs/binning_mode.md before anything touches the hardware."""
    width: int = FULL_WIDTH
    height: int = FULL_HEIGHT
    left: int = None  # Crop origin on the (binned) sensor; None = centred
    top: int = None
    binning: int = 0  # Key of BINNING

    def __post_init__(self):
        horizontal, vertical = BINNING[self.binning]
        max_width, max_height = FULL_WIDTH // horizontal, FULL_HEIGHT // vertical
        if self.width % 32 or self.height % 8:
            raise ValueError("width must be a multiple of 32 and height of 8")
        if not (0 < self.width <= max_width and 0 < self.height <= max_height):
            raise ValueError(f"{self.width}x{self.height} exceeds {max_width}x{max_height} for binning_mode {self.binning}")
        if self.left is None:
            self.left = (max_width - self.width) // 2 & ~1  # Even, so the Bayer order stays RGGB
        if self.top is None:
            self.top = (max_height - self.height) // 2 & ~1


def configure(nodes, fd, mode):  # docs/cropping.md steps 3-5, in the order VC's set_rpi5_pipeline uses
    ctl.ctrl_set(fd, ctl.CID_BINNING_MODE, mode.binning)  # First: it changes the sensor's maximum size
    fmt = f"fmt:{MBUS_FORMAT}/{mode.width}x{mode.height} field:none colorspace:srgb"
    for args in (["-V", f'"{nodes.entity}":0 [{fmt}]'],
                 ["-V", f'"{nodes.entity}":0 [crop:({mode.left},{mode.top})/{mode.width}x{mode.height}]'],
                 ["-V", f'"csi2":0 [{fmt}]'],
                 ["-V", f'"csi2":4 [{fmt}]'],
                 ["-l", f'"csi2":4 -> "{CFE_ENTITY}":0 [1]']):  # The capture link is off after every boot
        subprocess.run(["media-ctl", "-d", nodes.media, *args], check=True)


class Camera:
    """One IMX412C: open() configures and starts streaming, read() returns frames, close() releases the device."""

    def __init__(self, mode=None, *, exposure=None, gain=None, black_level=None, frame_rate=0,
                 trigger_mode=ctl.TRIGGER_FREE_RUN, io_mode=ctl.IO_DISABLED, buffers=4):
        self.mode = mode or Mode()
        self.controls = {name: value for name, value in {
            "exposure": exposure, "analogue_gain": gain, "black_level": black_level, "frame_rate": frame_rate,
            "trigger_mode": trigger_mode, "io_mode": io_mode}.items() if value is not None}
        self.buffers = buffers
        self.nodes = self.fd = self.cap = None

    def open(self):
        self.nodes = find_camera()
        self.fd = os.open(self.nodes.subdev, os.O_RDWR)  # Kept open: each set() is then one ioctl
        configure(self.nodes, self.fd, self.mode)
        self._apply(self.controls)
        self.cap = v4l2_capture.V4L2Capture(self.nodes.video)
        self.cap.open()
        self.cap.set_format(self.mode.width, self.mode.height, RG16)
        if (self.cap.width, self.cap.height, self.cap.pixelformat) != (self.mode.width, self.mode.height, RG16):
            raise RuntimeError(f"driver changed the format to {self.cap.width}x{self.cap.height}")
        self.cap.request_buffers(self.buffers)
        for i in range(len(self.cap.buffers)):
            self.cap.queue_buffer(i)
        self.cap.stream_on()
        self._settle()
        return self

    def close(self):
        if self.cap:
            self.cap.close()
            self.cap = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    __enter__ = open

    def __exit__(self, *exc):
        self.close()

    def _apply(self, controls):
        for name, value in controls.items():
            ctl.ctrl_set(self.fd, ctl.CID[name], value)

    def set(self, **controls):  # Live, e.g. cam.set(exposure=5000)
        self._apply(controls)
        self.controls.update(controls)
        if "exposure" in controls and self.cap:
            self._settle()

    def get(self, name):
        return ctl.ctrl_get(self.fd, ctl.CID[name])

    def actual_fps(self):  # Hz from the frame_rate control (mHz). Driver 0.7.0 leaves it at 0 when the maximum was asked for
        return self.get("frame_rate") / 1000

    def _settle(self):
        for _ in range(SETTLE_FRAMES):
            self.cap.capture_frame()

    def read(self):  # One frame as (height, width) uint16 of true 10-bit values; see docs/pixelformat.md for the >> 6
        return self.cap.capture_frame().view(np.uint16).reshape(self.mode.height, self.mode.width) >> 6

    def info(self):  # Everything needed to trace a frame back to how it was taken
        return {"sensor": sensor_name(self.nodes.subdev), "mode": vars(self.mode),
                "controls": {name: self.get(name) for name in self.controls}, "fps": self.actual_fps(),
                "nodes": vars(self.nodes), "time": time.strftime("%Y-%m-%dT%H:%M:%S")}


def sensor_name(subdev):  # Driver 0.7.0 calls the string control sensor_info ('IMX412C REV.0005'), newer ones sensor_name
    out = subprocess.run(["v4l2-ctl", "-d", subdev, "--get-ctrl=sensor_info"], capture_output=True, text=True).stdout
    return out.split(":", 1)[1].strip().strip("'") if ":" in out else ctl.read_sensor_name(subdev)


def save(frame, path, info=None):  # path without suffix: .npy keeps the exact 10-bit values, .json how they were taken
    path = Path(path)
    np.save(path.with_suffix(".npy"), frame)
    stats = {"min": int(frame.min()), "max": int(frame.max()), "mean": round(float(frame.mean()), 2)}
    path.with_suffix(".json").write_text(json.dumps({**(info or {}), **stats}, indent=1))


def load(path):
    path = Path(path)
    return np.load(path.with_suffix(".npy")), json.loads(path.with_suffix(".json").read_text())


def to_rgb8(frame):
    """Viewing aid only: each 2x2 RGGB block becomes one RGB pixel (half size), grey-world white balance, gamma.
    Not a demosaic; for full-size colour use cv2.cvtColor(frame, cv2.COLOR_BayerRG2BGR)."""
    rgb = np.stack([frame[0::2, 0::2], (frame[0::2, 1::2] + frame[1::2, 0::2]) / 2, frame[1::2, 1::2]], 2).astype(np.float32)
    rgb *= rgb.mean() / np.maximum(rgb.mean(axis=(0, 1)), 1)
    return (np.clip(rgb / max(np.percentile(rgb, 99.5), 1), 0, 1) ** (1 / 2.2) * 255).astype(np.uint8)
