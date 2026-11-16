"""IMX412C ONLY: Vision Components MIPI IMX412C, the colour camera ("color"), RAW10 Bayer RGGB.

Part 1 is what the live app uses through cameras.CAMERAS["color"]; tevs.py has the same names. Part 2 is Camera, this
camera on its own for scripts, with a free crop:

    with Camera(Mode(4032, 3040), exposure=30000) as cam:   # exposure in us, gain in mdB, frame_rate in mHz (0 = max)
        frame = cam.read()                                  # (3040, 4032) uint16, 10-bit values 0-1023
        info = cam.info()
    save(frame, "shot", info)                               # shot.npy (exact) + shot.json (how it was taken)

The Pi driver crops on the sensor. The Jetson driver only has the full frame, so crops are cut out in Python.
"""
from collections import namedtuple
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time

import numpy as np

from . import shared

# ---- Part 1: for the live app. tevs.py has the same names

NAME, SENSOR, MONO = "color", "IMX412C", False
TITLE = "Color: IMX412C"
ENTITY = r"vc_mipi \S+" if shared.IS_JETSON else r"vc_mipi_camera \S+"  # Media entity, e.g. 'vc_mipi 10-001a': driver, I2C bus, address
MBUS_FORMAT = "SRGGB10_1X10"  # Pi: sensor -> receiver
PIXEL_FORMAT = "RG10" if shared.IS_JETSON else "RG16"  # Receiver -> memory
FULL_WIDTH, FULL_HEIGHT = 4032, 3040
START = (FULL_WIDTH, FULL_HEIGHT, 24)  # Width, height, frames per second (None = fastest)
MODES = dict.fromkeys(["640x480", "1280x960", "1920x1080", "3840x2160", "4032x3040"], [0, 60, 30, 24, 15, 10, 5])  # Sizes (centre crops) and the frame rates the UI offers; 0 = fastest
BUFFERS = 4  # Driver buffers: a full frame is 25 MB
EXPOSURE_US = 30000
GAIN_RANGE = (1, 22, 0.1, "x") if shared.IS_JETSON else (0, 48, 0.1, "dB")  # Lowest, highest, step, unit
PREVIEW_FORMAT = "BGR"  # GStreamer's name for what preview() returns


def configure_pi(media, entity, subdev, width, height, fps, exposure):
    shared.wire_pi(media, entity, MBUS_FORMAT, width, height)
    crop = f"crop:({(FULL_WIDTH - width) // 4 * 2},{(FULL_HEIGHT - height) // 4 * 2})/{width}x{height}"  # Centred, on even offsets
    subprocess.run(["media-ctl", "-d", media, "-V", f'"{entity}":0 [{crop}]'])
    shared.set_controls(subdev, frame_rate=round((fps or 0) * 1350), exposure=exposure)  # mHz, 0 = fastest. x 1.35: asked for 30 fps the driver gives 22


def configure_jetson(subdev, fps, exposure):  # The driver always sends the full sensor; dual_stream cuts the centre out
    shared.set_controls(subdev, frame_rate=round((fps or 0) * 1000), exposure=exposure)  # mHz, 0 = fastest


def streaming_jetson(subdev, fps):  # Once frames flow: nothing to do here, tevs.py needs the step
    pass


def gain_control(gain):  # (driver control, its value) for a gain in GAIN_RANGE's unit
    if shared.IS_JETSON:  # The driver's 0-48000 maps evenly onto the sensor's register 0-1023, which amplifies by 1024 / (1024 - register)
        return "gain", round((1 - 1 / gain) * 1024 * 48000 / 1023)
    return "analogue_gain", round(gain * 1000) 


def preview(frame, step):  # Live view: each sampled 2x2 RGGB block becomes one BGR pixel, white balanced, stretched, 8-bit
    s = 2 * step
    return _balance(np.dstack((frame[1::s, 1::s], frame[::s, 1::s], frame[::s, ::s])))


def picture(frame):  # For Capture's .png: the live view's picture at full size, every pixel demosaiced, 8-bit BGR
    import cv2
    return _balance(cv2.cvtColor(frame, cv2.COLOR_BayerBG2BGR))  # RGGB


def _balance(bgr):  # Grey-world white balance, then the brightness stretch
    bgr = bgr.astype(np.float32)
    bgr *= bgr.mean() / np.maximum(bgr.mean(axis=(0, 1)), 1)
    return shared.stretch(bgr, np.uint8)


def to_image(frame):  # For cv2.imwrite, values unchanged: demosaiced BGR at full size, not white balanced
    import cv2 
    return cv2.cvtColor(frame, cv2.COLOR_BayerBG2BGR)  # RGGB


