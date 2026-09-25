"""Exercise CLI and SDK lifecycle using a deterministic SDK double."""
import csv
import os
from pathlib import Path
import subprocess
import sys
import tempfile

exe = str(Path(sys.argv[1]).resolve())
checks = 0

def run(args, scenario="", expected=0, lifecycle=None):
    global checks
    with tempfile.TemporaryDirectory() as root:
        root = Path(root)
        trace = root / "trace"
        output = root / "capture"
        args = [str(output) if a == "OUTPUT" else a for a in args]
        result = subprocess.run([exe, *args], env={**os.environ, "FAKE_SCENARIO": scenario,
                                "FAKE_TRACE": str(trace)}, capture_output=True, text=True)
        assert result.returncode == expected, (scenario, args, result.stdout, result.stderr)
        calls = trace.read_text().splitlines() if trace.exists() else []
        if lifecycle is not None:
            assert [c for c in calls if c in ("open", "start", "stop", "close")] == lifecycle, calls
        if expected == 0 and output.exists():
            rows = list(csv.DictReader((output / "frames.csv").open()))
            assert len(rows) == 2, rows
            for row in rows:
                assert (output / row["file"]).read_bytes() == bytes([42]) * 16
                assert row["media_type"] == "7"
        if scenario in ("timeout", "corrupt"):
            assert calls.count("capture") == 4, calls
        checks += 1

capture = ["--device", "0", "--format", "0", "--frames", "2", "--output", "OUTPUT"]
run(["--help"])
run(["--list"])
run(["--list"], "no_devices", 1)
run(["--device", "0", "--formats"], lifecycle=["open", "close"])
run(capture, lifecycle=["open", "start", "stop", "close"])
run(capture, "recover", lifecycle=["open", "start", "stop", "close"])
for scenario in ("timeout", "corrupt", "occupied", "error", "bad_size"):
    run(capture, scenario, 1, ["open", "start", "stop", "close"])
for scenario in ("verify", "usb", "empty_formats", "formats", "set"):
    run(capture, scenario, 1, ["open", "close"])
run(capture, "start", 1, ["open", "start", "close"])
run(capture, "open", 1, ["open"])
run(capture, "null_camera", 1, [])
run(capture, "interrupt", 130, ["open", "start", "stop", "close"])
run(capture, "stop", 1, ["open", "start", "stop", "stop", "close"])
run(capture, "close", 1, ["open", "start", "stop", "close", "close"])
run(["--device", "0", "--format", "1", "--output", "OUTPUT"], expected=1,
    lifecycle=["open", "close"])
run(["--device", "9", "--formats"], expected=1, lifecycle=[])
run(["--device", "0", "--format", "9", "--output", "OUTPUT"], expected=1,
    lifecycle=["open", "close"])
for args in ([], ["--unknown"], ["--device"], ["--device", "-1"],
             ["--timeout-ms", "65536"], ["--frames", "0"], ["--retries", "1x"]):
    run(args, expected=1, lifecycle=[])
with tempfile.TemporaryDirectory() as existing:
    result = subprocess.run([exe, "--device", "0", "--format", "0", "--output", existing],
                            capture_output=True)
    assert result.returncode == 1
    assert not list(Path(existing).iterdir())
    checks += 1
print(f"Passed {checks} capture behavior checks")
