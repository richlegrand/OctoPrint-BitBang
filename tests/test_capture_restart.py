"""Reopening a V4L2 capture after the camera drops off.

The policy under test is in V4l2H264Track._capture_loop: a run that lasted
past _RUN_ESTABLISHED is a disconnect and gets retried indefinitely, while
runs that end immediately are a configuration that cannot work and give up
after _MAX_FAILED_STARTS. See issue #6.

The clock is faked so a "ran for an hour" case costs no wall time, and the
backoff and device wait are patched to zero so only the policy is measured.
"""

import io
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


class _Recorder:
    """Collects log lines so a test can assert on what was actually said."""

    def __init__(self):
        self.lines = []

    def _add(self, msg):
        self.lines.append(msg)

    warning = info = error = debug = _add

    def text(self):
        return "\n".join(self.lines)


class TestContentionReporting(unittest.TestCase):
    """Who has the camera, when our own scan cannot see them.

    /proc/<pid>/fd is readable only for our own processes, so a streamer
    running as root -- OctoPi's webcamd, the usual culprit -- never shows up
    in device_holders. An empty result there is not "nobody has it"."""

    def test_names_a_running_streamer_when_our_scan_is_blind(self):
        t = _track()
        t._logger = rec = _Recorder()
        with patch.object(src, "device_holders", return_value=[]), \
             patch.object(src, "running_streamers",
                          return_value=["pid 619 (mjpg_streamer)"]):
            t._log_contention()
        self.assertIn("mjpg_streamer", rec.text())
        self.assertIn("systemctl disable", rec.text())

    def test_prefers_a_holder_we_can_actually_see(self):
        t = _track()
        t._logger = rec = _Recorder()
        with patch.object(src, "device_holders",
                          return_value=["pid 42 (python3)"]), \
             patch.object(src, "running_streamers",
                          return_value=["pid 619 (mjpg_streamer)"]) as streamers:
            t._log_contention()
        self.assertIn("pid 42 (python3)", rec.text())
        streamers.assert_not_called()

    def test_stays_quiet_when_there_is_nothing_to_report(self):
        t = _track()
        t._logger = rec = _Recorder()
        with patch.object(src, "device_holders", return_value=[]), \
             patch.object(src, "running_streamers", return_value=[]):
            t._log_contention()
        self.assertEqual(rec.lines, [])

    def test_running_streamers_reads_comm_not_fd(self):
        """comm is world-readable where fd is not, which is the whole point."""
        def fake_open(path, *a, **k):
            if path == "/proc/619/comm":
                return io.StringIO("mjpg_streamer\n")
            if path == "/proc/1/comm":
                return io.StringIO("systemd\n")
            raise OSError("not ours")

        with patch.object(src.os, "listdir",
                          return_value=["1", "619", "self", "cpuinfo"]), \
             patch("builtins.open", side_effect=fake_open):
            self.assertEqual(src.running_streamers(),
                             ["pid 619 (mjpg_streamer)"])


class TestRepeatFailureLogging(unittest.TestCase):
    """A repeat failure keeps its ffmpeg output when the output is new.

    The first failure of an episode is often the least informative: a killed
    process prints nothing, and it is the reopen that says "Device or resource
    busy". Suppressing every repeat throws that away."""

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

    def _run(self, tails):
        """Drive _capture_loop, giving each attempt the next stderr tail.
        Every run dies immediately, so the loop walks its failure budget."""
        t = _track()
        t._logger = rec = _Recorder()
        t._loop = _Loop()
        t._close_proc = lambda: None
        it = iter(tails)

        def run_once():
            try:
                tail = next(it)
            except StopIteration:
                t._stop.set()
                return None
            t._stderr_tail.clear()
            t._stderr_tail.extend(tail)
            self.clock.t += 0.2
            return "ffmpeg exited (rc=1)"

        t._run_once = run_once
        with patch.object(src, "device_holders", return_value=[]), \
             patch.object(src, "running_streamers", return_value=[]):
            t._capture_loop()
        return rec

    def test_a_new_error_on_a_repeat_is_printed(self):
        # Killed first (says nothing), then the reopen reports the real cause.
        rec = self._run([[], ["Device or resource busy"], []])
        self.assertIn("Device or resource busy", rec.text())

    def test_an_identical_error_is_not_reprinted(self):
        busy = ["Device or resource busy"]
        rec = self._run([busy, busy, busy, busy, busy])
        printed = [l for l in rec.lines if "ffmpeg: Device or resource busy" in l]
        self.assertEqual(len(printed), 1,
                         f"printed {len(printed)} times:\n{rec.text()}")

    def test_each_distinct_error_is_printed_once(self):
        rec = self._run([["first problem"], ["first problem"],
                         ["second problem"], ["second problem"]])
        out = rec.text()
        self.assertEqual(out.count("ffmpeg: first problem"), 1, out)
        self.assertEqual(out.count("ffmpeg: second problem"), 1, out)


if __name__ == "__main__":
    unittest.main()
