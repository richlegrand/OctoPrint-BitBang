"""USB UVC camera source: aiortc MediaPlayer + optional flip + V4L2 brightness.

A MediaStreamTrack rather than a thin holder, so a camera that drops out can be
reopened under a subscriber that never notices. The adapter hands `.video` to
the relay once; swapping the MediaPlayer underneath is invisible from there,
which is the whole reason this is a track and not just a reference to one.

Supervision happens in recv() rather than on a worker thread, because this is a
pull source: the player's track is awaited, so the failure arrives as a
MediaStreamError on the event loop. The rules for whether to reopen are the
same ones the push sources use, shared as ReopenPolicy -- see
capture_supervisor.

Brightness is applied by shelling to v4l2-ctl; the slider's -100..100 range is
mapped into the device's actual brightness control range, queried once at
startup.
"""

import asyncio
import logging
import os
import shutil
import subprocess
import time

from aiortc import MediaStreamTrack
from aiortc.contrib.media import MediaPlayer
from aiortc.mediastreams import MediaStreamError

from .capture_supervisor import ReopenPolicy
from .flip_track import FlippedTrack

_log = logging.getLogger(__name__)

# How long to wait for the device node to come back before reopening anyway.
_DEVICE_WAIT = 30.0


class UsbCameraSource(MediaStreamTrack):
    kind = "video"

    def __init__(self, device, format=None, options=None,
                 brightness=0, flip_horizontal=False, flip_vertical=False,
                 logger=None):
        super().__init__()
        self.device = device
        self._format = format
        self._options = options or {}
        self._flip_h = bool(flip_horizontal)
        self._flip_v = bool(flip_vertical)
        self._logger = logger or _log

        self._player = None
        self._raw_track = None
        self._track = None
        self._opened_at = 0.0
        self._policy = ReopenPolicy()
        self._stopped = False

        # Opened here, not lazily: the adapter's ladder depends on a camera it
        # cannot open raising, so the next rung gets a turn and the operator
        # gets "Could not open camera" rather than a track that fails later.
        self._open()

        self._brightness_range = self._query_brightness_range()
        if self._brightness_range:
            self.set_brightness(brightness)

    # -- capture lifecycle --

    def _open(self):
        self._player = MediaPlayer(self.device, format=self._format,
                                   options=self._options)
        track = self._player.video
        if track is None:
            raise RuntimeError(f"{self.device} offers no video track")
        self._raw_track = track
        if self._flip_h or self._flip_v:
            track = FlippedTrack(track, hflip=self._flip_h,
                                 vflip=self._flip_v)
        self._track = track
        self._opened_at = time.monotonic()

    def _close(self):
        """Release the device so a reopen is not fighting our own player."""
        for t in (self._track, self._raw_track):
            try:
                if t is not None:
                    t.stop()
            except Exception:
                pass
        self._player = None
        self._raw_track = None
        self._track = None

    async def _wait_for_device(self, timeout=_DEVICE_WAIT):
        """Wait for the device node to come back, up to `timeout`.

        A USB re-enumerate takes the node away and returns it a few seconds
        later. Reopening before it is back fails for a reason that says
        nothing about whether the camera is coming."""
        deadline = time.monotonic() + timeout
        while not self._stopped:
            if os.path.exists(self.device):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.5)
        return False

    async def _reopen(self):
        """Wait, then try to open again. False once the budget is spent."""
        if not await self._wait_for_device():
            self._logger.warning(
                f"BitBang: {self.device} has not come back; still trying")
        await asyncio.sleep(self._policy.backoff())
        if self._stopped:
            return False
        self._logger.info(f"BitBang: reopening {self.device}")
        try:
            self._open()
            return True
        except Exception as e:  # noqa: BLE001 - reported, then retried or given up
            return self._note_ended(str(e) or e.__class__.__name__,
                                    ran_seconds=0.0)

    def _note_ended(self, reason, ran_seconds):
        """Record a capture that ended and say whether to keep trying."""
        keep_going = self._policy.record(ran_seconds)
        if self._policy.first_failure_of_episode:
            self._logger.warning(
                f"BitBang: capture on {self.device} stopped after "
                f"{ran_seconds:.1f}s: {reason}")
        else:
            self._logger.warning(
                f"BitBang: capture on {self.device} failed again after "
                f"{ran_seconds:.1f}s "
                f"({self._policy.failed_starts}/"
                f"{self._policy.max_failed_starts}): {reason}")
        if not keep_going:
            self._logger.error(
                f"BitBang: giving up on {self.device} after "
                f"{self._policy.max_failed_starts} failed starts -- the "
                f"camera is gone, in use, or configured in a way this machine "
                f"cannot run")
        return keep_going

    async def recv(self):
        while True:
            if self._stopped:
                raise MediaStreamError("camera stopped")
            if self._track is None and not await self._reopen():
                raise MediaStreamError(
                    f"capture on {self.device} failed and will not reopen")
            try:
                return await self._track.recv()
            except MediaStreamError:
                if self._stopped:
                    raise
                ran = time.monotonic() - self._opened_at
                self._close()
                if not self._note_ended("the media stream ended", ran):
                    raise MediaStreamError(
                        f"capture on {self.device} failed and will not reopen")
                # Round the loop: _reopen handles the wait and the backoff.

    @property
    def video(self):
        # MediaPlayer-shaped interface, so the adapter treats us like the
        # other sources. Returning self is what lets the player underneath be
        # replaced without the relay noticing.
        return self

    # -- device controls --

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
        self._stopped = True
        self._close()
        super().stop()
