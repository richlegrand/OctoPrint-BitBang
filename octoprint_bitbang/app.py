"""OctoPrint BitBang prototype - test app for video + HTTP tunnel.

Run with:
    python -m octoprint_bitbang.app
    python -m octoprint_bitbang.app --proxy localhost:5000
    python -m octoprint_bitbang.app --proxy localhost:8080 --camera /dev/video2
"""

from .octoprint_adapter import OctoPrintBitBang
import os


def _make_test_app():
    """Create a simple ASGI test app serving the prototype HTML page."""
    _dir = os.path.dirname(__file__)

    async def test_app(scope, receive, send):
        if scope["type"] != "http":
            return
        path = scope.get("path", "/")

        if path == "/favicon.ico":
            favicon_path = os.path.join(_dir, "static", "favicon.png")
            try:
                with open(favicon_path, "rb") as f:
                    body = f.read()
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"image/png")]})
                await send({"type": "http.response.body", "body": body})
            except FileNotFoundError:
                await send({"type": "http.response.start", "status": 404, "headers": []})
                await send({"type": "http.response.body", "body": b""})
            return

        # Serve index.html for everything else
        html_path = os.path.join(_dir, "index.html")
        with open(html_path, "rb") as f:
            body = f.read()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/html")]})
        await send({"type": "http.response.body", "body": body})

    return test_app


def main():
    import argparse
    from bitbang.adapter import add_bitbang_args, bitbang_kwargs

    parser = argparse.ArgumentParser(description='OctoPrint via BitBang')
    add_bitbang_args(parser)
    parser.add_argument('--proxy',
                        help='Local server to proxy (e.g. localhost:5000)')
    parser.add_argument('--camera',
                        help='Camera source override (e.g. /dev/video0)')
    args = parser.parse_args()

    ws_target = None
    if args.proxy:
        from bitbang.proxy import ReverseProxyASGI
        asgi_app = ReverseProxyASGI(args.proxy)
        ws_target = args.proxy
        print(f"Proxying to {args.proxy}")
    else:
        asgi_app = _make_test_app()

    camera_source = None
    if args.camera:
        camera_source = {
            "type": "usb",
            "device": args.camera,
            "format": "v4l2",
            "options": {"framerate": "30", "video_size": "640x480"},
        }

    adapter = OctoPrintBitBang(
        asgi_app,
        camera_source=camera_source,
        ws_target=ws_target,
        **bitbang_kwargs(args, program_name='octoprint'),
    )
    adapter.run()


if __name__ == '__main__':
    main()
