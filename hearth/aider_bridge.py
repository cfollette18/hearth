"""Drive Aider from the Quinovo-look chat UI."""

from __future__ import annotations

import os
import re
import subprocess
import threading
import uuid
from pathlib import Path

import aider
from aider.coders import Coder
from aider.io import InputOutput
from aider.models import Model
from aider.repo import GitRepo

from hearth.tools import unified_diff

SKIP_PARTS = {".git", "__pycache__", ".aider.tags.cache.v4"}
SKIP_PREFIXES = (".aider",)
MAX_PREVIEW = 8000
ADDED_RE = re.compile(r"^Added (.+) to the chat")
APPLIED_RE = re.compile(r"^Applied edit to (.+)$")

os.environ.setdefault("NO_COLOR", "1")
os.environ.setdefault("AIDER_ANALYTICS", "false")


def configure_openai(base: str, key: str = "local") -> None:
    os.environ["OPENAI_API_BASE"] = base
    os.environ["OPENAI_API_KEY"] = key


def ensure_git(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if (root / ".git").is_dir():
        return
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=hearth@local",
            "-c",
            "user.name=hearth",
            "commit",
            "--allow-empty",
            "-m",
            "hearth workspace",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def snapshot(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        if any(path.name.startswith(p) for p in SKIP_PREFIXES):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        rel = str(path.relative_to(root))
        files[rel] = data.decode("utf-8", errors="replace")
    return files


def prose_from_aider(content: str) -> str:
    text = (content or "").strip()
    if "```" in text:
        text = text.split("```", 1)[0].strip()
    return text


def aider_version() -> str:
    return getattr(aider, "__version__", "unknown")


def _tool(
    tid: str,
    name: str,
    verb: str,
    icon: str,
    target: str,
    status: str,
    calling: str = "",
    summary: str = "",
    preview: str = "",
) -> dict:
    return {
        "type": "tool",
        "id": tid,
        "name": name,
        "verb": verb,
        "calling": calling,
        "icon": icon,
        "target": target,
        "status": status,
        "summary": summary,
        "preview": preview[:MAX_PREVIEW],
    }


class HearthIO(InputOutput):
    """Aider IO that forwards the tool log into Quinovo SSE cards."""

    def __init__(self, workspace: Path, emit):
        self._emit = emit
        self.workspace = workspace
        self.live_id = "aider-" + uuid.uuid4().hex[:8]
        self.live_log: list[str] = []
        self.added: list[str] = []
        self.applied: list[str] = []
        super().__init__(
            pretty=False,
            yes=True,
            fancy_input=False,
            encoding="utf-8",
            root=str(workspace),
        )

    def reset_turn(self, emit) -> None:
        self._emit = emit
        self.live_id = "aider-" + uuid.uuid4().hex[:8]
        self.live_log = []
        self.added = []
        self.applied = []

    def _on_tool_line(self, text: str) -> None:
        self.live_log.append(text)
        added = ADDED_RE.match(text)
        applied = APPLIED_RE.match(text)
        if added:
            self.added.append(added.group(1).strip())
        if applied:
            self.applied.append(applied.group(1).strip())
        self._emit(
            _tool(
                self.live_id,
                "aider",
                "Aider",
                "call",
                "workspace",
                "running",
                calling="Aider is working",
                summary=text[:120],
                preview="\n".join(self.live_log[-50:]),
            )
        )

    def tool_output(self, *messages, log_only=False, bold=False):
        text = " ".join(str(m) for m in messages).strip()
        if text and not log_only:
            self._on_tool_line(text)
        try:
            super().tool_output(*messages, log_only=log_only, bold=bold)
        except Exception:
            pass

    def tool_error(self, message="", strip=True):
        text = str(message or "").strip()
        if text:
            self._on_tool_line(text)
        try:
            super().tool_error(message, strip=strip)
        except Exception:
            pass

    def assistant_output(self, message, pretty=None):
        return

    def finish_live(self, summary: str) -> dict:
        card = _tool(
            self.live_id,
            "aider",
            "Aider",
            "call",
            "workspace",
            "done",
            summary=summary,
            preview="\n".join(self.live_log[-50:]),
        )
        self._emit(card)
        return card


class SessionAider:
    def __init__(self, workspace: Path, llama_host: str, llama_port: int, model: str):
        self.workspace = workspace.resolve()
        self.lock = threading.Lock()
        configure_openai(f"http://{llama_host}:{llama_port}/v1")
        ensure_git(self.workspace)
        self.io = HearthIO(self.workspace, lambda _event: None)
        git_repo = GitRepo(self.io, [], str(self.workspace))
        llm = Model("openai/" + model)
        llm.edit_format = "whole"
        if not llm.info:
            llm.info = {}
        llm.info.setdefault("max_input_tokens", 8192)
        self.coder = Coder.create(
            main_model=llm,
            edit_format="whole",
            io=self.io,
            repo=git_repo,
            fnames=[],
            auto_commits=False,
            dirty_commits=False,
            auto_lint=False,
            stream=False,
            map_tokens=1024,
            suggest_shell_commands=True,
            detect_urls=False,
        )

    def rel_of(self, path: str) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.workspace))
        except ValueError:
            return path

    def run(self, user_text: str, emit) -> tuple[str, list[dict]]:
        self.io.reset_turn(emit)
        self.coder.io = self.io
        before = snapshot(self.workspace)
        emit(
            _tool(
                self.io.live_id,
                "aider",
                "Aider",
                "call",
                "workspace",
                "running",
                calling="Aider is working",
                summary=self.coder.main_model.name,
            )
        )
        tools: list[dict] = []
        try:
            self.coder.run(with_message=user_text, preproc=True)
        except Exception as exc:
            self.io.finish_live(str(exc)[:120])
            raise
        after = snapshot(self.workspace)

        for rel in self.io.added:
            tid = uuid.uuid4().hex[:10]
            card = _tool(tid, "read_file", "Read", "read", rel, "done", summary="in chat")
            emit(card)
            tools.append(card)

        changed: list[str] = []
        for rel, new in after.items():
            if before.get(rel, "") != new:
                changed.append(rel)
        for rel in before:
            if rel not in after:
                changed.append(rel)
        edited = {self.rel_of(p) for p in (self.io.applied + list(self.coder.aider_edited_files or []))}
        seen_files: set[str] = set()
        for rel in sorted(set(changed) | edited):
            if rel in seen_files:
                continue
            seen_files.add(rel)
            old = before.get(rel, "")
            new = after.get(rel, "")
            diff = unified_diff(rel, old, new)
            if rel not in before:
                verb = "Wrote"
            elif rel not in after:
                verb = "Deleted"
            else:
                verb = "Edited"
            summary = f"{new.count(chr(10)) + (1 if new else 0)} lines" if new else "removed"
            tid = uuid.uuid4().hex[:10]
            card = _tool(
                tid,
                "write_file",
                verb,
                "write",
                rel,
                "done",
                summary=summary,
                preview=diff or new[:MAX_PREVIEW],
            )
            emit(card)
            tools.append(card)

        for cmd in list(getattr(self.coder, "shell_commands", None) or []):
            tid = uuid.uuid4().hex[:10]
            card = _tool(tid, "bash", "Ran", "run", str(cmd), "done", summary="shell")
            emit(card)
            tools.append(card)

        raw = self.coder.partial_response_content or ""
        text = prose_from_aider(raw)
        if not text:
            if seen_files:
                text = "Applied edits to " + ", ".join(sorted(seen_files)) + "."
            else:
                text = raw.strip() or "Aider finished without a text reply."
        emit({"type": "text", "text": text})
        n_edits = len(seen_files)
        summary = f"{n_edits} file{'s' if n_edits != 1 else ''} changed" if n_edits else "done"
        live = self.io.finish_live(summary)
        tools.insert(0, live)
        return text, tools


_sessions: dict[str, SessionAider] = {}
_sessions_lock = threading.Lock()


def get_session(
    sid: str, workspace: Path, llama_host: str, llama_port: int, model: str
) -> SessionAider:
    with _sessions_lock:
        session = _sessions.get(sid)
        if session is None:
            session = SessionAider(workspace, llama_host, llama_port, model)
            _sessions[sid] = session
        return session
