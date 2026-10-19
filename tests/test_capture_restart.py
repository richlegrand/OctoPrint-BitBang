"""Reopening a V4L2 capture after the camera drops off. See issue #6."""

import unittest
from unittest.mock import patch

from octoprint_bitbang.v4l2_h264_source import V4l2H264Track


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


if __name__ == "__main__":
    unittest.main()
