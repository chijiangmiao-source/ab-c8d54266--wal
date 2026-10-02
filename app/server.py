"""HTTP front-end for the WAL recovery engine.

Endpoints:
    GET  /health   -> {"status": "ok"}
    POST /recover  -> JSON in / JSON out, see README.md

Only Python's standard library is used so the image stays dependency-free.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.walrec import RecoverError, recover

MAX_BODY_SIZE = 32 * 1024 * 1024  # decoded WAL is capped at 2 MiB anyway


def _error_body(code: str, message: str, offset: int | None = None) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "offset": offset}}


class Handler(BaseHTTPRequestHandler):
    server_version = "WalRecover/1.0"

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, _error_body("not_found", "unknown endpoint"))

    def do_POST(self) -> None:
        if self.path != "/recover":
            self._json(404, _error_body("not_found", "unknown endpoint"))
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._json(411, _error_body("invalid_request", "missing Content-Length"))
            return
        if length > MAX_BODY_SIZE:
            self._json(413, _error_body("request_too_large", "request body too large"))
            return
        raw = self.rfile.read(length)
        try:
            request = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            self._json(400, _error_body("invalid_json", "request body is not valid JSON"))
            return
        if (
            not isinstance(request, dict)
            or "database" not in request
            or "wal" not in request
        ):
            self._json(
                400,
                _error_body(
                    "invalid_request", "request must carry 'database' and 'wal' fields"
                ),
            )
            return
        try:
            db = base64.b64decode(request["database"], validate=True)
            wal = base64.b64decode(request["wal"], validate=True)
        except (binascii.Error, TypeError, ValueError):
            self._json(
                400,
                _error_body(
                    "invalid_base64", "'database' and 'wal' must be base64 strings"
                ),
            )
            return
        stable = bool(request.get("stable_page_order", False))
        try:
            result = recover(db, wal, stable)
        except RecoverError as exc:
            self._json(422, _error_body(exc.code, exc.message, exc.offset))
            return
        image = result.pop("image")
        self._json(
            200,
            {
                "ok": True,
                **result,
                "image_size": len(image),
                "image_sha256": hashlib.sha256(image).hexdigest(),
                "image": base64.b64encode(image).decode("ascii"),
            },
        )


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"wal-recover listening on 0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
