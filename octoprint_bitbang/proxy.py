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

    def __init__(self, target="localhost:5000"):
        # Normalize target to include scheme
        if not target.startswith("http"):
            target = f"http://{target}"
        self.target = target.rstrip("/")

        # OctoPrint appends the port to cookie names (e.g. csrf_token_P5000).
        # When accessed via BitBang (port 443), the JS looks for _P443.
        # We rewrite cookie names in both directions to bridge the mismatch.
        from urllib.parse import urlparse
        parsed = urlparse(self.target)
        self._target_port = str(parsed.port or 80)
        self._cookie_suffix_target = f"_P{self._target_port}"
        self._cookie_suffix_remote = "_P443"

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
                if name.lower() == "host":
                    continue
                # Rewrite cookie names: _P443 -> _P5000
                if name.lower() == "cookie":
                    value = value.replace(self._cookie_suffix_remote,
                                          self._cookie_suffix_target)
                # Rewrite CSRF header cookie name too
                if name.lower() == "x-csrf-token":
                    pass  # value is the token itself, no rewriting needed
                headers[name] = value
        if environ.get("CONTENT_TYPE"):
            headers["Content-Type"] = environ["CONTENT_TYPE"]

        # Read request body
        body = None
        content_length = environ.get("CONTENT_LENGTH")
        if content_length and int(content_length) > 0:
            body = environ["wsgi.input"].read(int(content_length))

        # Forward request to target
        req = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            resp = urllib.request.urlopen(req, timeout=10)
            status = f"{resp.status} {resp.reason}"
            resp_headers = self._rewrite_response_cookies(resp.getheaders())
            start_response(status, resp_headers)
            # Stream response in chunks instead of buffering the whole thing.
            # This lets the WSGI adapter send SWSP frames incrementally and
            # avoids blocking on long-held streaming responses (SockJS).
            return _iter_response(resp)
        except urllib.error.HTTPError as e:
            status = f"{e.code} {e.reason}"
            resp_headers = self._rewrite_response_cookies(e.headers.items())
            body_bytes = e.read()
            start_response(status, resp_headers)
            return [body_bytes]
        except Exception as e:
            start_response("502 Bad Gateway", [("Content-Type", "text/plain")])
            return [f"Proxy error: {e}".encode()]


    def _rewrite_response_cookies(self, headers):
        """Rewrite Set-Cookie names: _P5000 -> _P443."""
        result = []
        for k, v in headers:
            if k.lower() == 'set-cookie':
                v = v.replace(self._cookie_suffix_target,
                              self._cookie_suffix_remote)
            result.append((k, v))
        return result


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
