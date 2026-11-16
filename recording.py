"""Lossless RAW10 video: recorded by camera_ui.py's Record button, read back here.

Each frame is compressed on its own with lossless JPEG 2000 (High-Throughput), so what comes back is bit for bit what
the camera delivered. On a Jetson the GPU compresses (NVIDIA nvJPEG2000). Anywhere else, the Raspberry Pi included,
the CPU does (OpenJPH, from the imagecodecs package): the same files, far slower, so a camera that sends more than
the CPU can compress loses frames, which are counted. One file per camera, <name>.j2ks:

    line 1     "PATH RAW10 recording 1"
    line 2     JSON: camera, sensor, width, height, pattern, frame rate, exposure, gain, start time, ...
    per frame  8 bytes capture time (float64, seconds, the driver's monotonic clock), 4 bytes length (uint32), then a
               JPEG 2000 codestream. Mono: one 10-bit plane. Colour: the four Bayer planes (R, G on the red row,
               G on the blue row, B), each half the width and height.

Reading uses NVIDIA's decoder, OpenJPH or OpenCV, whichever is installed. A copy to watch, <name>.mp4, is made beside
the recording right after it stops: lossy 8-bit H.264 of the pictures the live view showed, which is not every frame.

    rec = Recording("file.j2ks")              # rec.info, len(rec), rec.times; rec[i] is (height, width) uint16, 0-1023
    python recording.py file.j2ks             what is in it, and how many frames are missing
    python recording.py file.j2ks --check     also decodes every frame
    python recording.py file.j2ks --png 0     frame 0 as a 16-bit PNG beside it (colour demosaiced)
    python recording.py file.j2ks --mp4       replaces that .mp4 with one of every frame, at the live view's full size
"""
import collections
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import queue
import struct
import sys
import threading
import time

import numpy as np

SUFFIX = ".j2ks"
MAGIC = b"PATH RAW10 recording 1\n"
FRAME = struct.Struct("<dI")  # Capture time, length
PLANES = ((0, 0), (0, 1), (1, 0), (1, 1))  # Row and column offset of each Bayer plane in the mosaic
WAITING_BYTES = 60e6  # Memory for frames waiting for the encoder (at least 3 frames); beyond it frames are dropped
CPU_THREADS = max(1, (os.cpu_count() or 4) - 1)  # Without a GPU each recording compresses this many frames at once
VIDEO_FPS = 15  # About how often the live view gets a picture (dual_stream.DISPLAY_FPS)
VIDEO_WIDEST = 1024  # The .mp4 made of a recording is at most this wide
ENCODE = ("videoconvert ! video/x-raw,format=I420 "  # Left alone, colour becomes 4:4:4, which players refuse
          "! x264enc speed-preset=veryfast tune=zerolatency bitrate=6000 "  # zerolatency: no picture is held back, so none is lost at the end
          "! h264parse ! mp4mux ! filesink name=out")
RECORDING, ENCODING, LOCK = 0, set(), threading.Lock()  # How many Recorders are open; the Videos making their .mp4, paused while that is not 0


def _nvidia():  # NVIDIA's image codec module. Its first import prints errors about formats we do not use: stderr is off for it
    if "nvidia.nvimgcodec" not in sys.modules:
        sys.stderr.flush()
        keep, null = os.dup(2), os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, 2)
        try:
            import nvidia.nvimgcodec  # noqa: F401
        finally:
            os.dup2(keep, 2)
            os.close(keep)
            os.close(null)
    return sys.modules["nvidia.nvimgcodec"]


def encoder():  # What compresses recordings on this machine: "gpu" (NVIDIA nvJPEG2000), "cpu" (OpenJPH) or None
    try:
        if "nvjpeg2k_encoder" in _nvidia().get_encoder_ids():
            return "gpu"
    except ImportError:
        pass
    try:
        import imagecodecs
        return "cpu" if imagecodecs.HTJ2K.available else None
    except ImportError:
        return None


def unavailable():  # None if this machine can record, otherwise the reason
    return None if encoder() else "recording needs the imagecodecs package (uv sync); a Jetson can use NVIDIA's nvimgcodec and nvjpeg2k instead"


