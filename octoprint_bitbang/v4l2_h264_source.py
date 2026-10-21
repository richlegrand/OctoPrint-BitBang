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

from .capture_supervisor import CaptureSupervisor

_log = logging.getLogger(__name__)

# av.Packet timestamps use a monotonic microsecond clock; aiortc only needs
# monotonically increasing pts in a known time_base.
_TIME_BASE = Fraction(1, 1_000_000)

# Full-range BT.709 -- matches the Pi sensor/encoder pipeline at 720p+.
_VUI_BSF = ("h264_metadata=video_full_range_flag=1:"
            "matrix_coefficients=1:colour_primaries=1:transfer_characteristics=1")

# How long to wait for the device node to reappear before reopening anyway.
# The rest of the reopen policy -- when a run counts as established, the
# failed-start budget, the backoff -- lives in capture_supervisor, shared with
# the other capture sources.
_DEVICE_WAIT = 30.0

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
            try:
                same = os.path.realpath(os.path.join(fd_dir, fd)) == target
            except OSError:
                # Listing a process's fds does not guarantee every link is
                # readable, and one can vanish mid-scan. This runs from the
                # capture thread's failure path, where raising would kill the
                # thread without failing the track and leave recv() waiting
                # for a packet that never comes.
                continue
            if not same:
                continue
            try:
                with open(os.path.join("/proc", pid, "comm")) as f:
                    name = f.read().strip()
            except OSError:
                name = "?"
            holders.append(f"pid {pid} ({name})")
            break
    return holders


# Streamers that commonly own the camera on an OctoPrint host. OctoPi's
# webcamd runs mjpg_streamer; DietPi ships mjpg-streamer; newer images use
# camera-streamer.
_KNOWN_STREAMERS = ("mjpg_streamer", "mjpg-streamer", "camera-streamer",
                    "ustreamer", "libcamera-vid", "raspivid", "motion")


def running_streamers():
    """Best-effort list of "pid NAME" for streamer processes that are running,
    whoever owns them.

    This exists because device_holders cannot see the holder that matters
    most. /proc/<pid>/fd is readable only for our own processes, so a
    systemd-managed streamer running as root never appears there -- and on an
    OctoPrint host that is the usual reason the camera is busy.
    /proc/<pid>/comm is world-readable, so a name is still available.

    Not proof that one of these holds the device: a name is all we get. But
    naming a streamer that is running beats reporting nothing when the device
    is busy and our own scan came back empty.
    """
    found = []
    try:
        pids = os.listdir("/proc")
    except OSError:
        return found
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open(os.path.join("/proc", pid, "comm")) as f:
                name = f.read().strip()
        except OSError:
            continue  # exited mid-scan
        if name in _KNOWN_STREAMERS:
            found.append(f"pid {pid} ({name})")
    return found


def device_supports_h264(device):
    """True if the V4L2 device advertises an H.264 capture format."""
    if shutil.which("v4l2-ctl"):
        try:
            r = subprocess.run(
                ["v4l2-ctl", "-d", device, "--list-formats"],
                capture_output=True, text=True, timeout=5,
            )
            return "H264" in r.stdout
        except Exception:
            pass
    if shutil.which("ffmpeg"):
        try:
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-f", "v4l2", "-list_formats", "all", "-i", device],
                capture_output=True, text=True, timeout=5,
            )
            out = r.stdout + r.stderr
            return "h264" in out.lower() or "h.264" in out.lower()
        except Exception:
            pass
    return False


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
    if shutil.which("v4l2-ctl"):
        try:
            out = subprocess.run(
                ["v4l2-ctl", "-d", device, "--list-formats"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            if "MJPG" in out:
                return "mjpeg"
            if "YUYV" in out:
                return "yuyv422"
        except Exception:
            pass
    if shutil.which("ffmpeg"):
        try:
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-f", "v4l2", "-list_formats", "all", "-i", device],
                capture_output=True, text=True, timeout=5,
            )
            out = (r.stdout + r.stderr).lower()
            if "mjpeg" in out or "mjpg" in out:
                return "mjpeg"
            if "yuyv" in out:
                return "yuyv422"
        except Exception:
            pass
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


def has_vaapi_h264_encoder(device=None):
    """True if the platform has a usable VAAPI H.264 encoder -- i.e. ffmpeg has
    the h264_vaapi encoder and a DRI render node is present."""
    if not shutil.which("ffmpeg"):
        return False
    try:
        encs = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=8,
        ).stdout
        if "h264_vaapi" not in encs:
            return False
        if device:
            return os.path.exists(device)
        if os.path.exists("/dev/dri/renderD128"):
            return True
        import glob
        return bool(glob.glob("/dev/dri/renderD*"))
    except Exception:
        return False


def probe_vaapi_h264(device_path, video_size="1280x720"):
    """Test whether ffmpeg can actually encode a test frame with h264_vaapi
    using the specified VAAPI device."""
    if not shutil.which("ffmpeg"):
        return False
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-vaapi_device", device_path,
        "-f", "lavfi", "-i", f"testsrc=s={video_size}:r=1",
        "-frames:v", "1",
        "-vf", "format=nv12,hwupload",
        "-c:v", "h264_vaapi",
        "-f", "null", "-",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


