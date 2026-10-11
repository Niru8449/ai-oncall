import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.common import env_int, setup_logging

log = setup_logging("registry")

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = env_int("PORT", 5000)
PRIVATE_PREFIX = "private/"
REALM = "storefront-registry"


class Handler(BaseHTTPRequestHandler):
    server_version = "registry/2.0"

    def _send(self, status, body, headers=()):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Docker-Distribution-Api-Version", "registry/2.0")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _route(self):
        path = self.path.split("?", 1)[0]
        if path in ("/v2", "/v2/"):
            return self._send(200, {})
        parts = path.split("/")
        if len(parts) >= 5 and parts[1] == "v2" and parts[-2] in ("manifests", "blobs"):
            repo = "/".join(parts[2:-2])
            kind = parts[-2][:-1]
            if repo.startswith(PRIVATE_PREFIX):
                return self._send(
                    401,
                    {"errors": [{"code": "UNAUTHORIZED", "message": "authentication required", "detail": None}]},
                    [("WWW-Authenticate", f'Basic realm="{REALM}"')],
                )
            return self._send(
                404,
                {"errors": [{"code": f"{kind.upper()}_UNKNOWN", "message": f"{kind} unknown", "detail": {"name": repo, "reference": parts[-1]}}]},
            )
        return self._send(404, {"errors": [{"code": "NOT_FOUND", "message": "not found", "detail": None}]})

    do_GET = _route
    do_HEAD = _route

    def log_message(self, fmt, *args):
        log.info("%s", fmt % args)


def run():
    log.info("registry listening on %s:%d", HOST, PORT)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
