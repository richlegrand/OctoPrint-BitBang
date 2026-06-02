"""OctoPrint BitBang adapter - extends BitBangASGI with camera video track.

Subclasses BitBangASGI to add a camera video track alongside async HTTP
reverse proxy. Fully async -- no WSGI thread pool.
Camera source is auto-detected or explicitly configured.
"""

import logging

from bitbang import BitBangASGI
from aiortc.contrib.media import MediaRelay

from .camera import detect_camera

_log = logging.getLogger(__name__)


def force_h264(pc, sender):
    """Force H.264 codec on a transceiver so aiortc doesn't negotiate VP8."""
    from aiortc.rtcrtpsender import RTCRtpSender
    h264 = [c for c in RTCRtpSender.getCapabilities("video").codecs
            if c.name == "H264"]
    for t in pc.getTransceivers():
        if t.sender is sender:
            t.setCodecPreferences(h264)
            break


class OctoPrintBitBang(BitBangASGI):
    """BitBang adapter with camera video for OctoPrint remote access.

    Extends BitBangASGI to capture video from the best available camera
    source and share it with all connected clients using MediaRelay.
    Falls back to HTTP-only mode if no camera is found.
    """

    def __init__(self, app, camera_source=None, ws_target=None, logger=None, **kwargs):
        super().__init__(app, **kwargs)
        self.ws_target = ws_target  # host:port for WebSocket bridging
        self.relay = MediaRelay()
        self.player = None
        self._logger = logger or _log
        self._init_camera(camera_source)

    def _init_camera(self, camera_source):
        """Initialize camera from explicit source or auto-detect."""
        source = camera_source or detect_camera(logger=self._logger)
        if not source:
            self._logger.info("No camera - running in HTTP-only mode")
            return
        self.player = self._make_player(source)

    def _make_player(self, source):
        """Pick the best available capture path, highest quality first:
          1. picamera2                  -- libcamera CSI (HW on Pi 4, SW on Pi 5)
          2. V4L2 device emits H.264    -- passthrough, no re-encode
          3. V4L2 raw + Pi 4 M2M encoder-- GPU re-encode (h264_v4l2m2m)
          4. software encode (aiortc)   -- last resort
        Flat priority ladder: the first path that works wins. Returns a
        player (MediaPlayer-shaped: .video/.stop) or None for HTTP-only.
        """
        flip_h = source.get("flip_horizontal", False)
        flip_v = source.get("flip_vertical", False)
        brightness = source.get("brightness", 0)
        need_flip = flip_h or flip_v

        # 1. Pi CSI via libcamera/picamera2 (Annex-B H.264, aiortc passthrough)
        if source["type"] == "picamera2":
            try:
                from .pi_h264_source import PiH264Track
                size = source.get("size", (640, 480))
                framerate = source.get("framerate", 30)
                player = PiH264Track(
                    size=size, framerate=framerate,
                    bitrate=source.get("bitrate", 4_000_000), brightness=brightness,
                    flip_horizontal=flip_h, flip_vertical=flip_v)
                self._logger.info(
                    f"Opened Pi CSI camera via H264Encoder ({size[0]}x{size[1]}@{framerate})")
                return player
            except Exception as e:
                self._logger.warning(f"Could not open Pi CSI camera: {e}")
                return None

        # V4L2 device (USB webcam or legacy mmal CSI cam)
        device = source["device"]
        opts = source.get("options", {})
        common = dict(
            video_size=opts.get("video_size", "1280x720"),
            framerate=int(opts.get("framerate", 30)),
            bitrate=source.get("bitrate", 4_000_000),
            brightness=brightness, flip_horizontal=flip_h, flip_vertical=flip_v)
        try:
            from .v4l2_h264_source import (
                V4l2H264Track, device_supports_h264, device_supports_flip,
                has_v4l2m2m_h264_encoder, reencode_input_format)
            # 2. device emits H.264 -> passthrough. A requested flip needs the
            #    device's hardware flip controls (can't filter encoded video).
            if device_supports_h264(device) and (
                    not need_flip or device_supports_flip(device)):
                player = V4l2H264Track(device, source_is_h264=True,
                                       input_format="h264", **common)
                self._logger.info(
                    f"Opened {device} via built-in H.264 encoder "
                    f"(hardware passthrough{', hw flip' if need_flip else ''})")
                return player
            # 3. raw source + Pi 4 hardware M2M encoder -> GPU re-encode
            #    (flip via ffmpeg filter, so no device flip controls needed).
            in_fmt = reencode_input_format(device)
            if in_fmt and has_v4l2m2m_h264_encoder():
                player = V4l2H264Track(device, source_is_h264=False,
                                       input_format=in_fmt, **common)
                self._logger.info(
                    f"Opened {device} via h264_v4l2m2m hardware encoder "
                    f"(GPU re-encode from {in_fmt}{', flip' if need_flip else ''})")
                return player
        except Exception as e:
            self._logger.warning(
                f"Hardware H.264 path unavailable on {device} ({e}); "
                f"using software encode")

        # 4. software encode (aiortc) -- last resort (e.g. Pi 5, raw USB cam)
        try:
            from .usb_camera_source import UsbCameraSource
            player = UsbCameraSource(
                device=device, format=source.get("format"), options=opts,
                brightness=brightness, flip_horizontal=flip_h, flip_vertical=flip_v)
            self._logger.info(f"Opened USB camera (software encode): {device}")
            return player
        except Exception as e:
            self._logger.warning(f"Could not open camera '{device}': {e}")
            return None

    def setup_peer_connection(self, pc, client_id):
        """Add camera video track to peer connection."""
        if self.player and self.player.video:
            sender = pc.addTrack(self.relay.subscribe(self.player.video))
            force_h264(pc, sender)
            self._logger.info(f"Added camera video track for {client_id}")

    def get_stream_metadata(self):
        """Return stream name for video track."""
        if self.player and self.player.video:
            return {"0": "camera"}
        return {}

    async def close(self):
        """Close peer connections and media player."""
        await super().close()
        if self.player:
            if hasattr(self.player, "stop"):
                self.player.stop()
            elif self.player.video:
                self.player.video.stop()
            self.player = None
