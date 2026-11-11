"""Stream both cameras at the same time, each in its own window, for 60 s (Ctrl+C stops early).

  fl:    TechNexion TEVS-AR0822-M on CAM/DISP 0, mono RAW10 (TechNexion RAW patch: SGRBG10 -> 'GR16')
  color: Vision Components IMX412C on CAM/DISP 1, colour RAW10 Bayer RGGB ('RG16'), full sensor

Both cameras run at a fixed manual exposure (EXPOSURE_US; set_exposure changes it live): the TEVS's auto exposure
only works through its ISP, which RAW10 bypasses, and the VC driver has none. The windows are brightness stretched
and gamma corrected (the colour one also white balanced) like vc.to_rgb8; the saved frames are not.

Both arrive as 16-bit with the 10 bits at the top (value << 6). Saves each camera's last frame as raw
10-bit values (fl_raw10.npy, color_raw10.npy) and as previews (fl.png, color.png).
Run: uv run python dual_stream.py
"""
import fcntl
from pathlib import Path
import re
import select
import struct
import subprocess
import threading
import time

import cv2
import numpy as np
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

from vc import tegra, v4l2_capture

RUN_SECONDS = 60
DISPLAY_FPS = 15  # Windows refresh this often; every frame is still captured
CAMERAS = {  # name: entity pattern, media-bus format, V4L2 format, width, height, frame rate (None = sensor default)
    "fl": (r"tevs \S+", "SGRBG10_1X10", "GR16", 1920, 1080, 60),  # Or 2560, 1440, 30
    "color": (r"vc_mipi_camera \S+", "SRGGB10_1X10", "RG16", 4032, 3040, None),  # Full sensor; smaller sizes are centre crops
}
if tegra.IS_JETSON:  # Same cameras through NVIDIA's video nodes: 10-bit Bayer formats, no media-bus setup
    CAMERAS = {"fl": (r"tevs \S+", None, "BA10", 1920, 1080, 60), "color": (r"vc_mipi \S+", None, "RG10", 4032, 3040, None)}
GAIN = {  # Lowest, highest, step, unit, and the driver's control value for a gain. Both cameras start at the lowest
    "fl": (1, 64, 1, "x", round),
    "color": (0, 48, 0.1, "dB", lambda dB: round(dB * 1000)),  # Pi driver: analogue_gain in mdB (vc/vc_controls.py)
}
if tegra.IS_JETSON:  # VC's Jetson driver spreads its 0-48000 'gain' evenly over the sensor's gain register 0-1023, and the
    # IMX412 amplifies by 1024 / (1024 - register): measured 1.33x at 12000 and 1.71x at 20000. So ask for the real factor.
    GAIN["color"] = (1, 22, 0.1, "x", lambda x: round((1 - 1 / x) * 1024 * 48000 / 1023))
BUFFERS = {"fl": 8, "color": 4}  # Driver buffers: the FL camera's frames are 17 ms apart and small (4 MB), the colour ones 25 MB
EXPOSURE_US = {"fl": 4000, "color": 30000}  # Starting exposures; the drivers' own defaults (~1.2 ms fixed, 10 ms) are too dark indoors


def open_capture(video, fmt=None, buffers=4):
    """Start streaming and return VC's V4L2Capture, whose buffers run() reads in place. fmt = (width, height, fourcc) sets
    the format first. Piping frames out of v4l2-ctl instead lost over half of them: 8 of the IMX412C's 20 fps arrived.
    buffers: how many frames the driver can hold while run() is busy; a frame arriving with none free is skipped."""
    cap = v4l2_capture.V4L2Capture(video)
    cap.open()
    if fmt:
        cap.set_format(fmt[0], fmt[1], int.from_bytes(fmt[2].encode(), "little"))
    cap.request_buffers(buffers)
    for i in range(len(cap.buffers)):
        cap.queue_buffer(i)
    cap.stream_on()
    return cap