def find_vaapi_device(explicit_device=None, video_size="1280x720"):
    """Find a render node whose driver can actually encode H.264."""
    import glob
    if not has_vaapi_h264_encoder():
        return None
    if explicit_device:
        return explicit_device if probe_vaapi_h264(explicit_device, video_size=video_size) else None
    nodes = ["/dev/dri/renderD128"] + sorted(glob.glob("/dev/dri/renderD*"))
    for node in dict.fromkeys(nodes):
        if os.path.exists(node) and probe_vaapi_h264(node, video_size=video_size):
            return node
    return None


class V4l2H264Track(CaptureSupervisor, MediaStreamTrack):
    """aiortc video track backed by a V4L2 device's built-in H.264 encoder.

    A background thread runs `ffmpeg` (capture + VUI fix) and demuxes its output
    with PyAV, pushing each encoded packet onto an asyncio.Queue (dropping the
    oldest on overflow so the live stream never stalls). recv() returns
    av.Packet, matching PiH264Track's passthrough contract. If ffmpeg dies the
    reason is logged (stderr tail plus any other process holding the device) and
    the device is reopened; the track only fails once reopening keeps failing
    straight away, which is a camera that is gone rather than one that blinked.
    """

    kind = "video"

    def __init__(self, device, source_is_h264=True, input_format="h264",
                 encoder=None, vaapi_device=None, hwaccel_decode=False,
                 video_size="1280x720", framerate=30,
                 bitrate=4_000_000, gop=30, brightness=0,
                 flip_horizontal=False, flip_vertical=False, logger=None):
        super().__init__()
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg binary not found")

        self.device = device
        self._logger = logger or _log
        # encoder="copy" / source_is_h264=True  -> device emits H.264; ffmpeg `-c copy` (passthrough).
        # encoder="v4l2m2m"                     -> raw/MJPEG source; ffmpeg `-c:v h264_v4l2m2m`
        #                                          (Pi 4 GPU re-encode).
        # encoder="vaapi"                       -> raw/MJPEG source; ffmpeg `-c:v h264_vaapi`
        #                                          (VAAPI GPU re-encode).
        self._encoder = encoder or ("copy" if source_is_h264 else "v4l2m2m")
        self._vaapi_device = vaapi_device or "/dev/dri/renderD128"
        self._hwaccel_decode = bool(hwaccel_decode)
        self._input_format = input_format
        self._video_size = video_size
        self._framerate = int(framerate)
        self._bitrate = int(bitrate)
        self._gop = int(gop)
        # Passthrough: flip in hardware via the camera's V4L2 hflip/vflip (before
        # its encoder). Re-encode: flip with an ffmpeg filter before the
        # hardware encoder (we're decoding anyway), so it works on cams without
        # flip ctrls.
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
        # Presentation timestamps span every ffmpeg run, not one of them. A
        # reopened capture continues the timeline where the last one stopped,
        # rather than restarting at zero and sending pts backwards. The outage
        # itself is not represented: a gap of real seconds would be truthful
        # but hands the browser's jitter buffer a jump it has no reason to
        # tolerate, and nothing downstream needs to know the stream paused.
        self._pts_base_us = 0
        self._pts_last_us = None
        self._frame_us = max(1, int(1_000_000 / max(1, self._framerate)))

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
        if not shutil.which("v4l2-ctl") or self._encoder != "copy":
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
        ]
        # Hardware decode is applicable for VAAPI when the input is MJPEG.
        # A flip no longer rules it out: transpose_vaapi does the flip on the
        # VAAPI filter graph, so it no longer forces a software decode.
        use_hwaccel_decode = (
            self._encoder == "vaapi" and
            self._hwaccel_decode and
            self._input_format == "mjpeg"
        )
        if self._encoder == "vaapi":
            if use_hwaccel_decode:
                cmd += [
                    "-hwaccel", "vaapi",
                    "-hwaccel_device", self._vaapi_device,
                    "-hwaccel_output_format", "vaapi",
                ]
            cmd += ["-vaapi_device", self._vaapi_device]
        cmd += ["-f", "v4l2"]
        # A device that advertises neither MJPEG nor YUYV has no format we can
        # name, which is the software rung's business: let ffmpeg pick.
        if self._input_format:
            cmd += ["-input_format", self._input_format]
        cmd += [
            "-video_size", self._video_size, "-framerate", str(self._framerate),
            "-i", self.device, "-an",
        ]
        if self._encoder == "copy":
            cmd += ["-c", "copy"]                      # passthrough, no re-encode
        elif self._encoder == "vaapi":
            if use_hwaccel_decode:
                vf = ["scale_vaapi=format=nv12"]
                vf.extend([f for f, on in
                           (("transpose_vaapi=dir=hflip", self._flip_h),
                            ("transpose_vaapi=dir=vflip", self._flip_v)) if on])
                # Frames decoded directly into VAAPI surfaces; encode directly.
                cmd += ["-vf", ",".join(vf),
                        "-c:v", "h264_vaapi",
                        "-b:v", str(self._bitrate), "-g", str(self._gop),
                        "-bf", "0"]
            else:
                # Hardware VAAPI re-encode: upload nv12 frames to the VAAPI surface.
                vf = [f for f, on in (("hflip", self._flip_h),
                                      ("vflip", self._flip_v)) if on]
                vf.append("format=nv12")
                vf.append("hwupload")
                cmd += ["-vf", ",".join(vf),
                        "-c:v", "h264_vaapi",
                        "-b:v", str(self._bitrate), "-g", str(self._gop),
                        "-bf", "0"]
        elif self._encoder == "libx264":
            # Software encode in the ffmpeg child rather than in-process.
            # Speed is not the point -- isolation is. When a V4L2 device
            # disappears mid-stream, libav's capture thread can wedge in the
            # kernel's vb2_core_dqbuf and never return: no frame, no error,
            # and it keeps the device open. In a child that is a SIGKILL; in
            # OctoPrint's own process there is no way out of it.
            #
            # ultrafast/zerolatency because anything slower cannot hold frame
            # rate on a Pi, and baseline because that is what the browser
            # wants and what the other paths already produce.
            vf = [f for f, on in (("hflip", self._flip_h),
                                  ("vflip", self._flip_v)) if on]
            vf.append("format=yuv420p")
            cmd += ["-vf", ",".join(vf),
                    "-c:v", "libx264",
                    "-preset", "ultrafast", "-tune", "zerolatency",
                    "-profile:v", "baseline",
                    "-b:v", str(self._bitrate), "-g", str(self._gop),
                    "-bf", "0"]
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
        if self._encoder != "copy":
            # Re-encoders emit SPS/PPS only once at stream start (libav then
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

    def _begin_pts_run(self):
        """Start this run's timestamps one frame after the last run's final
        packet, so a reopened capture continues the timeline instead of
        restarting it."""
        if self._pts_last_us is not None:
            self._pts_base_us = self._pts_last_us + self._frame_us

    def _pts_for(self, now, base):
        """pts, in microseconds, for a packet captured at `now` in a run whose
        first packet arrived at `base`."""
        pts = self._pts_base_us + int((now - base) * 1_000_000)
        self._pts_last_us = pts
        return pts

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
        self._begin_pts_run()
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
            pkt.pts = self._pts_for(now, base)
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
        # Contention is reported by the supervisor, which calls it after this
        # and again on the give-up path.

    def _log_contention(self):
        """Say who else has the camera, as far as we can tell.

        Our own ffmpeg is already reaped by the time this runs, so anything
        still holding the device is someone else -- usually what caused the
        failure. Reported from both the failure path and the give-up path,
        because contention can start after the first failure: a streamer that
        retries will take the device during the gap, and then the first
        failure's report is the one that saw nothing.
        """
        holders = device_holders(self.device)
        if holders:
            self._logger.warning(
                f"BitBang:   {self.device} is held open by "
                f"{', '.join(holders)} -- another plugin or service is using "
                f"the camera")
            return
        # Nothing in our own scan, which is not the same as nothing at all --
        # see running_streamers.
        streamers = running_streamers()
        if streamers:
            self._logger.warning(
                f"BitBang:   nothing we can see holds {self.device}, but "
                f"{', '.join(streamers)} is running. A streamer owned by "
                f"another user does not appear in our scan, so this is the "
                f"likely holder. On OctoPi: "
                f"sudo systemctl disable --now webcamd ffmpeg_hls camera-streamer")

    def _wait_for_device(self, timeout=_DEVICE_WAIT):
        """Wait for the device node to come back, up to `timeout`.

        A USB re-enumerate takes the node away and returns it a few seconds
        later, usually under the same name. Reopening before it is back fails
        for a reason that says nothing about whether the camera is coming."""
        deadline = time.monotonic() + timeout
        while not self._stop.is_set():
            if os.path.exists(self.device):
                return True
            if time.monotonic() >= deadline:
                return False
            self._stop.wait(0.5)
        return False

    # -- CaptureSupervisor hooks --
    #
    # The loop itself is in capture_supervisor, shared with the other sources.
    # These say what "one capture" and "the source might work again" mean for a
    # V4L2 device, and hand it the reporting this class already had.

    @property
    def _source_name(self):
        return self.device

    def _wait_for_source(self):
        return self._wait_for_device()

    def _cleanup_capture(self):
        self._close_proc()

    def _report_failure(self, reason, ran_seconds):
        self._log_failure(reason, ran_seconds)

    def _report_contention(self):
        self._log_contention()

    def _capture_tail(self):
        # Prefixed, because the supervisor prints these for any source and
        # "ffmpeg:" is only true of this one.
        return [f"ffmpeg: {line}" for line in self._stderr_tail]

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
