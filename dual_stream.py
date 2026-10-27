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
from pathlib import Path
import re
import subprocess
import threading
import time

import cv2
import numpy as np
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

RUN_SECONDS = 60
DISPLAY_FPS = 15  # Windows refresh this often; every frame is still captured
CAMERAS = {  # name: entity pattern, media-bus format, V4L2 format, width, height, frame rate (None = sensor default)
    "fl": (r"tevs \S+", "SGRBG10_1X10", "GR16", 1920, 1080, 60),  # Or 2560, 1440, 30
    "color": (r"vc_mipi_camera \S+", "SRGGB10_1X10", "RG16", 4032, 3040, None),  # Full sensor; smaller sizes are centre crops
}
EXPOSURE_US = {"fl": 4000, "color": 30000}  # Starting exposures; the drivers' own defaults (~1.2 ms fixed, 10 ms) are too dark indoors


def start(name, pattern, mbus, pixfmt, width, height, fps, sink="autovideosink sync=false", exposure=None):
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
    stream = subprocess.Popen(["v4l2-ctl", "-d", video, "--stream-mmap", "--stream-to=-",
                               f"--set-fmt-video=width={width},height={height},pixelformat={pixfmt}"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              start_new_session=True)  # Ctrl+C in the terminal must not cut a frame in half
    step = max(1, width // 1280)
    shown = preview(name, np.zeros((height, width), np.uint16), step).shape
    window = Gst.parse_launch(f"appsrc name=src is-live=true format=time do-timestamp=true caps=video/x-raw,"
                              f"format={'GRAY16_LE' if name == 'fl' else 'BGR'},width={shown[1]},height={shown[0]},"
                              f"framerate={DISPLAY_FPS}/1 ! videoconvert ! {sink}")
    print(f"{name}: {sensor[1]} on {media} -> {video}, {width}x{height} {pixfmt}")
    return {"stream": stream, "window": window, "size": (height, width), "step": step, "frames": 0, "last": None,
            "subdev": subdev, "exposure": exposure}


def set_exposure(cam, us):  # Microseconds, applied live
    subprocess.run(["v4l2-ctl", "-d", cam["subdev"], "-c", f"exposure={us}"])
    cam["exposure"] = us


def preview(name, frame, step):  # Small image for the window, at most ~1280 px wide
    if name == "fl":
        return stretch(frame[::step, ::step].astype(np.float32), np.uint16)
    s = 2 * step  # Each sampled 2x2 RGGB block becomes one BGR pixel: much cheaper than demosaicing everything
    bgr = np.dstack((frame[1::s, 1::s], frame[::s, 1::s], frame[::s, ::s])).astype(np.float32)
    bgr *= bgr.mean() / np.maximum(bgr.mean(axis=(0, 1)), 1)  # Grey-world white balance
    return stretch(bgr, np.uint8)


def stretch(x, dtype):  # Brightest pixels (99.5th percentile, on a subsample for speed) to white, then gamma, as in vc.to_rgb8
    return (np.clip(x / max(np.percentile(x[::4, ::4], 99.5), 1), 0, 1) ** (1 / 2.2) * np.iinfo(dtype).max).astype(dtype)


def run(name, cam, stop):
    height, width = cam["size"]
    shown_at = 0
    while not stop.is_set():
        data = cam["stream"].stdout.read(width * height * 2)
        if len(data) < width * height * 2:
            break
        frame = np.frombuffer(data, np.uint16).reshape(height, width)
        if time.monotonic() - shown_at >= 1 / DISPLAY_FPS:  # Preview only: converting every frame costs too much
            shown = preview(name, frame, cam["step"]).tobytes()
            cam["window"].get_by_name("src").emit("push-buffer", Gst.Buffer.new_wrapped(shown))
            shown_at = time.monotonic()
        cam["last"], cam["frames"] = frame, cam["frames"] + 1


if __name__ == "__main__":
    Gst.init(None)
    cams = {name: cam for name, spec in CAMERAS.items() if (cam := start(name, *spec))}
    for cam in cams.values():
        cam["window"].set_state(Gst.State.PLAYING)
    stop = threading.Event()
    threads = [threading.Thread(target=run, args=(name, cam, stop)) for name, cam in cams.items()]
    for t in threads:
        t.start()
    try:
        time.sleep(RUN_SECONDS)
    except KeyboardInterrupt:
        pass
    stop.set()
    for name, cam in cams.items():
        cam["stream"].kill()
    for t in threads:
        t.join()
    for name, cam in cams.items():
        cam["window"].set_state(Gst.State.NULL)
        if cam["last"] is not None:
            raw = cam["last"] >> 6  # True 10-bit values, 0-1023
            np.save(f"{name}_raw10.npy", raw)
            preview = (cam["last"] >> 8).astype(np.uint8)
            cv2.imwrite(f"{name}.png", preview if name == "fl" else cv2.cvtColor(preview, cv2.COLOR_BayerBG2BGR))
        print(f"{name}: {cam['frames']} frames ({cam['frames'] / RUN_SECONDS:.1f} fps), last frame raw {cam['last'].min() >> 6 if cam['last'] is not None else '-'}"
              f"..{cam['last'].max() >> 6 if cam['last'] is not None else '-'} -> {name}_raw10.npy, {name}.png")
