"""Window showing both cameras live, with size and frame rate pickers and the measured frame rate.

  FL:    TechNexion TEVS-AR0822-M on CAM/DISP 0 (mono RAW10)
  Color: Vision Components IMX412C on CAM/DISP 1 (colour RAW10, full 4032x3040 sensor; smaller sizes are centre crops)

Both cameras use a fixed manual exposure and gain, set in the Exposure (microseconds) and Gain boxes, applied live. The live views are
brightness stretched (and the colour one white balanced) like vc.to_rgb8.
Picking a size or frame rate restarts that camera. Starting modes are CAMERAS in dual_stream.py.
Capture saves each camera's latest frame, full size, as ~/Desktop/PATH-captures/<sensor>/<time>/<size>_<fps>_<exposure>us_<gain>.png
and .raw. The .png is the picture the window shows (FL 16-bit mono; colour 8-bit, demosaiced, white balanced): its brightest pixels
are stretched to white frame by frame, so a dim frame is still visible but brightness cannot be compared between PNGs.
The .raw is what the camera measured, unchanged: 16-bit little-endian 10-bit values 0-1023, row by row, no header.
Record saves every frame of both cameras until it is pressed again, into the same folders as .j2ks: lossless, compressed
on the Jetson's GPU as it goes (recording.py says how to read them and how to make an .mp4 to watch). Size and frame rate
cannot be changed while recording. The status line counts frames lost: in the 25W power mode the colour camera loses a few
in a hundred when other programs are busy; MAXN_SUPER (sudo nvpmodel -m 2) has the room.
Close the window or press Ctrl+C to stop.
Run: uv run python camera_ui.py
"""
from pathlib import Path
import signal
import subprocess
import threading
import time

import cv2
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst, Gtk

from dual_stream import CAMERAS, EXPOSURE_US, GAIN, close, picture, run, set_exposure, set_gain, start
import recording
from vc import tegra

TITLES = {"fl": "FL: TEVS-AR0822-M (mono, CAM0)", "color": "Color: IMX412C (CAM1)"}
if tegra.IS_JETSON:  # The connectors are the other way round there, and can be swapped
    TITLES = {"fl": "FL: TEVS-AR0822-M (mono)", "color": "Color: IMX412C"}
SENSORS = {"fl": "TEVS-AR0822-M", "color": "IMX412C"}
CAPTURES = Path.home() / "Desktop" / "PATH-captures"  # Created on the first capture
POWER_HINT = ""  # Added to the dropped-frames message when the board is not in its fastest power mode
if tegra.IS_JETSON and "MAXN" not in subprocess.run(["nvpmodel", "-q"], capture_output=True, text=True).stdout:
    POWER_HINT = " (power mode is not MAXN_SUPER: sudo nvpmodel -m 2)"
TEVS = [60, 30, 20, 15, 10, 5]
MODES = {  # Sizes offered and the frame rates each allows (0 = as fast as the sensor can)
    "fl": {"640x480": TEVS, "1280x720": TEVS, "1920x1080": TEVS, "2560x1440": TEVS[1:], "3840x2160": TEVS[3:]},
    "color": dict.fromkeys(["640x480", "1280x960", "1920x1080", "3840x2160", "4032x3040"], [0, 60, 30, 15, 10, 5]),
}

Gst.init(None)
window = Gtk.Window(title="PATH Imaging cameras")
window.set_default_size(1400, 640)
page, row, bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=12), Gtk.Box(spacing=12, homogeneous=True), Gtk.Box(spacing=12)
button, record, status = Gtk.Button(label="Capture"), Gtk.ToggleButton(label="Record"), Gtk.Label()
bar.pack_start(button, False, False, 0)
bar.pack_start(record, False, False, 0)
bar.pack_start(status, False, False, 0)
page.pack_start(row, True, True, 0)
page.pack_start(bar, False, False, 0)
window.add(page)
cams, threads, ui = {}, {}, {}


def stop_camera(name):
    close(cams.pop(name), threads[name])


def restart(name):
    sizes, rates, slot, info, exposure, gain = ui[name]
    if name in cams:
        stop_camera(name)
    for child in slot.get_children():
        slot.remove(child)
    width, height = map(int, sizes.get_active_id().split("x"))
    cam = start(name, *CAMERAS[name][:3], width, height, int(rates.get_active_id()), sink="gtksink name=sink sync=false",
                exposure=int(exposure.get_value()))
    slot.pack_start(cam["window"].get_by_name("sink").get_property("widget") if cam else Gtk.Label(label="Camera not found"),
                    True, True, 0)
    slot.show_all()
    info.set_text("")
    if cam:
        set_gain(name, cam, gain.get_value())
        cam["window"].set_state(Gst.State.PLAYING)  # Only once the video widget is inside the window
        cam["counted"] = (0, time.monotonic())
        cams[name], threads[name] = cam, threading.Thread(target=run, args=(name, cam))
        threads[name].start()


def pick_size(sizes, name):  # Offer the frame rates this size allows, keeping the current one if it is among them
    rates = ui[name][1]
    keep = rates.get_active_id() or str(CAMERAS[name][5] or 0)
    rates.remove_all()  # Leaves nothing picked, which pick_rate ignores
    for fps in MODES[name][sizes.get_active_id()]:
        rates.append(str(fps), f"{fps} fps" if fps else "max")
    if not rates.set_active_id(keep):
        rates.set_active(0)


def pick_rate(rates, name):
    if rates.get_active_id():
        restart(name)


def pick_exposure(exposure, name):
    if name in cams:
        set_exposure(cams[name], int(exposure.get_value()))


def pick_gain(gain, name):
    if name in cams:
        set_gain(name, cams[name], gain.get_value())


