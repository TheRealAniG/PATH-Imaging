"""Temporary workaround so TechNexion's pyvizionsdk finds the camera on a Raspberry Pi CM5.

VizionSDK only accepts the Pi 5 board ID (raspberrypi,5-model-b); a CM5 reports
raspberrypi,5-compute-module, so VxDiscoverCameraDevices() finds nothing even though
the camera hardware path is identical. This restarts Python with a small preloaded
library (scripts/cm5_board_id_shim.c) that shows the SDK the Pi 5 board ID. Nothing
else is changed. Remove once TechNexion supports the CM5.

Either import it before pyvizionsdk:

    import vizion_cm5  # noqa: F401  (must come first)
    import pyvizionsdk

or run an unmodified script through it:

    python vizion_cm5.py pyvizionSample.py
"""
import os
from pathlib import Path
import runpy
import subprocess
import sys

_SOURCE = Path(__file__).resolve().parent / "scripts" / "cm5_board_id_shim.c"
_CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "path-imaging"
_BOARD_ID = Path("/sys/firmware/devicetree/base/compatible")


def _link_camera():
    """VizionSDK skips route setup on Raspberry Pi, and the CSI-2 -> capture link is off after
    boot, so discovery finds nothing until something enables it."""
    for media in sorted(Path("/dev").glob("media*")):
        result = subprocess.run(["media-ctl", "-d", media, "-l", '"csi2":4 -> "rp1-cfe-csi2_ch0":0 [1]'],
                                capture_output=True)
        if result.returncode == 0:
            return


def _enable():
    if "cm5_board_id_shim" in os.environ.get("LD_PRELOAD", ""):
        return  # Already running with the shim
    _link_camera()
    if b"raspberrypi,5-compute-module" not in _BOARD_ID.read_bytes():
        return  # Not a CM5: the SDK works as is
    _CACHE.mkdir(parents=True, exist_ok=True)
    shim = _CACHE / "cm5_board_id_shim.so"
    if not shim.exists() or shim.stat().st_mtime < _SOURCE.stat().st_mtime:
        subprocess.run(["gcc", "-shared", "-fPIC", "-o", shim, _SOURCE, "-ldl"], check=True)
    fake = _CACHE / "compatible"
    fake.write_bytes(b"raspberrypi,5-model-b\0brcm,bcm2712\0")
    os.environ["LD_PRELOAD"] = " ".join(filter(None, [str(shim), os.environ.get("LD_PRELOAD")]))
    os.environ["VX_FAKE_COMPATIBLE"] = str(fake)
    # The loader only reads LD_PRELOAD at startup, so restart this same command.
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python vizion_cm5.py script.py [args...]")
    _enable()
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    sys.path.insert(0, str(Path(script).resolve().parent))
    runpy.run_path(script, run_name="__main__")
else:
    _enable()
