"""OctoPrint-BitBang plugin class.

Kept separate from `__init__.py` so the class is defined at module top level
(idiomatic OctoPrint plugin layout) and `__init__.py` can stay a thin
metadata + conditional-load shim.

If the video stack (aiortc / bitbang / PyAV → FFmpeg) can't import, the
plugin still loads — `_VIDEO_IMPORT_ERROR` records why, `on_after_startup`
logs it prominently, and the BitBang signaling thread doesn't start. The
user sees the plugin in OctoPrint with a clear log line telling them what
to install, instead of the plugin silently disappearing.
"""

import asyncio
import logging
import threading

import flask
import octoprint.plugin
from octoprint.schema.webcam import Webcam, WebcamCompatibility

from . import __plugin_name__, __plugin_version__
from .camera import detect_camera

_log = logging.getLogger(__name__)

# Soft deps: the WebRTC + FFmpeg stack. Missing FFmpeg 7+ runtime is the
# most common failure on older 32-bit OctoPi images where PyAV's manylinux
# wheels don't exist and source builds fall through to a stale system
# libavformat. Caught here so the plugin still loads in a diagnostic state.
_VIDEO_IMPORT_ERROR = None
try:
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.contrib.media import MediaRelay
    from bitbang.proxy import ReverseProxyASGI
    from .octoprint_adapter import OctoPrintBitBang, force_h264
except ImportError as e:
    _VIDEO_IMPORT_ERROR = str(e)
    _log.warning(
        "BitBang video stack unavailable: %s. "
        "Plugin will load but video and BitBang remote access are disabled. "
        "This is usually a missing FFmpeg 7+ runtime — on Raspberry Pi OS "
        "Bookworm or newer, try `sudo apt install ffmpeg`. On 32-bit OctoPi, "
        "PyAV has no prebuilt wheel and source builds are fragile; 64-bit "
        "OctoPi is the smoothest path. See the project README.",
        e,
    )


# A PIN must be empty (remote access then gated/off) or at least this many
# characters. Mirrored by the wizard/settings JS; enforced server-side in
# on_settings_save as the authoritative backstop.
MIN_PIN_LENGTH = 4


class BitBangPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.ShutdownPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.BlueprintPlugin,
    octoprint.plugin.WebcamProviderPlugin,
    octoprint.plugin.WizardPlugin,
):
    def __init__(self):
        super().__init__()
        self._adapter = None
        self._thread = None
        self._local_pcs = set()  # track local WebRTC peer connections
        self._running = False  # whether the remote-access proxy is live

    def on_shutdown(self):
        # Release the camera cleanly when OctoPrint shuts down via its own
        # graceful path (and on in-process teardown such as a camera-device
        # change). NOTE: verified this does NOT fire on a systemd `service
        # octoprint restart` (SIGTERM) -- OctoPrint runs no plugin finalizers
        # there, so this does not cover the restart-while-streaming mmal wedge.
        # See V4l2H264Track.stop (graceful SIGINT release).
        try:
            player = getattr(self._adapter, "player", None) if self._adapter else None
            if player is not None:
                player.stop()
                self._logger.info("BitBang: stopped camera capture on shutdown")
        except Exception as e:
            self._logger.warning(f"BitBang: error stopping camera on shutdown: {e}")

    def on_after_startup(self):
        if _VIDEO_IMPORT_ERROR:
            # Soft-import failure already logged at module load. Re-log via
            # the plugin's own logger so it appears under `octoprint_bitbang`
            # in the OctoPrint UI's log filter as well.
            self._logger.error(
                "BitBang video stack unavailable (%s); plugin loaded but "
                "remote access and video are disabled. See module-level "
                "warning above for remediation.",
                _VIDEO_IMPORT_ERROR,
            )
            return
        self._probe_picamera2_sensor()
        if not self._settings.get_boolean(["enabled"]):
            self._logger.info("BitBang disabled in settings")
            return
        if not self._remote_access_allowed():
            self._logger.warning(
                "BitBang: remote access NOT started — no PIN is set. Set a PIN "
                "in the BitBang settings (or, advanced, explicitly allow running "
                "without one) to enable remote access."
            )
            return
        self._start_bitbang()

    def _remote_access_allowed(self):
        """Secure-by-default gate. The public tunnel is only exposed when a
        PIN protects it, or the user has explicitly opted into running without
        one. An empty PIN with no opt-in means remote access stays OFF — this
        is the enforcement behind the setup wizard (which merely prompts).

        Without this, anyone holding the share URL reaches OctoPrint with no
        BitBang-layer auth; for users who enabled OctoPrint's autologinLocal
        that is a full remote takeover (see the X-Forwarded-For handling in
        the Go proxy)."""
        pin = (self._settings.get(["pin"]) or "").strip()
        if pin:
            return True
        return self._settings.get_boolean(["allow_no_pin"])

    def _probe_picamera2_sensor(self):
        # Cache before the adapter opens the camera — picamera2 can't be
        # opened twice, so the resolutions endpoint relies on this.
        self._picam2_sensor_size = None
        try:
            from picamera2 import Picamera2
            cam = Picamera2()
            self._picam2_sensor_size = cam.sensor_resolution
            cam.close()
        except ImportError:
            # Expected on non-Pi systems.
            pass
        except Exception as e:
            # picamera2 is installed but instantiation failed (camera in
            # use by another process, libcamera misconfig, etc.). The
            # resolutions endpoint will fall back to v4l2 probing.
            self._logger.info(f"picamera2 sensor probe failed: {e}")

    def _start_bitbang(self):
        port = self._settings.global_get(["server", "port"]) or 5000
        proxy_app = ReverseProxyASGI(f"localhost:{port}")

        # Use configured camera or auto-detect
        camera_device = self._settings.get(["camera_device"])
        camera_resolution = self._settings.get(["camera_resolution"]) or "640x480"

        flip_h = self._settings.get_boolean(["flip_horizontal"])
        flip_v = self._settings.get_boolean(["flip_vertical"])

        brightness = self._settings.get_int(["brightness"]) or 0

        if camera_device == "picamera2":
            w, h = (int(x) for x in camera_resolution.split("x"))
            camera = {
                "type": "picamera2",
                "size": (w, h),
                "flip_horizontal": flip_h,
                "flip_vertical": flip_v,
                "brightness": brightness,
            }
            self._logger.info(f"Camera: picamera2 at {camera_resolution}")
        elif camera_device:
            camera = {
                "type": "usb",
                "device": camera_device,
                "format": "v4l2",
                "options": {"framerate": "30", "video_size": camera_resolution},
                "flip_horizontal": flip_h,
                "flip_vertical": flip_v,
                "brightness": brightness,
            }
            self._logger.info(f"Camera: {camera_device} at {camera_resolution}")
        else:
            camera = detect_camera(logger=self._logger)
            if camera:
                camera["flip_horizontal"] = flip_h
                camera["flip_vertical"] = flip_v
                camera["brightness"] = brightness
                if camera["type"] == "picamera2":
                    w, h = (int(x) for x in camera_resolution.split("x"))
                    camera["size"] = (w, h)
                else:
                    camera.setdefault("options", {})["video_size"] = camera_resolution
                self._logger.info(f"Camera: {camera['type']} at {camera_resolution}")
            else:
                self._logger.info("No camera detected, HTTP-only mode")

        pin = self._settings.get(["pin"]) or None
        signaling_server = self._settings.get(["signaling_server"]) or None

        self._adapter = OctoPrintBitBang(
            proxy_app,
            camera_source=camera,
            ws_target=f"localhost:{port}",
            program_name="octoprint",
            pin=pin,
            logger=self._logger,
            server=signaling_server,
        )
        self._logger.info(f"Signaling server: {signaling_server or '(bitbang default)'}")

        # Route the adapter's connection-request event into OctoPrint's
        # structured logger so the connecting browser IP shows up in
        # octoprint.log (and any plugin-log filter) rather than only
        # appearing in stdout/journald.
        @self._adapter.on_connection_request
        def _log_connection_request(client_id, browser_ip):
            self._logger.info(
                f"Connection request from {client_id} (browser_ip={browser_ip})"
            )

        # Split transport: the Go proxy (spawned in _start_video_bridge) now
        # owns signaling + data + HTTP/WS proxy under our shared identity. Python
        # no longer runs its own bitbang signaling — it only hosts a bare asyncio
        # loop for aiortc (the video bridge + the local-LAN /offer path).
        self._thread = threading.Thread(
            target=self._run_aiortc_loop,
            daemon=True,
            name="BitBangAiortcLoop",
        )
        self._thread.start()

        url = self._adapter.url
        self._settings.set(["url"], url)
        self._settings.save()
        self._logger.info(f"BitBang remote access: {url}")
        # Push the URL to the frontend so the navbar reflects it live, instead
        # of the browser polling the settings API.
        self._plugin_manager.send_plugin_message(self._identifier, {"url": url})

        # Split-transport: a supervised Go proxy (data + HTTP/WS over native-SCTP
        # pion) owns the transport under our shared identity, and our camera
        # track is fed into a video PeerConnection over a socketpair relay.
        self._start_video_bridge()
        self._running = True

    def _run_aiortc_loop(self):
        """Bare asyncio loop for aiortc. Replaces the adapter's own bitbang
        signaling — the Go proxy owns signaling/data/proxy now, so Python only
        runs the video bridge and the local-LAN /offer path on this loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._adapter._loop = loop
        try:
            loop.run_forever()
        finally:
            loop.close()

    def _go_binary(self):
        """Resolve the bundled Go proxy binary for this CPU arch (shipped in
        the wheel under octoprint_bitbang/bin/). Returns None if unsupported."""
        import os
        import platform
        import stat

        arch = {"aarch64": "arm64", "armv7l": "armv7",
                "armv6l": "armv6", "x86_64": "amd64"}.get(platform.machine())
        if not arch:
            self._logger.warning(f"[video-bridge] no Go binary for {platform.machine()}")
            return None
        path = os.path.join(os.path.dirname(__file__), "bin", f"bitbang-linux-{arch}")
        if not os.path.exists(path):
            self._logger.warning(
                f"[video-bridge] bundled Go binary missing: {path} -- remote "
                f"access and video are disabled. The proxy binaries ship only in "
                f"the PyPI package (and CI release artifacts), not the GitHub "
                f"source archive. Reinstall with 'pip install -U OctoPrint-BitBang' "
                f"(or remove + reinstall) to pull it."
            )
            return None
        try:  # pip/zip don't preserve the exec bit
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass
        return path

    def _start_video_bridge(self):
        import os
        import socket as _socket
        import subprocess

        go_bin = self._go_binary()
        if not go_bin:
            return

        port = self._settings.global_get(["server", "port"]) or 5000
        try:
            logs_dir = self._settings.global_get_basefolder("logs")
        except Exception:
            logs_dir = os.path.expanduser("~/.octoprint/logs")
        log_path = os.path.join(logs_dir, "bitbang-go.log")

        # Stale Go proxies (orphaned by a prior OctoPrint restart) would
        # re-register our shared UID and conflict — kill them first. Match on
        # args (path-independent); pkill never matches its own pid.
        subprocess.run(["pkill", "-f", "serve proxy -program octoprint"], check=False)

        self._go_stop = False
        threading.Thread(
            target=self._supervise_go, args=(go_bin, port, log_path),
            daemon=True, name="BitBangGoSupervisor").start()

    def _launch_bridge(self, parent):
        import time
        for _ in range(150):
            loop = getattr(self._adapter, "_loop", None)
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._adapter.run_video_bridge(parent), loop)
                self._logger.info("[video-bridge] launched")
                return
            time.sleep(0.1)
        self._logger.warning("[video-bridge] adapter loop never came up")

    def _supervise_go(self, go_bin, port, log_path):
        """Keep the Go proxy alive: spawn, feed the video bridge, wait()
        (reaps it), and restart with backoff on exit. Output goes to a
        line-buffered file so a crash trace is captured verbatim."""
        import socket as _socket
        import subprocess
        import time

        backoff = 1
        while not self._go_stop:
            parent, child = _socket.socketpair()
            try:
                logf = open(log_path, "a", buffering=1)
            except Exception:
                logf = subprocess.DEVNULL
            # Go shares our identity (-program) → one URL; -target serves
            # OctoPrint directly on the plain device URL.
            args = [go_bin, "serve", "proxy", "-program", "octoprint",
                    "-target", f"localhost:{port}", "-v", "-video-fd", str(child.fileno()),
                    # Always stamp the real browser IP as X-Forwarded-For — standard
                    # reverse-proxy behavior. Without it OctoPrint sees every request
                    # as coming from localhost, which (with autologinLocal) would
                    # auto-log-in a remote visitor. OctoPrint shows a one-time,
                    # per-browser "external access" notice as a result, which is
                    # accurate (the visitor IS external).
                    "-forward-client-ip"]
            # The Go proxy owns the data channel + signaling, so it's what must
            # enforce the PIN — passing it to the Python adapter does nothing now.
            pin = (self._settings.get(["pin"]) or "").strip()
            if pin:
                args += ["-pin", pin]
            proc = subprocess.Popen(
                args, pass_fds=(child.fileno(),), stdout=logf, stderr=subprocess.STDOUT)
            child.close()
            self._go_proc = proc
            self._logger.info(f"[video-bridge] Go proxy started (pid {proc.pid}); log: {log_path}")
            self._launch_bridge(parent)

            rc = proc.wait()  # blocks until exit; reaps the process
            try:
                parent.close()
            except Exception:
                pass
            if hasattr(logf, "close"):
                logf.close()
            if self._go_stop:
                break
            self._logger.warning(
                f"[video-bridge] Go proxy exited (rc={rc}); restarting in {backoff}s "
                f"— see {log_path}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

    # -- Local WebRTC video signaling --

    @octoprint.plugin.BlueprintPlugin.route("/ice-servers", methods=["GET"])
    def get_ice_servers(self):
        """Return TURN/STUN servers for local video WebRTC."""
        servers = self._adapter.get_ice_servers() if self._adapter else []
        return flask.jsonify(servers)

    @octoprint.plugin.BlueprintPlugin.route("/offer", methods=["POST"])
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

        ice_servers = self._adapter.get_ice_servers()

        future = asyncio.run_coroutine_threadsafe(
            self._handle_local_offer(offer_sdp, offer_type, ice_servers), loop
        )
        try:
            answer = future.result(timeout=10)
            answer['ice_servers'] = ice_servers
            return flask.jsonify(answer)
        except Exception as e:
            self._logger.error(f"Local WebRTC offer failed: {e}")
            return flask.jsonify({"error": str(e)}), 500

    def _strip_non_h264(self, sdp):
        """Remove non-H.264 video codecs from an SDP so aiortc has no
        choice but to negotiate H.264 (our track is pre-encoded H.264)."""
        import re
        lines = sdp.split("\r\n")
        h264_pts = [m.group(1) for m in (re.match(r"a=rtpmap:(\d+) H264/", l) for l in lines) if m]
        rtx_pts = [m.group(1) for m in (re.match(r"a=fmtp:(\d+) apt=(\d+)", l) for l in lines) if m and m.group(2) in h264_pts]
        keep = set(h264_pts) | set(rtx_pts)
        out = []
        for line in lines:
            m = re.match(r"(m=video \d+ \S+) (.+)", line)
            if m:
                header, pts = m.groups()
                kept = [p for p in pts.split() if p in keep]
                out.append(f"{header} {' '.join(kept)}")
                continue
            m = re.match(r"a=(rtpmap|fmtp|rtcp-fb):(\d+)", line)
            if m and m.group(2) not in keep:
                continue
            out.append(line)
        return "\r\n".join(out)

    async def _handle_local_offer(self, offer_sdp, offer_type, ice_servers=None):
        offer_sdp = self._strip_non_h264(offer_sdp)
        config = self._adapter._build_rtc_config(ice_servers) if ice_servers else None
        pc = RTCPeerConnection(config) if config else RTCPeerConnection()
        self._local_pcs.add(pc)

        @pc.on("connectionstatechange")
        async def on_state():
            if pc.connectionState in ("failed", "closed"):
                self._local_pcs.discard(pc)
                await pc.close()

        # Order matters: set remote description first so aiortc creates
        # the transceiver matching the client's mid. Then addTrack reuses
        # it and setCodecPreferences applies to the right one. Otherwise
        # the answer ends up negotiating VP8.
        offer = RTCSessionDescription(sdp=offer_sdp, type=offer_type)
        await pc.setRemoteDescription(offer)

        sender = pc.addTrack(self._adapter.relay.subscribe(self._adapter.player.video))

        force_h264(pc, sender)

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        }

    # -- Camera settings API --

    @octoprint.plugin.BlueprintPlugin.route("/camera/config", methods=["GET"])
    def camera_config(self):
        """Return current camera display / tuning config for the client."""
        return flask.jsonify({
            "flip_horizontal": bool(self._settings.get_boolean(["flip_horizontal"])),
            "flip_vertical": bool(self._settings.get_boolean(["flip_vertical"])),
            "brightness": self._settings.get_int(["brightness"]) or 0,
        })

    @octoprint.plugin.BlueprintPlugin.route("/camera/brightness", methods=["POST"])
    def set_brightness(self):
        """Update camera brightness live. Accepts {"value": int -100..100}."""
        try:
            value = int(flask.request.json.get("value"))
        except (TypeError, ValueError, AttributeError):
            return flask.jsonify({"error": "missing or invalid value"}), 400
        value = max(-100, min(100, value))

        player = self._adapter.player if self._adapter else None
        if player is None or not hasattr(player, "set_brightness"):
            return flask.jsonify({"error": "brightness not supported on this camera"}), 400

        if player.set_brightness(value) is False:
            return flask.jsonify({"error": "brightness not supported on this camera"}), 400
        self._settings.set_int(["brightness"], value)
        self._settings.save()
        return flask.jsonify({"value": value})

    @octoprint.plugin.BlueprintPlugin.route("/cameras", methods=["GET"])
    def list_cameras(self):
        """List actual camera choices, not every V4L2 node.

        A stock Pi exposes many /dev/video* nodes -- ISP, codec, and decoder
        processing blocks -- that look like capture devices but aren't cameras.
        We list only real cameras: USB/UVC webcams and the legacy mmal Pi
        camera (see _camera_info). Modern-stack CSI cameras come via picamera2.
        """
        import subprocess
        cameras = []
        # Pi CSI camera (modern libcamera stack) surfaces through picamera2.
        if getattr(self, "_picam2_sensor_size", None):
            cameras.append({"device": "picamera2", "name": "Pi Camera"})
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--list-devices"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if "/dev/video" not in line:
                    continue
                dev = line.strip()
                name = self._camera_info(dev)
                if name:
                    cameras.append({"device": dev, "name": name})
        except Exception as e:
            self._logger.warning(f"Failed to list cameras: {e}")
        # Disambiguate identical labels (e.g. two generic "USB Camera"s).
        totals = {}
        for c in cameras:
            totals[c["name"]] = totals.get(c["name"], 0) + 1
        nth = {}
        for c in cameras:
            if totals[c["name"]] > 1:
                nth[c["name"]] = nth.get(c["name"], 0) + 1
                c["name"] = f"{c['name']} {nth[c['name']]}"
        return flask.jsonify(cameras)

    def _camera_info(self, device):
        """If `device` is a real camera, return a friendly display name; else
        None. Allowlists by hardware: USB/UVC webcams (bus_info 'usb-') and the
        legacy mmal Pi camera ('bcm2835 mmal' driver), excluding ISP/codec/
        decoder nodes. Still requires a real capture format, so metadata-only
        nodes (e.g. a webcam's second node) are dropped too."""
        import subprocess
        try:
            info = subprocess.run(
                ["v4l2-ctl", "-d", device, "--info"],
                capture_output=True, text=True, timeout=5
            ).stdout
        except Exception:
            return None
        driver = bus = card = ""
        for ln in info.splitlines():
            s = ln.strip()
            if s.startswith("Driver name"):
                driver = s.split(":", 1)[1].strip().lower()
            elif s.startswith("Bus info"):
                bus = s.split(":", 1)[1].strip().lower()
            elif s.startswith("Card type"):
                card = s.split(":", 1)[1].strip()
        is_usb = bus.startswith("usb-")
        is_mmal = "mmal" in driver
        if not ((is_usb or is_mmal) and self._has_video_formats(device)):
            return None
        if is_mmal:
            return "Pi Camera"
        return self._clean_usb_name(card)

    @staticmethod
    def _clean_usb_name(card):
        """Friendly label for a USB webcam. V4L2 card strings are inconsistent
        (often the generic "UVC Camera (046d:0990)" with a USB vid:pid). Strip
        the vid:pid and fall back to a generic name when nothing descriptive
        remains, so the dropdown never shows raw vid:pid / driver noise."""
        import re
        name = re.sub(r"\s*\([0-9a-fA-F]{4}:[0-9a-fA-F]{4}\)\s*$", "",
                      card or "").strip()
        if not name or name.lower() in (
                "uvc camera", "usb camera", "uvc", "usb video class"):
            return "USB Camera"
        return name

    @octoprint.plugin.BlueprintPlugin.route("/resolutions", methods=["GET"])
    def list_resolutions(self):
        """List supported resolutions for a camera device."""
        import subprocess
        device = flask.request.args.get("device", "")
        # picamera2: explicit Pi CSI selection or auto-detect when present
        if device == "picamera2" or not device:
            picam_res = self._picamera2_resolutions()
            if picam_res is not None:
                return flask.jsonify(picam_res)
            if device == "picamera2":
                return flask.jsonify([])
            device = "/dev/video0"
        import re
        discrete = []
        stepwise_max = None
        seen = set()
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--list-formats-ext", "-d", device],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith("Size: Discrete"):
                    res = line.split("Discrete")[1].strip()
                    if res in seen:
                        continue
                    seen.add(res)
                    try:
                        if int(res.split("x")[0]) > self._MAX_VIDEO_WIDTH:
                            continue
                    except ValueError:
                        continue
                    discrete.append(res)
                elif (line.startswith("Size: Stepwise")
                      or line.startswith("Size: Continuous")):
                    # e.g. "Size: Stepwise 32x32 - 4056x3040 with step 2/2" --
                    # take the max corner (last WxH on the line).
                    pairs = re.findall(r"(\d+)x(\d+)", line)
                    if pairs:
                        w, h = int(pairs[-1][0]), int(pairs[-1][1])
                        if stepwise_max is None or w * h > stepwise_max[0] * stepwise_max[1]:
                            stepwise_max = (w, h)
        except Exception as e:
            self._logger.warning(f"Failed to list resolutions: {e}")

        if discrete:
            resolutions = discrete
        elif stepwise_max:
            # Continuous-range device (Pi mmal CSI camera): offer a curated set
            # of standard 4:3 and 16:9 modes that fit within the sensor max.
            max_w, max_h = stepwise_max
            resolutions = [
                f"{w}x{h}" for (w, h) in self._STANDARD_RESOLUTIONS
                if w <= max_w and h <= max_h and w <= self._MAX_VIDEO_WIDTH
            ]
        else:
            resolutions = []
        resolutions.sort(
            key=lambda r: (int(r.split("x")[0]), int(r.split("x")[1])))
        return flask.jsonify(resolutions)

    # Hard cap on offered video width, applied on every resolution path
    # (discrete USB, stepwise mmal, picamera2). 720p (1280-wide) is the
    # low-latency sweet spot; beyond it the USB re-encode path's MJPEG decode
    # and bandwidth scale poorly. We also OBSERVED the legacy mmal CSI driver
    # wedge the kernel at 1920x1080 -- a vb2_fop_release deadlock on teardown
    # that leaves the camera unusable until a reboot; capping width keeps the
    # mmal path at the more stable 720p operating point. Bump this one constant
    # to allow larger (and prefer picamera2/libcamera for high-res CSI).
    _MAX_VIDEO_WIDTH = 1280

    # Standard resolutions offered when a device reports a continuous size
    # range (Pi mmal CSI camera) or for picamera2 -- a mix of 4:3 and 16:9,
    # filtered by the sensor max and _MAX_VIDEO_WIDTH.
    _STANDARD_RESOLUTIONS = [
        (640, 480), (800, 600), (1024, 768), (1280, 960),   # 4:3
        (1280, 720), (1920, 1080),                          # 16:9
    ]
    _PICAMERA2_STANDARD_RESOLUTIONS = _STANDARD_RESOLUTIONS

    def _picamera2_resolutions(self):
        """Return list of supported resolutions for the Pi CSI sensor, or None."""
        sensor = getattr(self, "_picam2_sensor_size", None)
        if not sensor:
            return None
        max_w, max_h = sensor
        return [
            f"{w}x{h}"
            for (w, h) in self._PICAMERA2_STANDARD_RESOLUTIONS
            if w <= max_w and h <= max_h and w <= self._MAX_VIDEO_WIDTH
        ]

    def _has_video_formats(self, device):
        """True if the device is a single-planar video-capture camera with at
        least one advertised frame size. Accepts both discrete sizes (USB UVC)
        and stepwise/continuous ranges (Pi mmal CSI camera). Excludes M2M
        codec/ISP nodes, which report 'Type: Video Capture Multiplanar'."""
        import subprocess
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--list-formats-ext", "-d", device],
                capture_output=True, text=True, timeout=5
            )
        except Exception:
            return False
        out = result.stdout
        has_capture = any(
            ln.strip() == "Type: Video Capture" for ln in out.splitlines())
        has_sizes = any(
            s in out for s in
            ("Size: Discrete", "Size: Stepwise", "Size: Continuous"))
        return has_capture and has_sizes

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
        from octoprint.webcams import WebcamNotAbleToTakeSnapshotException

        if not self._adapter or not self._adapter.player or not self._adapter.player.video:
            raise WebcamNotAbleToTakeSnapshotException(webcamName)

        player = self._adapter.player
        try:
            # Pi CSI: grab directly via picamera2 (no H.264 decode) so we
            # stay within the Pi 4 CPU budget. USB falls through to the
            # relay-based decoded-frame path.
            if hasattr(player, "capture_snapshot"):
                return iter([player.capture_snapshot()])

            loop = self._adapter._loop
            if not loop:
                raise WebcamNotAbleToTakeSnapshotException(webcamName)
            future = asyncio.run_coroutine_threadsafe(self._capture_frame(), loop)
            return iter([future.result(timeout=5)])
        except WebcamNotAbleToTakeSnapshotException:
            raise
        except Exception as e:
            self._logger.error(f"Snapshot failed: {e}")
            raise WebcamNotAbleToTakeSnapshotException(webcamName)

    async def _capture_frame(self):
        """Grab one frame from the video relay and encode as JPEG.

        The hardware tracks (V4l2H264Track, PiH264Track) emit encoded av.Packet,
        so we decode on demand with a software H.264 decoder until a keyframe
        yields a frame -- decoding happens only for a snapshot, never during
        normal streaming. The software path (UsbCameraSource) already emits raw
        VideoFrames, which are used directly.
        """
        import io as _io
        import av as _av

        # Subscribe to the relay so we don't steal from existing WebRTC consumers.
        track = self._adapter.relay.subscribe(self._adapter.player.video)
        decoder = None
        frame = None
        got_keyframe = False
        try:
            for _ in range(150):  # a keyframe arrives within one GOP (~1s @30fps)
                obj = await track.recv()
                if isinstance(obj, _av.VideoFrame):    # software path: raw frame
                    frame = obj
                    break
                # Encoded path: a fresh software H.264 decoder must START at a
                # keyframe -- the IDR carries SPS/PPS (repeat_sequence_header /
                # inline headers). Subscribing mid-stream (a long-running print)
                # yields P-frames first, which decode as "Invalid data found
                # when processing input"; skip until the first keyframe.
                if not got_keyframe:
                    if not getattr(obj, "is_keyframe", False):
                        continue
                    got_keyframe = True
                if decoder is None:
                    decoder = _av.CodecContext.create("h264", "r")
                try:
                    decoded = decoder.decode(obj)
                except Exception:                      # tolerate a stray bad packet
                    continue
                if decoded:
                    frame = decoded[-1]
                    break
            if frame is None:
                raise RuntimeError("no frame available for snapshot")
        finally:
            track.stop()

        if decoder is None:
            # A raw relay frame may share its buffer with the encoder -- copy the
            # planes before any sws_scale (to_image), which would otherwise
            # segfault if the encoder touches the same buffer concurrently.
            planes = [bytes(frame.planes[i]) for i in range(len(frame.planes))]
            safe = _av.VideoFrame(frame.width, frame.height, frame.format.name)
            for i, data in enumerate(planes):
                safe.planes[i].update(data)
            frame = safe
        # A decoded frame is ours alone -- safe to convert directly.

        buf = _io.BytesIO()
        frame.to_image().save(buf, format="JPEG", quality=85)
        return buf.getvalue()

    def get_settings_defaults(self):
        return {
            "enabled": True,
            "pin": "",
            # Explicit opt-in to expose remote access with NO PIN. Default
            # False so a fresh install is gated until the wizard runs.
            "allow_no_pin": False,
            "url": "",
            "camera_device": "",
            "camera_resolution": "640x480",
            "flip_horizontal": False,
            "flip_vertical": False,
            "brightness": 0,
            "signaling_server": "bitba.ng",
        }

    def on_settings_save(self, data):
        # Server-side PIN-policy backstop. The wizard/settings JS validates
        # too, but this is authoritative: a non-empty PIN below the minimum
        # length is rejected (the prior value is kept) rather than persisted.
        if data.get("pin"):
            pin = str(data["pin"]).strip()
            if 0 < len(pin) < MIN_PIN_LENGTH:
                self._logger.warning(
                    "BitBang: rejected PIN shorter than %d characters",
                    MIN_PIN_LENGTH,
                )
                data["pin"] = self._settings.get(["pin"])
            else:
                data["pin"] = pin

        # Snapshot gate-relevant state, save, then reconcile if it changed.
        before = self._gate_state()
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        if self._gate_state() != before:
            self._reconcile_remote_access()

    def _gate_state(self):
        """The tuple of settings that determines whether/how the proxy runs."""
        return (
            self._settings.get_boolean(["enabled"]),
            (self._settings.get(["pin"]) or "").strip(),
            self._settings.get_boolean(["allow_no_pin"]),
        )

    def _reconcile_remote_access(self):
        """React to a gate-relevant settings change.

        Starting remote access when it wasn't running (e.g. the wizard just set
        the first PIN) is done live — nothing is connected yet, so there's no
        tunnel to drop. Any change to an ALREADY-running proxy (PIN change,
        disable) is NOT applied live: bouncing the proxy would drop active
        connections, including the very save request that triggered this if it
        arrived over the tunnel (the request would hang). Those changes take
        effect on the next OctoPrint restart; we tell the user."""
        if _VIDEO_IMPORT_ERROR:
            return  # video stack unavailable; nothing to start/stop
        try:
            should_run = self._settings.get_boolean(["enabled"]) and self._remote_access_allowed()
            if should_run and not self._running:
                self._logger.info("BitBang: remote access now permitted — starting")
                self._start_bitbang()
            elif self._running:
                self._logger.info(
                    "BitBang: change will take effect on the next OctoPrint restart"
                )
                self._plugin_manager.send_plugin_message(
                    self._identifier,
                    {"restart_needed": "BitBang: restart OctoPrint to apply this change."},
                )
        except Exception as e:
            self._logger.warning("BitBang: reconcile failed (%s)", e)

    def get_template_configs(self):
        return [
            {"type": "settings", "custom_bindings": True},
            {"type": "navbar", "custom_bindings": True},
            {"type": "wizard", "name": "BitBang", "template": "bitbang_wizard.jinja2"},
            # Render the live view as a proper webcam provider template so
            # OctoPrint shows it only when "BitBang Camera" is the selected
            # webcam -- no DOM-replacing the classic webcam.
            {"type": "webcam", "name": "BitBang Camera", "template": "bitbang_webcam.jinja2"},
        ]

    # -- Setup wizard (secure-by-default PIN prompt) --

    def is_wizard_required(self):
        # Show the wizard whenever remote access would be enabled but is
        # currently ungated (no PIN, no explicit opt-out). Covers fresh
        # installs and existing no-PIN users on upgrade.
        return self._settings.get_boolean(["enabled"]) and not self._remote_access_allowed()

    def get_wizard_version(self):
        # Bump to re-show the wizard to users who already cleared an older
        # version (e.g. if the PIN policy changes again).
        return 1

    def get_wizard_details(self):
        return {"required": self.is_wizard_required()}

    def get_template_vars(self):
        return {"plugin_version": __plugin_version__}

    def get_assets(self):
        return {
            "js": ["js/bitbang.js"],
        }

    def is_blueprint_csrf_protected(self):
        return True

    def is_template_autoescaped(self):
        return True


def _get_update_information():
    return {
        "bitbang": {
            "displayName": __plugin_name__,
            "displayVersion": __plugin_version__,
            "type": "github_release",
            "user": "richlegrand",
            "repo": "OctoPrint-BitBang",
            "current": __plugin_version__,
            "stable_branch": {
                "name": "Stable",
                "branch": "main",
                "commitish": ["main"],
            },
            "pip": "https://github.com/richlegrand/OctoPrint-BitBang/releases/download/{target_version}/release.zip",
        }
    }
