"""Both cameras live in one window: size, frame rate, exposure and gain per camera, the measured frame rate, Capture
and Record. For BOTH cameras: what differs comes from cameras/tevs.py and cameras/imx412.py through CAMERAS[name].
This file is the window; dual_stream.py runs the cameras and recording.py writes the recordings.

Capture saves each camera's latest frame under ~/Desktop/PATH-captures/<sensor>/<time>/ as .png and .raw. The .png is
the picture the window shows, at full size (FL 16-bit grey; colour 8-bit, demosaiced, white balanced): its brightest
pixels are stretched to white frame by frame, so a dim frame is still visible but brightness cannot be compared
between PNGs. The .raw is what the camera measured: 16-bit little-endian 10-bit values, row by row, no header.

Record saves every frame until it is pressed again, as lossless .j2ks: compressed on the Jetson's GPU, or on the CPU
where there is none (the Pi), which keeps up with less. An .mp4 of the live view, to watch, is made beside it right
after (see recording.py). The status line counts the frames lost.

Picking a size or frame rate restarts that camera. Close the window or press Ctrl+C to stop.
Run: uv run python camera_ui.py   (Jetson: .venv/bin/python camera_ui.py)
"""
from pathlib import Path
import signal
import subprocess
import threading
import time

import cv2
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from cameras import CAMERAS, shared
import dual_stream
import recording

CAPTURES = Path.home() / "Desktop" / "PATH-captures"
POWER_HINT = ""  # Added to the dropped-frames message when the Jetson is not in its fastest power mode
if shared.IS_JETSON:
    power_mode = subprocess.run(["nvpmodel", "-q"], capture_output=True, text=True).stdout
    if "MAXN" not in power_mode:
        POWER_HINT = " (power mode is not MAXN_SUPER: sudo nvpmodel -m 2)"

cams = {}  # By camera name: the cameras that are streaming, each the cam that dual_stream.start() returned
columns = {}  # By camera name: its widgets, see add_column()
making = {}  # After Record stops, until its .mp4s are made: "text" (the status line) and "recorders"

window = Gtk.Window(title="PATH Imaging cameras")
window.set_default_size(1400, 640)
page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=12)
row = Gtk.Box(spacing=12, homogeneous=True)  # One column per camera, see add_column()
bar = Gtk.Box(spacing=12)  # Along the bottom
capture_button = Gtk.Button(label="Capture")
record_button = Gtk.ToggleButton(label="Record")
status = Gtk.Label()
bar.pack_start(capture_button, False, False, 0)
bar.pack_start(record_button, False, False, 0)
bar.pack_start(status, False, False, 0)
page.pack_start(row, True, True, 0)
page.pack_start(bar, False, False, 0)
window.add(page)


def add_column(name):
    """One camera's part of the window: title, live video, controls, info line. Picking the start size at the end
    starts the camera."""
    camera = CAMERAS[name]
    title = Gtk.Label()
    title.set_markup(f"<b>{camera.TITLE}</b>")
    slot = Gtk.Box()  # restart() puts the live video here, or "Camera not found"
    sizes = Gtk.ComboBoxText()
    for size in camera.MODES:
        sizes.append(size, size.replace("x", " x "))
    rates = Gtk.ComboBoxText()  # pick_size() fills it in
    exposure = Gtk.SpinButton.new_with_range(1, 1000000, 500)
    exposure.set_value(camera.EXPOSURE_US)
    low, high, step, unit = camera.GAIN_RANGE
    gain = Gtk.SpinButton.new_with_range(low, high, step)
    info = Gtk.Label(xalign=0)

    controls = Gtk.Box(spacing=6)
    widgets = [Gtk.Label(label="Size"), sizes, Gtk.Label(label="Frame rate"), rates,
               Gtk.Label(label="Exposure µs"), exposure, Gtk.Label(label=f"Gain {unit}"), gain]
    for widget in widgets:
        controls.pack_start(widget, False, False, 0)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.pack_start(title, False, False, 0)
    box.pack_start(slot, True, True, 0)  # The video gets the spare room
    box.pack_start(controls, False, False, 0)
    box.pack_start(info, False, False, 0)  # On its own line: beside the controls the window got wider than the screen
    row.pack_start(box, True, True, 0)  # Before the camera starts: outside the window the video opens one of its own

    columns[name] = {"sizes": sizes, "rates": rates, "slot": slot, "info": info, "exposure": exposure, "gain": gain}
    sizes.connect("changed", pick_size, name)  # GTK then calls pick_size(sizes, name)
    rates.connect("changed", pick_rate, name)
    exposure.connect("value-changed", change_exposure, name)
    gain.connect("value-changed", change_gain, name)
    width, height, fps = camera.START
    sizes.set_active_id(f"{width}x{height}")  # Fills in the frame rates, which starts the camera


