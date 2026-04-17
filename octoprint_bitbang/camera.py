"""Camera auto-detection for OctoPrint BitBang.

Detects available camera sources in priority order:
1. camera-streamer RTSP (H.264 passthrough, zero CPU)
2. picamera2 CSI camera (Pi hardware, software encode in aiortc)
3. USB webcam via V4L2/dshow/avfoundation (software encode)
4. None (HTTP-only mode, no crash)
"""

import sys
import socket


def detect_camera(logger=None):
    """Detect best available camera source.

    Returns dict with keys: type, device/url, format, options
    Or None if no camera found.
    """
    log = logger.info if logger else lambda msg: print(f"[camera] {msg}")

    # 1. RTSP (camera-streamer)
    source = _try_rtsp()
    if source:
        log(f"Found camera-streamer RTSP at {source['url']}")
        return source

    # 2. picamera2 (Raspberry Pi CSI)
    source = _try_picamera2()
    if source:
        log("Found Pi CSI camera via picamera2")
        return source

    # 3. USB webcam (platform-specific)
    source = _try_usb_webcam()
    if source:
        log(f"Found USB webcam: {source['device']}")
        return source

    log("No camera found - running in HTTP-only mode")
    return None


def _try_rtsp(url="rtsp://localhost:8554/stream.h264", timeout=1.0):
    """Check if camera-streamer RTSP is available."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex(('localhost', 8554))
        sock.close()
        if result == 0:
            return {
                "type": "rtsp",
                "url": url,
                "format": "rtsp",
                "options": {"rtsp_transport": "tcp"},
                "decode": False,
            }
    except Exception:
        pass
    return None


def _try_picamera2():
    """Check if picamera2 is available (Raspberry Pi CSI camera)."""
    try:
        from picamera2 import Picamera2
        cam = Picamera2()
        cam.close()
        return {"type": "picamera2"}
    except Exception:
        return None


def _try_usb_webcam():
    """Check for USB webcam (platform-specific)."""
    if sys.platform == 'darwin':
        return {
            "type": "usb",
            "device": "0:none",
            "format": "avfoundation",
            "options": {"framerate": "30", "video_size": "640x480"},
        }
    elif sys.platform == 'win32':
        camera = _find_windows_camera()
        if camera:
            return {
                "type": "usb",
                "device": f"video={camera}",
                "format": "dshow",
                "options": {"framerate": "30", "video_size": "640x480"},
            }
        return None
    else:
        # Linux: check /dev/video0 through /dev/video3
        import os
        for i in range(4):
            dev = f"/dev/video{i}"
            if os.path.exists(dev):
                if _is_v4l2_capture(dev):
                    return {
                        "type": "usb",
                        "device": dev,
                        "format": "v4l2",
                        "options": {"framerate": "30", "video_size": "640x480"},
                    }
        return None


def _is_v4l2_capture(device):
    """Check if a V4L2 device is a video capture device (not encoder/decoder).

    Skips V4L2 M2M devices like /dev/video10, /dev/video11 on Raspberry Pi.
    """
    try:
        import fcntl
        import struct
        VIDIOC_QUERYCAP = 0x80685600
        with open(device, 'rb') as f:
            buf = bytearray(104)
            fcntl.ioctl(f, VIDIOC_QUERYCAP, buf)
            capabilities = struct.unpack_from('<I', buf, 84)[0]
            V4L2_CAP_VIDEO_CAPTURE = 0x00000001
            return bool(capabilities & V4L2_CAP_VIDEO_CAPTURE)
    except Exception:
        # If we can't probe, assume it's a capture device
        return True


def _find_windows_camera():
    """Discover first available camera on Windows via PowerShell."""
    import subprocess
    try:
        result = subprocess.run(
            ['powershell', '-Command',
             'Get-PnpDevice -Class Camera -Status OK | '
             'Select-Object -ExpandProperty FriendlyName'],
            capture_output=True, text=True, timeout=5
        )
        name = result.stdout.strip().split('\n')[0].strip()
        if name:
            return name
    except Exception:
        pass
    return None
