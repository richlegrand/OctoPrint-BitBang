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
    VaapiProbeSettings,
    find_vaapi_device,
    has_vaapi_h264_encoder,
    probe_vaapi_h264_encode,
    probe_vaapi_hwdecode_vpp_filters,
    reencode_input_format,
    device_supports_h264,
)


class TestVaapiProbeSettings(unittest.TestCase):

    def test_default_settings(self):
        s = VaapiProbeSettings()
        self.assertEqual(s.video_size, "1280x720")
        self.assertFalse(s.flip_horizontal)
        self.assertFalse(s.flip_vertical)

    def test_custom_settings(self):
        s = VaapiProbeSettings("1920x1080", flip_horizontal=True, flip_vertical=False)
        self.assertEqual(s.video_size, "1920x1080")
        self.assertTrue(s.flip_horizontal)
        self.assertFalse(s.flip_vertical)


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
        self.assertFalse(has_vaapi_h264_encoder())

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("subprocess.run")
    @patch("os.path.exists", return_value=False)
    @patch("glob.glob", return_value=["/dev/dri/renderD128"])
    def test_has_vaapi_h264_encoder_false_for_missing_specific_node(self, mock_glob, mock_exists, mock_run, mock_which):
        mock_run.return_value = MagicMock(stdout=" V..... h264_vaapi           H.264/AVC (VAAPI) (encoders: h264_vaapi )")
        self.assertFalse(has_vaapi_h264_encoder("/dev/dri/renderD129"))

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
    def test_probe_vaapi_h264_encode_success(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(probe_vaapi_h264_encode("/dev/dri/renderD128", settings=VaapiProbeSettings()))
        args, kwargs = mock_run.call_args
        cmd = args[0]
        self.assertIn("-vaapi_device", cmd)
        self.assertIn("/dev/dri/renderD128", cmd)
        self.assertIn("h264_vaapi", cmd)

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("subprocess.run")
    def test_probe_vaapi_h264_encode_failure(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(returncode=1)
        self.assertFalse(probe_vaapi_h264_encode("/dev/dri/renderD128", settings=VaapiProbeSettings()))

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("subprocess.run")
    def test_probe_vaapi_h264_encode_uses_video_size(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(returncode=0)
        settings = VaapiProbeSettings(video_size="1920x1080")
        self.assertTrue(probe_vaapi_h264_encode("/dev/dri/renderD128", settings=settings))
        args, _ = mock_run.call_args
        self.assertIn("testsrc=s=1920x1080:r=1", args[0])

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("subprocess.run")
    def test_probe_vaapi_h264_encode_uses_settings_object(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(returncode=0)
        settings = VaapiProbeSettings("1920x1080", flip_horizontal=True, flip_vertical=False)
        self.assertTrue(probe_vaapi_h264_encode("/dev/dri/renderD128", settings=settings))
        args, _ = mock_run.call_args
        cmd = args[0]
        self.assertIn("testsrc=s=1920x1080:r=1", cmd)
        idx_vf = cmd.index("-vf")
        self.assertEqual(cmd[idx_vf + 1], "format=nv12,hwupload")

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("subprocess.run")
    def test_probe_vaapi_hwaccel_decode_default(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(probe_vaapi_hwdecode_vpp_filters("/dev/dri/renderD128", settings=VaapiProbeSettings()))
        args, _ = mock_run.call_args
        cmd = args[0]
        self.assertIn("-vaapi_device", cmd)
        self.assertIn("/dev/dri/renderD128", cmd)
        self.assertIn("-vf", cmd)
        idx_vf = cmd.index("-vf")
        self.assertEqual(cmd[idx_vf + 1], "format=nv12,hwupload,scale_vaapi=format=nv12")
        self.assertIn("h264_vaapi", cmd)

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("subprocess.run")
    def test_probe_vaapi_hwaccel_decode_with_flips(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(returncode=0)
        settings = VaapiProbeSettings("1920x1080", flip_horizontal=True, flip_vertical=True)
        self.assertTrue(probe_vaapi_hwdecode_vpp_filters("/dev/dri/renderD128", settings=settings))
        args, _ = mock_run.call_args
        cmd = args[0]
        self.assertIn("testsrc=s=1920x1080:r=1", cmd)
        idx_vf = cmd.index("-vf")
        self.assertEqual(
            cmd[idx_vf + 1],
            "format=nv12,hwupload,scale_vaapi=format=nv12,transpose_vaapi=dir=hflip,transpose_vaapi=dir=vflip"
        )

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("subprocess.run")
    def test_probe_vaapi_hwaccel_decode_failure(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(returncode=1)
        self.assertFalse(probe_vaapi_hwdecode_vpp_filters("/dev/dri/renderD128", settings=VaapiProbeSettings()))

    @patch("shutil.which", return_value=None)
    def test_probe_vaapi_hwaccel_decode_no_ffmpeg(self, mock_which):
        self.assertFalse(probe_vaapi_hwdecode_vpp_filters("/dev/dri/renderD128", settings=VaapiProbeSettings()))

    @patch("octoprint_bitbang.v4l2_h264_source.probe_vaapi_h264_encode", return_value=True)
    @patch("octoprint_bitbang.v4l2_h264_source.has_vaapi_h264_encoder", return_value=True)
    def test_find_vaapi_device_explicit(self, mock_has, mock_probe):
        settings = VaapiProbeSettings()
        dev = find_vaapi_device("/dev/dri/renderD129", settings=settings)
        self.assertEqual(dev, "/dev/dri/renderD129")
        mock_probe.assert_called_once_with("/dev/dri/renderD129", settings=settings)

    @patch("octoprint_bitbang.v4l2_h264_source.probe_vaapi_h264_encode", return_value=True)
    @patch("octoprint_bitbang.v4l2_h264_source.has_vaapi_h264_encoder", return_value=True)
    def test_find_vaapi_device_with_settings_object(self, mock_has, mock_probe):
        settings = VaapiProbeSettings("1920x1080", flip_horizontal=True)
        dev = find_vaapi_device("/dev/dri/renderD129", settings=settings)
        self.assertEqual(dev, "/dev/dri/renderD129")
        mock_probe.assert_called_once_with("/dev/dri/renderD129", settings=settings)

    @patch("octoprint_bitbang.v4l2_h264_source.probe_vaapi_h264_encode", return_value=False)
    @patch("octoprint_bitbang.v4l2_h264_source.has_vaapi_h264_encoder", return_value=True)
    def test_find_vaapi_device_explicit_fails_probe(self, mock_has, mock_probe):
        self.assertIsNone(find_vaapi_device("/dev/dri/renderD129", settings=VaapiProbeSettings()))

    @patch("octoprint_bitbang.v4l2_h264_source.probe_vaapi_h264_encode", return_value=True)
    @patch("octoprint_bitbang.v4l2_h264_source.has_vaapi_h264_encoder", return_value=True)
    @patch("os.path.exists", side_effect=lambda p: p == "/dev/dri/renderD128")
    def test_find_vaapi_device_autodetect_renderD128(self, mock_exists, mock_has, mock_probe):
        settings = VaapiProbeSettings()
        dev = find_vaapi_device(None, settings=settings)
        self.assertEqual(dev, "/dev/dri/renderD128")
        mock_probe.assert_called_once_with("/dev/dri/renderD128", settings=settings)

    @patch("octoprint_bitbang.v4l2_h264_source.probe_vaapi_h264_encode",
           side_effect=lambda p, **kw: p == "/dev/dri/renderD129")
    @patch("octoprint_bitbang.v4l2_h264_source.has_vaapi_h264_encoder", return_value=True)
    @patch("glob.glob", return_value=["/dev/dri/renderD128", "/dev/dri/renderD129"])
    @patch("os.path.exists", return_value=True)
    def test_find_vaapi_device_skips_non_encoding_node(self, mock_exists, mock_glob, mock_has, mock_probe):
        dev = find_vaapi_device(None, settings=VaapiProbeSettings())
        self.assertEqual(dev, "/dev/dri/renderD129")

    @patch("octoprint_bitbang.v4l2_h264_source.probe_vaapi_h264_encode", return_value=False)
    @patch("octoprint_bitbang.v4l2_h264_source.has_vaapi_h264_encoder", return_value=True)
    @patch("glob.glob", return_value=["/dev/dri/renderD128", "/dev/dri/renderD129"])
    @patch("os.path.exists", return_value=True)
    def test_find_vaapi_device_none_when_no_node_can_encode(self, mock_exists, mock_glob, mock_has, mock_probe):
        self.assertIsNone(find_vaapi_device(None, settings=VaapiProbeSettings()))

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
        self.assertIn("-nostdin", cmd)
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
        self.assertIn("-nostdin", cmd)
        self.assertIn("-hwaccel", cmd)
        idx_hw = cmd.index("-hwaccel")
        self.assertEqual(cmd[idx_hw + 1], "vaapi")
        self.assertIn("-hwaccel_device", cmd)
        idx_dev = cmd.index("-hwaccel_device")
        self.assertEqual(cmd[idx_dev + 1], "/dev/dri/renderD128")
        self.assertIn("-hwaccel_output_format", cmd)
        idx_out = cmd.index("-hwaccel_output_format")
        self.assertEqual(cmd[idx_out + 1], "vaapi")
        self.assertIn("-vaapi_device", cmd)
        idx_vaapi = cmd.index("-vaapi_device")
        self.assertEqual(cmd[idx_vaapi + 1], "/dev/dri/renderD128")
        # Direct vaapi encode; scale_vaapi converts to nv12 without hwupload
        self.assertIn("-vf", cmd)
        idx_vf = cmd.index("-vf")
        self.assertEqual(cmd[idx_vf + 1], "scale_vaapi=format=nv12")
        self.assertIn("-c:v", cmd)
        idx_c = cmd.index("-c:v")
        self.assertEqual(cmd[idx_c + 1], "h264_vaapi")

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    def test_v4l2_h264_track_cmd_vaapi_hwaccel_decode_with_flip(self, mock_which):
        # When orientation flip is requested with hwaccel decode, hardware
        # transpose_vaapi filters are chained onto the VAAPI filter graph.
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
            flip_vertical=False,
        )
        cmd = track._ffmpeg_cmd()
        self.assertIn("-nostdin", cmd)
        self.assertIn("-hwaccel", cmd)
        self.assertEqual(cmd[cmd.index("-hwaccel") + 1], "vaapi")
        self.assertIn("-hwaccel_device", cmd)
        self.assertEqual(cmd[cmd.index("-hwaccel_device") + 1], "/dev/dri/renderD128")
        self.assertIn("-hwaccel_output_format", cmd)
        self.assertEqual(cmd[cmd.index("-hwaccel_output_format") + 1], "vaapi")
        self.assertIn("-vaapi_device", cmd)
        self.assertEqual(cmd[cmd.index("-vaapi_device") + 1], "/dev/dri/renderD128")
        self.assertIn("-vf", cmd)
        idx_vf = cmd.index("-vf")
        self.assertEqual(
            cmd[idx_vf + 1],
            "scale_vaapi=format=nv12,transpose_vaapi=dir=hflip",
        )

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    def test_v4l2_h264_track_cmd_vaapi_hwaccel_decode_with_both_flips(self, mock_which):
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
            flip_vertical=True,
        )
        cmd = track._ffmpeg_cmd()
        self.assertIn("-nostdin", cmd)
        self.assertIn("-hwaccel", cmd)
        self.assertIn("-vf", cmd)
        idx_vf = cmd.index("-vf")
        self.assertEqual(
            cmd[idx_vf + 1],
            "scale_vaapi=format=nv12,transpose_vaapi=dir=hflip,transpose_vaapi=dir=vflip",
        )

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    def test_v4l2_h264_track_cmd_libx264(self, mock_which):
        """Software encode goes through ffmpeg, so it gets the supervisor.

        ultrafast/zerolatency is not a nicety: the default preset cannot hold
        frame rate on a Pi."""
        track = V4l2H264Track(
            device="/dev/video0",
            source_is_h264=False,
            encoder="libx264",
            input_format="mjpeg",
            video_size="640x480",
        )
        cmd = track._ffmpeg_cmd()
        self.assertIn("-nostdin", cmd)
        idx = cmd.index("-c:v")
        self.assertEqual(cmd[idx + 1], "libx264")
        self.assertEqual(cmd[cmd.index("-preset") + 1], "ultrafast")
        self.assertEqual(cmd[cmd.index("-tune") + 1], "zerolatency")
        self.assertEqual(cmd[cmd.index("-profile:v") + 1], "baseline")
        self.assertEqual(cmd[cmd.index("-vf") + 1], "format=yuv420p")
        # Re-encoders need the parameter sets re-inserted or a browser that
        # joins mid-stream renders black.
        self.assertIn("dump_extra=freq=keyframe", " ".join(cmd))

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    def test_v4l2_h264_track_cmd_without_an_input_format(self, mock_which):
        """A device advertising neither MJPEG nor YUYV names no format, which
        is the software rung's case: let ffmpeg pick rather than pass None."""
        track = V4l2H264Track(
            device="/dev/video0",
            source_is_h264=False,
            encoder="libx264",
            input_format=None,
        )
        cmd = track._ffmpeg_cmd()
        self.assertIn("-nostdin", cmd)
        self.assertNotIn("-input_format", cmd)
        self.assertNotIn(None, cmd)
        self.assertEqual(cmd[cmd.index("-i") + 1], "/dev/video0")

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
        self.assertIn("-nostdin", cmd)
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
        self.assertIn("-nostdin", cmd)
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
    @patch("octoprint_bitbang.v4l2_h264_source.probe_vaapi_hwdecode_vpp_filters", return_value=True)
    def test_adapter_picks_vaapi_hwaccel_decode(self, mock_probe, mock_vaapi_dev, mock_h264, mock_m2m, mock_fmt, mock_which):
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
        mock_probe.assert_called_once_with("/dev/dri/renderD128", settings=VaapiProbeSettings())

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("octoprint_bitbang.v4l2_h264_source.reencode_input_format", return_value="mjpeg")
    @patch("octoprint_bitbang.v4l2_h264_source.has_v4l2m2m_h264_encoder", return_value=False)
    @patch("octoprint_bitbang.v4l2_h264_source.device_supports_h264", return_value=False)
    @patch("octoprint_bitbang.v4l2_h264_source.find_vaapi_device", return_value="/dev/dri/renderD128")
    @patch("octoprint_bitbang.v4l2_h264_source.probe_vaapi_hwdecode_vpp_filters", return_value=False)
    def test_adapter_hwaccel_decode_disabled_when_vpp_unsupported(self, mock_probe, mock_vaapi_dev, mock_h264, mock_m2m, mock_fmt, mock_which):
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
        # Treated as unavailable: stays on rung 4 (VAAPI encode), falls back to software decode
        self.assertEqual(adapter.player._encoder, "vaapi")
        self.assertFalse(adapter.player._hwaccel_decode)
        mock_probe.assert_called_once_with("/dev/dri/renderD128", settings=VaapiProbeSettings())

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("octoprint_bitbang.v4l2_h264_source.reencode_input_format", return_value="mjpeg")
    @patch("octoprint_bitbang.v4l2_h264_source.has_v4l2m2m_h264_encoder", return_value=False)
    @patch("octoprint_bitbang.v4l2_h264_source.device_supports_h264", return_value=False)
    @patch("octoprint_bitbang.v4l2_h264_source.find_vaapi_device", return_value=None)
    @patch.object(V4l2H264Track, "_query_brightness_range", return_value=None)
    def test_adapter_falls_back_to_software_when_no_hw_encoder(self, mock_bright, mock_vaapi_dev, mock_h264, mock_m2m, mock_fmt, mock_which):
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
        # Software encode runs in an ffmpeg child now, not in process, so it
        # gets the same supervisor as the hardware rungs.
        self.assertIsInstance(adapter.player, V4l2H264Track)
        self.assertEqual(adapter.player._encoder, "libx264")

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("octoprint_bitbang.v4l2_h264_source.reencode_input_format", return_value="mjpeg")
    @patch.object(V4l2H264Track, "_query_brightness_range", return_value=None)
    def test_adapter_software_pref_skips_hardware(self, mock_bright, mock_fmt, mock_which):
        from octoprint_bitbang.octoprint_adapter import OctoPrintBitBang

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
        self.assertIsInstance(adapter.player, V4l2H264Track)
        self.assertEqual(adapter.player._encoder, "libx264")

    # Without ffmpeg there is no child to encode in, so the in-process aiortc
    # path is the only thing left -- unsupervised, which is why it is last.
    @patch("shutil.which", return_value=None)
    @patch("octoprint_bitbang.usb_camera_source.MediaPlayer")
    def test_adapter_uses_in_process_encode_without_ffmpeg(self, mock_player, mock_which):
        from octoprint_bitbang.octoprint_adapter import OctoPrintBitBang
        from octoprint_bitbang.usb_camera_source import UsbCameraSource

        mock_player.return_value = MagicMock(video=MagicMock())

        async def _app(scope, receive, send):
            pass

        source = {
            "type": "usb",
            "device": "/dev/video0",
            "format": "v4l2",
            "encoder": "auto",
        }
        adapter = OctoPrintBitBang(_app, camera_source=source, ephemeral=True)
        self.assertIsInstance(adapter.player, UsbCameraSource)

    @patch("shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("octoprint_bitbang.v4l2_h264_source.reencode_input_format", return_value="mjpeg")
    @patch("octoprint_bitbang.v4l2_h264_source.has_v4l2m2m_h264_encoder", return_value=False)
    @patch("octoprint_bitbang.v4l2_h264_source.device_supports_h264", return_value=False)
    @patch("octoprint_bitbang.v4l2_h264_source.find_vaapi_device", return_value="/dev/dri/renderD129")
    def test_adapter_explicit_vaapi_pref(self, mock_vaapi_dev, mock_h264, mock_m2m, mock_fmt, mock_which):
        from octoprint_bitbang.octoprint_adapter import OctoPrintBitBang

        async def _app(scope, receive, send):
            pass

        source = {
            "type": "usb",
            "device": "/dev/video0",
            "format": "v4l2",
            "encoder": "vaapi",
            "vaapi_device": "/dev/dri/renderD129",
            "options": {"video_size": "1920x1080"},
        }
        adapter = OctoPrintBitBang(_app, camera_source=source, ephemeral=True)
        self.assertIsNotNone(adapter.player)
        self.assertIsInstance(adapter.player, V4l2H264Track)
        self.assertEqual(adapter.player._encoder, "vaapi")
        self.assertEqual(adapter.player._vaapi_device, "/dev/dri/renderD129")
        mock_vaapi_dev.assert_called_once_with(
            "/dev/dri/renderD129",
            settings=VaapiProbeSettings(video_size="1920x1080"))


if __name__ == "__main__":
    unittest.main()
