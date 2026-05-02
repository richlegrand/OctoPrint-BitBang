"""picamera2 H264Encoder → aiortc MediaStreamTrack passthrough.

Hardware H.264 on Pi 4 (V4L2 M2M at /dev/video11), software (LibavH264Encoder)
on Pi 5. picamera2 delivers Annex-B NAL units plus a microsecond timestamp
via Output.outputframe(); we wrap each as an av.Packet with pts set, so
aiortc's H264Payloader.pack() packetizes into RTP without re-encoding.
"""

import asyncio
import io
from fractions import Fraction

import av
from aiortc import MediaStreamTrack
from picamera2.outputs import Output

# picamera2's V4L2Encoder reports timestamps as integer microseconds
# (seconds * 1_000_000 + microseconds from V4L2 buffer timestamp).
_TIME_BASE = Fraction(1, 1_000_000)


class _QueueOutput(Output):
    """picamera2 Output that turns each encoded frame into an av.Packet
    and enqueues it on a provided asyncio.Queue (thread-safely)."""

    def __init__(self, loop, queue):
        super().__init__()
        self._loop = loop
        self._queue = queue

    def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
        if timestamp is None:
            return
        data = frame if isinstance(frame, (bytes, bytearray)) else bytes(frame)
        pkt = av.Packet(data)
        pkt.pts = timestamp
        pkt.dts = timestamp
        pkt.time_base = _TIME_BASE
        pkt.is_keyframe = keyframe
        self._loop.call_soon_threadsafe(self._try_put, pkt)

    def _try_put(self, pkt):
        # Drop oldest on overflow so live stream doesn't stall.
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(pkt)
        except asyncio.QueueFull:
            pass


class PiH264Track(MediaStreamTrack):
    """aiortc video track backed by picamera2's H264Encoder."""

    kind = "video"

    def __init__(self, size=(640, 480), framerate=30, bitrate=1_500_000,
                 brightness=0, flip_horizontal=False, flip_vertical=False):
        super().__init__()
        from picamera2 import Picamera2
        from picamera2.encoders import H264Encoder
        from libcamera import Transform

        self._size = size
        self._framerate = framerate
        self._brightness = brightness  # -100..100, scaled to picamera2's -1.0..1.0

        self.picam2 = Picamera2()
        config = self.picam2.create_video_configuration(
            main={"size": size, "format": "YUV420"},
            transform=Transform(hflip=bool(flip_horizontal), vflip=bool(flip_vertical)),
            controls={
                "FrameRate": float(framerate),
                "Brightness": self._brightness / 100.0,
            },
        )
        self.picam2.configure(config)
        # Start the camera up front (no encoder yet) so capture_image works
        # before any WebRTC client connects — needed for timelapse snapshots.
        self.picam2.start()

        # iperiod = framerate → 1s GOP. repeat=True → SPS/PPS before each IDR
        # so late-joining WebRTC subscribers can start decoding quickly.
        self.encoder = H264Encoder(
            bitrate=bitrate,
            iperiod=framerate,
            repeat=True,
            profile="baseline",
        )

        self._encoder_started = False
        self._queue: asyncio.Queue | None = None
        self._output: _QueueOutput | None = None

    def _ensure_started(self):
        if self._encoder_started:
            return
        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=30)
        self._output = _QueueOutput(loop, self._queue)
        self.picam2.start_encoder(self.encoder, self._output)
        self._encoder_started = True
        # V4L2 hw encoder on Pi 4 doesn't honor iperiod reliably; force a
        # keyframe every second so late-joining WebRTC peers can sync.
        self._keyframe_task = loop.create_task(self._keyframe_loop())

    async def _keyframe_loop(self):
        try:
            while True:
                await asyncio.sleep(1.0)
                self.encoder.force_key_frame()
        except asyncio.CancelledError:
            pass

    async def recv(self):
        self._ensure_started()
        return await self._queue.get()

    def set_brightness(self, value):
        """Update brightness live. `value` is -100..100 (mapped to -1.0..1.0)."""
        self._brightness = max(-100, min(100, int(value)))
        self.picam2.set_controls({"Brightness": self._brightness / 100.0})

    def capture_snapshot(self):
        """Return a JPEG snapshot grabbed straight from the camera. Safe to
        call concurrently with the H.264 encoder — picamera2/libcamera
        delivers a separate frame to the application without disturbing
        the encoder pipeline."""
        image = self.picam2.capture_image("main")
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=85)
        return buf.getvalue()

    @property
    def video(self):
        # MediaPlayer-shaped interface so the adapter can treat us like one.
        return self

    def stop(self):
        super().stop()
        try:
            if self._encoder_started:
                self.picam2.stop_encoder()
        except Exception:
            pass
        try:
            self.picam2.stop()
        except Exception:
            pass
        try:
            self.picam2.close()
        except Exception:
            pass