# ---- Part 2: Camera, the IMX412C on its own (scripts/capture_raw10.py). The live app uses nothing below

SETTLE_FRAMES = 4  # Dropped after start or an exposure change: they predate the new settings
JETSON_CONTROLS = {"exposure": "exposure", "analogue_gain": "gain", "frame_rate": "frame_rate"}  # Our names -> the Jetson driver's. Same units
CID = {"black_level": 0x0098090b, 
       "exposure": 0x00980911, 
       "io_mode": 0x0098fff1, 
       "frame_rate": 0x0098fff2, 
       "live_roi": 0x0098fff5, 
       "analogue_gain": 0x009e0903}  # The Pi driver's control ids
VIDIOC_G_CTRL, VIDIOC_S_CTRL = 0xC008561B, 0xC008561C  # ioctls that read / write one control: struct v4l2_control {id, value}
Nodes = namedtuple("Nodes", "media entity subdev video")  # /dev/mediaN, the sensor's entity, /dev/v4l-subdevN (controls), /dev/videoN (frames)


def _control_pi(fd, name, value=None):  # Write one control, or read it when value is None 
    buf = bytearray(struct.pack("Ii", CID[name], int(value or 0)))
    try:
        fcntl.ioctl(fd, VIDIOC_G_CTRL if value is None else VIDIOC_S_CTRL, buf)
    except OSError as error:
        return print(f"control {name}: {error}", file=sys.stderr)
    return struct.unpack("Ii", buf)[1]


def find_camera():
    if found := shared.find_camera(ENTITY):
        return Nodes(*found)
    raise RuntimeError("no vc_mipi camera: see docs/jetson-orin-nano-setup.md" if shared.IS_JETSON else
                       "no vc_mipi_camera on any /dev/media*: is the VC driver loaded and the camera on cam1?")


@dataclass
class Mode:
    """Output size and crop, checked against VC's driver tables before anything touches the hardware."""
    width: int = FULL_WIDTH
    height: int = FULL_HEIGHT
    left: int = None  # Crop origin on the sensor; None = centred
    top: int = None

    def __post_init__(self):
        if self.width % 32 or self.height % 8:
            raise ValueError("width must be a multiple of 32 and height of 8")
        if not (0 < self.width <= FULL_WIDTH and 0 < self.height <= FULL_HEIGHT):
            raise ValueError(f"{self.width}x{self.height} exceeds {FULL_WIDTH}x{FULL_HEIGHT}")
        if self.left is None:
            self.left = (FULL_WIDTH - self.width) // 2 & ~1  # Even, so the Bayer order stays RGGB
        if self.top is None:
            self.top = (FULL_HEIGHT - self.height) // 2 & ~1