def pick_size(sizes, name):
    """A size was picked: offer the frame rates it allows, keeping the current one if it is among them.
    That picks a rate, which starts the camera again."""
    camera = CAMERAS[name]
    rates = columns[name]["rates"]
    keep = rates.get_active_id()
    if keep is None:  # At startup: the camera's own start rate
        start_width, start_height, start_fps = camera.START
        if start_fps is None:  # Fastest, which the list calls 0
            start_fps = 0
        keep = str(start_fps)
    rates.remove_all()  # Fires "changed" with nothing picked, which pick_rate() ignores
    for fps in camera.MODES[sizes.get_active_id()]:
        if fps == 0:
            label = "max"
        else:
            label = f"{fps} fps"
        rates.append(str(fps), label)
    kept = rates.set_active_id(keep)
    if not kept:  # This size does not allow that rate: take its first
        rates.set_active(0)


def pick_rate(rates, name):  # Also called with nothing picked, while pick_size() refills the list
    if rates.get_active_id() is not None:
        restart(name)


def restart(name):
    """Stop the camera if it is streaming, then start it with what its column shows. Also its first start."""
    column = columns[name]
    slot = column["slot"]
    if name in cams:
        old = cams.pop(name)  # Forgotten first: if anything below fails, no closed camera is left in cams
        dual_stream.close(old)
    for child in slot.get_children():  # The old video, or "Camera not found"
        slot.remove(child)
    column["info"].set_text("")
    width, height = column["sizes"].get_active_id().split("x")  # The id is like "1920x1080"
    fps = int(column["rates"].get_active_id())  # 0 = fastest
    exposure = int(column["exposure"].get_value())
    cam = dual_stream.start(name, int(width), int(height), fps, sink=dual_stream.GTK_SINK, exposure=exposure)
    if cam is None:
        slot.pack_start(Gtk.Label(label="Camera not found"), True, True, 0)
        slot.show_all()  # A new widget stays hidden until it is shown
        return
    slot.pack_start(dual_stream.video_widget(cam), True, True, 0)
    slot.show_all()
    dual_stream.set_gain(cam, column["gain"].get_value())
    dual_stream.play(cam)  # Only once the video widget is inside the window
    cams[name] = cam


def change_exposure(exposure, name):  # Nothing to do for a camera that was not found
    if name in cams:
        dual_stream.set_exposure(cams[name], int(exposure.get_value()))


def change_gain(gain, name):
    if name in cams:
        dual_stream.set_gain(cams[name], gain.get_value())


def gain_text(cam):  # Like "2.5x"; the colour camera on the Pi: "12dB"
    low, high, step, unit = CAMERAS[cam["name"]].GAIN_RANGE
    gain = cam["gain"]
    return f"{gain:g}{unit}"  # :g writes 4.0 as 4


def file_path(cam, stamp):
    """Where a capture or a recording of this camera goes, without the ending. A gain like 2.5x puts a dot in the
    name, so the endings (.raw, .png, .j2ks) are added as text."""
    sensor = CAMERAS[cam["name"]].SENSOR
    width = cam["width"]
    height = cam["height"]
    if cam["fps"] == 0:
        rate = "max"
    else:
        rate = cam["fps"]
    exposure = cam["exposure"]
    gain = gain_text(cam)
    return str(CAPTURES / sensor / stamp / f"{width}x{height}_{rate}fps_{exposure}us_{gain}")


def capture(button):  # Each camera's newest frame, saved in the background so the live views do not pause
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    for cam in cams.values():
        frame = cam["last"]
        if frame is not None:
            path = file_path(cam, stamp)
            threading.Thread(target=save, args=[cam["name"], frame, path]).start()
    status.set_text(f"Saving {stamp} ...")


def save(name, frame, path):  # In a background thread, which may not touch GTK: idle_add has the main thread do it
    folder = Path(path).parent
    folder.mkdir(parents=True, exist_ok=True)
    (frame >> 6).tofile(path + ".raw")  # True 10-bit values
    cv2.imwrite(path + ".png", CAMERAS[name].picture(frame))  # As the window shows it: unstretched, a dim scene looks black
    GLib.idle_add(status.set_text, f"Saved {CAPTURES}/*/{folder.name}/")


