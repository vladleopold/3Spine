#!/usr/bin/env python3
"""
Spine Converter — backend service (Linux, stdlib only, no pip deps).

Endpoints:
  POST /api/convert         body = ZIP archive with the user's folder
                            -> JSON {"token", "ok", "failed", "logs", "files"}
  GET  /results/<token>.zip -> converted skeleton JSON + images, as ZIP
  GET  /health              -> {"status": "ok", "converter": true/false}

Run:
  python3 server.py [port]
  Default port: 8080
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import converter

BASE = Path(__file__).resolve().parent
RESULT_DIR = Path(os.environ.get("SPINE_RESULT_DIR", BASE / "results"))
MAX_BYTES = int(os.environ.get("SPINE_MAX_BYTES", 500 * 1024 * 1024))  # 500 MB upload limit

ALLOWED_ORIGINS = [
    "http://localhost",
    "http://127.0.0.1",
    "https://vladleopold.github.io",
]
CORS_ANY = os.environ.get("SPINE_CORS_ANY", "").lower() in {"1", "yes", "true"}

LOCK = threading.Lock()
LAST_CLEANUP = [0.0]


class Handler(BaseHTTPRequestHandler):
    server_version = "SpineConverter/1.0"

    # ---------- CORS ----------

    def _origin_ok(self) -> bool:
        if CORS_ANY:
            return True
        origin = self.headers.get("Origin", "")
        return any(origin == o or origin.startswith(o + "/") or origin.startswith(o + ":") for o in ALLOWED_ORIGINS)

    def _cors_headers(self, extra: dict | None = None):
        if self._origin_ok():
            self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "*"))
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # Private Network Access: public (GitHub Pages) -> local backend
        self.send_header("Access-Control-Allow-Private-Network", "true")
        for k, v in (extra or {}).items():
            self.send_header(k, str(v))

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, msg: str):
        self._json(status, {"error": msg})

    # ---------- routing ----------

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._json(200, {"status": "ok", "converter": converter.binary_ok(), "version": "1.0"})
        elif path.startswith("/results/"):
            self._serve_zip(path.removeprefix("/results/"))
        else:
            self._error(404, "not found")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/convert":
            self._error(404, "not found")
            return
        self._cleanup_old()
        try:
            self._handle_convert()
        except Exception as e:  # noqa: BLE001
            self._error(500, f"{type(e).__name__}: {e}")

    # ---------- convert ----------

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("empty upload")
        if length > MAX_BYTES:
            raise ValueError(f"upload too large ({length // (1024*1024)} MB, limit {MAX_BYTES // (1024*1024)} MB)")
        return self.rfile.read(length)

    def _handle_convert(self):
        data = self._read_body()
        try:
            with zipfile.ZipFile(__import__("io").BytesIO(data)) as zf:
                names = zf.namelist()
                if not names:
                    raise ValueError("ZIP is empty")
        except zipfile.BadZipFile:
            raise ValueError("not a valid ZIP archive") from None

        token = uuid.uuid4().hex[:16]
        job = RESULT_DIR / token
        work = job / "work"
        work.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(__import__("io").BytesIO(data)) as zf:
            for info in zf.infolist():
                dst = (work / info.filename).resolve()
                if not str(dst).startswith(str(work.resolve())):
                    raise ValueError("unsafe path in ZIP")
                if info.is_dir():
                    dst.mkdir(parents=True, exist_ok=True)
                    continue
                parent = dst.parent
                if parent != work:
                    parent = work / parent.name
                parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dst, "wb") as out:
                    import shutil
                    shutil.copyfileobj(src, out)

        logs: list[str] = []
        out_root = job / "converted"
        ok, failed = converter.run_conversion(work, out_root, logs)

        result_zip = job / "result.zip"
        with zipfile.ZipFile(result_zip, "w", zipfile.ZIP_DEFLATED) as z:
            if out_root.exists():
                for p in sorted(out_root.rglob("*")):
                    if p.is_file():
                        z.write(p, p.relative_to(out_root).as_posix())

        (job / "logs.json").write_text(json.dumps(logs, ensure_ascii=False), encoding="utf-8")
        self._json(200, {"token": token, "ok": ok, "failed": failed, "logs": logs})

    # ---------- files ----------

    def _serve_zip(self, name: str):
        if not name.endswith(".zip"):
            self._error(404, "not found")
            return
        token = name[:-4]
        zp = RESULT_DIR / token / "result.zip"
        if not zp.is_file():
            self._error(404, "result not found or expired")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", "attachment; filename=spine-converted.zip")
        self.send_header("Content-Length", str(zp.stat().st_size))
        self._cors_headers({"Cache-Control": "no-store"})
        self.end_headers()
        with open(zp, "rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # ---------- housekeeping ----------

    def _cleanup_old(self):
        with LOCK:
            if time.time() - LAST_CLEANUP[0] < 3600:
                return
            LAST_CLEANUP[0] = time.time()
        cutoff = time.time() - 24 * 3600
        if not RESULT_DIR.exists():
            return
        for child in RESULT_DIR.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                pass

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("SPINE_PORT", 8080))
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    conv = converter.binary_ok()
    print(f"Spine Converter v1.0 — listening on 0.0.0.0:{port}")
    print(f"converter binary: {'OK: ' + str(converter.converter_path()) if conv else 'NOT FOUND (' + str(converter.converter_path()) + ')'}")
    print(f"results dir: {RESULT_DIR}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstop")


if __name__ == "__main__":
    main()