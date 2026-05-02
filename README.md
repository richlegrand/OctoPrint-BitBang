# OctoPrint-BitBang

**Remote access to your OctoPrint instance — full UI and live H.264 video — over a single shareable link. No account, no subscription, no port forwarding, no VPN.**

<!-- TODO: demo.gif - 30–60s screencast: share the link, open on phone, watch live video, flip to control tab -->
![Demo](docs/demo.gif)

## Why

Most "OctoPrint remote access" solutions involve either a paid SaaS relay, a VPN you set up yourself, or opening a port on your home network. BitBang is a peer-to-peer WebRTC tunnel: once the plugin registers your printer with the bitba.ng signaling service, clients connect **directly** to your Pi through a data channel. The cloud service only brokers the handshake — all traffic, including video, is end-to-end between your browser and your Pi.

## What you get

- **One link, no accounts.** Share a URL like `https://bitba.ng/abc123…` — optionally gated behind a PIN — and the recipient sees the full OctoPrint UI through a WebRTC tunnel. No sign-up on either end.
- **Live H.264 video.** Frames come straight from the camera, hardware-encoded on Pi 4 (`/dev/video11` V4L2 M2M) and software-encoded on Pi 5, then packetized by aiortc and delivered as a WebRTC video track. CPU footprint is low enough to leave plenty of headroom for a print.
- **The whole OctoPrint UI.** HTTP, WebSockets (SockJS), file uploads, GCode viewer — all tunneled. Not just a minimal dashboard.
- **Pi CSI camera or USB webcam.** Auto-detected (IMX477, IMX219, IMX708, or any V4L2-capable USB webcam). BitBang opens the camera directly — disable `mjpg-streamer` / `camera-streamer` services first so the device isn't already held.
- **Runtime camera controls.** Live brightness slider, image flip (H/V at the sensor level so it carries through snapshots and fullscreen), and resolution selection (VGA up to 1080p).
- **Snapshots and timelapse.** Integrates with OctoPrint's `WebcamProviderPlugin` API — snapshots are grabbed from the live stream, so no second camera pipeline to configure.
- **Mobile friendly.** Fullscreen, touch controls, shareable links work from phones.
- **PIN protection.** Optional PIN required to access the remote URL.

## Installation

### Plugin Manager (recommended)

Settings → Plugin Manager → Get More → "… from URL" →

```
https://github.com/richlegrand/OctoPrint-BitBang/archive/main.zip
```

### Manual

Inside your OctoPrint venv:

```bash
pip install https://github.com/richlegrand/OctoPrint-BitBang/archive/main.zip
```

Restart OctoPrint.

## Usage

1. Open **Settings → BitBang**.
2. Confirm the detected camera and pick a resolution.
3. (Optional) set a PIN.
4. Save and **restart OctoPrint**.
5. After restart, the **Remote URL** field in the same settings panel shows your shareable link (e.g. `https://bitba.ng/abc123…`). Share it.

Whoever opens that URL — on desktop or mobile — sees the full OctoPrint UI with the live video feed. Close the tab and the P2P connection tears down.

## Configuration

All settings live in **Settings → BitBang**:

| Setting | Effect |
|---|---|
| Enabled | Toggle BitBang remote access |
| PIN | Optional 4+ digit PIN prompt on the remote URL |
| Camera | Auto-detect, or pin to a specific `/dev/video*` |
| Resolution | VGA → 1080p (filtered to what your sensor supports) |
| Flip horizontal / vertical | Baked into the bitstream (picamera2) or applied via PyAV filter (USB) |

Camera settings take effect on OctoPrint restart. Brightness is live-tunable via the slider overlay on the webcam view (Control tab).

## Known limitations

- **Login after restart.** OctoPrint's preemptive cache can serve the login page on first load after a server restart — refresh bypasses it.
- **Mobile fullscreen + flip.** On mobile, fullscreen mode uses the browser's native `<video>` fullscreen, which skips CSS transforms. Sensor-level flip (picamera2) is used so this only affects USB webcams with flip enabled.
- **Multi-session on mobile.** Single-session use is supported cleanly; multiple concurrent mobile sessions aren't tested.
- **Font rendering on mobile refresh.** Font Awesome icons may not render after a mobile page refresh — navigate away and back to fix.

## How it works

```
Browser ─── WebRTC data channel ───→ Pi (your machine)
                                        │
  (all HTTP, WS, video tunneled)        ├─ picamera2 / V4L2 → H.264 track → WebRTC video
                                        └─ ASGI reverse proxy → OctoPrint on :5000
```

- The `bitbang` Python package handles WebRTC signaling, identity, and the ASGI tunnel.
- This plugin wraps it with OctoPrint integration: settings UI, `WebcamProviderPlugin` hooks, camera auto-detect, CSRF-safe cookie handling, and the JavaScript that injects the `<video>` element into OctoPrint's Control tab.
- The bitba.ng cloud acts purely as a signaling relay — no video or API traffic ever traverses it.

## Supported hardware

- **Raspberry Pi 4** — hardware H.264 on Pi 4 via V4L2 M2M; tested with IMX477, IMX219
- **Raspberry Pi 5** — software H.264 via picamera2's `LibavH264Encoder`; 1080p@30 comfortably
- **USB webcams** — any device that offers a V4L2 capture format; aiortc software-encodes to H.264

## License

MIT. See [LICENSE](LICENSE).

## Credits

Built on [aiortc](https://github.com/aiortc/aiortc), [picamera2](https://github.com/raspberrypi/picamera2), and the [bitbang](https://github.com/richlegrand/bitbang) library. Plugin scaffold uses OctoPrint's [plugin API](https://docs.octoprint.org/en/master/plugins/).
