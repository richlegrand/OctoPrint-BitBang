
# OctoPrint-BitBang

This is an [OctoPrint](https://octoprint.org/) plugin that offers full remote access to your OctoPrint instance including live H.264 video over a single HTTPS shareable link. It uses [BitBang](https://github.com/richlegrand/bitbang) which creates a secure, fast peer-to-peer connection that requires no account, no subscription, port forwarding, tunnel, or VPN.

![BitBang plugin](https://raw.githubusercontent.com/richlegrand/OctoPrint-BitBang/refs/heads/main/assets/octoprint_bitbang.webp)


This is part of the [BitBang project](https://github.com/richlegrand/bitbang). 

## What you get

- **Full remote access:** You get full access from anywhere through a secure HTTPS URL. Configure, upload G-code, start jobs, see live video, etc. 
- **One link, no account set-up:** Remote access, share the URL, share your printer.
- **Live H.264 video:** Frames come straight from the camera, hardware-encoded on Pi 4 (`/dev/video11` V4L2 M2M) and software-encoded on Pi 5 or any other Linux host, then packetized by aiortc and delivered as a WebRTC media stream. CPU footprint is around 40% (single core) on Pi4. 
- **BitBang URL access is optional:** Video streaming works with local access through local network URL also.
- **Pi CSI camera or USB webcam:** Auto-detected (IMX477, IMX219, IMX708, or any V4L2-capable USB webcam). 
- **Camera controls:** Camera selection, live brightness slider, fullscreen button, image flip H/V buttons, and resolution selection (VGA up to 720p).
- **Snapshots and timelapse:** Integrates with OctoPrint's `WebcamProviderPlugin` API -- snapshots are grabbed from the live stream, so no second camera pipeline to configure.
- **Mobile friendly:** BitBang URL works with phones/tablets.
- **PIN protection:** Optional PIN required to access the remote URL.

## Installation

### Plugin Manager (recommended, once accepted into the OctoPrint plugin registry)

In OctoPrint: **Settings → Plugin Manager → Get More**, search for **BitBang**, click **Install**.

### Plugin Manager, install from URL

**Settings → Plugin Manager → Get More → "… from URL"**, then paste:

```
https://github.com/richlegrand/OctoPrint-BitBang/archive/main.zip
```

Click **Install**, then restart OctoPrint when prompted.

### pip

Inside your OctoPrint venv:

```bash
pip install OctoPrint-BitBang
```

Restart OctoPrint.

### Did it work?

After OctoPrint restarts, you should see a **BitBang** button in the navbar and a new **BitBang** entry in Settings. If both are there, you're done -- continue to [Usage](#usage).

If the plugin doesn't show up, or `octoprint.log` contains `BitBang plugin not loaded` or `BitBang video stack unavailable`, see [Installation Notes](#installation-notes) -- usually a pre-3.10 Python image, or (on 32-bit) an `aiortc`/`libvpx` mismatch that needs one extra step.

## Usage

1. If you are using a separate program for camera streaming (e.g. camera-streamer, mjpg-streamer, ustreamer) you should stop these processes before running BitBang plugin to avoid camera access contention. See commands below.

Stable octopi stack:
```bash
sudo systemctl stop webcamd
sudo systemctl stop ffmpeg_hls
sudo systemctl disable webcamd
sudo systemctl disable ffmpeg_hls
```

New camera stack:
```bash
sudo systemctl stop camera-streamer
sudo systemctl disable camera-streamer
```

2. Point your browser to your local OctoPrint server. Open **Settings → BitBang**.
3. Choose camera from dropdown.

![Camera dropdown](https://raw.githubusercontent.com/richlegrand/OctoPrint-BitBang/refs/heads/main/assets/camera_select.png)

4. Choose resolution.

![Resolution dropdown](https://raw.githubusercontent.com/richlegrand/OctoPrint-BitBang/refs/heads/main/assets/resolution_select.png)

5. Set a PIN (Optional).
6. Save and **restart OctoPrint**.
7. Refresh the OctoPrint tab in your browser. A button labeled BitBang is available in the menu bar -- click it for the URL.

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

## Privacy

The BitBang plugin connects through the `bitba.ng` cloud signaling service to broker peer-to-peer connections. Here is what `bitba.ng` does and does not see:

- **Signaling:** When the plugin starts, it registers with `bitba.ng` using a public key derived from a locally-generated keypair (the private key never leaves your device). `bitba.ng` sees this public key, the derived UID that becomes part of your URL, and connection metadata (timestamps, IPs of peers attempting to connect).
- **Media path:** Once a peer connects, video and HTTP traffic flow **directly** between the browser and your OctoPrint host over an encrypted WebRTC data channel (DTLS-SRTP). `bitba.ng` does not see this traffic.
- **TURN fallback:** If a direct connection cannot be established (strict NAT/firewall), `bitba.ng` may relay the *encrypted* WebRTC stream via TURN. Even in that case, the relay sees ciphertext only — it cannot decrypt your video, OctoPrint UI, or credentials.
- **No account, no tracking:** The plugin does not create an account, send telemetry, or upload usage data.
- **Access control:** Anyone with your URL can reach your OctoPrint instance. Set a **PIN** in the plugin settings to require a passcode on the remote URL.

See the [BitBang project page](https://github.com/richlegrand/bitbang) for the full signaling protocol and identity specifications.

## Supported hardware

- **Raspberry Pi 4 (32- or 64-bit OS)** -- hardware H.264 via the V4L2 M2M encoder (`h264_v4l2m2m`); tested with IMX477, IMX219
- **Raspberry Pi 5** -- no hardware H.264 encoder; software H.264 (picamera2's `LibavH264Encoder` for CSI, aiortc for V4L2), which the A76 CPU handles at 720p@30
- **CSI cameras** -- via picamera2/libcamera where available, or the legacy mmal device (`/dev/video2`) as a direct H.264 passthrough
- **USB webcams** -- cams with onboard H.264 stream as a zero-encode passthrough; otherwise hardware-re-encoded on Pi 4 (`h264_v4l2m2m`) or software-encoded elsewhere
- **Generic Linux PC/laptop/SBC with webcam** -- software H.264 via aiortc

> **`ffmpeg` is required for the hardware H.264 paths.** The Pi CSI (legacy/mmal) passthrough and the USB hardware re-encode (`h264_v4l2m2m`) drive the system `ffmpeg` binary, which is present by default on OctoPi (the timelapse renderer depends on it). If `ffmpeg` is missing, BitBang automatically falls back to software encoding.

## Installation Notes

If the basic [Installation](#installation) worked, skip this section.

The video stack depends on `av` ([PyAV](https://github.com/PyAV-Org/PyAV)) and `aiortc`, installed as prebuilt wheels. As of v0.1.7 the plugin pins them (`aiortc<1.11`, and `av<12` on 32-bit ARM) so pip resolves to versions that work on current OctoPi -- **including the 32-bit stable image**:

- **64-bit Linux** (`aarch64`/`x86_64`) -- PyPI ships `av` wheels with FFmpeg bundled; nothing system-level needed.
- **32-bit Raspberry Pi OS** (`armv7l`) -- [piwheels](https://www.piwheels.org/) ships an `av` wheel built against the **system FFmpeg 5.1**, and the `av<12` pin selects it. Supported, with one possible extra step (see below).
- **Python 3.10+** is required either way (`av` wheels start at cp310).

### Quick check

```bash
uname -m            # aarch64 = 64-bit Pi; armv7l = 32-bit (also supported)
python --version    # must be 3.10 or newer
```

### By OctoPi version

| Version | Notes |
|---|---|
| **1.1.0** | Bookworm + Python 3.11. The **stable image is 32-bit** (`armv7l`) on every Pi model; 64-bit is nightly-only. v0.1.7 supports both. |
| **1.0.x** | Bullseye + Python 3.9 -- below the 3.10 minimum. Upgrade to 1.1.0. |
| **Pre-1.0** | Older base. Upgrade. |

### 32-bit: aiortc / libvpx mismatch

On some 32-bit images the piwheels `aiortc` wheel is built against a newer `libvpx` than the OS ships, so `octoprint.log` shows `BitBang video stack unavailable: libvpx.so.9: cannot open shared object file`. Install the codec dev headers and rebuild aiortc against the system libvpx (in your OctoPrint venv):

```bash
sudo apt install -y libvpx-dev libopus-dev
pip install --no-binary aiortc --force-reinstall --no-deps "aiortc<1.11"
```

### Old Python (3.9 or earlier)

`av` wheels start at Python 3.10, so OctoPi 1.0.x (Python 3.9) has no usable wheel -- upgrade the image to 1.1.0.

### Diagnostic mode

If the video stack fails to import for any reason, the plugin still loads -- settings and navbar are visible, and `octoprint.log` shows a clear `BitBang video stack unavailable: <reason>` line. You can see the missing piece in OctoPrint instead of grepping logs.

## License

MIT. See [LICENSE](LICENSE).

## Credits

Built on [aiortc](https://github.com/aiortc/aiortc), [picamera2](https://github.com/raspberrypi/picamera2), and the [bitbang-python](https://github.com/richlegrand/bitbang-python) library. Plugin scaffold uses OctoPrint's [plugin API](https://docs.octoprint.org/en/master/plugins/).

## Contributing

Issues and PRs are welcome. 
