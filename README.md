
# OctoPrint-BitBang

This is an [OctoPrint](https://octoprint.org/) plugin that offers full remote access to your OctoPrint instance including live H.264 video over a single HTTPS shareable link. It uses [BitBang](https://github.com/richlegrand/bitbang) which creates a secure, fast peer-to-peer connection that requires no account, no subscription, port forwarding, tunnel, or VPN.

![BitBang plugin](https://raw.githubusercontent.com/richlegrand/OctoPrint-BitBang/refs/heads/main/assets/octoprint_bitbang.webp)


This is part of the [BitBang project](https://github.com/richlegrand/bitbang). 

## What you get

- **Full remote access:** You get full access from anywhere through a secure HTTPS URL. Configure, upload G-code, start jobs, see live video, etc. 
- **One link, no account set-up:** Remote access, share the URL, share your printer.
- **Live H.264 video:** Frames come straight from the camera, hardware-encoded on Pi 4 (`/dev/video11` V4L2 M2M) and software-encoded on Pi 5 or x86-64 computer, then packetized by aiortc and delivered as a WebRTC media stream. CPU footprint is around 40% (single core) on Pi4. 
- **BitBang URL access is optional:** Video streaming works with local access through local network URL also.
- **Pi CSI camera or USB webcam:** Auto-detected (IMX477, IMX219, IMX708, or any V4L2-capable USB webcam). 
- **Camera controls:** Camera selection, live brightness slider, fullscreen button, image flip H/V buttons, and resolution selection (VGA up to 720p).
- **Snapshots and timelapse:** Integrates with OctoPrint's `WebcamProviderPlugin` API -- snapshots are grabbed from the live stream, so no second camera pipeline to configure.
- **Mobile friendly:** BitBang URL works with phones/tablets.
- **PIN protection:** Optional PIN required to access the remote URL.

## Installation

[//]: # ### Plugin Manager (recommended)

[//]: # Settings → Plugin Manager → Get More → "… from URL" →

[//]: # ```
[//]: # https://github.com/richlegrand/OctoPrint-BitBang/archive/main.zip
[//]: # ```

[//]: # ### Manual

Inside your OctoPrint venv:

```bash
pip install OctoPrint-BitBang
```

Restart OctoPrint.

## Usage

0. If you are using a separate program for camera streaming (e.g. camera-streamer, mjpg-streamer, ustreamer) you should stop these processes before running BitBang plugin to avoid camera access contention. 
1. Point your browser to your local OctoPrint server. Open **Settings → BitBang**.
2. Choose camera from dropdown.

![Camera dropdown](https://raw.githubusercontent.com/richlegrand/OctoPrint-BitBang/refs/heads/main/assets/camera_select.png)

3. Choose resolution.

![Resolution dropdown](https://raw.githubusercontent.com/richlegrand/OctoPrint-BitBang/refs/heads/main/assets/resolution_select.png)

4. Set a PIN (Optional).
5. Save and **restart OctoPrint**.
6. Refresh the OctoPrint tab in your browser. A button labeled BitBang is available in the menu bar -- click it for the URL.

![Camera dropdown](https://raw.githubusercontent.com/richlegrand/OctoPrint-BitBang/refs/heads/main/assets/bitbang_select.png)

![BitBang URL](https://raw.githubusercontent.com/richlegrand/OctoPrint-BitBang/refs/heads/main/assets/bitbang_url.png)


This URL allows remote access to your printer.

## Configuration

All settings live in **Settings → BitBang**:

| Setting | Effect |
|---|---|
| Enabled | Toggle BitBang remote access |
| PIN | Optional 4+ digit PIN prompt on the remote URL |
| Camera | Auto-detect, or select from dropdown list |
| Resolution | VGA → HD (depending on what selected camera supports) |
| Flip horizontal / vertical | Flip video if necessary |

All settings take effect on OctoPrint restart. Full-screen button and brightness slider are overlaid on the video window (Control tab) and update immediately.

## How it works

- The `bitbang-python` package handles WebRTC signaling, identity, and the ASGI interface.
- This plugin wraps it with OctoPrint integration: settings UI, `WebcamProviderPlugin` hooks, camera auto-detect, CSRF-safe cookie handling, and the JavaScript that injects the `<video>` element into OctoPrint's Control tab.
- The bitba.ng cloud acts purely as a signaling relay to broker a direct connection. If a direct connection isn't available, bitba.ng will use TURN instead.

## Supported hardware

- **Raspberry Pi 4** -- hardware H.264 on Pi 4 via V4L2 M2M; tested with IMX477, IMX219
- **Raspberry Pi 5** -- software H.264 via picamera2's `LibavH264Encoder`; 720p@30 comfortably
- **USB webcams** -- any device that offers a V4L2 capture format; aiortc software-encodes to H.264

## License

MIT. See [LICENSE](LICENSE).

## Credits

Built on [aiortc](https://github.com/aiortc/aiortc), [picamera2](https://github.com/raspberrypi/picamera2), and the [bitbang-python](https://github.com/richlegrand/bitbang-python) library. Plugin scaffold uses OctoPrint's [plugin API](https://docs.octoprint.org/en/master/plugins/).

## Contributing

This is a one-person project. Issues and PRs are welcome and genuinely appreciated. I'll do my best to respond promptly.
