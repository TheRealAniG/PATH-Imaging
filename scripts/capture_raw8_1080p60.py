"""Save ten RAW8 1920x1080 frames at the camera's 60 fps mode, without a window.

Run from the worktree root: uv run scripts/capture_raw8_1080p60.py
"""
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parent.parent
    subprocess.run([
        sys.executable, str(root / "camera_capture.py"),
        "--raw8", "--frames", "10", "--width", "1920", "--height", "1080",
        "--fps", "60",
    ], cwd=root, check=True)


if __name__ == "__main__":
    main()
