"""OctoPrint-BitBang plugin.

Remote OctoPrint access with live H.264 video via BitBang WebRTC.
No account, no subscription, no port forwarding. One shareable link.
"""

__plugin_name__ = "BitBang"
__plugin_version__ = "0.2.1"
__plugin_description__ = "Remote OctoPrint access with live H.264 video via BitBang WebRTC. No account, no port forwarding, one shareable link."
__plugin_url__ = "https://github.com/richlegrand/OctoPrint-BitBang"
__plugin_author__ = "Rich LeGrand"
__plugin_license__ = "MIT"
__plugin_privacypolicy__ = "https://github.com/richlegrand/OctoPrint-BitBang/blob/main/PRIVACY.md"
__plugin_pythoncompat__ = ">=3.10,<4"


def __plugin_check__():
    return True


try:
    import octoprint.plugin  # noqa: F401  presence check for plugin context
    from ._plugin import BitBangPlugin, _get_update_information
except ImportError as e:
    # Two distinct cases land here:
    #  1. Standalone CLI mode — `octoprint` isn't installed. Expected: this
    #     package can be imported by `python -m octoprint_bitbang.app` to
    #     run the BitBang prototype without OctoPrint. Silent skip.
    #  2. OctoPrint context but a hard dep (e.g. octoprint.schema.webcam on
    #     pre-1.9 OctoPrint, or one of the python-only imports) is missing.
    #     Surface it so the user sees what's wrong instead of the plugin
    #     silently disappearing. Soft video-stack failures (aiortc/FFmpeg)
    #     are handled inside `_plugin.py` and do not land here.
    if "octoprint" not in str(e).lower():
        import logging
        logging.getLogger(__name__).warning(
            "BitBang plugin not loaded: %s", e
        )
else:
    __plugin_implementation__ = BitBangPlugin()
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": _get_update_information,
    }
