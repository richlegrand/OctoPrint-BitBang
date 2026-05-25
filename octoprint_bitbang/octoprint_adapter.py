"""OctoPrint BitBang adapter - extends BitBangASGI with camera video track.

Subclasses BitBangASGI to add a camera video track alongside async HTTP
reverse proxy. Fully async -- no WSGI thread pool.
Camera source is auto-detected or explicitly configured.
"""

from bitbang import BitBangASGI
from aiortc.contrib.media import MediaRelay

from .camera import detect_camera


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

    def __init__(self, app, camera_source=None, ws_target=None, **kwargs):
        super().__init__(app, **kwargs)
        self.ws_target = ws_target  # host:port for WebSocket bridging
        self.relay = MediaRelay()
        self.player = None
        self._init_camera(camera_source)

    def _init_camera(self, camera_source):
        """Initialize camera from explicit source or auto-detect."""
        source = camera_source or detect_camera()
        if not source:
            print("No camera - running in HTTP-only mode")
            return

        if source["type"] == "usb":
            # USB webcam (software H.264 encode via aiortc)
            try:
                from .usb_camera_source import UsbCameraSource
                self.player = UsbCameraSource(
                    device=source["device"],
                    format=source.get("format"),
                    options=source.get("options", {}),
                    brightness=source.get("brightness", 0),
                    flip_horizontal=source.get("flip_horizontal", False),
                    flip_vertical=source.get("flip_vertical", False),
                )
                print(f"Opened USB camera: {source['device']}")
            except Exception as e:
                print(f"Warning: Could not open camera '{source['device']}': {e}")

        elif source["type"] == "picamera2":
            # Pi CSI camera - picamera2 H264Encoder (hw on Pi 4, sw on Pi 5)
            # emits Annex-B H.264 that aiortc packetizes without re-encoding.
            try:
                from .pi_h264_source import PiH264Track
                size = source.get("size", (640, 480))
                framerate = source.get("framerate", 30)
                bitrate = source.get("bitrate", 4_000_000)
                brightness = source.get("brightness", 0)
                self.player = PiH264Track(
                    size=size, framerate=framerate, bitrate=bitrate,
                    brightness=brightness,
                    flip_horizontal=source.get("flip_horizontal", False),
                    flip_vertical=source.get("flip_vertical", False),
                )
                print(f"Opened Pi CSI camera via H264Encoder ({size[0]}x{size[1]}@{framerate})")
            except Exception as e:
                print(f"Warning: Could not open Pi CSI camera: {e}")

    def setup_peer_connection(self, pc, client_id):
        """Add camera video track to peer connection."""
        if self.player and self.player.video:
            sender = pc.addTrack(self.relay.subscribe(self.player.video))
            force_h264(pc, sender)
            print(f"Added camera video track for {client_id}")

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
