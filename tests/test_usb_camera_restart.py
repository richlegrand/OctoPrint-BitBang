"""Reopening the software-encode camera after it drops out. See issue #6.

UsbCameraSource is a pull source: aiortc's MediaPlayer hands out a track that
is awaited, so a failure arrives as MediaStreamError on the event loop rather
than as a thread returning a reason. Supervision therefore lives in recv(),
and only the rules are shared with the push sources -- ReopenPolicy.

MediaPlayer is faked here. The point under test is what recv() does when the
stream ends, not whether aiortc can open a webcam.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiortc.mediastreams import MediaStreamError

from octoprint_bitbang import capture_supervisor as sup
from octoprint_bitbang import usb_camera_source as ucs
from octoprint_bitbang.usb_camera_source import UsbCameraSource


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


class _FakeTrack:
    """A player track that yields `frames` frames and then ends."""

    kind = "video"

    def __init__(self, frames, tag, on_end=None):
        self._left = frames
        self.tag = tag
        self._on_end = on_end
        self.stopped = False

    async def recv(self):
        if self._left <= 0:
            if self._on_end:
                self._on_end()
            raise MediaStreamError("ended")
        self._left -= 1
        return f"frame/{self.tag}"

    def stop(self):
        self.stopped = True


class _FakePlayers:
    """Stands in for MediaPlayer, handing out one scripted track per open."""

    def __init__(self, scripts, on_end=None):
        self.scripts = list(scripts)
        self.tracks = []
        self._on_end = on_end

    def __call__(self, device, format=None, options=None):
        if not self.scripts:
            raise RuntimeError(f"{device}: no camera")
        frames = self.scripts.pop(0)
        track = _FakeTrack(frames, f"open{len(self.tracks)}", self._on_end)
        self.tracks.append(track)
        return SimpleNamespace(video=track)

    @property
    def opens(self):
        return len(self.tracks)


class UsbCameraRestartTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.clock = _Clock()
        self.patches = [
            patch.object(ucs, "time", self.clock),
            patch.object(sup, "_REOPEN_BACKOFF", (0.0,) * 5),
            patch.object(UsbCameraSource, "_query_brightness_range",
                         return_value=None),
            patch("os.path.exists", return_value=True),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def _source(self, players):
        with patch.object(ucs, "MediaPlayer", players):
            return UsbCameraSource("/dev/video0", format="v4l2")

    async def test_a_dropped_stream_is_reopened_under_the_subscriber(self):
        players = _FakePlayers([2, 2])
        with patch.object(ucs, "MediaPlayer", players):
            cam = self._source(players)
            frames = [await cam.recv() for _ in range(4)]
        self.assertEqual(players.opens, 2, "should have opened a second player")
        self.assertEqual(frames,
                         ["frame/open0", "frame/open0",
                          "frame/open1", "frame/open1"])

    async def test_the_old_player_is_released_before_reopening(self):
        """Otherwise the reopen competes with our own handle on the device."""
        players = _FakePlayers([1, 1])
        with patch.object(ucs, "MediaPlayer", players):
            cam = self._source(players)
            await cam.recv()
            await cam.recv()
        self.assertTrue(players.tracks[0].stopped)

    async def test_repeated_immediate_failures_give_up(self):
        players = _FakePlayers([0] * 20)
        with patch.object(ucs, "MediaPlayer", players):
            cam = self._source(players)
            with self.assertRaises(MediaStreamError):
                for _ in range(20):
                    await cam.recv()
        self.assertEqual(players.opens, sup._MAX_FAILED_STARTS,
                         "should stop opening once the budget is spent")

    async def test_a_run_that_lasted_resets_the_budget(self):
        # Each stream ends only after an hour of clock, so none of them counts
        # as a failed start and the budget never runs down.
        players = _FakePlayers([1] * 12,
                               on_end=lambda: setattr(self.clock, "t",
                                                      self.clock.t + 3600.0))
        with patch.object(ucs, "MediaPlayer", players):
            cam = self._source(players)
            for _ in range(10):
                await cam.recv()
        self.assertEqual(players.opens, 10,
                         "a camera that drops hourly must keep recovering")

    async def test_stop_does_not_reopen(self):
        players = _FakePlayers([1, 1])
        with patch.object(ucs, "MediaPlayer", players):
            cam = self._source(players)
            await cam.recv()
            cam.stop()
            with self.assertRaises(MediaStreamError):
                await cam.recv()
        self.assertEqual(players.opens, 1)

    async def test_video_is_the_track_itself(self):
        """The relay subscribes once, so the swap has to happen underneath."""
        players = _FakePlayers([1])
        with patch.object(ucs, "MediaPlayer", players):
            cam = self._source(players)
        self.assertIs(cam.video, cam)


if __name__ == "__main__":
    unittest.main()
