"""OctoPrint-BitBang plugin.

Remote OctoPrint access with live H.264 video via BitBang WebRTC.
No account, no subscription, no port forwarding. One shareable link.
"""

__plugin_name__ = "BitBang"
__plugin_pythoncompat__ = ">=3.7,<4"

try:
    import threading
    import octoprint.plugin

    from bitbang.proxy import ReverseProxyASGI
    from .octoprint_adapter import OctoPrintBitBang
    from .camera import detect_camera

    import flask
    import json
    import asyncio
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.contrib.media import MediaRelay

    from octoprint.schema.webcam import Webcam, WebcamCompatibility

    class BitBangPlugin(
        octoprint.plugin.StartupPlugin,
        octoprint.plugin.ShutdownPlugin,
        octoprint.plugin.SettingsPlugin,
        octoprint.plugin.TemplatePlugin,
        octoprint.plugin.AssetPlugin,
        octoprint.plugin.BlueprintPlugin,
        octoprint.plugin.WebcamProviderPlugin,
    ):
        def __init__(self):
            super().__init__()
            self._adapter = None
            self._thread = None
            self._local_pcs = set()  # track local WebRTC peer connections

        def on_after_startup(self):
            if not self._settings.get_boolean(["enabled"]):
                self._logger.info("BitBang disabled in settings")
                return
            self._start_bitbang()

        def _start_bitbang(self):
            port = self._settings.global_get(["server", "port"]) or 5000
            proxy_app = ReverseProxyASGI(f"localhost:{port}")

            # Use configured camera or auto-detect
            camera_device = self._settings.get(["camera_device"])
            camera_resolution = self._settings.get(["camera_resolution"]) or "640x480"

            if camera_device:
                camera = {
                    "type": "usb",
                    "device": camera_device,
                    "format": "v4l2",
                    "options": {"framerate": "30", "video_size": camera_resolution},
                }
                self._logger.info(f"Camera: {camera_device} at {camera_resolution}")
            else:
                camera = detect_camera(logger=self._logger)
                if camera:
                    camera.setdefault("options", {})["video_size"] = camera_resolution
                    self._logger.info(f"Camera: {camera['type']} at {camera_resolution}")
                else:
                    self._logger.info("No camera detected, HTTP-only mode")

            pin = self._settings.get(["pin"]) or None

            self._adapter = OctoPrintBitBang(
                proxy_app,
                camera_source=camera,
                ws_target=f"localhost:{port}",
                program_name="octoprint",
                pin=pin,
            )

            self._thread = threading.Thread(
                target=self._adapter.run,
                daemon=True,
                name="BitBangThread",
            )
            self._thread.start()

            url = f"https://bitba.ng/{self._adapter.uid}"
            self._settings.set(["url"], url)
            self._settings.save()
            self._logger.info(f"BitBang remote access: {url}")

        # -- Local WebRTC video signaling --

        @octoprint.plugin.BlueprintPlugin.route("/offer", methods=["POST"])
        @octoprint.plugin.BlueprintPlugin.csrf_exempt()
        def local_offer(self):
            """Exchange WebRTC SDP for local H.264 video streaming."""
            if not self._adapter or not self._adapter.player or not self._adapter.player.video:
                return flask.jsonify({"error": "no camera"}), 503

            offer_sdp = flask.request.json.get("sdp")
            offer_type = flask.request.json.get("type", "offer")
            if not offer_sdp:
                return flask.jsonify({"error": "missing sdp"}), 400

            # Run the async WebRTC handshake in the adapter's event loop
            loop = self._adapter._loop
            if not loop:
                return flask.jsonify({"error": "not ready"}), 503

            future = asyncio.run_coroutine_threadsafe(
                self._handle_local_offer(offer_sdp, offer_type), loop
            )
            try:
                answer = future.result(timeout=10)
                return flask.jsonify(answer)
            except Exception as e:
                self._logger.error(f"Local WebRTC offer failed: {e}")
                return flask.jsonify({"error": str(e)}), 500

        async def _handle_local_offer(self, offer_sdp, offer_type):
            pc = RTCPeerConnection()
            self._local_pcs.add(pc)

            @pc.on("connectionstatechange")
            async def on_state():
                if pc.connectionState in ("failed", "closed"):
                    self._local_pcs.discard(pc)
                    await pc.close()

            # Add camera video track
            pc.addTrack(self._adapter.relay.subscribe(self._adapter.player.video))

            # Set remote offer and create answer
            offer = RTCSessionDescription(sdp=offer_sdp, type=offer_type)
            await pc.setRemoteDescription(offer)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)

            return {
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type,
            }

        # -- Camera settings API --

        @octoprint.plugin.BlueprintPlugin.route("/cameras", methods=["GET"])
        def list_cameras(self):
            """List available video capture devices."""
            import subprocess
            cameras = []
            try:
                result = subprocess.run(
                    ["v4l2-ctl", "--list-devices"],
                    capture_output=True, text=True, timeout=5
                )
                current_name = None
                for line in result.stdout.splitlines():
                    if not line.startswith("\t"):
                        current_name = line.strip().rstrip(":")
                    elif "/dev/video" in line:
                        dev = line.strip()
                        # Only include devices that have video formats
                        # (filters out metadata-only nodes like /dev/video1)
                        if self._has_video_formats(dev):
                            cameras.append({"device": dev, "name": current_name or dev})
            except Exception as e:
                self._logger.warning(f"Failed to list cameras: {e}")
            return flask.jsonify(cameras)

        @octoprint.plugin.BlueprintPlugin.route("/resolutions", methods=["GET"])
        def list_resolutions(self):
            """List supported resolutions for a camera device."""
            import subprocess
            device = flask.request.args.get("device", "/dev/video0")
            resolutions = []
            try:
                result = subprocess.run(
                    ["v4l2-ctl", "--list-formats-ext", "-d", device],
                    capture_output=True, text=True, timeout=5
                )
                seen = set()
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("Size: Discrete"):
                        res = line.split("Discrete")[1].strip()
                        if res not in seen:
                            seen.add(res)
                            resolutions.append(res)
            except Exception as e:
                self._logger.warning(f"Failed to list resolutions: {e}")
            # Sort by width
            resolutions.sort(key=lambda r: int(r.split("x")[0]))
            return flask.jsonify(resolutions)

        def _has_video_formats(self, device):
            """Check if a V4L2 device has any video capture formats."""
            import subprocess
            try:
                result = subprocess.run(
                    ["v4l2-ctl", "--list-formats-ext", "-d", device],
                    capture_output=True, text=True, timeout=5
                )
                return "Size: Discrete" in result.stdout
            except Exception:
                return False

        # -- WebcamProviderPlugin API --

        def get_webcam_configurations(self):
            return [
                Webcam(
                    name="bitbang",
                    displayName="BitBang Camera",
                    canSnapshot=True,
                    snapshotDisplay="BitBang plugin captures snapshot from video stream",
                )
            ]

        def take_webcam_snapshot(self, webcamName):
            """Grab a frame from the video track and return JPEG bytes."""
            if not self._adapter or not self._adapter.player or not self._adapter.player.video:
                from octoprint.webcams import WebcamNotAbleToTakeSnapshotException
                raise WebcamNotAbleToTakeSnapshotException(webcamName)

            import io
            loop = self._adapter._loop
            if not loop:
                from octoprint.webcams import WebcamNotAbleToTakeSnapshotException
                raise WebcamNotAbleToTakeSnapshotException(webcamName)

            future = asyncio.run_coroutine_threadsafe(
                self._capture_frame(), loop
            )
            try:
                jpeg_bytes = future.result(timeout=5)
                return iter([jpeg_bytes])
            except Exception as e:
                self._logger.error(f"Snapshot failed: {e}")
                from octoprint.webcams import WebcamNotAbleToTakeSnapshotException
                raise WebcamNotAbleToTakeSnapshotException(webcamName)

        async def _capture_frame(self):
            """Grab one frame from the video relay and encode as JPEG."""
            import io as _io
            import av as _av

            # Subscribe to the relay to get a frame without
            # stealing from existing WebRTC consumers
            track = self._adapter.relay.subscribe(self._adapter.player.video)
            try:
                frame = await track.recv()
            finally:
                track.stop()

            # CRITICAL: copy the raw plane data immediately. The relay
            # shares frame buffers with the encoder -- any sws_scale call
            # (to_ndarray, to_image, reformat) will segfault if the encoder
            # is concurrently accessing the same buffer.
            planes_data = [bytes(frame.planes[i]) for i in range(len(frame.planes))]
            width, height = frame.width, frame.height
            fmt = frame.format.name

            # Build a new independent frame from the copied data
            new_frame = _av.VideoFrame(width, height, fmt)
            for i, data in enumerate(planes_data):
                new_frame.planes[i].update(data)

            # Now safe to convert -- this frame's buffer is ours alone
            buf = _io.BytesIO()
            new_frame.to_image().save(buf, format="JPEG", quality=85)
            return buf.getvalue()

        def on_shutdown(self):
            pass  # Daemon thread exits with OctoPrint

        def get_settings_defaults(self):
            return {
                "enabled": True,
                "pin": "",
                "url": "",
                "camera_device": "",
                "camera_resolution": "640x480",
            }

        def get_template_configs(self):
            return [
                {"type": "settings", "custom_bindings": False},
                {"type": "navbar", "custom_bindings": False},
            ]

        def get_template_vars(self):
            return {"plugin_version": "0.1.0"}

        def get_assets(self):
            return {
                "js": ["js/bitbang.js"],
            }

        def is_blueprint_csrf_protected(self):
            return True

        def is_template_autoescaped(self):
            return True

    __plugin_implementation__ = BitBangPlugin()

except ImportError:
    # OctoPrint not installed - standalone CLI mode
    pass
