# OctoPrint-BitBang: Pi Implementation Guide

## Current State

The plugin works today on any machine with a USB webcam:
- Camera capture via V4L2 (Linux), AVFoundation (macOS), DirectShow (Windows)
- H.264 encoding via aiortc's built-in libx264 (software)
- Full OctoPrint UI proxied through BitBang WebRTC tunnel
- WebSocket bridging for real-time updates (SockJS)
- CSRF cookie handling for proper authentication

## Pi Hardware Encoding Landscape

### Pi 5 (BCM2712)
- **No hardware H.264 encoder.** The encoding hardware blocks were removed.
- picamera2's `H264Encoder` is an alias for `LibavH264Encoder` (software via FFmpeg/libav)
- Pi 5's CPU handles software encoding well: 1080p60 is achievable
- `/dev/video11` does not exist for encoding

### Pi 4 (BCM2711)
- Hardware H.264 via `/dev/video11` (V4L2 M2M) still works
- picamera2's `H264Encoder` uses hardware when available
- Also capable of software encoding via CPU

### Pi 3 (BCM2837)
- Older, slower quad-core Cortex-A53
- Software H.264 at 640x480 may be too CPU-heavy
- Low priority -- Pi 3 is underpowered for OctoPrint + video

### Conclusion

Since Pi 5 has no hardware encoder and must use software encoding,
**software H.264 is the only universal path across Pi 4 and Pi 5.**
There is no reason to implement a hardware encoder bypass for aiortc --
it would only help Pi 4 and adds significant complexity.

## Chosen Architecture

One code path for all platforms:

```
picamera2 capture -> raw frames -> aiortc software H.264 (libx264) -> WebRTC
```

For non-Pi platforms (USB webcam):
```
V4L2/AVFoundation/DirectShow -> aiortc MediaPlayer -> software H.264 -> WebRTC
```

Runtime detection (already in camera.py):
```
Is picamera2 available?
  YES -> PiCameraTrack (capture frames, aiortc encodes)
  NO  -> Is USB webcam present?
           YES -> MediaPlayer (existing path)
           NO  -> HTTP-only mode (no video)
```

Note: camera-streamer RTSP passthrough is also auto-detected if running
on port 8554. This is a zero-CPU bonus for users who have it installed,
but we don't depend on it.

## Implementation Steps

### Step 1: Build PiCameraTrack

Create a custom aiortc MediaStreamTrack that captures frames from
picamera2 and lets aiortc encode them.

File: `octoprint_bitbang/pi_camera_track.py`

```python
import asyncio
import fractions
from av import VideoFrame
from aiortc import MediaStreamTrack

class PiCameraTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, size=(640, 480), framerate=30):
        super().__init__()
        from picamera2 import Picamera2
        self.picam2 = Picamera2()
        config = self.picam2.create_video_configuration(
            main={"size": size, "format": "RGB888"}
        )
        self.picam2.configure(config)
        self.picam2.start()
        self.framerate = framerate
        self._timestamp = 0
        self._time_base = fractions.Fraction(1, 90000)

    async def recv(self):
        loop = asyncio.get_event_loop()
        array = await loop.run_in_executor(None, self.picam2.capture_array)
        frame = VideoFrame.from_ndarray(array, format="rgb24")
        frame.pts = self._timestamp
        frame.time_base = self._time_base
        self._timestamp += int(90000 / self.framerate)
        return frame

    def stop(self):
        super().stop()
        self.picam2.stop()
        self.picam2.close()
```

### Step 2: Wire into adapter

In `octoprint_adapter.py`, handle the picamera2 source type:

```python
elif source["type"] == "picamera2":
    from .pi_camera_track import PiCameraTrack
    self._pi_track = PiCameraTrack(size=(640, 480), framerate=30)
```

And in setup_peer_connection:

```python
def setup_peer_connection(self, pc, client_id):
    if hasattr(self, '_pi_track') and self._pi_track:
        pc.addTrack(self.relay.subscribe(self._pi_track))
    elif self.player and self.player.video:
        pc.addTrack(self.relay.subscribe(self.player.video))
```

