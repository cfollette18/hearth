"""Drive Aider from the Quinovo-look chat UI."""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
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
STREAM_MS = 0.08
EDIT_RE = re.compile(
    r"(?i)\b(write|create|edit|change|fix|implement|delete|update|replace|rename|"
    r"remove|append|modify|refactor)\b|\badd a\b|\badd the\b|\bmake a\b|\bmake an\b"
)
QUESTION_RE = re.compile(
    r"(?i)^\s*(what|why|how|who|where|which|explain|list|tell me|can you|could you|"
    r"would you|is there|are there)\b|[?]\s*$"
)
LIST_RE = re.compile(
    r"(?i)\b(list (the )?files|what'?s in (the )?(workspace|repo|dir|folder)|show (me )?(the )?files)\b"
)
FILE_RE = re.compile(r"[\w./-]+\.[A-Za-z0-9]+")
NO_EDIT_HINT = (
    "\n\nDo not output file listings and do not change any files. "
    "Answer the question in prose only."
)

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


def filenames_in_text(text: str) -> set[str]:
    return {m.lower() for m in FILE_RE.findall(text or "")}


def user_wants_edits(text: str) -> bool:
    named = filenames_in_text(text)
    if QUESTION_RE.search(text or ""):
        return bool(EDIT_RE.search(text or "") and named)
    return bool(EDIT_RE.search(text or "") or named)


def clip(text: str, n: int = MAX_PREVIEW) -> str:
    if len(text) <= n:
        return text
    return text[:n] + "\n… truncated …"


def clean_fname(raw: str) -> str:
    fname = (raw or "").strip()
    fname = fname.strip("*")
    fname = fname.rstrip(":")
    fname = fname.strip("`")
    fname = fname.lstrip("#").strip()
    if len(fname) > 250:
        return ""
    return fname


def parse_stream_files(content: str) -> tuple[str, list[tuple[str, str, bool]]]:
    """Split whole-file output into prose and (filename, body, closed) blocks."""
    lines = (content or "").splitlines(keepends=True)
    prose: list[str] = []
    files: list[tuple[str, str, bool]] = []
    fname: str | None = None
    body: list[str] = []
    for i, line in enumerate(lines):
        if line.startswith("```"):
            if fname is not None:
                files.append((fname, "".join(body), True))
                fname = None
                body = []
                continue
            prev = clean_fname(lines[i - 1]) if i else ""
            if prev and prose and prose[-1].strip() == prev:
                prose.pop()
            fname = prev or "(file)"
            body = []
            continue
        if fname is not None:
            body.append(line)
        else:
            prose.append(line)
    if fname is not None:
        files.append((fname, "".join(body), False))
    return "".join(prose).strip(), files


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
        "preview": clip(preview),
    }


