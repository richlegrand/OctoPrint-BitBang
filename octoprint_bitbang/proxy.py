"""WSGI reverse proxy for OctoPrint BitBang.

A WSGI application that forwards requests to a local HTTP server
(typically OctoPrint at localhost:5000). Used as the `app` argument
to OctoPrintBitBang so that OctoPrint's full UI is served through
the BitBang WebRTC tunnel.
"""

import urllib.request
import urllib.error

CHUNK_SIZE = 32768  # 32KB chunks for streaming responses


class ReverseProxy:
    """WSGI app that proxies requests to a local HTTP server."""

    def __init__(self, target="localhost:5000", api_key=None):
        # Normalize target to include scheme
        if not target.startswith("http"):
            target = f"http://{target}"
        self.target = target.rstrip("/")
        self.api_key = api_key

    def __call__(self, environ, start_response):
        method = environ["REQUEST_METHOD"]
        path = environ.get("PATH_INFO", "/")
        query = environ.get("QUERY_STRING", "")

        url = f"{self.target}{path}"
        if query:
            url += f"?{query}"

        # Build request headers from WSGI environ
        headers = {}
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                name = key[5:].replace("_", "-").title()
                # Don't forward Host -- let urllib set it for the target
                if name.lower() != "host":
                    headers[name] = value
        if environ.get("CONTENT_TYPE"):
            headers["Content-Type"] = environ["CONTENT_TYPE"]

        # Read request body
        body = None
        content_length = environ.get("CONTENT_LENGTH")
        if content_length and int(content_length) > 0:
            body = environ["wsgi.input"].read(int(content_length))

        # Inject API key to bypass CSRF checks (OctoPrint exempts
        # API-key-authenticated requests from CSRF verification)
        if self.api_key:
            headers["X-Api-Key"] = self.api_key

        # Forward request to target
        req = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            resp = urllib.request.urlopen(req, timeout=10)
            status = f"{resp.status} {resp.reason}"
            resp_headers = [(k, v) for k, v in resp.getheaders()]
            start_response(status, resp_headers)
            # Stream response in chunks instead of buffering the whole thing.
            # This lets the WSGI adapter send SWSP frames incrementally and
            # avoids blocking on long-held streaming responses (SockJS).
            return _iter_response(resp)
        except urllib.error.HTTPError as e:
            status = f"{e.code} {e.reason}"
            resp_headers = [(k, v) for k, v in e.headers.items()]
            body_bytes = e.read()
            start_response(status, resp_headers)
            return [body_bytes]
        except Exception as e:
            start_response("502 Bad Gateway", [("Content-Type", "text/plain")])
            return [f"Proxy error: {e}".encode()]


def _iter_response(resp):
    """Yield response body in chunks."""
    try:
        while True:
            chunk = resp.read(CHUNK_SIZE)
            if not chunk:
                break
            yield chunk
    except Exception:
        pass
    finally:
        resp.close()