def start_jetson(name, pattern, pixfmt, width, height, fps, sink, exposure):
    found = tegra.find_camera(pattern)
    if not found:
        print(f"{name}: camera not found, skipping")
        return None
    media, sensor, subdev, video = found
    exposure = exposure or EXPOSURE_US[name]
    fmt = tegra.set_format(video, width, height, pixfmt)  # IMX412C: always the full sensor, cropped to the centre in run()
    if sensor.startswith("vc_mipi"):  # Frame rate in mHz, 0 = fastest
        tegra.set_controls(subdev, frame_rate=round((fps or 0) * 1000), exposure=exposure)
    else:  # TEVS: manual mode, otherwise the exposure value is ignored
        tegra.set_controls(subdev, exposure_mode=0, exposure=exposure, **({"max_fps": fps} if fps else {}))
    cap = open_capture(video, buffers=BUFFERS[name])
    step = max(1, width // 1280)
    shown = preview(name, np.zeros((height, width), np.uint16), step).shape
    window = Gst.parse_launch(f"appsrc name=src is-live=true format=time do-timestamp=true caps=video/x-raw,"
                              f"format={'GRAY16_LE' if name == 'fl' else 'BGR'},width={shown[1]},height={shown[0]},"
                              f"framerate={DISPLAY_FPS}/1 ! videoconvert ! {sink}")
    print(f"{name}: {sensor} on {media} -> {video}, {width}x{height} {pixfmt}" + ("" if fmt[:2] == (width, height) else f" (centre of {fmt[0]}x{fmt[1]})"))
    return {"cap": cap, "stop": threading.Event(), "window": window, "size": (height, width), "step": step, "frames": 0, "last": None,
            "subdev": subdev, "exposure": exposure, "gain_control": "gain",
            "unpack": lambda mem: tegra.window(mem, fmt, width, height) & 0xFFC0}  # As on the Pi: value << 6, low bits zero


def start(name, pattern, mbus, pixfmt, width, height, fps, sink="autovideosink sync=false", exposure=None):
    if tegra.IS_JETSON:
        return start_jetson(name, pattern, pixfmt, width, height, fps, sink, exposure)
    for media in sorted(Path("/dev").glob("media*")):  # Media numbering changes between boots
        sensor = re.search(rf"entity \d+: ({pattern})", subprocess.run(
            ["media-ctl", "-d", media, "-p"], capture_output=True, text=True).stdout)
        if sensor:
            break
    else:
        print(f"{name}: camera not found, skipping")
        return None
    rate = f"@1/{fps}" if fps and sensor[1].startswith("tevs") else ""  # TEVS: the frame rate is part of the mode
    for args in (["-l", '"csi2":4 -> "rp1-cfe-csi2_ch0":0 [1]'],
                 ["-V", f'"{sensor[1]}":0 [fmt:{mbus}/{width}x{height}{rate} field:none]'],
                 ["-V", f'"csi2":0 [fmt:{mbus}/{width}x{height} field:none]'],
                 ["-V", f'"csi2":4 [fmt:{mbus}/{width}x{height} field:none]']):
        subprocess.run(["media-ctl", "-d", media, *args])
    subdev = subprocess.run(["media-ctl", "-d", media, "-e", sensor[1]], capture_output=True, text=True).stdout.strip()
    exposure = exposure or EXPOSURE_US[name]
    if sensor[1].startswith("vc_mipi"):  # IMX412C: centre the crop on the 4032x3040 sensor; frame rate in mHz, 0 = fastest
        subprocess.run(["media-ctl", "-d", media, "-V",
                        f'"{sensor[1]}":0 [crop:({(4032 - width) // 4 * 2},{(3040 - height) // 4 * 2})/{width}x{height}]'])
        # The VC driver assumes too short a line time: asking it for 30 fps gives 22.07, so ask 1.35x more
        subprocess.run(["v4l2-ctl", "-d", subdev, "-c", f"frame_rate={round((fps or 0) * 1350)}", "-c", f"exposure={exposure}"])
    else:  # TEVS: manual mode, otherwise the exposure value is ignored
        subprocess.run(["v4l2-ctl", "-d", subdev, "-c", "exposure_mode=0", "-c", f"exposure={exposure}"])
    video =subprocess.run(["media-ctl", "-d", media, "-e", "rp1-cfe-csi2_ch0"], capture_output=True, text=True).stdout.strip()
    cap = open_capture(video, (width, height, pixfmt), BUFFERS[name])
    step = max(1, width // 1280)
    shown = preview(name, np.zeros((height, width), np.uint16), step).shape
    window = Gst.parse_launch(f"appsrc name=src is-live=true format=time do-timestamp=true caps=video/x-raw,"
                              f"format={'GRAY16_LE' if name == 'fl' else 'BGR'},width={shown[1]},height={shown[0]},"
                              f"framerate={DISPLAY_FPS}/1 ! videoconvert ! {sink}")
    print(f"{name}: {sensor[1]} on {media} -> {video}, {width}x{height} {pixfmt}")
    return {"cap": cap, "stop": threading.Event(), "window": window, "size": (height, width), "step": step, "frames": 0, "last": None,
            "subdev": subdev, "exposure": exposure, "gain_control": "analogue_gain" if sensor[1].startswith("vc_mipi") else "gain",
            "unpack": lambda mem: np.frombuffer(mem, np.uint16, width * height).reshape(height, width).copy()}


def set_exposure(cam, us):  # Microseconds, applied live
    subprocess.run(["v4l2-ctl", "-d", cam["subdev"], "-c", f"exposure={us}"])
    cam["exposure"] = us


def set_gain(name, cam, gain):  # In GAIN's unit, applied live
    subprocess.run(["v4l2-ctl", "-d", cam["subdev"], "-c", f"{cam['gain_control']}={GAIN[name][4](gain)}"])
    cam["gain"] = round(gain, 1)


def preview(name, frame, step):  # Small image for the window, at most ~1280 px wide
    if name == "fl":
        return stretch(frame[::step, ::step].astype(np.float32), np.uint16)
    s = 2 * step  # Each sampled 2x2 RGGB block becomes one BGR pixel: much cheaper than demosaicing everything
    bgr = np.dstack((frame[1::s, 1::s], frame[::s, 1::s], frame[::s, ::s])).astype(np.float32)
    bgr *= bgr.mean() / np.maximum(bgr.mean(axis=(0, 1)), 1)  # Grey-world white balance
    return stretch(bgr, np.uint8)


def stretch(x, dtype):  # Brightest pixels (99.5th percentile, on a subsample for speed) to white, then gamma, as in vc.to_rgb8
    return (np.clip(x / max(np.percentile(x[::4, ::4], 99.5), 1), 0, 1) ** (1 / 2.2) * np.iinfo(dtype).max).astype(dtype)


def take(cap):
    """The next filled buffer: (its index, the driver's capture time for that frame in seconds). The same request as
    V4L2Capture.dequeue_buffer, which does not return the time."""
    buf = bytearray(88)  # struct v4l2_buffer: index at 0, type at 4, timestamp (two 64-bit numbers) at 24, memory at 60
    struct.pack_into("I", buf, 4, v4l2_capture.V4L2_BUF_TYPE_VIDEO_CAPTURE)
    struct.pack_into("I", buf, 60, v4l2_capture.V4L2_MEMORY_MMAP)
    fcntl.ioctl(cap.fd, v4l2_capture.VIDIOC_DQBUF, buf)
    seconds, microseconds = struct.unpack_from("qq", buf, 24)
    return struct.unpack_from("I", buf, 0)[0], seconds + microseconds / 1e6


def run(name, cam):  # Capture thread: one per camera, until close() sets cam["stop"]
    cap, shown_at = cam["cap"], 0
    while not cam["stop"].is_set():
        if not select.select([cap.fd], [], [], 0.2)[0]:  # No frame yet: look at the stop flag again
            continue
        index, stamp = take(cap)
        frame = cam["unpack"](cap.buffers[index]["mem"])  # A copy, so the buffer can go straight back to the driver
        cap.queue_buffer(index)
        if recorder := cam.get("recorder"):  # camera_ui.py's Record button
            recorder.add(frame, stamp)
        if time.monotonic() - shown_at >= 1 / DISPLAY_FPS:  # Preview only: converting every frame costs too much
            shown = preview(name, frame, cam["step"]).tobytes()
            cam["window"].get_by_name("src").emit("push-buffer", Gst.Buffer.new_wrapped(shown))
            shown_at = time.monotonic()
        cam["last"], cam["frames"] = frame, cam["frames"] + 1


def close(cam, thread):  # End the capture thread first: it reads the buffers that closing unmaps
    cam["stop"].set()
    thread.join()
    cam["cap"].close()
    cam["window"].set_state(Gst.State.NULL)


if __name__ == "__main__":
    Gst.init(None)
    cams = {name: cam for name, spec in CAMERAS.items() if (cam := start(name, *spec))}
    for cam in cams.values():
        cam["window"].set_state(Gst.State.PLAYING)
    threads = {name: threading.Thread(target=run, args=(name, cam)) for name, cam in cams.items()}
    for t in threads.values():
        t.start()
    try:
        time.sleep(RUN_SECONDS)
    except KeyboardInterrupt:
        pass
    for name, cam in cams.items():
        close(cam, threads[name])
    for name, cam in cams.items():
        if cam["last"] is not None:
            raw = cam["last"] >> 6  # True 10-bit values, 0-1023
            np.save(f"{name}_raw10.npy", raw)
            small = (cam["last"] >> 8).astype(np.uint8)
            cv2.imwrite(f"{name}.png", small if name == "fl" else cv2.cvtColor(small, cv2.COLOR_BayerBG2BGR))
        print(f"{name}: {cam['frames']} frames ({cam['frames'] / RUN_SECONDS:.1f} fps), last frame raw {cam['last'].min() >> 6 if cam['last'] is not None else '-'}"
              f"..{cam['last'].max() >> 6 if cam['last'] is not None else '-'} -> {name}_raw10.npy, {name}.png")