class TurnUI:
    """Quinovo SSE cards for one Aider turn."""

    def __init__(self, emit, workspace: Path, before: dict[str, str]):
        self.emit = emit
        self.workspace = workspace
        self.before = before
        self.order: list[str] = []
        self.cards: dict[str, dict] = {}
        self.seen_reads: set[str] = set()
        self.prose = ""
        self.last_stream = 0.0
        self.allow_edits = True
        self.named_files: set[str] = set()

    def put(self, card: dict) -> dict:
        tid = card["id"]
        if tid not in self.cards:
            self.order.append(tid)
        self.cards[tid] = card
        self.emit(card)
        return card

    def tools(self) -> list[dict]:
        return [self.cards[tid] for tid in self.order if tid in self.cards]

    def list_workspace(self) -> None:
        names = []
        for path in sorted(self.workspace.rglob("*")):
            if not path.is_file() and not path.is_dir():
                continue
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            if any(path.name.startswith(p) for p in SKIP_PREFIXES):
                continue
            rel = str(path.relative_to(self.workspace))
            if rel == ".":
                continue
            names.append(rel + ("/" if path.is_dir() else ""))
        listing = "\n".join(names) or "(empty)"
        self.put(
            _tool(
                "list:.",
                "list_dir",
                "Listed",
                "list",
                ".",
                "done",
                summary=f"{len(names)} entries",
                preview=listing,
            )
        )

    def read_file(self, rel: str, reason: str = "") -> None:
        rel = rel.strip()
        if not rel or rel in self.seen_reads:
            return
        self.seen_reads.add(rel)
        tid = "read:" + rel
        path = self.workspace / rel
        self.put(
            _tool(
                tid,
                "read_file",
                "Read",
                "read",
                rel,
                "running",
                calling="Reading",
                summary=reason or "file",
            )
        )
        preview = ""
        summary = "missing"
        if path.is_file():
            try:
                preview = path.read_text(encoding="utf-8", errors="replace")
                lines = preview.count("\n") + (0 if preview.endswith("\n") or not preview else 1)
                summary = f"{lines} lines" + (f" · {reason}" if reason else "")
            except OSError as exc:
                summary = str(exc)[:80]
        self.put(
            _tool(
                tid,
                "read_file",
                "Read",
                "read",
                rel,
                "done",
                summary=summary,
                preview=preview,
            )
        )

    def write_live(self, rel: str, new_text: str, closed: bool) -> None:
        if self.named_files:
            key = rel.lower()
            base = Path(rel).name.lower()
            if key not in self.named_files and base not in self.named_files:
                return
        if rel not in self.seen_reads and (self.workspace / rel).is_file():
            self.read_file(rel)
        tid = "write:" + rel
        old = self.before.get(rel, "")
        if rel not in self.before and not new_text:
            preview = ""
        else:
            preview = unified_diff(rel, old, new_text) or new_text
        lines = new_text.count("\n") + (1 if new_text and not new_text.endswith("\n") else 0)
        if closed:
            verb = "Wrote" if rel not in self.before else "Edited"
            self.put(
                _tool(
                    tid,
                    "write_file",
                    verb,
                    "write",
                    rel,
                    "running",
                    calling="Writing",
                    summary=f"{lines} lines",
                    preview=preview,
                )
            )
            return
        self.put(
            _tool(
                tid,
                "write_file",
                "Edited",
                "write",
                rel,
                "running",
                calling="Writing",
                summary="streaming",
                preview=preview,
            )
        )

    def write_done(self, rel: str, after: dict[str, str]) -> None:
        tid = "write:" + rel
        old = self.before.get(rel, "")
        new = after.get(rel, "")
        diff = unified_diff(rel, old, new)
        if rel not in self.before:
            verb = "Wrote"
        elif rel not in after:
            verb = "Deleted"
        else:
            verb = "Edited"
        summary = f"{new.count(chr(10)) + (1 if new else 0)} lines" if new else "removed"
        self.put(
            _tool(
                tid,
                "write_file",
                verb,
                "write",
                rel,
                "done",
                summary=summary,
                preview=diff or new,
            )
        )

    def run_cmd(self, command: str) -> None:
        tid = "run:" + uuid.uuid4().hex[:8]
        self.put(
            _tool(
                tid,
                "bash",
                "Ran",
                "run",
                str(command),
                "done",
                summary="shell",
            )
        )

    def on_stream(self, raw: str, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self.last_stream) < STREAM_MS:
            return
        self.last_stream = now
        prose, files = parse_stream_files(raw)
        committed = prose if files else (prose.rsplit("\n", 1)[0] if "\n" in prose else "")
        if len(committed) > len(self.prose):
            delta = committed[len(self.prose) :]
            self.prose = committed
            if delta.strip():
                self.emit({"type": "text", "text": delta})
        else:
            self.prose = committed
        for fname, body, closed in files:
            if not fname or fname == "(file)":
                continue
            if not self.allow_edits:
                continue
            self.write_live(fname, body, closed)


class HearthIO(InputOutput):
    """Aider IO that forwards file add/apply into Quinovo cards."""

    def __init__(self, workspace: Path, ui: TurnUI | None):
        self.ui = ui
        super().__init__(
            pretty=False,
            yes=True,
            fancy_input=False,
            encoding="utf-8",
            root=str(workspace),
        )

    def tool_output(self, *messages, log_only=False, bold=False):
        text = " ".join(str(m) for m in messages).strip()
        if text and not log_only and self.ui:
            added = ADDED_RE.match(text)
            applied = APPLIED_RE.match(text)
            if added:
                self.ui.read_file(added.group(1).strip())
            elif applied:
                pass
            elif text.startswith("Repo-map:"):
                pass
        try:
            super().tool_output(*messages, log_only=log_only, bold=bold)
        except Exception:
            pass

    def tool_error(self, message="", strip=True):
        try:
            super().tool_error(message, strip=strip)
        except Exception:
            pass

    def assistant_output(self, message, pretty=None):
        return


