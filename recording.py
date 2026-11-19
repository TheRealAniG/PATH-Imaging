"""Lossless RAW10 video: recorded by camera_ui.py's Record button, read back here.

Each frame is compressed on its own with lossless JPEG 2000 (High-Throughput), so what comes back is bit for bit what
the camera delivered. On a Jetson the GPU compresses, live (NVIDIA nvJPEG2000). Anywhere else, the Raspberry Pi
included, the CPU does (OpenJPH, from the imagecodecs package): the same files, far slower than the cameras send. So
there every frame is first stored raw in <name>.frames beside the recording (packed to 10 bits a pixel), and
low-priority threads compress the stored frames into the .j2ks: while recording as far as storing leaves cores
free, and after it with all of them, until none waits. Frames are then only lost when the disk cannot take them
as fast as they come, which is counted.
One file per camera, <name>.j2ks:

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
    python recording.py file.j2ks --finish    compresses the frames still waiting in file.frames (after a quit or power loss)
"""
import collections
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import mmap
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
WAITING_SHARE = 0.1  # Without a GPU frames wait in memory only for the disk: up to this share of the free memory,
WAITING_MOST = 1e9  # and at most this many bytes, so a short stall of the disk loses nothing
FRAMES = ".frames"  # Without a GPU: the folder beside a recording that holds its frames until they are compressed
BLOCK = 4096  # A waiting frame goes straight to the disk, past the kernel's cache, which takes whole blocks of this many bytes
LOW_BITS = 0x00C000C000C000C0  # Four pixels (value << 6) read as one 64-bit number: the 2 bits of each that pack_low() keeps
GATHER = (1 << 50) + (1 << 36) + (1 << 22) + (1 << 8)  # Times this, those four pairs of bits land side by side in the top byte
CPU_THREADS = max(1, (os.cpu_count() or 4) - 1)  # Without a GPU each recording compresses this many frames at once,
RECORDING_THREADS = max(1, CPU_THREADS // 2)  # but only this many while anything records (with all, full-size colour skipped a tenth of its frames),
BUSY_MOST = 0.5  # none while storing a frame takes more than this share of the time between two frames,
HOLD = 10  # and none for this many seconds after a camera skipped a frame: the machine was too busy to take it
COMPRESSING = set()  # The Backlogs that still have frames to compress
STORING = set()  # Without a GPU: the Recorders that are recording right now
STOP = threading.Event()  # Set to end all compressing early: the frames left stay on disk for --finish
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


def _free_memory():  # Bytes of memory the kernel calls available, or None if it does not say
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def _storing_busy():
    """True while a recorder has frames piling up in memory, needs most of its time to store them, or just had its
    camera skip a frame. Compressing then starts no new frame: capturing and storing go first, because a frame that
    is not stored in time is lost, and one that is not compressed yet only waits."""
    for recorder in list(STORING):
        if recorder.waiting.qsize() > 2 or recorder.backlog.busy > BUSY_MOST or time.monotonic() < recorder.hold:
            return True
    return False


def pack_high(frame, high):  # Of a frame (uint16, value << 6): the top 8 bits of every pixel, into high
    both = frame.view(np.uint8).reshape(frame.shape[0], frame.shape[1], 2)  # Of every pixel: its low byte, then its high byte
    high[:] = both[:, :, 1]


def pack_low(frame, low, work):
    """Of a frame (uint16, value << 6): the 2 bits below the top 8 of every pixel, into low, four pixels to a byte with
    the first pixel in the lowest bits. Four pixels are handled as one 64-bit number, which is far faster than pixel
    by pixel: so the width has to be a multiple of 4. work: room for those numbers, (height, width // 4) uint64."""
    fours = frame.view(np.uint64)
    np.bitwise_and(fours, LOW_BITS, out=work)
    np.multiply(work, GATHER, out=work)
    low[:] = work.view(np.uint8).reshape(work.shape[0], work.shape[1], 8)[:, :, 7]  # The top byte of each


def unpack10(data, height, width):  # What pack_high and pack_low made (high, then low), back as (height, width) uint16 of the 10-bit values
    high = np.frombuffer(data, np.uint8, height * width).reshape(height, width)
    low = np.frombuffer(data, np.uint8, height * width // 4, height * width).reshape(height, width // 4)
    values = high.astype(np.uint16)
    values <<= 2
    values[:, 0::4] |= low & 3
    values[:, 1::4] |= (low >> 2) & 3
    values[:, 2::4] |= (low >> 4) & 3
    values[:, 3::4] |= low >> 6
    return values


def compress(values, mono):  # One frame of 10-bit values as the codestream the GPU would write (OpenJPH, on the CPU)
    import imagecodecs
    if mono:
        return imagecodecs.htj2k_encode(values, bitspersample=10)
    planes = np.empty((4, values.shape[0] // 2, values.shape[1] // 2), np.uint16)
    for plane, (row, column) in zip(planes, PLANES):
        plane[:] = values[row::2, column::2]
    return imagecodecs.htj2k_encode(planes, planar=True, rgb=False, bitspersample=10)


def compress_file(path, height, width, mono):  # In a pool thread: one waiting frame's file -> (capture time, codestream)
    with open(path, "rb") as file:
        stamp, length = FRAME.unpack(file.read(FRAME.size))
        data = file.read(length)
    values = unpack10(data, height, width)
    return stamp, compress(values, mono)


class Backlog:
    """Without a GPU: the frames of one recording that wait on disk until they are compressed, each a file in
    <name>.frames beside the .j2ks. store() puts a frame there, packed to 10 bits. run() compresses them into the
    .j2ks on low-priority threads: oldest first, several at once, written in the order they were captured. It ends
    once closed is set and no frame waits; then the folder is gone, complete is True and done is set.

    Storing has to keep up with the camera, so it is made cheap. A frame's file is written straight to the disk, past
    the kernel's cache: full-size colour is 370 MB a second, and copying that through the cache kept more than one
    core busy (measured). And while one frame is on its way to the disk the next is already packed, into a second
    buffer.

    file: the .j2ks, open at its end. written: frames it already holds. files: frame files that already wait, oldest
    first. counts: the object whose written, bytes and error are kept up to date (a Recorder; this one if None)."""
    def __init__(self, path, file, height, width, mono, written, files, counts=None):
        if width % 4:
            raise ValueError(f"cannot pack a width of {width}: not a multiple of 4")
        self.path = Path(path)
        self.folder = self.path.with_suffix(FRAMES)
        self.file = file
        self.height = height
        self.width = width
        self.mono = mono
        self.written = written
        self.bytes = 0
        self.error = None
        if counts is None:
            counts = self
        self.counts = counts
        self.files = collections.deque(files)  # Waiting frames: store() adds at the end, run() takes from the front
        self.stored = written + len(files)  # Frames stored so far, compressed or not
        self.number = 0  # The next frame file's number
        if files:
            self.number = int(files[-1].stem) + 1
        used = FRAME.size + height * width * 5 // 4  # Bytes of one stored frame: capture time and length, high, low
        self.buffers = []  # Two frames as their files hold them: "bytes" is all of it, "high" and "low" are the two parts of the frame in it
        for _ in range(2):
            buffer = mmap.mmap(-1, (used + BLOCK - 1) // BLOCK * BLOCK)  # Whole blocks; mmap's memory starts at a page, as writing past the cache needs
            high = np.frombuffer(buffer, np.uint8, height * width, FRAME.size).reshape(height, width)
            low = np.frombuffer(buffer, np.uint8, height * width // 4, FRAME.size + height * width).reshape(height, width // 4)
            self.buffers.append({"bytes": buffer, "high": high, "low": low})
        self.work = np.empty((height, width // 4), np.uint64)  # For pack_low()
        self.writing = None  # The write of the frame stored last, on a helper thread
        self.busy = 1.0  # The share of the time between two frames that store() takes, averaged over the last ones
        self.newest = None  # Capture time of the frame stored last
        self.closed = threading.Event()  # No more frames will be stored
        self.done = threading.Event()  # run() has ended
        self.complete = False  # Every frame is in the .j2ks
        self.helpers = ThreadPoolExecutor(2)  # For store(): one packs half of a frame, one writes the frame before it
        self.pool = ThreadPoolExecutor(CPU_THREADS, initializer=_background, initargs=[width * height / 1e6])  # For run()
        self.folder.mkdir(parents=True, exist_ok=True)
        COMPRESSING.add(self)

    def left(self):  # Frames that still wait to be compressed
        return self.stored - self.counts.written

    def store(self, stamp, frame):  # One frame as the capture loop has it (value << 6) and its capture time, into its own file
        began = time.monotonic()
        frame = np.ascontiguousarray(frame)
        buffer = self.buffers[self.number % 2]  # The other one may still be on its way to the disk
        FRAME.pack_into(buffer["bytes"], 0, stamp, buffer["high"].nbytes + buffer["low"].nbytes)
        lows = self.helpers.submit(pack_low, frame, buffer["low"], self.work)  # On a second core
        pack_high(frame, buffer["high"])
        lows.result()
        self.flush()
        self.writing = self.helpers.submit(self._write, buffer["bytes"], self.number)
        self.number += 1
        if self.newest is not None and stamp > self.newest:
            share = (time.monotonic() - began) / (stamp - self.newest)
            self.busy = 0.9 * self.busy + 0.1 * share
        self.newest = stamp

    def flush(self):  # Returns once the frame stored last is on disk; if writing it failed, raises why
        writing = self.writing
        self.writing = None
        if writing is not None:
            writing.result()

    def _write(self, buffer, number):  # On a helper thread: one packed frame into its file
        final = self.folder / f"{number:08d}.raw10"
        part = final.with_suffix(".part")
        try:
            file = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_DIRECT, 0o644)  # O_DIRECT: past the kernel's cache
        except OSError:
            file = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)  # A disk that cannot do that: through the cache
        try:
            if os.write(file, buffer) != len(buffer):
                raise OSError("the disk took only part of a frame")
        finally:
            os.close(file)
        os.replace(part, final)  # Only a whole frame gets the final name
        self.files.append(final)
        self.stored += 1

    def close(self):  # No more frames will be stored: run() ends once none waits
        self.helpers.shutdown()  # Waits for the last frame's write
        self.buffers.clear()  # Their memory is free again
        self.closed.set()

    def _forget(self, files):  # Their frames are in the .j2ks: once that is safely on disk, the files can go
        os.fsync(self.file.fileno())
        for path in files:
            path.unlink()
        files.clear()

    def run(self):
        pending = collections.deque()  # Frames being compressed, oldest first: (file, what the pool will return)
        written_files = []  # Frame files whose frames were written to the .j2ks since the last _forget()
        forgot_at = time.monotonic()
        failed = False
        while not failed and not STOP.is_set():
            closed = self.closed.is_set()  # Read before looking for files: once it is set, none is added
            limit = CPU_THREADS
            if STORING:  # Something is recording
                limit = RECORDING_THREADS
            if self.files and len(pending) < limit and not _storing_busy():
                path = self.files.popleft()
                pending.append((path, self.pool.submit(compress_file, path, self.height, self.width, self.mono)))
                continue
            if not pending:
                if closed and not self.files:
                    break  # Nothing waits and nothing more will come
                time.sleep(0.05)
                continue
            path, compressed = pending[0]
            try:
                stamp, data = compressed.result(timeout=0.05)  # The oldest first, so frames go in in the order they came
            except concurrent.futures.TimeoutError:
                continue  # Not ready yet: look for new frames meanwhile
            except Exception as error:
                self.counts.error = f"compressing stopped after {self.counts.written} frames: {error}"
                failed = True
                continue
            pending.popleft()
            try:
                self.file.write(FRAME.pack(stamp, len(data)))
                self.file.write(data)
            except OSError as error:
                self.counts.error = f"compressing stopped after {self.counts.written} frames: {error}"
                failed = True
                continue
            self.counts.written += 1
            self.counts.bytes += len(data)
            written_files.append(path)
            if time.monotonic() - forgot_at >= 1:
                self._forget(written_files)
                forgot_at = time.monotonic()
        try:
            self._forget(written_files)
        except OSError as error:
            self.counts.error = f"compressing stopped after {self.counts.written} frames: {error}"
            failed = True
        self.pool.shutdown(cancel_futures=True)  # Frames it had not begun stay on disk
        if not failed and self.closed.is_set() and self.left() == 0:
            self.complete = True
            try:
                self.folder.rmdir()
            except OSError:
                pass  # Something else is in it: leave it
        self.file.close()  # Also gives up the lock on it
        if self.counts is self and self.error:  # Finishing an old recording: nobody else reports it
            print(f"{self.path.name}: {self.error}")
        COMPRESSING.discard(self)
        self.done.set()


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
        info: a dict kept in the file's header. mono: one plane, otherwise Bayer RGGB (the camera's MONO).
        written counts the frames in the .j2ks, waiting_frames the ones stored but not compressed yet (no GPU);
        done is set once every frame is in."""
        self.path, self.mono, self.gpu = Path(path), mono, encoder() == "gpu"
        self.written = self.dropped = self.bytes = 0
        self.error = None
        self.started = time.time()
        self.times = []  # Capture time of every frame offered, to count the ones the camera skipped
        self.buf = np.empty((height, width, 1) if mono else (4, height // 2, width // 2), np.uint16)  # GPU: the frame as the encoder takes it
        room = WAITING_BYTES
        self.behind = "the encoder"  # What was too slow when a frame is dropped
        if not self.gpu:
            self.behind = "the disk"
            free = _free_memory()
            if free is not None:
                room = max(WAITING_BYTES, min(WAITING_MOST, free * WAITING_SHARE))
        self.waiting = queue.Queue(max(3, int(room // (width * height * 2))))
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
        if self.gpu:
            self.pool = ThreadPoolExecutor(4)  # Splits a colour frame into its planes: 6 ms on four threads, 13 ms on one
            self.done = threading.Event()
            self.thread = threading.Thread(target=self._encode)
            self.thread.start()
        else:
            fcntl.flock(self.file, fcntl.LOCK_EX)  # Held until the last frame is compressed, so finish() leaves it alone
            self.backlog = Backlog(self.path, self.file, height, width, mono, 0, [], counts=self)
            self.done = self.backlog.done
            self.hold = 0.0  # Compressing starts no new frame before this time (time.monotonic()), see add()
            STORING.add(self)
            self.thread = threading.Thread(target=self._store)
            self.thread.start()
            threading.Thread(target=self._compress_stored).start()
        self.ready.wait()

    def add(self, frame, stamp):  # One frame as the capture loop has it (value << 6) and its capture time. Never waits
        if not self.gpu and len(self.times) > 1 and stamp - self.times[-1] > 1.5 * (self.times[-1] - self.times[-2]):
            self.hold = time.monotonic() + HOLD  # The camera skipped a frame: compressing gets out of the way for a while
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

    def _store(self):  # Writer thread, without a GPU: every frame from the queue onto the disk, see Backlog
        taken = 0  # Frames taken from the queue
        item = self.waiting.get()
        try:
            while item is not None:
                taken += 1
                stamp, frame = item
                self.backlog.store(stamp, frame)
                item = self.waiting.get()
            self.backlog.flush()  # The last frame is on disk too
        except Exception as error:  # Mostly OSError: the disk is full, or gone
            self.error = f"storing stopped after {self.backlog.stored} frames: {error}"
        while item is not None:  # After a failure: keep emptying the queue so close() returns
            item = self.waiting.get()
            if item is not None:
                taken += 1
        self.dropped += taken - self.backlog.stored  # The frames that did not get onto the disk

    def _compress_stored(self):  # Compressor thread, without a GPU: the stored frames into the .j2ks, until close() and none waits
        try:
            compress(np.zeros((64, 64), np.uint16), self.mono)  # The first one loads OpenJPH, which is slow: before recording
        except Exception as error:
            self.error = f"CPU encoder did not start: {error}"
        self.ready.set()
        self.backlog.run()

    @property
    def skipped(self):  # Frames the camera did not deliver while recording
        return missing(self.times)[0]

    @property
    def waiting_frames(self):  # Without a GPU: frames stored on disk that are not compressed yet
        if self.gpu:
            return 0
        return self.backlog.left()

    def close(self):
        """GPU: saves the frames still waiting and closes the file. Without one: returns once those frames are on disk;
        compressing goes on and done is set when it ends. The .mp4 follows either way (video.done)."""
        self.waiting.put(None)
        self.thread.join()
        if self.gpu:
            self.pool.shutdown()
            self.file.close()
            self.done.set()
        else:
            STORING.discard(self)
            self.backlog.close()  # Its run() ends once no frame waits, and closes the file
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
        self.start = self.file.tell()  # Where the frames begin
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


def unfinished(top):  # The recordings under top (a PATH-captures folder) that still have frames waiting: their .j2ks files
    found = []
    for folder in sorted(Path(top).glob("*/*/*" + FRAMES)):
        found.append(folder.with_suffix(SUFFIX))
    return found


def finish(path):
    """Go on compressing a recording that still has frames waiting in its .frames folder: the program was closed, or
    the power went, before it was done. The work runs in the background; returns its Backlog (see
    wait_until_compressed()), or None if no frame waits or another program is at it."""
    path = Path(path)
    folder = path.with_suffix(FRAMES)
    if not folder.is_dir():
        return None
    try:
        file = open(path, "r+b", buffering=0)
    except FileNotFoundError:
        return None
    try:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:  # Locked: another program is compressing it
        file.close()
        return None
    recording = Recording(path)  # The header, and the frames that are whole
    info = recording.info
    written = len(recording)
    end = recording.start
    newest = None  # Capture time of the last frame in the .j2ks
    if written:
        offset, length = recording.where[-1]
        end = offset + length
        newest = float(recording.times[-1])
    recording.close()
    file.truncate(end)  # Cuts off a frame that was only half written
    file.seek(end)
    for part in folder.glob("*.part"):
        part.unlink()
    whole = FRAME.size + info["height"] * info["width"] * 5 // 4  # Bytes of one stored frame; its file is as long or a little longer
    files = []
    for frame_file in sorted(folder.glob("*.raw10")):
        keep = frame_file.stat().st_size >= whole  # Shorter: the power went while it was written
        if keep and newest is not None:
            with open(frame_file, "rb") as head:
                stamp, length = FRAME.unpack(head.read(FRAME.size))
            keep = stamp > newest  # Otherwise it is in the .j2ks already
        if keep:
            files.append(frame_file)
        else:
            frame_file.unlink()
    backlog = Backlog(path, file, info["height"], info["width"], info["pattern"] == "mono", written, files)
    backlog.close()
    threading.Thread(target=backlog.run).start()
    return backlog


def wait_until_compressed():
    """Wait until the frames of every recording that still wait are compressed, printing how many are left. Ctrl+C
    stops the compressing: the frames left stay on disk, and the commands that finish them are printed."""
    jobs = list(COMPRESSING)
    ending = "done"
    try:
        while COMPRESSING:
            left = 0
            for job in list(COMPRESSING):
                left += job.left()
            print(f"\rcompressing: {left} frames left   ", end="", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        ending = "stopped"
        STOP.set()
        while COMPRESSING:  # Each stops after the frames it is working on
            time.sleep(0.1)
    if jobs:
        print(f"\rcompressing: {ending}" + " " * 20)
    for job in jobs:
        if not job.complete:
            print(f"{job.left()} frames still wait: python recording.py '{job.path}' --finish")


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
    waiting = len(list(Path(sys.argv[1]).with_suffix(FRAMES).glob("*.raw10")))
    if "--finish" in sys.argv:
        if finish(sys.argv[1]) is None and waiting:
            print("another program is compressing this recording")
        wait_until_compressed()
    elif waiting:
        print(f"{waiting} frames still wait to be compressed: python recording.py '{sys.argv[1]}' --finish")
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
