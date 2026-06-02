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
import shutil
import signal
import subprocess
import threading
import time
from fractions import Fraction

import av
from aiortc import MediaStreamTrack

# av.Packet timestamps use a monotonic microsecond clock; aiortc only needs
# monotonically increasing pts in a known time_base.
_TIME_BASE = Fraction(1, 1_000_000)

# Full-range BT.709 -- matches the Pi sensor/encoder pipeline at 720p+.
_VUI_BSF = ("h264_metadata=video_full_range_flag=1:"
            "matrix_coefficients=1:colour_primaries=1:transfer_characteristics=1")


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
    av.Packet, matching PiH264Track's passthrough contract.
    """

    kind = "video"

    def __init__(self, device, source_is_h264=True, input_format="h264",
                 video_size="1280x720", framerate=30,
                 bitrate=4_000_000, gop=30, brightness=0,
                 flip_horizontal=False, flip_vertical=False):
        super().__init__()
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg binary not found")

        self.device = device
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
        cmd += ["-bsf:v", _VUI_BSF, "-flush_packets", "1", "-f", "h264", "pipe:1"]
        return cmd

    def _capture_loop(self):
        try:
            self._proc = subprocess.Popen(
                self._ffmpeg_cmd(), stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0)
            self._container = av.open(self._proc.stdout, format="h264")
            stream = self._container.streams.video[0]
            base = None
            for packet in self._container.demux(stream):
                if self._stop.is_set():
                    break
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
        except Exception as e:  # noqa: BLE001 - surface to recv()
            if not self._stop.is_set():
                self._loop.call_soon_threadsafe(self._fail, e)

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

    def stop(self):
        super().stop()
        self._stop.set()
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
