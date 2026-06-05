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

    async def run_video_bridge(self, sock):
        """Negotiate a per-browser video PeerConnection over a socketpair to the
        Go proxy, which relays our offer/answer/ICE to the browser over its data
        channel. Reuses the existing camera track (self.relay + self.player) and
        force_h264 — only the *signaling path* moves off our own aiortc bitbang
        connection and onto the Go device. Newline-JSON protocol, keyed by
        client: open / offer / answer / candidate / close.
        """
        import asyncio
        import json
        from aiortc import (RTCPeerConnection, RTCSessionDescription,
                            RTCConfiguration, RTCIceServer)
        from aiortc.sdp import candidate_from_sdp

        loop = asyncio.get_running_loop()
        sock.setblocking(False)
        pcs = {}

        async def send(obj):
            await loop.sock_sendall(sock, (json.dumps(obj) + "\n").encode())

        def rtc_config(ice_servers):
            # The Go data PC's STUN/TURN, forwarded so the video PC can reach
            # peers with no direct path. None → aiortc's default (host only).
            if not ice_servers:
                return None
            servers = []
            for s in ice_servers:
                if not s.get("urls"):
                    continue
                servers.append(RTCIceServer(
                    urls=s["urls"], username=s.get("username"),
                    credential=s.get("credential")))
            return RTCConfiguration(servers) if servers else None

        async def on_open(client, ice_servers):
            if client in pcs or not self.player:
                return
            self._logger.info(
                f"[video-bridge] {client} open (ice_servers={len(ice_servers or [])})")
            config = rtc_config(ice_servers)
            pc = RTCPeerConnection(configuration=config) if config else RTCPeerConnection()
            pcs[client] = pc

            @pc.on("connectionstatechange")
            async def _():
                self._logger.info(f"[video-bridge] {client} -> {pc.connectionState}")
                if pc.connectionState in ("failed", "closed"):
                    await pc.close()
                    pcs.pop(client, None)

            sender = pc.addTrack(self.relay.subscribe(self.player.video))
            force_h264(pc, sender)
            # setLocalDescription blocks until ICE gathering completes; a
            # forwarded TURN server that's unreachable or whose hourly creds
            # have rotated can hang it indefinitely. Bound it so a bad gather
            # tears down this one PC instead of leaking it (the per-client task
            # model below already keeps it from starving other sessions).
            try:
                await asyncio.wait_for(
                    pc.setLocalDescription(await pc.createOffer()), 20)
            except Exception:
                await pc.close()
                pcs.pop(client, None)
                raise
            await send({"kind": "offer", "client": client, "sdp": pc.localDescription.sdp})

        async def on_answer(client, sdp):
            pc = pcs.get(client)
            if pc:
                await pc.setRemoteDescription(RTCSessionDescription(sdp, "answer"))

        async def on_candidate(client, cand):
            pc = pcs.get(client)
            if not (pc and cand and cand.get("candidate")):
                return
            try:
                s = cand["candidate"]
                if s.startswith("candidate:"):
                    s = s[len("candidate:"):]
                c = candidate_from_sdp(s)
                c.sdpMid = cand.get("sdpMid")
                c.sdpMLineIndex = cand.get("sdpMLineIndex")
                await pc.addIceCandidate(c)
            except Exception as e:
                self._logger.warning(f"[video-bridge] addIceCandidate failed: {e}")

        async def on_close(client):
            pc = pcs.pop(client, None)
            if pc:
                await pc.close()

        locks = {}
        tasks = set()

        async def dispatch(kind, client, msg):
            # Serialize per client so a session's offer precedes its
            # answer/candidates, but run clients concurrently: the read loop
            # must never block on one session's handshake (a hung ICE gather
            # used to freeze the loop and starve every later session of video).
            lock = locks.setdefault(client, asyncio.Lock())
            try:
                async with lock:
                    if kind == "open":
                        await on_open(client, msg.get("ice_servers"))
                    elif kind == "answer":
                        await on_answer(client, msg.get("sdp"))
                    elif kind == "candidate":
                        await on_candidate(client, msg.get("candidate"))
                    elif kind == "close":
                        await on_close(client)
            except Exception as e:
                self._logger.warning(f"[video-bridge] {kind} for {client} failed: {e}")
                await on_close(client)
            finally:
                if kind == "close":
                    locks.pop(client, None)

        def schedule(kind, client, msg):
            t = asyncio.ensure_future(dispatch(kind, client, msg))
            tasks.add(t)
            t.add_done_callback(tasks.discard)

        self._logger.info("[video-bridge] running")
        buf = b""
        try:
            while True:
                chunk = await loop.sock_recv(sock, 65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    kind, client = msg.get("kind"), msg.get("client")
                    if kind and client:
                        schedule(kind, client, msg)
        finally:
            for t in list(tasks):
                t.cancel()
            for pc in list(pcs.values()):
                await pc.close()
            self._logger.info("[video-bridge] stopped")

    async def close(self):
        """Close peer connections and media player."""
        await super().close()
        if self.player:
            if hasattr(self.player, "stop"):
                self.player.stop()
            elif self.player.video:
                self.player.video.stop()
            self.player = None
