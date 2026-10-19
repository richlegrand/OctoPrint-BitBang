"""Reopening a V4L2 capture after the camera drops off.

The policy under test is in V4l2H264Track._capture_loop: a run that lasted
past _RUN_ESTABLISHED is a disconnect and gets retried indefinitely, while
runs that end immediately are a configuration that cannot work and give up
after _MAX_FAILED_STARTS. See issue #6.

The clock is faked so a "ran for an hour" case costs no wall time, and the
backoff and device wait are patched to zero so only the policy is measured.
"""

import unittest
from unittest.mock import patch

from octoprint_bitbang import v4l2_h264_source as src
from octoprint_bitbang.v4l2_h264_source import V4l2H264Track


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


class _Loop:
    """Stands in for the asyncio loop; records what was handed to it."""

    def __init__(self):
        self.calls = []

    def call_soon_threadsafe(self, fn, *args):
        self.calls.append((fn, args))


def _track(framerate=30):
    with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch.object(V4l2H264Track, "_query_brightness_range",
                      return_value=None):
        return V4l2H264Track(device="/dev/video0", source_is_h264=True,
                             input_format="h264", framerate=framerate)


class TestPtsAcrossReopen(unittest.TestCase):
    """pts has to keep rising when a capture is reopened. Restarting at zero
    sends timestamps backwards, which aiortc and the browser object to."""

    def test_first_run_starts_at_zero(self):
        t = _track()
        t._begin_pts_run()
        self.assertEqual(t._pts_for(now=5.0, base=5.0), 0)
        self.assertEqual(t._pts_for(now=5.5, base=5.0), 500_000)

    def test_second_run_continues_the_timeline(self):
        t = _track(framerate=30)
        t._begin_pts_run()
        t._pts_for(now=10.0, base=10.0)
        last = t._pts_for(now=11.0, base=10.0)
        self.assertEqual(last, 1_000_000)

        # The camera drops and comes back; the new run's clock is unrelated.
        t._begin_pts_run()
        first_after = t._pts_for(now=900.0, base=900.0)
        self.assertEqual(first_after, last + t._frame_us)
        self.assertGreater(first_after, last)

    def test_pts_never_goes_backwards_over_many_reopens(self):
        t = _track()
        seen = []
        for run in range(6):
            t._begin_pts_run()
            origin = 100.0 * (run + 1)        # each run's clock starts anywhere
            for frame in range(4):
                seen.append(t._pts_for(now=origin + frame * 0.033, base=origin))
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(len(seen), len(set(seen)))


class TestReopenPolicy(unittest.TestCase):
    """When to reopen, and when to stop."""

    def setUp(self):
        self.clock = _Clock()
        self.patches = [
            patch.object(src, "time", self.clock),
            patch.object(src, "_REOPEN_BACKOFF", (0.0,) * 5),
            patch.object(src, "_DEVICE_WAIT", 0.0),
            patch("os.path.exists", return_value=True),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def _run(self, track, durations):
        """Drive _capture_loop, giving each run the next duration in turn.
        Stops the track once the durations run out."""
        track._loop = _Loop()
        track._close_proc = lambda: None
        track._log_failure = lambda reason, ran: None
        it = iter(durations)
        attempts = []

        def run_once():
            try:
                d = next(it)
            except StopIteration:
                track._stop.set()
                return None
            attempts.append(d)
            self.clock.t += d
            return "ffmpeg exited (rc=0)"

        track._run_once = run_once
        track._capture_loop()
        return attempts, track._loop.calls

    def test_a_long_run_that_dies_is_retried(self):
        t = _track()
        attempts, calls = self._run(t, [3600.0, 3600.0, 3600.0])
        self.assertEqual(len(attempts), 3, "each disconnect should reopen")
        self.assertEqual(calls, [], "a disconnect must not fail the track")

    def test_repeated_failed_starts_give_up(self):
        t = _track()
        # Every run dies immediately: a device that is gone, or an encoder this
        # machine does not have.
        attempts, calls = self._run(t, [0.2] * 20)
        self.assertEqual(len(attempts), src._MAX_FAILED_STARTS)
        self.assertEqual(len(calls), 1, "should fail the track exactly once")
        self.assertEqual(calls[0][0].__func__, V4l2H264Track._fail)
        self.assertIsInstance(calls[0][1][0], RuntimeError)

    def test_a_long_run_resets_the_failure_count(self):
        t = _track()
        # Four quick failures, then one that holds, repeated. A camera that
        # drops daily must never run out of attempts.
        pattern = ([0.2] * 4 + [3600.0]) * 4
        attempts, calls = self._run(t, pattern)
        self.assertEqual(len(attempts), len(pattern))
        self.assertEqual(calls, [])

    def test_a_deliberate_stop_does_not_reopen(self):
        t = _track()
        t._loop = _Loop()
        t._close_proc = lambda: None
        seen = []

        def run_once():
            seen.append(1)
            return None            # None means stop() asked us to end

        t._run_once = run_once
        t._capture_loop()
        self.assertEqual(len(seen), 1)
        self.assertEqual(t._loop.calls, [])


class TestWaitForDevice(unittest.TestCase):

    def test_returns_as_soon_as_the_node_is_back(self):
        t = _track()
        with patch("os.path.exists", return_value=True):
            self.assertTrue(t._wait_for_device(timeout=0.0))

    def test_gives_up_when_the_node_stays_away(self):
        t = _track()
        with patch("os.path.exists", return_value=False):
            self.assertFalse(t._wait_for_device(timeout=0.0))

    def test_a_stopped_track_stops_waiting(self):
        t = _track()
        t._stop.set()
        with patch("os.path.exists", return_value=False):
            self.assertFalse(t._wait_for_device(timeout=10.0))


if __name__ == "__main__":
    unittest.main()