class SessionAider:
    def __init__(self, workspace: Path, llama_host: str, llama_port: int, model: str):
        self.workspace = workspace.resolve()
        self.lock = threading.Lock()
        configure_openai(f"http://{llama_host}:{llama_port}/v1")
        ensure_git(self.workspace)
        self.ui = TurnUI(lambda _event: None, self.workspace, {})
        self.io = HearthIO(self.workspace, self.ui)
        git_repo = GitRepo(self.io, [], str(self.workspace))
        llm = Model("openai/" + model)
        llm.edit_format = "whole"
        llm.streaming = True
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
            stream=True,
            map_tokens=0,
            suggest_shell_commands=True,
            detect_urls=False,
        )
        self.coder.stream = True
        self.coder.gpt_prompts.example_messages = []
        self.coder.gpt_prompts.main_system = (
            "Act as an expert software developer.\n"
            "Answer questions in prose.\n"
            "Only output a file listing if the user asked you to create or edit a file.\n"
            "If they asked a question, do not modify files.\n"
            "{final_reminders}\n"
        )
        self._mention_files = False
        self._apply_edits = False
        self._named_files: set[str] = set()
        self._patch_coder()

    def _patch_coder(self) -> None:
        coder = self.coder
        orig_add = coder.add_rel_fname
        orig_stream = coder.show_send_output_stream
        orig_show = coder.show_send_output
        orig_apply = coder.apply_updates
        orig_shell = coder.run_shell_commands
        orig_check = coder.check_for_file_mentions
        orig_preproc = coder.preproc_user_input

        def add_rel_fname(rel_fname):
            orig_add(rel_fname)
            self.ui.read_file(rel_fname)

        def show_send_output_stream(completion):
            for text in orig_stream(completion):
                self.ui.on_stream(coder.partial_response_content or "")
                yield text
            self.ui.on_stream(coder.partial_response_content or "", force=True)

        def show_send_output(completion):
            orig_show(completion)
            self.ui.on_stream(coder.partial_response_content or "", force=True)

        def check_for_file_mentions(content):
            if not self._mention_files:
                return None
            return orig_check(content)

        def preproc_user_input(inp):
            self._mention_files = True
            try:
                return orig_preproc(inp)
            finally:
                self._mention_files = False

        def apply_updates():
            if not self._apply_edits:
                return set()
            edited = orig_apply()
            self._revert_unasked_edits()
            return edited

        def run_shell_commands():
            pending = list(getattr(coder, "shell_commands", None) or [])
            result = orig_shell()
            for cmd in pending:
                self.ui.run_cmd(cmd)
            return result

        coder.add_rel_fname = add_rel_fname
        coder.show_send_output_stream = show_send_output_stream
        coder.show_send_output = show_send_output
        coder.apply_updates = apply_updates
        coder.run_shell_commands = run_shell_commands
        coder.check_for_file_mentions = check_for_file_mentions
        coder.preproc_user_input = preproc_user_input

    def rel_of(self, path: str) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.workspace))
        except ValueError:
            return path

    def _drop_chat_files(self) -> None:
        self.coder.abs_fnames.clear()

    def _revert_unasked_edits(self) -> None:
        named = self._named_files
        if not named:
            return
        after = snapshot(self.workspace)
        for rel, new in after.items():
            old = self.ui.before.get(rel, "")
            if new == old:
                continue
            key = rel.lower()
            base = Path(rel).name.lower()
            if key in named or base in named:
                continue
            path = self.workspace / rel
            if rel in self.ui.before:
                path.write_text(old, encoding="utf-8")
            elif path.is_file():
                path.unlink()

    def run(self, user_text: str, emit) -> tuple[str, list[dict]]:
        before = snapshot(self.workspace)
        self._drop_chat_files()
        wants = user_wants_edits(user_text)
        self._apply_edits = wants
        self._named_files = filenames_in_text(user_text)
        message = user_text if wants else (user_text + NO_EDIT_HINT)
        self.ui = TurnUI(emit, self.workspace, before)
        self.ui.allow_edits = wants
        self.ui.named_files = self._named_files
        self.io.ui = self.ui
        self.coder.io = self.io
        if LIST_RE.search(user_text):
            self.ui.list_workspace()
        self.coder.run(with_message=message, preproc=True)
        text = self.ui.prose or parse_stream_files(self.coder.partial_response_content or "")[0]
        after = snapshot(self.workspace)
        changed = set()
        for rel, new in after.items():
            if before.get(rel, "") != new:
                changed.add(rel)
        for rel in before:
            if rel not in after:
                changed.add(rel)
        for rel in sorted(changed):
            self.ui.write_done(rel, after)
        if not text:
            if changed:
                text = "Applied edits to " + ", ".join(sorted(changed)) + "."
            else:
                text = (self.coder.partial_response_content or "").strip() or "Aider finished without a text reply."
            emit({"type": "text", "text": text})
        return text, self.ui.tools()


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
