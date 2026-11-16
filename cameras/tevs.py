"""TEVS ONLY: TechNexion TEVS-AR0822-M, the mono fluorescence camera ("fl"), RAW10. imx412.py has the same names.

RAW10 bypasses the sensor's ISP, the only place its auto exposure works, so exposure is manual.
"""
import numpy as np

from . import shared

NAME, SENSOR, MONO = "fl", "TEVS-AR0822-M", True
TITLE = "FL: TEVS-AR0822-M (mono)"
ENTITY = r"tevs \S+"  # Media entity, e.g. 'tevs 9-0048': driver, I2C bus, address
MBUS_FORMAT = "SGRBG10_1X10"  # Pi: sensor -> receiver
PIXEL_FORMAT = "BA10" if shared.IS_JETSON else "GR16"  # Receiver -> memory
START = (1920, 1080, 24)  # Width, height, frames per second
_RATES = [60, 30, 24, 20, 15, 10, 5]
MODES = {"640x480": _RATES, "1280x720": _RATES, "1920x1080": _RATES, "2560x1440": _RATES[1:], "3840x2160": _RATES[4:]}  # The driver's sizes and the frame rates each allows
BUFFERS = 8  # Driver buffers: frames are 17 ms apart
EXPOSURE_US = 4000
GAIN_RANGE = (1, 64, 1, "x")  # Lowest, highest, step, unit
PREVIEW_FORMAT = "GRAY16_LE"  # GStreamer's name for what preview() returns


def configure_pi(media, entity, subdev, width, height, fps, exposure):
    shared.wire_pi(media, entity, MBUS_FORMAT, width, height, rate=f"@1/{fps}" if fps else "")
    shared.set_controls(subdev, exposure_mode=0, exposure=exposure)  # 0 = manual; otherwise the exposure is ignored


def configure_jetson(subdev, fps, exposure):
    shared.set_controls(subdev, exposure_mode=0, exposure=exposure, **({"max_fps": fps} if fps else {}))


def streaming_jetson(subdev, fps):  # Once frames flow: starting puts a rate the driver does not list (24) back to 60, but the sensor takes any
    if fps:
        shared.set_controls(subdev, max_fps=fps)


def gain_control(gain):  # (driver control, its value) for a gain in GAIN_RANGE's unit
    return "gain", round(gain)


def preview(frame, step):  # Live view: every step-th pixel, brightness stretched, 16-bit grey
    return shared.stretch(frame[::step, ::step].astype(np.float32), np.uint16)


def picture(frame):  # For Capture's .png: the live view's picture at full size, brightness stretched, 16-bit grey
    return shared.stretch(frame.astype(np.float32), np.uint16)


def to_image(frame):  # For cv2.imwrite, values unchanged. Mono: nothing to do
    return frame
