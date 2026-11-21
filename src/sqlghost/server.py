"""Minimal local web UI on the standard library HTTP server.

Binds to 127.0.0.1 by default. There is no authentication, so do not expose it.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .db import SourceError
from .engine import Ghost
from .nl import EXAMPLES

STATIC = Path(__file__).parent / "static"


def schema_payload(ghost: Ghost, provider: str) -> dict[str, Any]:
    shadows = ghost.shadows
    tables = []
    for name in ghost.schema.dependency_order():
        t = ghost.schema.tables[name]
        cols = []
        for c in t.columns:
            flags = []
            if c.primary_key:
                flags.append("PK")
            if not c.nullable and not c.primary_key:
                flags.append("NOT NULL")
            if c.unique and not c.primary_key:
                flags.append("UNIQUE")
            for fk in t.foreign_keys:
                if c.name in fk.columns:
                    flags.append(f"FK {fk.ref_table}")
            if c.check_values or c.check_min is not None or c.check_max is not None:
                flags.append("CHECK")
            cols.append({"name": c.name, "type": c.type, "flags": flags})
        tables.append(
            {
                "name": name,
                "rows": ghost.profile.row_count(name),
                "shadow_rows": shadows.info.rows.get(name, 0),
                "columns": cols,
            }
        )
    return {
        "source": ghost.source.label,
        "dialect": ghost.dialect,
        "provider": provider,
        "seed": shadows.info.seed,
        "tables": tables,
        "examples": EXAMPLES,
    }


def make_handler(ghost: Ghost, provider: str) -> type[BaseHTTPRequestHandler]:
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "sqlghost"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter console
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any, status: int = 200) -> None:
            self._send(status, json.dumps(data, default=str).encode(), "application/json")

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/schema":
                with lock:
                    self._json(schema_payload(ghost, provider))
            elif self.path == "/api/profile":
                self._json(ghost.profile.to_dict())
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            length = min(int(self.headers.get("Content-Length", 0)), 100_000)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json({"error": "invalid JSON"}, 400)
                return
            try:
                with lock:
                    if self.path == "/api/ask":
                        res = ghost.ask(
                            str(body.get("question", "")),
                            provider=body.get("provider") or provider,
                            run=bool(body.get("run", True)),
                        )
                        self._json(res.to_dict())
                    elif self.path == "/api/validate":
                        report = ghost.validate(str(body.get("sql", "")))
                        data = {
                            "sql": report.sql,
                            "report": report.to_dict(),
                            "result": None,
                            "error": None,
                            "engine": "manual",
                        }
                        if report.passed and body.get("run", True):
                            try:
                                data["result"] = ghost.run_real(report).to_dict()
                            except SourceError as exc:
                                data["error"] = str(exc)
                        self._json(data)
                    else:
                        self._send(404, b"not found", "text/plain")
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    return Handler


def make_server(ghost: Ghost, host: str = "127.0.0.1", port: int = 8765, provider: str = "auto") -> ThreadingHTTPServer:
    ghost.shadows  # build before the first request  # noqa: B018
    return ThreadingHTTPServer((host, port), make_handler(ghost, provider))


def serve(ghost: Ghost, host: str = "127.0.0.1", port: int = 8765, provider: str = "auto") -> None:
    httpd = make_server(ghost, host, port, provider)
    print(f"sql-ghost UI on http://{host}:{httpd.server_address[1]}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