def _background(megapixels):
    """For a thread that compresses on the CPU: capture and the window go first, and so does a camera with smaller
    frames. Then one that sends more than the CPU can compress (full-size colour) loses its own frames, not the
    other camera's. Linux gives each thread its own niceness."""
    os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), min(19, 10 + round(megapixels / 2)))


class Video:
    """An .mp4 to watch: lossy 8-bit H.264 of the pictures the live view shows (16-bit grey or 8-bit BGR), each at its
    capture time. fps: about how many pictures a second to expect; the encoder plans its bit rate on it.

    live is for while recording. H.264 runs on the CPU, and encoding then cost the colour camera frames, so add() only
    stores the pictures (thinned to VIDEO_WIDEST, never waiting: one there is no room for is left out) in <name>.mkv,
    and close() starts turning that into the .mp4 in the background; done is set once the .mp4 is there.
    Otherwise the .mp4 is written as the pictures come, every one, at its own size.
    error says why there is no .mp4, if there is none (None otherwise): the recording itself does not depend on it."""
    def __init__(self, path, fps, live=False):
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        self.Gst, self.path, self.fps, self.live, self.first = Gst, Path(path), max(1, round(fps)), live, None
        self.stored = self.path.with_suffix(".mkv") if live else self.path
        self.done = threading.Event()
        self.error = next((f"no .mp4: GStreamer has no {name} (sudo apt install gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly)"
                           for name in ("matroskamux", "matroskademux", "x264enc", "h264parse", "mp4mux") if not Gst.ElementFactory.find(name)), None)
        if self.error:
            return self.done.set()
        self.pipe = self._start("appsrc name=src format=time " + (
            "max-buffers=30 leaky-type=downstream ! matroskamux ! filesink name=out" if live else "max-buffers=4 block=true ! " + ENCODE), self.stored)
        self.src = self.pipe.get_by_name("src")

    def _start(self, description, out, source=None, playing=True):
        pipe = self.Gst.parse_launch(description)
        pipe.get_by_name("out").set_property("location", str(out))
        if source:
            pipe.get_by_name("in").set_property("location", str(source))
        pipe.set_state(self.Gst.State.PLAYING if playing else self.Gst.State.PAUSED)
        return pipe

    def _wait(self, pipe):  # Until the pipeline has written its file: True, or it failed: False
        return pipe.get_bus().timed_pop_filtered(self.Gst.CLOCK_TIME_NONE, self.Gst.MessageType.EOS | self.Gst.MessageType.ERROR).type == self.Gst.MessageType.EOS

    def add(self, shown, stamp):
        if self.error:
            return
        step = -(-shown.shape[1] // VIDEO_WIDEST) if self.live else 1
        shown = shown[::step, ::step]
        shown = shown[:shown.shape[0] // 2 * 2, :shown.shape[1] // 4 * 4]  # H.264 wants even sizes, GStreamer rows of whole 4 bytes
        if shown.dtype != np.uint8:
            shown = (shown >> 8).astype(np.uint8)
        if self.first is None:
            self.first = self.last = stamp
            self.src.set_property("caps", self.Gst.Caps.from_string(
                f"video/x-raw,format={'GRAY8' if shown.ndim == 2 else 'BGR'},width={shown.shape[1]},height={shown.shape[0]},framerate={self.fps}/1"))
        buffer = self.Gst.Buffer.new_wrapped(shown.tobytes())
        buffer.pts = buffer.dts = int((stamp - self.first) * self.Gst.SECOND)
        if stamp > self.last:  # Only the last picture's is used (the others end where the next begins); without it players skip that picture
            buffer.duration = int((stamp - self.last) * self.Gst.SECOND)
        self.last = stamp
        self.src.emit("push-buffer", buffer)

    def close(self):  # Finishes the file. live: returns once the pictures are stored; done says when the .mp4 is made
        if self.error:
            return
        if self.first is None:  # Not a single picture: nothing to play, no file
            self.pipe.set_state(self.Gst.State.NULL)
            self.stored.unlink(missing_ok=True)
            return self.done.set()
        self.src.emit("end-of-stream")
        self._wait(self.pipe)
        self.pipe.set_state(self.Gst.State.NULL)
        if self.live:
            threading.Thread(target=self._encode).start()
        else:
            self.done.set()

    def _encode(self):  # The stored pictures into the .mp4, pausing while anything records
        with LOCK:  # So a recording that starts or ends right now finds this pipeline
            self.encoder = self._start("filesrc name=in ! matroskademux ! " + ENCODE, self.path, self.stored, playing=not RECORDING)
            ENCODING.add(self)
        made = self._wait(self.encoder)
        with LOCK:
            ENCODING.discard(self)
        self.encoder.set_state(self.Gst.State.NULL)
        if made:
            self.stored.unlink()
        else:
            self.error = f"no .mp4: making it failed; the pictures are in {self.stored.name}"
        self.done.set()


def _recording(change):  # A recording starts (1) or ends (-1): .mp4s are only made while none runs
    global RECORDING
    with LOCK:
        RECORDING += change
        for video in ENCODING:
            video.encoder.set_state(video.Gst.State.PAUSED if RECORDING else video.Gst.State.PLAYING)


class Recorder:
    def __init__(self, path, name, height, width, info, mono):
        """Record (height, width) frames of camera name to path; returns once the encoder is ready.
        info: a dict kept in the file's header. mono: one plane, otherwise Bayer RGGB (the camera's MONO)."""
        self.path, self.mono, self.gpu = Path(path), mono, encoder() == "gpu"
        self.written = self.dropped = self.bytes = 0
        self.error = None
        self.started = time.time()
        self.times = []  # Capture time of every frame offered, to count the ones the camera skipped
        self.buf = np.empty((height, width, 1) if mono else (4, height // 2, width // 2), np.uint16)  # GPU: the frame as the encoder takes it
        self.waiting = queue.Queue(max(3, int(WAITING_BYTES // (width * height * 2))))
        if self.gpu:
            self.pool = ThreadPoolExecutor(4)  # Splits a colour frame into its planes: 6 ms on four threads, 13 ms on one
        else:
            self.pool = ThreadPoolExecutor(CPU_THREADS, initializer=_background, initargs=[width * height / 1e6])  # Each compresses whole frames
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, "wb", buffering=0)
        self.file.write(MAGIC + json.dumps({
            **info, "camera": name, "width": width, "height": height, "pattern": "mono" if mono else "RGGB", "bits": 10,
            "codec": "JPEG 2000 High-Throughput, lossless", "encoder": "NVIDIA nvJPEG2000 (GPU)" if self.gpu else "OpenJPH (CPU)",
            "planes": 1 if mono else 4,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started))}).encode() + b"\n")
        self.video = Video(self.path.with_suffix(".mp4"), VIDEO_FPS, live=True)
        _recording(1)
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._encode if self.gpu else self._encode_cpu)
        self.thread.start()
        self.ready.wait()

    def add(self, frame, stamp):  # One frame as the capture loop has it (value << 6) and its capture time. Never waits
        self.times.append(stamp)
        try:
            self.waiting.put_nowait((stamp, frame))
        except queue.Full:
            self.dropped += 1

    def show(self, shown, stamp):  # A picture the live view is showing and its frame's capture time: for the .mp4. Never waits
        self.video.add(shown, stamp)

    def _image(self, n, frame):  # The true 10-bit values in the layout the encoder takes
        if self.mono:
            np.right_shift(frame, 6, out=self.buf[:, :, 0])
            return n.as_image(self.buf, precision=10)
        list(self.pool.map(lambda plane: np.right_shift(frame[PLANES[plane][0]::2, PLANES[plane][1]::2], 6, out=self.buf[plane]), range(4)))
        return n.as_image(self.buf, sample_format=n.SampleFormat.P_UNCHANGED, precision=10)

    def _write(self, stamp, data):
        data = memoryview(data)
        self.file.write(FRAME.pack(stamp, data.nbytes))
        self.file.write(data)
        self.written, self.bytes = self.written + 1, self.bytes + data.nbytes

    def _encode(self):  # Encoder thread, GPU
        n = _nvidia()
        try:
            encoder = n.Encoder(backends=[n.Backend(n.BackendKind.GPU_ONLY)])
            params = n.EncodeParams(
                quality_type=n.QualityType.LOSSLESS, color_spec=n.ColorSpec.GRAY if self.mono else n.ColorSpec.UNCHANGED,
                jpeg2k_encode_params=n.Jpeg2kEncodeParams(ht=True, bitstream_type=n.Jpeg2kBitstreamType.J2K))
            shape = self.buf.shape[:2] if self.mono else (self.buf.shape[1] * 2, self.buf.shape[2] * 2)
            noise = np.random.default_rng(0).integers(0, 1024, shape, np.uint16) << 6
            encoder.encode(self._image(n, noise), "jpeg2k", params=params)  # The first encode is slow: do it before recording
        except Exception as error:
            self.error = f"GPU encoder did not start: {error}"
        self.ready.set()
        while (item := self.waiting.get()) is not None:
            if self.error:
                continue  # Keep emptying the queue so close() returns
            stamp, frame = item
            try:
                self._write(stamp, encoder.encode(self._image(n, frame), "jpeg2k", params=params))
            except Exception as error:
                self.error = f"stopped after {self.written} frames: {error}"

    def _compress(self, frame):  # CPU, in a pool thread: one frame as the codestream the GPU would write (OpenJPH)
        import imagecodecs
        if self.mono:
            return imagecodecs.htj2k_encode(frame >> 6, bitspersample=10)
        planes = np.empty((4, frame.shape[0] // 2, frame.shape[1] // 2), np.uint16)
        for plane, (row, column) in zip(planes, PLANES):
            np.right_shift(frame[row::2, column::2], 6, out=plane)
        return imagecodecs.htj2k_encode(planes, planar=True, rgb=False, bitspersample=10)

    def _encode_cpu(self):  # Encoder thread, CPU: frames are compressed several at once and written in the order they came
        try:
            self._compress(np.zeros((self.buf.shape[0], self.buf.shape[1]) if self.mono else (self.buf.shape[1] * 2, self.buf.shape[2] * 2), np.uint16))
        except Exception as error:
            self.error = f"CPU encoder did not start: {error}"
        self.ready.set()
        pending, item = collections.deque(), ()  # (capture time, frame being compressed), oldest first
        while item is not None:
            try:
                item = self.waiting.get(timeout=0.05)
            except queue.Empty:
                item = ()
            if item and not self.error:
                pending.append((item[0], self.pool.submit(self._compress, item[1])))
            while pending and (item is None or pending[0][1].done() or len(pending) >= CPU_THREADS):  # Full: wait for the oldest, and the queue fills
                stamp, compressed = pending.popleft()
                try:
                    self._write(stamp, compressed.result())
                except Exception as error:
                    self.error = f"stopped after {self.written} frames: {error}"

    @property
    def skipped(self):  # Frames the camera did not deliver while recording
        return missing(self.times)[0]

    def close(self):  # Saves the frames still waiting and closes the file; the .mp4 follows (video.done)
        self.waiting.put(None)
        self.thread.join()
        self.pool.shutdown()
        self.file.close()
        _recording(-1)
        self.video.close()


def missing(times):
    """(frames missing, frames per second) from capture times. A camera's frames are evenly spaced, so a gap of two
    frame times means the driver had no free buffer for one frame."""
    if len(times) < 3:
        return 0, 0.0
    gaps = np.diff(times)
    interval = float(np.median(gaps))
    return int(np.maximum(np.rint(gaps / interval) - 1, 0).sum()), 1 / interval


def _decoder():  # A function from one stored picture to its planes, (planes, height, width): NVIDIA's decoder if installed, else OpenJPH, else OpenCV
    try:
        n = _nvidia()
        decoder, params = n.Decoder(), n.DecodeParams(color_spec=n.ColorSpec.UNCHANGED, allow_any_depth=True)
        return lambda data: np.asarray(decoder.decode(data, params=params).cpu()).transpose(2, 0, 1)
    except ImportError:
        pass
    try:
        import imagecodecs
        if imagecodecs.HTJ2K.available:
            def planes(data):
                image = imagecodecs.htj2k_decode(data)
                return image.reshape(-1, *image.shape[-2:])  # Mono comes back without the planes axis
            return planes
    except ImportError:
        pass
    import cv2
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)  # It warns on every frame that the pictures name no colour space

    def planes(data):
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
        return image[None] if image.ndim == 2 else image[:, :, [2, 1, 0, 3]].transpose(2, 0, 1)  # OpenCV swaps planes 0 and 2
    return planes


class Recording:
    def __init__(self, path):
        self.path = Path(path)
        self.file = open(self.path, "rb")
        if self.file.readline() != MAGIC:
            raise ValueError(f"{path} is not a PATH RAW10 recording")
        self.info = json.loads(self.file.readline())
        self.times, self.where, end = [], [], self.path.stat().st_size
        while len(head := self.file.read(FRAME.size)) == FRAME.size:
            stamp, length = FRAME.unpack(head)
            if self.file.tell() + length > end:
                break  # The last frame was cut off (power loss, full disk): leave it out
            self.times.append(stamp)
            self.where.append((self.file.tell(), length))
            self.file.seek(length, 1)
        self.times = np.array(self.times)
        self._decode = None

    def __len__(self):
        return len(self.where)

    def __getitem__(self, i):  # Frame i: (height, width) uint16 of the 10-bit values, 0-1023
        self._decode = self._decode or _decoder()
        offset, length = self.where[range(len(self))[i]]
        self.file.seek(offset)
        planes = self._decode(self.file.read(length))
        if len(planes) == 1:
            return planes[0]
        frame = np.empty((self.info["height"], self.info["width"]), np.uint16)
        for plane, (row, column) in zip(planes, PLANES):
            frame[row::2, column::2] = plane
        return frame

    def skipped(self):  # (frames the camera skipped, frames per second it ran at)
        return missing(self.times)

    def close(self):
        self._decode = None
        self.file.close()


def to_mp4(rec, out):  # A copy to watch: every frame as the live view shows it, at its capture time
    from cameras import CAMERAS
    preview, step = CAMERAS[rec.info["camera"]].preview, max(1, rec.info["width"] // (1280 if rec.info["pattern"] == "mono" else 2000))  # Colour halves again: 4032 wide becomes 1008
    video = Video(out, rec.skipped()[1])
    for i in range(len(rec)):
        video.add(preview(rec[i] << 6, step), rec.times[i])
        print(f"\r{i + 1} of {len(rec)} frames", end="", flush=True)
    video.close()
    print(f"\rsaved {out}" + " " * 20)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    rec = Recording(sys.argv[1])
    print(*(f"{key}: {value}" for key, value in rec.info.items()), sep="\n")
    (gone, fps), size = rec.skipped(), rec.path.stat().st_size
    raw = len(rec) * rec.info["width"] * rec.info["height"] * 2
    seconds = rec.times[-1] - rec.times[0] if len(rec) > 1 else 0
    print(f"{len(rec)} frames over {seconds:.1f} s at {fps:.2f} frames/s; {gone} missing; "
          f"{size / 1e6:.0f} MB, {100 * size / max(raw, 1):.1f} % of the {raw / 1e6:.0f} MB as 16-bit raw")
    if "--check" in sys.argv:
        for i in range(len(rec)):
            frame = rec[i]
            print(f"\rdecoded {i + 1} of {len(rec)}", end="", flush=True)
        print(f"\rall {len(rec)} frames decode; the last one spans {frame.min()}..{frame.max()}")
    if "--png" in sys.argv:
        import cv2
        i = int(sys.argv[sys.argv.index("--png") + 1])
        out = f"{rec.path.with_suffix('')}_frame{i}.png"
        cv2.imwrite(out, rec[i] << 6 if rec.info["pattern"] == "mono" else cv2.cvtColor(rec[i] << 6, cv2.COLOR_BayerBG2BGR))
        print(f"saved {out}")
    if "--mp4" in sys.argv:
        to_mp4(rec, f"{rec.path.with_suffix('')}.mp4")
    rec.close()
