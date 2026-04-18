"""OctoPrint-BitBang plugin.

Remote OctoPrint access with live H.264 video via BitBang WebRTC.
No account, no subscription, no port forwarding. One shareable link.
"""

__plugin_name__ = "BitBang"
__plugin_pythoncompat__ = ">=3.7,<4"

try:
    import threading
    import octoprint.plugin

    from .proxy import ReverseProxy
    from .octoprint_adapter import OctoPrintBitBang
    from .camera import detect_camera

    class BitBangPlugin(
        octoprint.plugin.StartupPlugin,
        octoprint.plugin.ShutdownPlugin,
        octoprint.plugin.SettingsPlugin,
        octoprint.plugin.TemplatePlugin,
        octoprint.plugin.AssetPlugin,
    ):
        def __init__(self):
            super().__init__()
            self._adapter = None
            self._thread = None

        def on_after_startup(self):
            if not self._settings.get_boolean(["enabled"]):
                self._logger.info("BitBang disabled in settings")
                return
            self._start_bitbang()

        def _start_bitbang(self):
            port = self._settings.global_get(["server", "port"]) or 5000
            api_key = self._settings.global_get(["api", "key"])
            proxy_app = ReverseProxy(f"localhost:{port}", api_key=api_key)

            camera = detect_camera(logger=self._logger)
            if camera:
                self._logger.info(f"Camera: {camera['type']}")
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

        def on_shutdown(self):
            pass  # Daemon thread exits with OctoPrint

        def get_settings_defaults(self):
            return {
                "enabled": True,
                "pin": "",
                "url": "",
            }

        def get_template_configs(self):
            return [
                {"type": "settings", "custom_bindings": False}
            ]

        def get_assets(self):
            return {
                "js": ["js/bitbang.js"],
            }

    __plugin_implementation__ = BitBangPlugin()

except ImportError:
    # OctoPrint not installed - standalone CLI mode
    pass
