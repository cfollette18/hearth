#!/usr/bin/env python3
"""Hearth: Quinovo-look chat that is Aider, on local llama-server."""

from __future__ import annotations

import json
import os
import traceback
import uuid
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from hearth.aider_bridge import aider_version, get_session

LISTEN_HOST = os.environ.get("HEARTH_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("HEARTH_PORT", "8090"))
LLAMA_HOST = os.environ.get("HEARTH_LLAMA_HOST", "127.0.0.1")
LLAMA_PORT = int(os.environ.get("HEARTH_LLAMA_PORT", "8082"))
MODEL = os.environ.get("HEARTH_MODEL", "qwen")
WORKSPACE = Path(os.environ.get("HEARTH_WORKSPACE", str(Path.home() / "hearth-workspace")))
STATIC = Path(__file__).with_name("static")
SESSIONS = Path(os.environ.get("HEARTH_SESSIONS", str(Path.home() / ".local/share/hearth/sessions")))


def _ensure_dirs() -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    SESSIONS.mkdir(parents=True, exist_ok=True)


def _session_path(sid: str) -> Path:
    return SESSIONS / f"{sid}.json"


def load_session(sid: str) -> dict:
    path = _session_path(sid)
    if not path.is_file():
        return {"id": sid, "title": "New chat", "messages": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_session(data: dict) -> None:
    _session_path(data["id"]).write_text(json.dumps(data, indent=2), encoding="utf-8")


def list_sessions() -> list:
    rows = []
    for path in sorted(SESSIONS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        rows.append({"id": data.get("id"), "title": data.get("title") or "Chat"})
    return rows[:80]


def run_agent(user_text: str, session: dict, emit) -> None:
    history = session.setdefault("messages", [])
    history.append({"role": "user", "content": user_text, "tools": []})
    if session.get("title") in ("", "New chat"):
        session["title"] = user_text[:42]
        emit({"type": "session", "id": session["id"], "title": session["title"]})

    assistant_ui = {"role": "assistant", "content": "", "tools": []}
    history.append(assistant_ui)

    aider = get_session(session["id"], WORKSPACE, LLAMA_HOST, LLAMA_PORT, MODEL)
    with aider.lock:
        text, tools = aider.run(user_text, emit)
    assistant_ui["content"] = text
    assistant_ui["tools"] = tools
    save_session(session)
    emit({"type": "done", "text": text})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._file(STATIC / "index.html", "text/html; charset=utf-8")
        elif path == "/api/health":
            self._proxy_health()
        elif path == "/chat/sessions":
            self._json(200, {"sessions": list_sessions()})
        elif path.startswith("/chat/sessions/"):
            sid = path.split("/")[-1]
            self._json(200, load_session(sid))
        elif path == "/settings.json":
            self._json(
                200,
                {
                    "ready": True,
                    "harness": "aider",
                    "aider": aider_version(),
                    "model": MODEL,
                    "workspace": str(WORKSPACE),
                },
            )
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/chat/stream":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"})
            return
        text = str(body.get("text") or "").strip()
        sid = str(body.get("session_id") or "") or uuid.uuid4().hex[:12]
        session = load_session(sid)
        session["id"] = sid
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(event: dict) -> None:
            payload = "data: " + json.dumps(event) + "\n\n"
            try:
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
            except BrokenPipeError:
                return

        emit({"type": "session", "id": sid, "title": session.get("title") or "New chat"})
        try:
            run_agent(text, session, emit)
        except Exception as exc:
            traceback.print_exc()
            emit({"type": "error", "message": str(exc)[:400]})
            emit({"type": "done", "text": ""})

    def _file(self, path: Path, ctype: str) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _proxy_health(self) -> None:
        conn = HTTPConnection(LLAMA_HOST, LLAMA_PORT, timeout=3)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            body = resp.read()
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.getheader("Content-Type") or "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self._json(503, {"status": "llama down"})
        finally:
            conn.close()


def main() -> None:
    _ensure_dirs()
    print(
        f"hearth chat http://{LISTEN_HOST}:{LISTEN_PORT} "
        f"harness=aider/{aider_version()} workspace={WORKSPACE}",
        flush=True,
    )
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
