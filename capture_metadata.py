"""Read camera state without changing settings; summarize host capture timing."""
from datetime import datetime, timezone
import importlib.metadata
import platform
from pathlib import Path
import re
import subprocess
import time

import numpy as np
import camera_API as vx


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def command(*args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return {"returncode": result.returncode, "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip()}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"error": str(error)}


def camera_snapshot(camera):
    state = {"utc": utc_now(), "monotonic_ns": time.monotonic_ns()}
    try:
        media, sensor, video = vx._camera_route()
        node = command("media-ctl", "-d", media, "-e", sensor)
        state.update(media_device=media, sensor_entity=sensor, video_device=video,
                     sensor_device=node.get("stdout"))
        state["media_topology"] = command("media-ctl", "-d", media, "-p")
        state["video_format"] = command("v4l2-ctl", "-d", video, "--get-fmt-video")
        if node.get("returncode") == 0:
            controls = command("v4l2-ctl", "-d", node["stdout"], "--list-ctrls-menus")
            state["v4l2_controls_raw"] = controls
            state["v4l2_controls"] = {
                match[1]: {"value": int(match[2]), "label": match[3]}
                for match in re.finditer(
                    r"^\s*(\w+) 0x[^\n]*?\bvalue=(-?\d+)(?: \(([^\n)]*)\))?",
                    controls.get("stdout", ""), re.MULTILINE)
            }
    except (RuntimeError, subprocess.CalledProcessError) as error:
        state["pipeline_error"] = str(error)
    bypass = Path("/sys/module/tevs/parameters/bypass_isp")
    state["isp_bypassed"] = bypass.read_text().strip() == "Y" if bypass.exists() else None
    for name in ("VxGetCurrentExposure", "VxGetTEVSFirmwareVersion", "VxGetSensorFirmwareVersion"):
        try:
            result, value = getattr(vx, name)(camera)
            state[name] = {"returncode": int(result), "value": value if result == 0 else None}
        except (AttributeError, RuntimeError, TypeError) as error:
            state[name] = {"error": str(error)}
    state["exposure_notes"] = (
        "V4L2 values are driver control snapshots, potentially cached; exposure and AE limits "
        "use microseconds. Auto-exposure can change actual exposure/gain. "
        "VxGetCurrentExposure is an SDK snapshot, not tied to a specific saved frame; "
        "its raw returned value is retained without inferring units."
    )
    return state


def provenance():
    files = ("camera_capture.py", "camera_API.py", "capture_metadata.py",
             "scripts/capture_raw8_1080p60.py")
    import hashlib
    return {"kernel": platform.release(), "machine": platform.machine(),
            "python": platform.python_version(),
            "packages": {name: importlib.metadata.version(name)
                         for name in ("numpy", "pillow", "pyvizionsdk")},
            "git_commit_at_capture": command("git", "rev-parse", "HEAD"),
            "git_status_at_capture": command("git", "status", "--porcelain"),
            "source_sha256": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
                              for name in files},
            "driver": command("modinfo", "tevs")}


def timing_summary(frames, acquisition_ns, startup_ns, timeouts):
    arrivals = [frame["received_monotonic_ns"] for frame in frames]
    intervals = np.diff(arrivals).astype(np.float64) / 1e6
    span_ns = arrivals[-1] - arrivals[0] if len(arrivals) > 1 else 0
    return {"timestamp_source": "host monotonic clock after VxGetImage returns",
            "sensor_timestamps_available": False, "sensor_sequence_numbers_available": False,
            "dropped_frames": None, "startup_seconds": startup_ns / 1e9,
            "acquisition_seconds": acquisition_ns / 1e9, "timeouts": timeouts,
            "host_delivery_fps": (len(arrivals) - 1) * 1e9 / span_ns if span_ns else None,
            "interarrival_ms": {"min": float(intervals.min()), "mean": float(intervals.mean()),
                                "median": float(np.median(intervals)),
                                "p95": float(np.percentile(intervals, 95)),
                                "max": float(intervals.max()), "std": float(intervals.std())}
                               if intervals.size else None,
            "notes": "Frames are buffered in RAM and saved after streaming stops. Host delivery "
                     "includes pipe buffering and copying; it does not prove sensor FPS or "
                     "absence of dropped frames. A ten-frame sample is a short performance check."}
