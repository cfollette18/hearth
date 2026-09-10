"""Workspace tools for the hearth coding agent."""

from __future__ import annotations

import difflib
import json
import os
import subprocess
from pathlib import Path

MAX_READ = 80_000
MAX_OUT = 12_000
MAX_BASH_SEC = 30

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a text file. Always show the user what you wrote.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact substring in a file with new text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files in a workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory, default ."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents in the workspace (regex).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "Relative file or directory"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the workspace. No network.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    },
]

META = {
    "read_file": {"verb": "Read", "calling": "Reading", "icon": "read"},
    "write_file": {"verb": "Wrote", "calling": "Writing", "icon": "write"},
    "edit_file": {"verb": "Edited", "calling": "Editing", "icon": "write"},
    "list_dir": {"verb": "Listed", "calling": "Listing", "icon": "list"},
    "grep": {"verb": "Searched", "calling": "Searching", "icon": "search"},
    "bash": {"verb": "Ran", "calling": "Running", "icon": "run"},
}


def _clip(text: str, n: int = MAX_OUT) -> str:
    if len(text) <= n:
        return text
    return text[:n] + "\n… truncated …"


def resolve(root: Path, rel: str) -> Path:
    root = root.resolve()
    path = (root / (rel or ".")).resolve()
    if path != root and root not in path.parents:
        raise ValueError("path escapes workspace")
    return path


def unified_diff(path: str, before: str, after: str) -> str:
    a = before.splitlines(keepends=True)
    b = after.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(a, b, fromfile="a/" + path, tofile="b/" + path, n=3)
    )


def run_tool(root: Path, name: str, args: dict) -> dict:
    """Return {ok, summary, payload, preview, target}."""
    target = str(args.get("path") or args.get("command") or args.get("pattern") or "")
    try:
        if name == "read_file":
            path = resolve(root, args["path"])
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
            shown = _clip(text, MAX_READ)
            return {
                "ok": True,
                "target": str(path.relative_to(root)),
                "summary": f"{lines} lines",
                "payload": shown,
                "preview": shown if len(shown) < 4000 else shown[:4000] + "\n…",
            }
        if name == "write_file":
            path = resolve(root, args["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            before = path.read_text(encoding="utf-8") if path.exists() else ""
            after = args.get("content") or ""
            path.write_text(after, encoding="utf-8")
            diff = unified_diff(str(path.relative_to(root)), before, after)
            return {
                "ok": True,
                "target": str(path.relative_to(root)),
                "summary": f"{after.count(chr(10)) + 1} lines written",
                "payload": "wrote " + str(path.relative_to(root)),
                "preview": diff or after[:4000],
            }
        if name == "edit_file":
            path = resolve(root, args["path"])
            before = path.read_text(encoding="utf-8")
            old, new = args.get("old_string") or "", args.get("new_string") or ""
            if old not in before:
                return {
                    "ok": False,
                    "target": str(path.relative_to(root)),
                    "summary": "old_string not found",
                    "payload": "old_string not found",
                    "preview": "",
                }
            after = before.replace(old, new, 1)
            path.write_text(after, encoding="utf-8")
            diff = unified_diff(str(path.relative_to(root)), before, after)
            return {
                "ok": True,
                "target": str(path.relative_to(root)),
                "summary": "1 replacement",
                "payload": "edited " + str(path.relative_to(root)),
                "preview": diff,
            }
        if name == "list_dir":
            path = resolve(root, args.get("path") or ".")
            if not path.is_dir():
                raise ValueError("not a directory")
            names = []
            for child in sorted(path.iterdir())[:200]:
                names.append(child.name + ("/" if child.is_dir() else ""))
            listing = "\n".join(names) or "(empty)"
            return {
                "ok": True,
                "target": str(path.relative_to(root)) or ".",
                "summary": f"{len(names)} entries",
                "payload": listing,
                "preview": listing,
            }
        if name == "grep":
            path = resolve(root, args.get("path") or ".")
            pattern = args["pattern"]
            cmd = ["grep", "-RIn", "-E", "--exclude-dir=.git", pattern]
            if path.is_file():
                cmd.append(str(path))
            else:
                cmd.append(str(path))
            proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=20)
            out = proc.stdout or proc.stderr or "(no matches)"
            hits = 0 if out.startswith("(no") else out.count("\n")
            return {
                "ok": True,
                "target": pattern,
                "summary": f"{hits} hits",
                "payload": _clip(out),
                "preview": _clip(out, 3000),
            }
        if name == "bash":
            command = args["command"]
            proc = subprocess.run(
                command,
                shell=True,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=MAX_BASH_SEC,
                env={**os.environ, "PWD": str(root)},
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            out = _clip(out.strip() or f"(exit {proc.returncode})")
            return {
                "ok": proc.returncode == 0,
                "target": command,
                "summary": f"exit {proc.returncode}",
                "payload": out,
                "preview": out[:4000],
            }
        return {
            "ok": False,
            "target": target,
            "summary": "unknown tool",
            "payload": "unknown tool " + name,
            "preview": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "target": target,
            "summary": str(exc)[:120],
            "payload": str(exc),
            "preview": "",
        }


def parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}
