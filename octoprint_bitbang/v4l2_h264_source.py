"""Direct V4L2 H.264 capture -> aiortc passthrough (no software re-encode).

For devices that emit H.264 themselves: the Raspberry Pi legacy/mmal CSI camera
(/dev/video2) and UVC webcams with an onboard H.264 encoder. The camera/GPU does
the encoding; we read Annex-B packets and hand them straight to aiortc, which
RTP-packetizes without re-encoding -- the same passthrough contract as
PiH264Track.

Measured on a Pi 4 (32-bit OctoPi), 1280x720@30: ~1% CPU, full frame rate --
versus software libx264 which can't hold frame rate on this hardware and builds
unbounded latency.

Capture is driven through the `ffmpeg` binary rather than PyAV's av.open(), for
one reason: the Pi's encoder writes an SPS with NO VUI colour signalling
(ffprobe reports color_range/space/primaries/transfer all "unknown"), and the
sensor pipeline is full-range BT.601 (measured YMIN=0). Browsers then assume
limited-range/BT.709 and render with wrong gamma/colour. We fix this by stamping
the correct VUI into the SPS with ffmpeg's `h264_metadata` bitstream filter --
no re-encode. PyAV 11 (the pinned 32-bit wheel) exposes no bitstream-filter API,
so we use the ffmpeg binary (present on every OctoPi) and let PyAV demux its
output. If a future PyAV gains BSF support this can move in-process.
"""

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from fractions import Fraction

import av
from aiortc import MediaStreamTrack

_log = logging.getLogger(__name__)

# av.Packet timestamps use a monotonic microsecond clock; aiortc only needs
# monotonically increasing pts in a known time_base.
_TIME_BASE = Fraction(1, 1_000_000)

# Full-range BT.709 -- matches the Pi sensor/encoder pipeline at 720p+.
_VUI_BSF = ("h264_metadata=video_full_range_flag=1:"
            "matrix_coefficients=1:colour_primaries=1:transfer_characteristics=1")