def file_path(name, cam, stamp):  # Without the ending (.png, .raw, .j2ks). A gain like 1.5x puts a dot in it, so endings are added as text
    height, width = cam["size"]
    fps = ui[name][1].get_active_id()
    return str(Path(CAPTURES, SENSORS[name], stamp, f"{width}x{height}_{fps if fps != '0' else 'max'}fps_{cam['exposure']}us_{cam['gain']:g}{GAIN[name][3]}"))


def capture(button):  # Takes whatever frame each camera has just delivered; saving runs in the background
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    for name, cam in cams.items():
        if cam["last"] is not None:
            threading.Thread(target=save, args=(name, cam["last"], file_path(name, cam, stamp))).start()
    status.set_text(f"Saving {stamp} ...")


def save(name, frame, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    (frame >> 6).tofile(path + ".raw")  # True 10-bit values
    cv2.imwrite(path + ".png", picture(name, frame))  # As the window shows it: the unstretched values look black when the scene is dim
    GLib.idle_add(status.set_text, f"Saved {CAPTURES}/*/{Path(path).parent.name}/")


def toggle_record(record):
    for sizes, rates, *_ in ui.values():  # A new size or frame rate restarts the camera, which would cut the recording
        sizes.set_sensitive(not record.get_active())
        rates.set_sensitive(not record.get_active())
    record.set_label("Stop recording" if record.get_active() else "Record")
    if not record.get_active():
        return stop_recording()
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    made = {name: recording.Recorder(file_path(name, cam, stamp) + recording.SUFFIX, name, *cam["size"], {
        "sensor": SENSORS[name], "frame_rate": ui[name][1].get_active_id(), "exposure_us": cam["exposure"],
        "gain": f"{cam['gain']:g}{GAIN[name][3]}"}) for name, cam in cams.items()}
    for name, recorder in made.items():  # Only once both GPU encoders are up: starting one briefly stalls the cameras
        cams[name]["recorder"] = recorder


def stop_recording():
    recorders = [cam.pop("recorder") for cam in cams.values() if "recorder" in cam]
    for recorder in recorders:
        recorder.close()
    if recorders:
        status.set_text(f"Recorded {recording_text(recorders)}  ·  {CAPTURES}/*/{recorders[0].path.parent.name}/")


def recording_text(recorders):  # "12 s  ·  TEVS-AR0822-M 720 frames, IMX412C 240 frames  ·  2.41 GB  ·  none lost"
    lost = [f"{count} {what}" for count, what in ((sum(r.dropped for r in recorders), "DROPPED: the GPU fell behind" + POWER_HINT),
                                                  (sum(r.skipped for r in recorders), "skipped by a camera")) if count]
    return (f"{time.time() - recorders[0].started:.0f} s  ·  " + ", ".join(f"{r.path.parents[1].name} {r.written} frames" for r in recorders)
            + f"  ·  {sum(r.bytes for r in recorders) / 1e9:.2f} GB  ·  " + (", ".join(lost) or "none lost")
            + "".join(f"  ·  {r.error}" for r in recorders if r.error))


def update_info():
    for name, cam in cams.items():
        frames, then = cam["counted"]
        now = time.monotonic()
        height, width = cam["size"]
        ui[name][3].set_text(f"{width} x {height}  ·  RAW10  ·  {(cam['frames'] - frames) / (now - then):.1f} fps")
        cam["counted"] = (cam["frames"], now)
    if recorders := [cam["recorder"] for cam in cams.values() if "recorder" in cam]:
        status.set_text(f"Recording {recording_text(recorders)}")
    return True  # Keep updating


for name, spec in CAMERAS.items():
    column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    title = Gtk.Label()
    title.set_markup(f"<b>{TITLES.get(name, name)}</b>")
    sizes, rates, slot, info = Gtk.ComboBoxText(), Gtk.ComboBoxText(), Gtk.Box(), Gtk.Label()
    exposure = Gtk.SpinButton.new_with_range(1, 1000000, 500)
    exposure.set_value(EXPOSURE_US[name])
    low, high, step, unit, _ = GAIN[name]
    gain = Gtk.SpinButton.new_with_range(low, high, step)
    for size in MODES[name]:
        sizes.append(size, size.replace("x", " x "))
    controls = Gtk.Box(spacing=6)
    for widget in (Gtk.Label(label="Size"), sizes, Gtk.Label(label="Frame rate"), rates, Gtk.Label(label="Exposure µs"), exposure,
                   Gtk.Label(label=f"Gain {unit}"), gain):
        controls.pack_start(widget, False, False, 0)
    info.set_xalign(0)  # On its own line: beside the boxes it made the window wider than a 1920 px screen
    for widget, expand in ((title, False), (slot, True), (controls, False), (info, False)):
        column.pack_start(widget, expand, expand, 0)
    row.pack_start(column, True, True, 0)
    ui[name] = sizes, rates, slot, info, exposure, gain
    sizes.connect("changed", pick_size, name)
    rates.connect("changed", pick_rate, name)
    exposure.connect("value-changed", pick_exposure, name)
    gain.connect("value-changed", pick_gain, name)
    sizes.set_active_id(f"{spec[3]}x{spec[4]}")  # Fills in the frame rates, which starts the camera

button.connect("clicked", capture)
record.connect("toggled", toggle_record)
if reason := recording.unavailable():
    record.set_sensitive(False)
    record.set_tooltip_text(reason)
GLib.timeout_add(1000, update_info)
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, Gtk.main_quit)
window.connect("destroy", Gtk.main_quit)
window.show_all()
Gtk.main()

stop_recording()
for name in list(cams):
    stop_camera(name)
