# OctoPrint-BitBang

This is an [Octoprint](https://octoprint.org/) plug-in that offers full remote access to your OctoPrint instance including live H.264 video over a single HTTPS shareable link. It benefits from the [BitBang](https://github.com/richlegrand/bitbang) features -- a secure, fast peer-to-peer connection that requires no account, no subscription, no port forwarding, no tunnel, and no VPN.

TODO video

## What you get

- **Full remote access:** You get full access from anywhere through a secure HTTPS URL. Configure, upload G-code, start jobs, see live video, etc. 
- **One link, no account set-up:** Share the URL `https://bitba.ng/abc123…`, optionally gated behind a PIN, and share your printer.  
- **Live H.264 video:** Frames come straight from the camera, hardware-encoded on Pi 4 (`/dev/video11` V4L2 M2M) and software-encoded on Pi 5 or x86-64 computer, then packetized by aiortc and delivered as a WebRTC media stream. CPU footprint is around 40% (single core) on Pi4. 
- **BitBang access is optional:** Video streaming works for local access through local network URL.
- **Pi CSI camera or USB webcam:** Auto-detected (IMX477, IMX219, IMX708, or any V4L2-capable USB webcam). `camera-streamer` RTSP is also picked up automatically if you already run it.
- **Camera controls:** Camera selection, live brightness slider, fullscreen button, image flip H/V buttons, and resolution selection (VGA up to 720p).
- **Snapshots and timelapse:** Integrates with OctoPrint's `WebcamProviderPlugin` API -- snapshots are grabbed from the live stream, so no second camera pipeline to configure.
- **Mobile friendly:** Fullscreen, touch controls, shareable links work from phones.
- **PIN protection:** Optional PIN required to access the remote URL.

## Installation

### Plugin Manager (recommended)

Settings → Plugin Manager → Get More → "… from URL" →

```
https://github.com/richlegrand/OctoPrint-BitBang/archive/main.zip
```

### Manual

Inside your OctoPrint venv:

```bash
pip install Octoprint-BitBang
```

Restart OctoPrint.

## Usage

1. Open **Settings → BitBang**.
2. Confirm the detected camera and pick a resolution.
3. (Optional) set a PIN.
4. Save and **restart OctoPrint**.
5. After restart, the BitBang remote URL is available in the menu bar (e.g. `https://bitba.ng/abc123…`).
TODO image

Whoever opens that URL -- on desktop or mobile -- sees the full OctoPrint UI with the live video feed. Close the tab and the P2P connection tears down.

## Configuration

All settings live in **Settings → BitBang**:

| Setting | Effect |
|---|---|
| Enabled | Toggle BitBang remote access |
| PIN | Optional 4+ digit PIN prompt on the remote URL |
| Camera | Auto-detect, or pin to a specific `/dev/video*` |
| Resolution | VGA → 720p (filtered to what your sensor supports) |
| Flip horizontal / vertical | Flip video if necessary |

Camera settings take effect on OctoPrint restart. Full-screen button and brightness slider are overlaid on the video window (Control tab) and update immediately.

## How it works

TODO image

- The `bitbang` Python package handles WebRTC signaling, identity, and the ASGI interface.
- This plugin wraps it with OctoPrint integration: settings UI, `WebcamProviderPlugin` hooks, camera auto-detect, CSRF-safe cookie handling, and the JavaScript that injects the `<video>` element into OctoPrint's Control tab.
- The bitba.ng cloud acts purely as a signaling relay to broker a direct connection. If a direct connection isn't available, bitba.ng will use TURN instead.

## Supported hardware

- **Raspberry Pi 4** -- hardware H.264 on Pi 4 via V4L2 M2M; tested with IMX477, IMX219
- **Raspberry Pi 5** -- software H.264 via picamera2's `LibavH264Encoder`; 720p@30 comfortably
- **USB webcams** -- any device that offers a V4L2 capture format; aiortc software-encodes to H.264

## License

MIT. See [LICENSE](LICENSE).

## Credits

Built on [aiortc](https://github.com/aiortc/aiortc), [picamera2](https://github.com/raspberrypi/picamera2), and the [bitbang](https://github.com/richlegrand/bitbang-python) library. Plugin scaffold uses OctoPrint's [plugin API](https://docs.octoprint.org/en/master/plugins/).