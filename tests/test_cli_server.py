from __future__ import annotations

import json
import threading
import urllib.request

from sqlghost.cli import main
from sqlghost.server import make_server


def test_cli_validate_codes(capsys, monkeypatch, cache_dir):
    monkeypatch.setenv("SQLGHOST_CACHE", str(cache_dir))
    monkeypatch.setenv("NO_COLOR", "1")
    assert main(["validate", "SELECT COUNT(*) FROM plazas"]) == 0
    assert "APPROVED" in capsys.readouterr().out
    assert main(["validate", "DROP TABLE plazas"]) == 1
    assert "BLOCKED" in capsys.readouterr().out


def test_cli_ask_json(capsys, monkeypatch, cache_dir):
    monkeypatch.setenv("SQLGHOST_CACHE", str(cache_dir))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert main(["ask", "--json", "Top 3 plazas by number of trips"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["engine"] == "rules"
    assert len(data["result"]["rows"]) == 3


def test_web_api(ghost):
    server = make_server(ghost, port=0, provider="rules")
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "sql-ghost" in page
        schema = json.loads(urllib.request.urlopen(base + "/api/schema").read())
        assert {t["name"] for t in schema["tables"]} >= {"trips", "plazas"}
        req = urllib.request.Request(
            base + "/api/validate",
            method="POST",
            data=json.dumps({"sql": "SELECT 1; DROP TABLE trips"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        data = json.loads(urllib.request.urlopen(req).read())
        assert data["report"]["passed"] is False
        assert data["result"] is None
    finally:
        server.shutdown()
        server.server_close()
