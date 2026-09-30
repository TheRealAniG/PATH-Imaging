"""Load Debian's GTK/GStreamer bindings from a uv-managed Python environment."""
from pathlib import Path
import sys

try:
    import gi
except ModuleNotFoundError as error:
    if error.name != "gi":
        raise
    system_packages = Path("/usr/lib/python3/dist-packages")
    if not (system_packages / "gi").is_dir():
        raise ModuleNotFoundError(
            "GTK bindings are missing. Install python3-gi and the GTK/GStreamer "
            "packages listed in README.md."
        ) from error
    # Append so packages installed by uv retain priority over system packages.
    sys.path.append(str(system_packages))
    import gi