def toggle_record(button):
    on = button.get_active()
    for column in columns.values():  # Picking a size or rate restarts the camera, which would cut the recording
        column["sizes"].set_sensitive(not on)
        column["rates"].set_sensitive(not on)
    if on:
        button.set_label("Stop recording")
        start_recording()
    else:
        button.set_label("Record")
        stop_recording()


def start_recording():
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    made = {}  # By camera name
    for name, cam in cams.items():
        camera = CAMERAS[name]
        header = {  # Kept in the file
            "sensor": camera.SENSOR,
            "frame_rate": str(cam["fps"]),
            "exposure_us": cam["exposure"],
            "gain": gain_text(cam),
        }
        path = file_path(cam, stamp) + recording.SUFFIX
        made[name] = recording.Recorder(path, name, cam["height"], cam["width"], header, mono=camera.MONO)
    for name, recorder in made.items():  # Only once every GPU encoder is up: starting one briefly stalls the cameras
        cams[name]["recorder"] = recorder


def stop_recording():  # Also at exit. Does nothing when not recording
    recorders = active_recorders()
    for cam in cams.values():  # First tell the cameras to stop handing over frames, then close the files
        cam["recorder"] = None
    for recorder in recorders:
        recorder.close()  # Writes the frames still waiting and closes the file; the .mp4 follows
    if recorders:
        text = recording_text(recorders)
        folder = recorders[0].path.parent.name
        making["text"] = f"Recorded {text}  ·  {CAPTURES}/*/{folder}/"
        making["recorders"] = recorders
        status.set_text(making["text"] + "  ·  making the .mp4s ...")


def active_recorders():  # One per camera while recording, otherwise none
    recorders = []
    for cam in cams.values():
        if cam["recorder"] is not None:
            recorders.append(cam["recorder"])
    return recorders


def mp4s_made(recorders):  # True once every recorder's .mp4 is finished
    for recorder in recorders:
        if not recorder.video.done.is_set():
            return False
    return True


def mp4_error(recorders):  # Why a recording has no .mp4 (its tools are missing, or making it failed), or None
    for recorder in recorders:
        if recorder.video.error:
            return recorder.video.error
    return None


def recording_text(recorders):
    """For the status line: 12 s  ·  TEVS-AR0822-M 720 frames, IMX412C 240 frames  ·  2.41 GB  ·  none lost"""
    seconds = time.time() - recorders[0].started
    frames = []
    errors = []
    dropped = 0
    skipped = 0
    total_bytes = 0
    for recorder in recorders:
        sensor = recorder.path.parent.parent.name  # The file is in <sensor>/<time>/
        frames.append(f"{sensor} {recorder.written} frames")
        dropped += recorder.dropped
        skipped += recorder.skipped
        total_bytes += recorder.bytes
        if recorder.error:
            errors.append(recorder.error)
    lost = []
    if dropped:
        lost.append(f"{dropped} DROPPED: the encoder fell behind{POWER_HINT}")
    if skipped:
        lost.append(f"{skipped} skipped by a camera")
    if not lost:
        lost.append("none lost")
    gigabytes = total_bytes / 1e9
    parts = [f"{seconds:.0f} s", ", ".join(frames), f"{gigabytes:.2f} GB", ", ".join(lost)]
    return "  ·  ".join(parts + errors)


def update_info():  # Once a second: the measured frame rates, and the recording's progress
    for name, cam in cams.items():
        width = cam["width"]
        height = cam["height"]
        fps = dual_stream.measured_fps(cam)
        columns[name]["info"].set_text(f"{width} x {height}  ·  RAW10  ·  {fps:.1f} fps")
    recorders = active_recorders()
    if recorders:
        status.set_text("Recording " + recording_text(recorders))
    elif making and mp4s_made(making["recorders"]):
        error = mp4_error(making["recorders"])
        if error:
            status.set_text(making["text"] + "  ·  " + error)
        else:
            status.set_text(making["text"] + "  ·  .mp4s ready")
        making.clear()
    return True  # Keep being called


for camera_name in CAMERAS:
    add_column(camera_name)
capture_button.connect("clicked", capture)
record_button.connect("toggled", toggle_record)
reason = recording.unavailable()  # None if this machine can record
if reason:
    record_button.set_sensitive(False)
    record_button.set_tooltip_text(reason)
GLib.timeout_add(1000, update_info)
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, Gtk.main_quit)  # Ctrl+C: like closing the window
window.connect("destroy", Gtk.main_quit)
window.show_all()
Gtk.main()

stop_recording()  # Before the cameras close, so both recordings end at the same moment
for cam in cams.values():
    dual_stream.close(cam)