### Step 3: Benchmark on Pi hardware

Test matrix (target: 640x480@30fps):

| Pi Model | Camera | Measure                   | Pass if    |
|----------|--------|---------------------------|------------|
| Pi 5     | CSI    | CPU % over 5 minutes      | < 25%      |
| Pi 4     | CSI    | CPU % over 5 minutes      | < 40%      |
| Pi 4     | USB    | CPU % (existing path)     | < 40%      |
| Pi 5     | USB    | CPU % (existing path)     | < 25%      |

How to measure:
```bash
# Start OctoPrint with plugin, connect from browser
# In another terminal:
top -p $(pgrep -f octoprint)
```

If CPU is too high at 30fps, test at 15fps. If Pi 4 at 15fps is still
too high, then hardware encoding would be needed (Pi 4 only -- Pi 5
doesn't have it). Cross that bridge if we get there.

### Step 4: Move generic code into bitbang package

Before shipping, move reusable components from Octoprint-BitBang into
the bitbang package:

- `ReverseProxy` (without cookie rewriting) -> `bitbang/proxy.py`
- `detect_camera()` -> `bitbang/camera.py`
- `PiCameraTrack` -> `bitbang/pi_camera.py`

The OctoPrint plugin becomes a thin wrapper:
- Plugin lifecycle (StartupPlugin, SettingsPlugin, etc.)
- OctoPrint cookie rewriting (_P5000 -> _P443)
- Settings UI (navbar, settings panel)
- Video injection JS (bitbang.js)

### Step 5: Package and test

1. Bump version in pyproject.toml
2. Build: `python -m build`
3. Test install in a fresh OctoPrint venv:
   ```bash
   python3 -m venv test-venv
   test-venv/bin/pip install octoprint
   test-venv/bin/pip install dist/octoprint_bitbang-*.whl
   test-venv/bin/octoprint serve
   ```
4. Verify: plugin in settings, remote access works, video streams

### Step 6: Publish to PyPI

```bash
pip install twine
twine upload dist/*
```

Users can then: `pip install OctoPrint-BitBang`

### Step 7: Submit to OctoPrint Plugin Repository

https://github.com/OctoPrint/plugins.octoprint.org

1. Fork the repository
2. Create `_plugins/bitbang.md`:
   ```yaml
   ---
   layout: plugin
   id: bitbang
   title: BitBang
   description: Remote OctoPrint access with live H.264 video via WebRTC
   authors:
   - Rich LeGrand
   license: MIT
   date: 2026-04-18
   homepage: https://github.com/richlegrand/OctoPrint-BitBang
   source: https://github.com/richlegrand/OctoPrint-BitBang
   archive: https://github.com/richlegrand/OctoPrint-BitBang/archive/main.zip
   compatibility:
     python: ">=3.7,<4"
     octoprint:
     - 1.9.0
   tags:
   - remote access
   - webcam
   - webrtc
   - video
   ---

   Remote access to your OctoPrint instance with live H.264 video.
   No account, no subscription, no port forwarding. One shareable link.
   ```
3. Submit PR, maintainers review and merge
4. Plugin appears in OctoPrint's Plugin Manager search:
   Settings -> Plugin Manager -> Get More -> Search "BitBang"

## Known Limitations

- **Login after restart**: OctoPrint's preemptive cache serves the login
  page on first load after server restart. Refresh bypasses it.
- **Mobile white screen after login**: On mobile browsers that strip
  referrers, the SW may not route the post-login redirect correctly.
  Fixed for single-session usage. Multi-session on mobile unsupported.
- **Font rendering on mobile refresh**: Font-awesome icons may not
  render after a mobile page refresh. Navigate away and back to fix.

## Future Enhancements

- **Timelapse support**: Implement WebcamProviderPlugin to provide
  take_webcam_snapshot() for OctoPrint's built-in timelapse
- **Multiple cameras**: Support multiple video tracks
- **Bandwidth adaptation**: Dynamic resolution/framerate based on
  connection quality
- **camera-streamer integration**: Already auto-detected if running.
  Will be a zero-CPU bonus if camera-streamer becomes default on OctoPi.