def device_holders(device):
    """Best-effort list of "pid NAME" strings for processes holding `device`
    open.

    Reads /proc/*/fd, which is only readable for processes owned by the same
    user -- that covers the case we care about (another OctoPrint plugin
    shelling out to v4l2-ctl runs as the OctoPrint user). Root-owned holders
    such as a systemd-managed streamer stay invisible, so an empty result is
    not proof that nothing else has the device.
    """
    target = os.path.realpath(device)
    holders = []
    try:
        pids = os.listdir("/proc")
    except OSError:
        return holders
    for pid in pids:
        if not pid.isdigit():
            continue
        fd_dir = os.path.join("/proc", pid, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue  # another user's process, or it exited mid-scan
        for fd in fds:
            if os.path.realpath(os.path.join(fd_dir, fd)) != target:
                continue
            try:
                with open(os.path.join("/proc", pid, "comm")) as f:
                    name = f.read().strip()
            except OSError:
                name = "?"
            holders.append(f"pid {pid} ({name})")
            break
    return holders


def device_supports_h264(device):
    """True if the V4L2 device advertises an H.264 capture format."""
    if not shutil.which("v4l2-ctl"):
        return False
    try:
        r = subprocess.run(
            ["v4l2-ctl", "-d", device, "--list-formats"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return False
    return "H264" in r.stdout


def device_supports_flip(device):
    """True if the device exposes V4L2 hflip/vflip controls, i.e. it can flip
    in hardware before encoding. The Pi mmal camera does; most USB H.264 cams
    don't (those must fall back to the software flip path)."""
    if not shutil.which("v4l2-ctl"):
        return False
    try:
        r = subprocess.run(
            ["v4l2-ctl", "-d", device, "--list-ctrls"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return False
    return "horizontal_flip" in r.stdout and "vertical_flip" in r.stdout


def reencode_input_format(device):
    """Pick a raw/decodable V4L2 input format to feed the hardware re-encoder:
    prefer MJPEG (compact over USB), else YUYV. None if the device offers
    neither (then there's nothing to hardware-encode)."""
    if not shutil.which("v4l2-ctl"):
        return None
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", device, "--list-formats"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return None
    if "MJPG" in out:
        return "mjpeg"
    if "YUYV" in out:
        return "yuyv422"
    return None


def has_v4l2m2m_h264_encoder():
    """True if the platform has a usable V4L2 M2M H.264 encoder -- i.e. the Pi
    4's bcm2835 codec. The Pi 5 has no hardware H.264 encoder, so this returns
    False there and the caller drops to software encode."""
    if not shutil.which("ffmpeg") or not shutil.which("v4l2-ctl"):
        return False
    try:
        encs = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=8,
        ).stdout
        if "h264_v4l2m2m" not in encs:
            return False
        devs = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        return "bcm2835-codec" in devs
    except Exception:
        return False


class V4l2H264Track(MediaStreamTrack):
    """aiortc video track backed by a V4L2 device's built-in H.264 encoder.

    A background thread runs `ffmpeg` (capture + VUI fix) and demuxes its output
    with PyAV, pushing each encoded packet onto an asyncio.Queue (dropping the
    oldest on overflow so the live stream never stalls). recv() returns
    av.Packet, matching PiH264Track's passthrough contract. If ffmpeg dies the
    reason is logged (stderr tail plus any other process holding the device) and
    the track fails; recovery is an OctoPrint restart.
    """

    kind = "video"

    def __init__(self, device, source_is_h264=True, input_format="h264",
                 video_size="1280x720", framerate=30,
                 bitrate=4_000_000, gop=30, brightness=0,
                 flip_horizontal=False, flip_vertical=False, logger=None):
        super().__init__()
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg binary not found")

        self.device = device
        self._logger = logger or _log
        # source_is_h264=True  -> device emits H.264; ffmpeg `-c copy` (passthrough).
        # source_is_h264=False -> raw/MJPEG source; ffmpeg `-c:v h264_v4l2m2m`
        #                         (Pi 4 GPU re-encode). Same downstream pipeline.
        self._source_is_h264 = bool(source_is_h264)
        self._input_format = input_format
        self._video_size = video_size
        self._framerate = int(framerate)
        self._bitrate = int(bitrate)
        self._gop = int(gop)
        # Passthrough: flip in hardware via the camera's V4L2 hflip/vflip (before
        # its encoder). Re-encode: flip with an ffmpeg filter before the M2M
        # encoder (we're decoding anyway), so it works on cams without flip ctrls.
        self._flip_h = bool(flip_horizontal)
        self._flip_v = bool(flip_vertical)

        self._loop = None
        self._queue = None
        self._error = None
        self._thread = None
        self._stop = threading.Event()
        self._proc = None
        self._container = None
        self._started = False
        # Tail of ffmpeg's stderr, so a failure can say *why* rather than just
        # going quiet ("Device or resource busy" lands here).
        self._stderr_tail = deque(maxlen=20)

        self._brightness_range = self._query_brightness_range()
        if self._brightness_range:
            self.set_brightness(brightness)

    # -- device controls (best-effort, via v4l2-ctl) --

    def _set_ctrl(self, ctrl):
        subprocess.run(
            ["v4l2-ctl", "-d", self.device, "--set-ctrl", ctrl],
            capture_output=True, timeout=5, check=False,
        )

    def _configure_encoder(self):
        """Passthrough only: tune the camera's *own* H.264 encoder (mmal) --
        repeat SPS/PPS before every IDR (late joiners), short GOP, bitrate, and
        hardware flip. Must run before ffmpeg opens the device. For the
        re-encode path these are ffmpeg args / a filter instead, so skip them
        (they're mmal-specific and meaningless on a USB cam)."""
        if not shutil.which("v4l2-ctl") or not self._source_is_h264:
            return
        self._set_ctrl("repeat_sequence_header=1")
        self._set_ctrl(f"h264_i_frame_period={self._gop}")
        self._set_ctrl(f"video_bitrate={self._bitrate}")
        self._set_ctrl(f"horizontal_flip={1 if self._flip_h else 0}")
        self._set_ctrl(f"vertical_flip={1 if self._flip_v else 0}")

    def _query_brightness_range(self):
        if not shutil.which("v4l2-ctl"):
            return None
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--list-ctrls", "-d", self.device],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            return None
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped.startswith("brightness"):
                continue
            try:
                attrs = stripped.split(":", 1)[1]
                kv = dict(p.split("=", 1) for p in attrs.split() if "=" in p)
                return int(kv["min"]), int(kv["max"])
            except (KeyError, ValueError, IndexError):
                return None
        return None

    def set_brightness(self, value):
        """Slider -100..100 -> linear interp into the device's V4L2 brightness
        range. Returns True if applied, False if unsupported."""
        if not self._brightness_range:
            return False
        value = max(-100, min(100, int(value)))
        lo, hi = self._brightness_range
        v4l2_value = round(lo + (value + 100) * (hi - lo) / 200)
        self._set_ctrl(f"brightness={v4l2_value}")
        return True

    # -- capture --

    def _ffmpeg_cmd(self):
        # Shared capture front-end; only the encode stage differs.
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer",
            "-f", "v4l2", "-input_format", self._input_format,
            "-video_size", self._video_size, "-framerate", str(self._framerate),
            "-i", self.device, "-an",
        ]
        if self._source_is_h264:
            cmd += ["-c", "copy"]                      # passthrough, no re-encode
        else:
            # h264_v4l2m2m requires yuv420p input; MJPEG/YUYV decode to other
            # pixel formats, so always convert (and apply any flip in the same
            # filter pass).
            vf = [f for f, on in (("hflip", self._flip_h),
                                  ("vflip", self._flip_v)) if on]
            vf.append("format=yuv420p")
            cmd += ["-vf", ",".join(vf),
                    "-c:v", "h264_v4l2m2m",            # Pi 4 GPU encoder
                    "-b:v", str(self._bitrate), "-g", str(self._gop)]
        bsf = _VUI_BSF
        if not self._source_is_h264:
            # h264_v4l2m2m emits SPS/PPS only once at stream start (libav then
            # captures them as extradata and strips them from later packets),
            # so a decoder that joins mid-stream — e.g. a browser opening the
            # WebRTC video track — never sees parameter sets and renders black.
            # dump_extra re-inserts SPS/PPS before every keyframe.
            bsf += ",dump_extra=freq=keyframe"
        cmd += ["-bsf:v", bsf, "-flush_packets", "1", "-f", "h264", "pipe:1"]
        return cmd

    def _drain_stderr(self, pipe):
        """Keep the tail of ffmpeg's stderr. Has to run in its own thread: an
        unread stderr pipe fills up and blocks ffmpeg."""
        try:
            for line in iter(pipe.readline, b""):
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self._stderr_tail.append(text)
        except Exception:
            pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    def _run_once(self):
        """Run one ffmpeg capture until it ends. Returns None if we stopped it
        on purpose, otherwise a short string describing why it ended."""
        self._stderr_tail.clear()
        self._configure_encoder()
        self._proc = subprocess.Popen(
            self._ffmpeg_cmd(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0)
        threading.Thread(target=self._drain_stderr, args=(self._proc.stderr,),
                         name="v4l2-h264-err", daemon=True).start()
        self._container = av.open(self._proc.stdout, format="h264")
        stream = self._container.streams.video[0]
        base = None
        for packet in self._container.demux(stream):
            if self._stop.is_set():
                return None
            if not packet.size:
                continue
            data = bytes(packet)
            now = time.monotonic()
            if base is None:
                base = now
            pkt = av.Packet(data)
            pkt.pts = int((now - base) * 1_000_000)
            pkt.dts = pkt.pts
            pkt.time_base = _TIME_BASE
            pkt.is_keyframe = bool(packet.is_keyframe)
            self._loop.call_soon_threadsafe(self._enqueue, pkt)
        # Demux ran dry, i.e. ffmpeg exited on its own. Previously this ended
        # the thread silently and the video just stopped with nothing logged.
        return f"ffmpeg exited (rc={self._proc.poll()})"

    def _log_failure(self, reason, ran_seconds):
        self._logger.warning(
            f"BitBang: capture on {self.device} stopped after "
            f"{ran_seconds:.1f}s: {reason}")
        for line in self._stderr_tail:
            self._logger.warning(f"BitBang:   ffmpeg: {line}")
        # Our own ffmpeg is already reaped by now, so anything still holding the
        # device is someone else -- usually what caused the failure.
        holders = device_holders(self.device)
        if holders:
            self._logger.warning(
                f"BitBang:   {self.device} is held open by "
                f"{', '.join(holders)} -- another plugin or service is using "
                f"the camera")

    def _capture_loop(self):
        """Run the capture, and report why it ended. A dead capture stays dead
        (the track fails and the user restarts OctoPrint); the point here is
        that the log says what happened instead of going silent."""
        started = time.monotonic()
        try:
            reason = self._run_once()
        except Exception as e:  # noqa: BLE001 - reported, then surfaced to recv()
            reason = str(e) or e.__class__.__name__
        finally:
            self._close_proc()
        if self._stop.is_set() or reason is None:
            return
        self._log_failure(reason, time.monotonic() - started)
        self._logger.error(
            f"BitBang: no more video from {self.device} -- restart OctoPrint "
            f"once the camera is free")
        self._loop.call_soon_threadsafe(
            self._fail,
            RuntimeError(f"V4L2 capture on {self.device} failed: {reason}"))

    def _enqueue(self, pkt):
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(pkt)
        except asyncio.QueueFull:
            pass

    def _fail(self, exc):
        self._error = exc
        try:
            self._queue.put_nowait(None)  # wake recv()
        except asyncio.QueueFull:
            pass

    def _ensure_started(self):
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=30)
        self._configure_encoder()
        self._thread = threading.Thread(
            target=self._capture_loop, name="v4l2-h264", daemon=True)
        self._thread.start()
        self._started = True

    async def recv(self):
        self._ensure_started()
        pkt = await self._queue.get()
        if pkt is None:
            raise self._error or RuntimeError("V4L2 H.264 capture stopped")
        return pkt

    @property
    def video(self):
        # MediaPlayer-shaped interface so the adapter treats us like the others.
        return self

    def _close_proc(self):
        """Reap the ffmpeg child and close the demuxer. Used both by stop() and
        between restart attempts, so a retry never races the previous run."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            # Shut ffmpeg down *gracefully* first: SIGINT makes it issue
            # VIDIOC_STREAMOFF and release the V4L2 device cleanly, which avoids
            # the legacy mmal vb2_fop_release kernel deadlock that an abrupt kill
            # can trigger (unkillable D-state, camera wedged until reboot).
            # Escalate only if it doesn't exit in time.
            for sig, wait in ((signal.SIGINT, 4), (signal.SIGTERM, 2),
                              (signal.SIGKILL, 1)):
                try:
                    proc.send_signal(sig)
                    proc.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
                except Exception:
                    break
        try:
            if self._container is not None:
                self._container.close()
        except Exception:
            pass
        self._container = None

    def stop(self):
        super().stop()
        self._stop.set()
        self._close_proc()
