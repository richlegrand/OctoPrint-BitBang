"""OctoPrint BitBang prototype - test app for video + HTTP tunnel.

Run with:
    python -m octoprint_bitbang.app
    python -m octoprint_bitbang.app --proxy localhost:5000
    python -m octoprint_bitbang.app --proxy localhost:8080 --camera /dev/video2
"""

from flask import Flask, render_template, send_file
from .octoprint_adapter import OctoPrintBitBang

import os

_dir = os.path.dirname(__file__)

app = Flask(__name__, template_folder=_dir)


@app.route('/favicon.ico')
def favicon():
    return send_file(os.path.join(_dir, 'static', 'favicon.png'),
                     mimetype='image/png')


@app.route('/')
def index():
    return render_template('index.html')


def main():
    import argparse
    from bitbang.adapter import add_bitbang_args, bitbang_kwargs

    parser = argparse.ArgumentParser(description='OctoPrint via BitBang (prototype)')
    add_bitbang_args(parser)
    parser.add_argument('--proxy',
                        help='Local server to proxy (e.g. localhost:5000)')
    parser.add_argument('--api-key',
                        help='OctoPrint API key (bypasses CSRF)')
    parser.add_argument('--camera',
                        help='Camera source override (e.g. /dev/video0, rtsp://...)')
    args = parser.parse_args()

    # Choose WSGI app: reverse proxy or built-in test page
    ws_target = None
    if args.proxy:
        from .proxy import ReverseProxy
        wsgi_app = ReverseProxy(args.proxy, api_key=getattr(args, 'api_key', None))
        ws_target = args.proxy  # WebSocket bridging to same target
        print(f"Proxying to {args.proxy}")
    else:
        wsgi_app = app

    camera_source = None
    if args.camera:
        if args.camera.startswith('rtsp://'):
            camera_source = {
                "type": "rtsp",
                "url": args.camera,
                "format": "rtsp",
                "options": {"rtsp_transport": "tcp"},
                "decode": False,
            }
        else:
            camera_source = {
                "type": "usb",
                "device": args.camera,
                "format": "v4l2",
                "options": {"framerate": "30", "video_size": "640x480"},
            }

    adapter = OctoPrintBitBang(
        wsgi_app,
        camera_source=camera_source,
        ws_target=ws_target,
        **bitbang_kwargs(args, program_name='octoprint'),
    )
    adapter.run()


if __name__ == '__main__':
    main()
