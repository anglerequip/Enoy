"""A scripted local HTTP server standing in for model providers. No network is used."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeAPI:
    """Replies are queued per path. A reply is (status, body[, headers]) or a callable(request) returning one.

    `body` may be a dict (sent as JSON) or a str. Every request is recorded in `requests`.
    """

    def __init__(self):
        self.replies: dict[str, list] = {}
        self.requests: list[dict] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                req = {"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                       "body": json.loads(raw) if raw else None}
                api.requests.append(req)
                path = self.path.split("?")[0]
                queue = api.replies.get(path) or []
                reply = queue.pop(0) if queue else (404, {"error": {"message": f"no scripted reply for {path}"}})
                if callable(reply):
                    reply = reply(req)
                status, body, *rest = reply
                headers = rest[0] if rest else {}
                data = (json.dumps(body) if isinstance(body, (dict, list)) else str(body)).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def add(self, path: str, *replies):
        self.replies.setdefault(path, []).extend(replies)
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