class Camera:
    """One IMX412C: open() configures and starts streaming, read() returns a frame, close() releases the device."""

    def __init__(self, mode=None, *, exposure=None, gain=None, black_level=None, frame_rate=0, io_mode=0, buffers=4):
        self.mode, self.buffers = mode or Mode(), buffers
        controls = dict(exposure=exposure, analogue_gain=gain, black_level=black_level, frame_rate=frame_rate, io_mode=io_mode)
        self.controls = {name: value for name, value in controls.items() if value is not None}
        self.nodes = self.fd = self.cap = None

    def open(self):
        mode, nodes = self.mode, find_camera()
        self.nodes = nodes
        if shared.IS_JETSON:  # Nothing to wire. The driver only has the full frame: read() cuts the mode's crop out
            unsupported = [name for name in ("black_level", "io_mode") if self.controls.pop(name, 0)]
            if unsupported:
                raise NotImplementedError(f"{unsupported}: needs VC's patches to NVIDIA's tegra-camera, see docs/jetson-orin-nano-setup.md")
            self.fmt, self.origin = shared.set_format_jetson(nodes.video, FULL_WIDTH, FULL_HEIGHT, PIXEL_FORMAT), (mode.left, mode.top)
            self._apply(self.controls)
            self.cap = shared.open_capture(nodes.video, buffers=self.buffers)
        else:  # The sensor crops: the frame is exactly the mode's size
            self.fd = os.open(nodes.subdev, os.O_RDWR)  # Kept open: each set() is then one ioctl
            self._wire_pi()
            self._apply(self.controls)
            self.fmt, self.origin = (mode.width, mode.height, 2 * mode.width), (0, 0)
            self.cap = shared.open_capture(nodes.video, (mode.width, mode.height, PIXEL_FORMAT), self.buffers)
            if (got := (self.cap.width, self.cap.height, self.cap.pixelformat)) != (mode.width, mode.height, shared.fourcc(PIXEL_FORMAT)):
                self.close()
                raise RuntimeError(f"driver changed the format to {got[0]}x{got[1]}")
        self._settle()
        return self

    def _wire_pi(self):  # VC's docs/cropping.md steps 3-5, in the order of their set_rpi5_pipeline
        mode, entity = self.mode, self.nodes.entity
        fmt = f"fmt:{MBUS_FORMAT}/{mode.width}x{mode.height} field:none colorspace:srgb"
        for args in (["-V", f'"{entity}":0 [{fmt}]'], ["-V", f'"{entity}":0 [crop:({mode.left},{mode.top})/{mode.width}x{mode.height}]'],
                     ["-V", f'"csi2":0 [{fmt}]'], ["-V", f'"csi2":4 [{fmt}]'],
                     ["-l", f'"csi2":4 -> "{shared.PI_CAPTURE_ENTITY}":0 [1]']):  # The capture link is off after every boot
            subprocess.run(["media-ctl", "-d", self.nodes.media, *args], check=True)

    def close(self):
        if self.cap:
            self.cap.close()
        if self.fd is not None:
            os.close(self.fd)
        self.cap = self.fd = None

    __enter__ = open

    def __exit__(self, *exc):
        self.close()

    def _apply(self, controls):
        if shared.IS_JETSON:
            return shared.set_controls(self.nodes.subdev, **{JETSON_CONTROLS[name]: value for name, value in controls.items()})
        for name, value in controls.items():
            _control_pi(self.fd, name, value)

    def set(self, **controls):  # Live, e.g. cam.set(exposure=5000)
        self._apply(controls)
        self.controls.update(controls)
        if "exposure" in controls and self.cap:
            self._settle()

    def get(self, name):
        if shared.IS_JETSON:
            return shared.get_control(self.nodes.subdev, JETSON_CONTROLS[name])
        return _control_pi(self.fd, name)

    def actual_fps(self):  # Hz. The Pi driver 0.7.0 leaves frame_rate at 0 when the fastest was asked for
        return self.get("frame_rate") / 1000

    def _settle(self):
        for _ in range(SETTLE_FRAMES):
            self.cap.queue_buffer(self.cap.dequeue_buffer()[0])

    def read(self):  # One frame: (height, width) uint16 of true 10-bit values, 0-1023
        index, _ = self.cap.dequeue_buffer()
        frame = shared.window(self.cap.buffers[index], self.fmt, self.mode.width, self.mode.height, *self.origin) >> 6  # The shift copies
        self.cap.queue_buffer(index)
        return frame

    def info(self):  # How a frame was taken
        return {"sensor": sensor_name(self.nodes.subdev), "mode": vars(self.mode), "controls": {name: self.get(name) for name in self.controls},
                "fps": self.actual_fps(), "nodes": self.nodes._asdict(), "time": time.strftime("%Y-%m-%dT%H:%M:%S")}


def sensor_name(subdev):  # Pi driver 0.7.0 calls the control sensor_info, newer ones sensor_name; the Jetson driver has neither
    for control in ("sensor_info", "sensor_name"):
        out = subprocess.run(["v4l2-ctl", "-d", subdev, f"--get-ctrl={control}"], capture_output=True, text=True).stdout
        if ":" in out:
            return out.split(":", 1)[1].strip().strip("'")
    return "(unknown)"


def save(frame, path, info=None):  # path without suffix: .npy keeps the exact values, .json how they were taken
    path = Path(path)
    np.save(path.with_suffix(".npy"), frame)
    stats = {"min": int(frame.min()), "max": int(frame.max()), "mean": round(float(frame.mean()), 2)}
    path.with_suffix(".json").write_text(json.dumps({**(info or {}), **stats}, indent=1))


def load(path):
    path = Path(path)
    return np.load(path.with_suffix(".npy")), json.loads(path.with_suffix(".json").read_text())


def to_rgb8(frame):  # Viewing aid, not a demosaic: each 2x2 RGGB block becomes one RGB pixel, white balanced, gamma
    rgb = np.stack([frame[0::2, 0::2], (frame[0::2, 1::2] + frame[1::2, 0::2]) / 2, frame[1::2, 1::2]], 2).astype(np.float32)
    rgb *= rgb.mean() / np.maximum(rgb.mean(axis=(0, 1)), 1)
    return (np.clip(rgb / max(np.percentile(rgb, 99.5), 1), 0, 1) ** (1 / 2.2) * 255).astype(np.uint8)
