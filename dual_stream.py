"""Stream both cameras for 60 s, each in its own window (Ctrl+C stops early), then save each camera's last frame as
<name>_raw10.npy (10-bit values) and <name>.png. camera_ui.py runs the cameras with the same functions:
cam = start(...), video_widget(cam), play(cam), set_exposure(cam, us), set_gain(cam, gain),
measured_fps(cam), close(cam).

Everything here is for BOTH cameras. What differs is in cameras/tevs.py ("fl") and cameras/imx412.py ("color"),
reached through CAMERAS[name]. Frames are 16-bit with the 10 bits at the top (value << 6); exposure is manual.
Run: uv run python dual_stream.py   (Jetson: .venv/bin/python dual_stream.py)
"""
import select
import threading
import time

import cv2
import numpy as np
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

from cameras import CAMERAS, shared

RUN_SECONDS = 60
DISPLAY_FPS = 15  # The windows refresh at most this often; every frame is still captured. recording.VIDEO_FPS repeats it
GTK_SINK = "gtksink name=sink sync=false"  # For start(sink=...): the video is drawn in video_widget(cam)

Gst.init(None)


def start(name, width, height, fps, sink="autovideosink sync=false", exposure=None):
    """Set up camera name ('fl' or 'color') and start it streaming; play(cam) then takes the frames and shows them.
    fps: None or 0 = fastest. exposure: in microseconds, None = the camera's EXPOSURE_US. Returns the dict the other
    functions take as cam, or None if the camera is not connected."""
    camera = CAMERAS[name]
    found = shared.find_camera(camera.ENTITY)
    if found is None:
        print(f"{name}: camera not found, skipping")
        return None
    media, entity, subdev, video = found
    if not exposure:
        exposure = camera.EXPOSURE_US
    if shared.IS_JETSON:
        fmt = shared.set_format_jetson(video, width, height, camera.PIXEL_FORMAT)  # May be bigger: run() cuts the centre out
        camera.configure_jetson(subdev, fps, exposure)
        cap = shared.open_capture(video, buffers=camera.BUFFERS)
        camera.streaming_jetson(subdev, fps)  # open_capture() started the stream, which can undo the FL frame rate
    else:
        fmt = (width, height, 2 * width)  # No centre to cut: the Pi gives the size asked for, 2 bytes per pixel
        camera.configure_pi(media, entity, subdev, width, height, fps, exposure)
        cap = shared.open_capture(video, (width, height, camera.PIXEL_FORMAT), camera.BUFFERS)

    step = max(1, width // 1280)  # preview() keeps every step-th pixel (colour: every step-th 2x2 block)
    blank = np.zeros((height, width), np.uint16)
    shown = camera.preview(blank, step)  # The preview of a blank frame has the shape run() will push
    shown_height = shown.shape[0]
    shown_width = shown.shape[1]
    size = f"width={shown_width},height={shown_height}"
    caps = f"video/x-raw,format={camera.PREVIEW_FORMAT},{size},framerate={DISPLAY_FPS}/1"
    source = f"appsrc name=src is-live=true format=time do-timestamp=true caps={caps}"
    pipeline = Gst.parse_launch(f"{source} ! videoconvert ! {sink}")

    line = f"{name}: {entity} on {media} -> {video}, {width}x{height} {camera.PIXEL_FORMAT}"
    full_width, full_height, stride = fmt
    if full_width != width or full_height != height:
        line += f" (centre of {full_width}x{full_height})"
    print(line)

    cam = {
        "name": name,
        "width": width,
        "height": height,
        "fps": fps,
        "exposure": exposure,  # As last set, see set_exposure()
        "gain": None,  # Not known until set_gain()
        "subdev": subdev,  # Exposure and gain are set on it
        "cap": cap,  # The open capture: run() takes the frames from it
        "fmt": fmt,  # (width, height, bytes per line) of the driver's frame
        "pipeline": pipeline,  # GStreamer: shows the preview
        "step": step,
        "stop": threading.Event(),  # close() sets it to end run()
        "frames": 0,  # Frames captured so far. Only run() writes "frames" and "last", from its thread
        "last": None,  # The newest frame. Each frame is a new array, never changed: Capture and Record keep them
        "counted": 0,  # For measured_fps(): the frame count and the time at its last call
        "counted_at": None,
        "recorder": None,  # camera_ui.py's Record button puts a Recorder here, and run() gives it every frame
    }
    cam["thread"] = threading.Thread(target=run, args=[cam])  # The capture thread, which play() starts
    return cam


def video_widget(cam):  # The GTK widget a camera started with sink=GTK_SINK draws its video in
    gtksink = cam["pipeline"].get_by_name("sink")
    return gtksink.get_property("widget")


def play(cam):  # Show the video and start capturing. With GTK_SINK: only once video_widget(cam) is inside a window
    cam["pipeline"].set_state(Gst.State.PLAYING)
    cam["counted_at"] = time.monotonic()
    cam["thread"].start()


def run(cam):  # The capture thread: one per camera, until close() sets cam["stop"]
    camera = CAMERAS[cam["name"]]
    cap = cam["cap"]
    src = cam["pipeline"].get_by_name("src")
    shown_at = 0
    while not cam["stop"].is_set():
        readable, writable, failed = select.select([cap.fd], [], [], 0.2)  # Wait for a frame, 0.2 s at most
        if not readable:  # No frame yet: look at the stop flag again
            continue
        index, stamp = cap.dequeue_buffer()
        frame = shared.unpack(cap.buffers[index], cam["fmt"], cam["width"], cam["height"])
        cap.queue_buffer(index)  # Straight back to the driver: frame is a copy
        recorder = cam["recorder"]  # Read once: the Record button changes it while this loop runs
        if recorder is not None:
            recorder.add(frame, stamp)
        if time.monotonic() - shown_at >= 1 / DISPLAY_FPS:
            shown = camera.preview(frame, cam["step"])
            src.emit("push-buffer", Gst.Buffer.new_wrapped(shown.tobytes()))
            if recorder is not None:
                recorder.show(shown, stamp)  # The .mp4 beside the recording gets what the window gets
            shown_at = time.monotonic()
        cam["last"] = frame
        cam["frames"] += 1


def close(cam):  # The capture thread first: it reads the buffers that closing unmaps
    cam["stop"].set()
    cam["thread"].join()
    cam["cap"].close()
    cam["pipeline"].set_state(Gst.State.NULL)


def set_exposure(cam, us):  # Microseconds, applied live. Both drivers call the control 'exposure'
    shared.set_controls(cam["subdev"], exposure=us)
    cam["exposure"] = us


def set_gain(cam, gain):  # In the camera's GAIN_RANGE unit, applied live
    control, value = CAMERAS[cam["name"]].gain_control(gain)
    controls = {control: value}  # A dict, because the control's name differs between the drivers
    shared.set_controls(cam["subdev"], **controls)
    cam["gain"] = round(gain, 1)


def measured_fps(cam):  # Frames per second since the last call (the first time: since play())
    frames = cam["frames"]  # Read once: run() goes on counting
    now = time.monotonic()
    fps = (frames - cam["counted"]) / (now - cam["counted_at"])
    cam["counted"] = frames
    cam["counted_at"] = now
    return fps


if __name__ == "__main__":
    cams = []
    for name, camera in CAMERAS.items():
        width, height, fps = camera.START
        cam = start(name, width, height, fps)
        if cam is not None:
            cams.append(cam)
    for cam in cams:  # Once both are set up, so both run for the same time
        play(cam)
    started = time.monotonic()
    try:
        time.sleep(RUN_SECONDS)
    except KeyboardInterrupt:  # Ctrl+C
        pass
    seconds = time.monotonic() - started
    for cam in cams:
        close(cam)
    for cam in cams:
        name = cam["name"]
        last = cam["last"]
        if last is None:
            print(f"{name}: no frames")
            continue
        np.save(f"{name}_raw10.npy", last >> 6)  # True 10-bit values, 0-1023
        eight_bit = (last >> 8).astype(np.uint8)
        cv2.imwrite(f"{name}.png", CAMERAS[name].to_image(eight_bit))
        frames = cam["frames"]
        rate = frames / seconds
        low = last.min() >> 6
        high = last.max() >> 6
        print(f"{name}: {frames} frames ({rate:.1f} fps), last frame {low}..{high} -> {name}_raw10.npy, {name}.png")
