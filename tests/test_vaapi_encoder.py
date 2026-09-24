import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Provide mock stubs if dependencies (e.g. PyAV, aiortc) are not installed in the current environment
for mod in [
    "av", "aiortc", "aiortc.contrib", "aiortc.contrib.media", "aiortc.rtcrtpsender",
    "bitbang", "bitbang.proxy",
]:
    if mod not in sys.modules:
        try:
            __import__(mod)
        except ImportError:
            mock = MagicMock()
            if mod == "aiortc":
                mock.MediaStreamTrack = type("MediaStreamTrack", (), {
                    "__init__": lambda self, *args, **kwargs: None,
                    "stop": lambda self: None,
                })
            elif mod == "bitbang":
                mock.BitBangASGI = type("BitBangASGI", (), {
                    "__init__": lambda self, *args, **kwargs: None,
                })
            sys.modules[mod] = mock

from octoprint_bitbang.v4l2_h264_source import (
    V4l2H264Track,
    find_vaapi_device,
    has_vaapi_h264_encoder,
    probe_vaapi_h264,
    reencode_input_format,
    device_supports_h264,
)


class TestVaapiEncoder(unittest.TestCase):

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("subprocess.run")
    @patch("os.path.exists", return_value=True)
    def test_has_vaapi_h264_encoder_true_when_renderD128_present(self, mock_exists, mock_run, mock_which):
        mock_run.return_value = MagicMock(stdout=" V..... h264_vaapi           H.264/AVC (VAAPI) (encoders: h264_vaapi )")
        self.assertTrue(has_vaapi_h264_encoder("/dev/dri/renderD128"))
        mock_exists.assert_called_with("/dev/dri/renderD128")

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("subprocess.run")
    @patch("os.path.exists", return_value=False)
    @patch("glob.glob", return_value=[])
    def test_has_vaapi_h264_encoder_false_when_renderD128_missing(self, mock_glob, mock_exists, mock_run, mock_which):
        mock_run.return_value = MagicMock(stdout=" V..... h264_vaapi           H.264/AVC (VAAPI) (encoders: h264_vaapi )")
        self.assertFalse(has_vaapi_h264_encoder("/dev/dri/renderD128"))

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("subprocess.run")
    def test_has_vaapi_h264_encoder_false_when_no_codec(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(stdout=" V..... libx264              libx264 H.264")
        self.assertFalse(has_vaapi_h264_encoder())

    @patch("shutil.which", return_value=None)
    def test_has_vaapi_h264_encoder_no_ffmpeg(self, mock_which):
        self.assertFalse(has_vaapi_h264_encoder())

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("subprocess.run")
    def test_probe_vaapi_h264_success(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(probe_vaapi_h264("/dev/dri/renderD128"))
        args, kwargs = mock_run.call_args
        cmd = args[0]
        self.assertIn("-vaapi_device", cmd)
        self.assertIn("/dev/dri/renderD128", cmd)
        self.assertIn("h264_vaapi", cmd)

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("subprocess.run")
    def test_probe_vaapi_h264_failure(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(returncode=1)
        self.assertFalse(probe_vaapi_h264("/dev/dri/renderD128"))

    @patch("octoprint_bitbang.v4l2_h264_source.has_vaapi_h264_encoder", return_value=True)
    @patch("os.path.exists", return_value=True)
    def test_find_vaapi_device_explicit(self, mock_exists, mock_has):
        dev = find_vaapi_device("/dev/dri/renderD129")
        self.assertEqual(dev, "/dev/dri/renderD129")

    @patch("octoprint_bitbang.v4l2_h264_source.has_vaapi_h264_encoder", return_value=True)
    @patch("os.path.exists", side_effect=lambda p: p == "/dev/dri/renderD128")
    def test_find_vaapi_device_autodetect_renderD128(self, mock_exists, mock_has):
        dev = find_vaapi_device()
        self.assertEqual(dev, "/dev/dri/renderD128")

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    def test_v4l2_h264_track_cmd_vaapi_upload(self, mock_which):
        track = V4l2H264Track(
            device="/dev/video0",
            source_is_h264=False,
            encoder="vaapi",
            vaapi_device="/dev/dri/renderD128",
            input_format="mjpeg",
            video_size="1920x1080",
            framerate=30,
            bitrate=2_000_000,
            gop=30,
            flip_horizontal=True,
            flip_vertical=False,
        )
        cmd = track._ffmpeg_cmd()
        self.assertIn("-vaapi_device", cmd)
        idx = cmd.index("-vaapi_device")
        self.assertEqual(cmd[idx + 1], "/dev/dri/renderD128")
        self.assertIn("-c:v", cmd)
        idx_c = cmd.index("-c:v")
        self.assertEqual(cmd[idx_c + 1], "h264_vaapi")
        self.assertIn("-b:v", cmd)
        self.assertIn("2000000", cmd)
        self.assertIn("-bf", cmd)
        idx_bf = cmd.index("-bf")
        self.assertEqual(cmd[idx_bf + 1], "0")
        self.assertIn("-vf", cmd)
        idx_vf = cmd.index("-vf")
        self.assertEqual(cmd[idx_vf + 1], "hflip,format=nv12,hwupload")
        self.assertIn("-bsf:v", cmd)
        idx_bsf = cmd.index("-bsf:v")
        self.assertIn("dump_extra=freq=keyframe", cmd[idx_bsf + 1])

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    def test_v4l2_h264_track_cmd_vaapi_hwaccel_decode(self, mock_which):
        track = V4l2H264Track(
            device="/dev/video0",
            source_is_h264=False,
            encoder="vaapi",
            vaapi_device="/dev/dri/renderD128",
            hwaccel_decode=True,
            input_format="mjpeg",
            video_size="1920x1080",
            framerate=30,
            bitrate=4_000_000,
            gop=30,
            flip_horizontal=False,
            flip_vertical=False,
        )
        cmd = track._ffmpeg_cmd()
        self.assertIn("-hwaccel", cmd)
        idx_hw = cmd.index("-hwaccel")
        self.assertEqual(cmd[idx_hw + 1], "vaapi")
        self.assertIn("-hwaccel_device", cmd)
        idx_dev = cmd.index("-hwaccel_device")
        self.assertEqual(cmd[idx_dev + 1], "/dev/dri/renderD128")
        self.assertIn("-hwaccel_output_format", cmd)
        idx_out = cmd.index("-hwaccel_output_format")
        self.assertEqual(cmd[idx_out + 1], "vaapi")
        # Direct vaapi encode; no hwupload filter needed
        self.assertNotIn("-vf", cmd)
        self.assertIn("-c:v", cmd)
        idx_c = cmd.index("-c:v")
        self.assertEqual(cmd[idx_c + 1], "h264_vaapi")

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    def test_v4l2_h264_track_cmd_vaapi_hwaccel_decode_with_flip_falls_back_to_upload(self, mock_which):
        # When orientation flip is requested, software filters are required so
        # hwaccel_output_format=vaapi cannot be used; it must use format=nv12,hwupload
        track = V4l2H264Track(
            device="/dev/video0",
            source_is_h264=False,
            encoder="vaapi",
            vaapi_device="/dev/dri/renderD128",
            hwaccel_decode=True,
            input_format="mjpeg",
            video_size="1280x720",
            framerate=30,
            flip_horizontal=True,
        )
        cmd = track._ffmpeg_cmd()
        self.assertNotIn("-hwaccel", cmd)
        self.assertIn("-vaapi_device", cmd)
        self.assertIn("-vf", cmd)
        idx_vf = cmd.index("-vf")
        self.assertEqual(cmd[idx_vf + 1], "hflip,format=nv12,hwupload")

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    def test_v4l2_h264_track_cmd_v4l2m2m(self, mock_which):
        track = V4l2H264Track(
            device="/dev/video0",
            source_is_h264=False,
            encoder="v4l2m2m",
            input_format="mjpeg",
            video_size="1280x720",
        )
        cmd = track._ffmpeg_cmd()
        self.assertNotIn("-vaapi_device", cmd)
        self.assertIn("-c:v", cmd)
        idx = cmd.index("-c:v")
        self.assertEqual(cmd[idx + 1], "h264_v4l2m2m")
        self.assertIn("-vf", cmd)
        idx_vf = cmd.index("-vf")
        self.assertEqual(cmd[idx_vf + 1], "format=yuv420p")

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    def test_v4l2_h264_track_cmd_copy(self, mock_which):
        track = V4l2H264Track(
            device="/dev/video0",
            source_is_h264=True,
            encoder="copy",
            input_format="h264",
        )
        cmd = track._ffmpeg_cmd()
        self.assertNotIn("-vaapi_device", cmd)
        self.assertIn("-c", cmd)
        idx = cmd.index("-c")
        self.assertEqual(cmd[idx + 1], "copy")


class TestOctoPrintAdapterVaapiLadder(unittest.TestCase):

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("octoprint_bitbang.v4l2_h264_source.reencode_input_format", return_value="mjpeg")
    @patch("octoprint_bitbang.v4l2_h264_source.has_v4l2m2m_h264_encoder", return_value=False)
    @patch("octoprint_bitbang.v4l2_h264_source.device_supports_h264", return_value=False)
    @patch("octoprint_bitbang.v4l2_h264_source.find_vaapi_device", return_value="/dev/dri/renderD128")
    def test_adapter_picks_vaapi_when_available(self, mock_vaapi_dev, mock_h264, mock_m2m, mock_fmt, mock_which):
        from octoprint_bitbang.octoprint_adapter import OctoPrintBitBang

        async def _app(scope, receive, send):
            pass

        source = {
            "type": "usb",
            "device": "/dev/video0",
            "format": "v4l2",
            "encoder": "auto",
        }
        adapter = OctoPrintBitBang(_app, camera_source=source, ephemeral=True)
        self.assertIsNotNone(adapter.player)
        self.assertIsInstance(adapter.player, V4l2H264Track)
        self.assertEqual(adapter.player._encoder, "vaapi")
        self.assertEqual(adapter.player._vaapi_device, "/dev/dri/renderD128")

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("octoprint_bitbang.v4l2_h264_source.reencode_input_format", return_value="mjpeg")
    @patch("octoprint_bitbang.v4l2_h264_source.has_v4l2m2m_h264_encoder", return_value=False)
    @patch("octoprint_bitbang.v4l2_h264_source.device_supports_h264", return_value=False)
    @patch("octoprint_bitbang.v4l2_h264_source.find_vaapi_device", return_value="/dev/dri/renderD128")
    def test_adapter_picks_vaapi_hwaccel_decode(self, mock_vaapi_dev, mock_h264, mock_m2m, mock_fmt, mock_which):
        from octoprint_bitbang.octoprint_adapter import OctoPrintBitBang

        async def _app(scope, receive, send):
            pass

        source = {
            "type": "usb",
            "device": "/dev/video0",
            "format": "v4l2",
            "encoder": "vaapi",
            "vaapi_hwaccel_decode": True,
        }
        adapter = OctoPrintBitBang(_app, camera_source=source, ephemeral=True)
        self.assertIsNotNone(adapter.player)
        self.assertIsInstance(adapter.player, V4l2H264Track)
        self.assertTrue(adapter.player._hwaccel_decode)

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("octoprint_bitbang.v4l2_h264_source.reencode_input_format", return_value="mjpeg")
    @patch("octoprint_bitbang.v4l2_h264_source.has_v4l2m2m_h264_encoder", return_value=False)
    @patch("octoprint_bitbang.v4l2_h264_source.device_supports_h264", return_value=False)
    @patch("octoprint_bitbang.v4l2_h264_source.find_vaapi_device", return_value=None)
    def test_adapter_falls_back_to_software_when_no_hw_encoder(self, mock_vaapi_dev, mock_h264, mock_m2m, mock_fmt, mock_which):
        from octoprint_bitbang.octoprint_adapter import OctoPrintBitBang
        from octoprint_bitbang.usb_camera_source import UsbCameraSource

        async def _app(scope, receive, send):
            pass

        source = {
            "type": "usb",
            "device": "/dev/video0",
            "format": "v4l2",
            "encoder": "auto",
        }
        adapter = OctoPrintBitBang(_app, camera_source=source, ephemeral=True)
        self.assertIsNotNone(adapter.player)
        self.assertIsInstance(adapter.player, UsbCameraSource)

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    def test_adapter_software_pref_skips_hardware(self, mock_which):
        from octoprint_bitbang.octoprint_adapter import OctoPrintBitBang
        from octoprint_bitbang.usb_camera_source import UsbCameraSource

        async def _app(scope, receive, send):
            pass

        source = {
            "type": "usb",
            "device": "/dev/video0",
            "format": "v4l2",
            "encoder": "software",
        }
        adapter = OctoPrintBitBang(_app, camera_source=source, ephemeral=True)
        self.assertIsNotNone(adapter.player)
        self.assertIsInstance(adapter.player, UsbCameraSource)

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("octoprint_bitbang.v4l2_h264_source.reencode_input_format", return_value="mjpeg")
    @patch("octoprint_bitbang.v4l2_h264_source.has_v4l2m2m_h264_encoder", return_value=False)
    @patch("octoprint_bitbang.v4l2_h264_source.find_vaapi_device", return_value="/dev/dri/renderD129")
    def test_adapter_explicit_vaapi_pref(self, mock_vaapi_dev, mock_m2m, mock_fmt, mock_which):
        from octoprint_bitbang.octoprint_adapter import OctoPrintBitBang

        async def _app(scope, receive, send):
            pass

        source = {
            "type": "usb",
            "device": "/dev/video0",
            "format": "v4l2",
            "encoder": "vaapi",
            "vaapi_device": "/dev/dri/renderD129",
        }
        adapter = OctoPrintBitBang(_app, camera_source=source, ephemeral=True)
        self.assertIsNotNone(adapter.player)
        self.assertIsInstance(adapter.player, V4l2H264Track)
        self.assertEqual(adapter.player._encoder, "vaapi")
        self.assertEqual(adapter.player._vaapi_device, "/dev/dri/renderD129")


if __name__ == "__main__":
    unittest.main()
