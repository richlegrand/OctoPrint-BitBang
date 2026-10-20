"""USB UVC camera source: aiortc MediaPlayer + optional flip + V4L2 brightness.

Exposes a MediaPlayer-shaped interface (.video, set_brightness, stop) so the
adapter can treat it the same way as PiH264Track. Brightness is applied by
shelling to v4l2-ctl; the slider's -100..100 range is mapped into the device's
actual brightness control range queried once at startup.
"""

import shutil
import subprocess

from aiortc.contrib.media import MediaPlayer

from .flip_track import FlippedTrack


class UsbCameraSource:
    def __init__(self, device, format=None, options=None,
                 brightness=0, flip_horizontal=False, flip_vertical=False):
        self.device = device
        self._player = MediaPlayer(device, format=format, options=options or {})
        track = self._player.video
        if track and (flip_horizontal or flip_vertical):
            track = FlippedTrack(track, hflip=flip_horizontal, vflip=flip_vertical)
        self.video = track
        self._brightness_range = self._query_brightness_range()
        if self._brightness_range:
            self.set_brightness(brightness)

    def _query_brightness_range(self):
        """Return (min, max) for the device's V4L2 brightness control, or
        None if v4l2-ctl is missing or the device has no such control."""
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
        """Slider -100..100 → linear interp into the device's brightness
        range. Returns True if applied, False if the device has no
        brightness control."""
        if not self._brightness_range:
            return False
        value = max(-100, min(100, int(value)))
        lo, hi = self._brightness_range
        v4l2_value = round(lo + (value + 100) * (hi - lo) / 200)
        subprocess.run(
            ["v4l2-ctl", "-d", self.device, "--set-ctrl", f"brightness={v4l2_value}"],
            capture_output=True, timeout=5, check=False,
        )
        return True

    def stop(self):
        if self.video and hasattr(self.video, "stop"):
            self.video.stop()
