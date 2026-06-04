#!/usr/bin/env python3
"""
codex_handoff.py

Zero-dependency utility for turning OpenAI Codex CLI session JSONL files into
Markdown handoff documents.

V0.6 is a doc-first research/workspace handoff packer. It treats revised Markdown/README/HANDOFF/LOGIC files as the primary source of truth, uses Codex sessions as supporting evidence, and supports a two-phase GPT workflow for history mapping followed by final reporting.

Designed for shared/remote servers where the user has no sudo, no node, and no npm.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_SESSIONS_DIR = Path("~/.codex/sessions").expanduser()

DEFAULT_HISTORY_KEYWORDS = ",".join([
    "v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10",
    "v11", "v12", "v13", "v14", "v15", "v16",
    "README", "HANDOFF", "LOGIC", "audit", "revised", "final",
    "DIA-NN", "UEP", "HLA", "pMHC", "TCR", "circRNA", "BSJ",
    "manuscript", "publication", "validation", "evidence",
])


SECRET_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|authorization|bearer)\s*[:=]\s*['\"]?([A-Za-z0-9._\-+/=]{8,})['\"]?"), r"\1=<REDACTED>"),
    (re.compile(r"(?i)(Authorization:\s*Bearer\s+)[A-Za-z0-9._\-+/=]+"), r"\1<REDACTED>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "<REDACTED_PRIVATE_KEY>"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "sk-<REDACTED>"),
    (re.compile(r"ghp_[A-Za-z0-9_]{20,}"), "ghp_<REDACTED>"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github_pat_<REDACTED>"),
]


@dataclass
class SessionInfo:
    path: Path
    size: int
    mtime: float
    sha12: str
    session_id: str
    cwd: str
    first_user: str
    events: int

    @property
    def mtime_iso(self) -> str:
        return _dt.datetime.fromtimestamp(self.mtime).strftime("%Y-%m-%d %H:%M:%S")

    @property
    def short_name(self) -> str:
        stem = self.path.stem
        if stem.startswith("rollout-"):
            stem = stem[len("rollout-"):]
        return stem or self.sha12


def redact(text: str, enabled: bool = True) -> str:
    if not enabled or not text:
        return text
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def short(text: str, limit: int) -> str:
    if text is None:
        return ""
    text = str(text).strip()
    if limit and len(text) > limit:
        return text[:limit] + f"\n\n...[truncated {len(text) - limit} chars]..."
    return text


def sha12(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def load_jsonl(path: Path, max_events: Optional[int] = None) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if max_events is not None and i >= max_events:
                break
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                events.append(obj)
    return events


def walk(obj: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk(item)


def extract_text(value: Any) -> str:
    """Best-effort extraction from OpenAI-style content structures."""
    if value is None:
        return ""

    if isinstance(value, str):
        return value

    if isinstance(value, (int, float, bool)):
        return str(value)

    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            t = extract_text(item)
            if t:
                parts.append(t)
        return "\n".join(parts)

    if isinstance(value, dict):
        # OpenAI text content shapes.
        for key in ("text", "output_text", "input_text"):
            if key in value:
                t = extract_text(value.get(key))
                if t:
                    return t

        # Some function output shapes.
        for key in ("output", "stdout", "stderr", "result"):
            if key in value and isinstance(value.get(key), str):
                return value.get(key) or ""

        # Message/content fallback.
        for key in ("content", "message", "summary"):
            if key in value:
                t = extract_text(value.get(key))
                if t:
                    return t

        # Function call arguments can be JSON string.
        if "arguments" in value:
            args = value.get("arguments")
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                    return json.dumps(parsed, ensure_ascii=False, indent=2)
                except Exception:
                    return args
            return json.dumps(args, ensure_ascii=False, indent=2)

    return ""


def safe_json(value: Any, limit: int = 4000) -> str:
    try:
        s = json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        s = str(value)
    return short(s, limit)


def parse_timestamp(event: Dict[str, Any]) -> str:
    for key in ("timestamp", "time", "ts", "created_at"):
        value = event.get(key)
        if value:
            return str(value)
    return ""


def event_type(event: Dict[str, Any]) -> str:
    typ = event.get("type") or event.get("event") or ""
    if not typ and isinstance(event.get("payload"), dict):
        typ = event["payload"].get("type", "")
    return str(typ)


def find_first_key(obj: Any, keys: Sequence[str]) -> Optional[Any]:
    for d in walk(obj):
        for k in keys:
            if k in d and d.get(k) not in (None, ""):
                return d.get(k)
    return None


def detect_role(event: Dict[str, Any]) -> Optional[str]:
    for d in walk(event):
        role = d.get("role")
        if role in {"user", "assistant", "system", "tool", "developer"}:
            return role

    t = event_type(event).lower()
    if "user" in t:
        return "user"
    if "assistant" in t or "agent" in t:
        return "assistant"
    if "tool" in t or "function" in t or "exec" in t or "command" in t:
        return "tool"
    return None


def is_reasoning_like(event: Dict[str, Any]) -> bool:
    """Skip private/low-value reasoning traces by default."""
    t = event_type(event).lower()
    if "reasoning" in t or "analysis" in t or "chain" in t:
        return True
    for d in walk(event):
        typ = str(d.get("type", "")).lower()
        if "reasoning" in typ or "analysis" in typ or "chain" in typ:
            return True
    return False


def detect_message_text(event: Dict[str, Any]) -> str:
    """Best effort extraction of user/assistant message text."""
    # Prefer role-bearing message dictionaries.
    for d in walk(event):
        role = d.get("role")
        if role in {"user", "assistant", "system", "tool", "developer"}:
            for key in ("content", "message", "text", "output"):
                if key in d:
                    t = extract_text(d.get(key))
                    if t:
                        return t

    # OpenAI response item payloads often place item under payload.
    payload = event.get("payload")
    if isinstance(payload, dict):
        for key in ("content", "message", "text", "output"):
            if key in payload:
                t = extract_text(payload.get(key))
                if t:
                    return t

    # Fallback at top level.
    for key in ("content", "message", "text", "output", "summary"):
        if key in event:
            t = extract_text(event.get(key))
            if t:
                return t

    return ""


def detect_function_call(event: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Return (name, arguments) if event looks like a tool/function call."""
    candidates = list(walk(event))
    for d in candidates:
        typ = str(d.get("type", "")).lower()
        has_args = "arguments" in d or "args" in d or "input" in d
        has_name = "name" in d or "tool_name" in d or "function" in d
        if ("function_call" in typ or "tool_call" in typ or ("call" in typ and has_args)) and has_name:
            name = str(d.get("name") or d.get("tool_name") or d.get("function") or "tool")
            args = d.get("arguments", d.get("args", d.get("input", "")))
            return name, extract_text(args) or safe_json(args)

    # Shell-specific shapes.
    for d in candidates:
        for key in ("cmd", "command"):
            if key in d:
                return "shell", extract_text(d.get(key))
    return None


def detect_function_output(event: Dict[str, Any]) -> Optional[str]:
    t = event_type(event).lower()
    if "function_call_output" in t or "tool_output" in t:
        return detect_message_text(event) or safe_json(event, 6000)

    for d in walk(event):
        typ = str(d.get("type", "")).lower()
        if "function_call_output" in typ or "tool_output" in typ:
            txt = extract_text(d.get("output") or d.get("content") or d.get("result"))
            if txt:
                return txt

    return None


def detect_files(event: Dict[str, Any]) -> List[str]:
    files: List[str] = []
    for d in walk(event):
        for key in ("path", "file", "filename", "file_path", "filepath"):
            value = d.get(key)
            if isinstance(value, str) and len(value) < 300:
                # Avoid random words; prefer path-looking values.
                name = Path(value).name
                if "/" in value or "\\" in value or "." in name:
                    files.append(value)
    # Also find common relative paths in text-like fields, conservatively.
    text = detect_message_text(event)
    for match in re.finditer(r"(?<![\w.-])([A-Za-z0-9_./-]+\.(?:py|md|txt|json|jsonl|yaml|yml|toml|sh|csv|tsv|js|ts|tsx|jsx|html|css|ipynb|R|r|m|mat|sql|Dockerfile))(?![\w.-])", text):
        files.append(match.group(1))
    return list(dict.fromkeys(files))



NOISE_PATH_PARTS = {
    "/.codex/skills/",
    "/.cursor/skills",
    "/.vscode-server/",
    "/node_modules/",
    "/__pycache__/",
    "/.git/",
    "/.venv/",
    "/venv/",
}
NOISE_BASENAMES = {
    "SKILL.md",
    "output.txt",
}


def is_noise_path(path: str, include_system_files: bool = False) -> bool:
    if include_system_files:
        return False
    p = path.replace("\\", "/")
    base = Path(p).name
    if base in NOISE_BASENAMES:
        return True
    if p in {"/home", "/home/pcc", "/home/pcc/.codex", "/home/pcc/.git"}:
        return True
    return any(part in p for part in NOISE_PATH_PARTS)


def rank_files(paths: Sequence[str], include_system_files: bool = False) -> List[Tuple[str, int]]:
    counts: Dict[str, int] = {}
    for raw in paths:
        p = str(raw).strip()
        if not p or is_noise_path(p, include_system_files=include_system_files):
            continue
        counts[p] = counts.get(p, 0) + 1

    def score(item: Tuple[str, int]) -> Tuple[int, int, str]:
        p, count = item
        name = Path(p).name
        # Prefer concrete project files over generic names.
        specificity = 0
        if "/" in p:
            specificity += 2
        if name.lower() not in {"readme.md", "index.md"}:
            specificity += 1
        if re.search(r"\.(py|R|r|md|tsv|csv|json|yaml|yml|sh|ipynb)$", name):
            specificity += 1
        return (count, specificity, p)

    return sorted(counts.items(), key=score, reverse=True)


def short_line(text: str, limit: int = 180) -> str:
    text = " ".join(str(text).strip().split())
    if len(text) > limit:
        return text[:limit] + "..."
    return text


@dataclass
class RenderOptions:
    redact: bool = True
    include_raw: bool = False
    include_reasoning: bool = False
    mode: str = "digest"
    max_message_chars: int = 2500
    max_tool_output_chars: int = 800
    max_raw_event_chars: int = 3000
    max_files: int = 80
    max_tool_calls: int = 80
    max_tool_outputs: int = 20
    include_tool_outputs: bool = False
    include_system_files: bool = False


MODE_PRESETS: Dict[str, Dict[str, Any]] = {
    # Best default for ChatGPT handoff input. Keeps signal, drops bulk.
    "digest": {
        "max_message_chars": 2500,
        "max_tool_output_chars": 800,
        "max_files": 80,
        "max_tool_calls": 80,
        "max_tool_outputs": 10,
        "include_tool_outputs": False,
    },
    # Good for a single important session where commands matter.
    "standard": {
        "max_message_chars": 5000,
        "max_tool_output_chars": 1500,
        "max_files": 150,
        "max_tool_calls": 160,
        "max_tool_outputs": 30,
        "include_tool_outputs": True,
    },
    # Near-transcript export. Use as archive, not as the default ChatGPT input.
    "full": {
        "max_message_chars": 12000,
        "max_tool_output_chars": 4000,
        "max_files": 300,
        "max_tool_calls": 500,
        "max_tool_outputs": 200,
        "include_tool_outputs": True,
    },
    # Debug parser/schema issues.
    "debug": {
        "max_message_chars": 12000,
        "max_tool_output_chars": 4000,
        "max_files": 500,
        "max_tool_calls": 800,
        "max_tool_outputs": 300,
        "include_tool_outputs": True,
        "include_raw": True,
    },
}


def apply_mode_preset(options: RenderOptions) -> RenderOptions:
    preset = MODE_PRESETS.get(options.mode, MODE_PRESETS["digest"])
    for key, value in preset.items():
        # Command-line explicit values are applied after preset in command handlers.
        setattr(options, key, value)
    return options


def inspect_session(path: Path, quick: bool = True) -> SessionInfo:
    path = path.expanduser()
    size = path.stat().st_size
    mtime = path.stat().st_mtime
    digest = sha12(path)
    events = load_jsonl(path, max_events=200 if quick else None)

    session_id = None
    cwd = None
    first_user = ""

    for event in events:
        if session_id is None:
            session_id = find_first_key(event, ["session_id", "conversation_id", "id"])
        if cwd is None:
            cwd = find_first_key(event, ["cwd", "working_dir", "current_working_directory", "workdir"])
        if not first_user:
            role = detect_role(event)
            text = detect_message_text(event)
            if role == "user" and text:
                first_user = text.strip().replace("\n", " ")
                if len(first_user) > 120:
                    first_user = first_user[:120] + "..."

    if not session_id:
        session_id = path.stem.replace("rollout-", "") or digest

    return SessionInfo(
        path=path,
        size=size,
        mtime=mtime,
        sha12=digest,
        session_id=str(session_id),
        cwd=str(cwd or ""),
        first_user=first_user,
        events=len(events),
    )


def find_session_files(root: Path = DEFAULT_SESSIONS_DIR) -> List[Path]:
    root = root.expanduser()
    if not root.exists():
        return []
    # Codex normally stores JSONL. Also include extensionless files as fallback.
    files: List[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.name.startswith("."):
            continue
        if p.suffix.lower() in {".jsonl", ".json"} or "rollout" in p.name.lower():
            files.append(p)
    return sorted(files, key=lambda p: p.stat().st_mtime)


def resolve_session(selector: str, root: Path = DEFAULT_SESSIONS_DIR) -> Path:
    selector = selector.strip()
    if selector in {"latest", "last"}:
        files = find_session_files(root)
        if not files:
            raise SystemExit(f"No session files found under {root}")
        return files[-1]

    p = Path(selector).expanduser()
    if p.exists():
        return p

    files = find_session_files(root)
    matches: List[Path] = []
    for f in files:
        if selector in str(f) or selector in f.stem or selector in inspect_session(f).session_id:
            matches.append(f)

    if not matches:
        raise SystemExit(f"No session matched: {selector}")
    if len(matches) > 1:
        msg = "\n".join(f"- {m}" for m in matches[-20:])
        raise SystemExit(f"Multiple sessions matched {selector!r}. Use a longer id/path:\n{msg}")
    return matches[0]



WRAPPER_TEXT_PATTERNS = [
    re.compile(r"<environment_context>.*?</environment_context>", re.S),
    re.compile(r"# Context from my IDE setup.*?(?=\n#|\Z)", re.S),
    re.compile(r"<user_instructions>.*?</user_instructions>", re.S),
]


def is_wrapper_text(text: str) -> bool:
    if not text:
        return True
    s = str(text).strip()
    if not s:
        return True
    low = s.lower()
    if low.startswith("<environment_context>"):
        return True
    if low.startswith("# context from my ide setup"):
        return True
    if "open tabs:" in low and len(s) < 3000:
        return True
    if "we need modify" in low and "tool" in low and "analysis" in low:
        return True
    return False


def clean_message_text_for_handoff(text: str) -> str:
    s = text or ""
    for pat in WRAPPER_TEXT_PATTERNS:
        s = pat.sub("", s)
    s = s.strip()
    return s


def command_args_text(args: str) -> str:
    try:
        obj = json.loads(args)
        if isinstance(obj, dict):
            for key in ("cmd", "command", "script"):
                if key in obj:
                    return str(obj[key])
    except Exception:
        pass
    return args or ""


def command_category(name: str, args: str) -> str:
    blob = (name + "\n" + command_args_text(args)).lower()
    if "apply_patch" in blob or "*** begin patch" in blob or re.search(r"\b(write|create|update|edit|patch)\b", blob):
        return "edit"
    if re.search(r"\b(pytest|unittest|sha256sum|md5sum|diff|cmp|check|validate|verify|sessioninfo|py_compile)\b", blob):
        return "verify"
    if re.search(r"\b(python3?|rscript|bash|sh|snakemake|make|nextflow)\b", blob):
        return "run"
    if re.search(r"\b(error|failed|traceback|exception)\b", blob):
        return "fail"
    if re.search(r"\b(sed|head|tail|cat|less|find|ls|rg|grep|awk|cut|wc)\b", blob):
        return "read"
    return "other"


def is_important_tool_call(name: str, args: str) -> bool:
    return command_category(name, args) in {"edit", "run", "verify", "fail"}


def first_real_turn(turns: Sequence[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
    for ts, txt in turns:
        cleaned = clean_message_text_for_handoff(txt)
        if cleaned and not is_wrapper_text(cleaned):
            return ts, cleaned
    return None


def last_real_turn(turns: Sequence[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
    for ts, txt in reversed(turns):
        cleaned = clean_message_text_for_handoff(txt)
        if cleaned and not is_wrapper_text(cleaned):
            return ts, cleaned
    return None

def render_session_markdown(path: Path, options: RenderOptions) -> str:
    info = inspect_session(path, quick=False)
    events = load_jsonl(path)

    timestamps = [parse_timestamp(e) for e in events if parse_timestamp(e)]
    files_seen: List[str] = []
    messages: List[Tuple[str, str, str]] = []  # timestamp, role, text
    tool_calls: List[Tuple[str, str, str]] = []  # timestamp, name, args
    tool_outputs: List[Tuple[str, str]] = []

    for event in events:
        if is_reasoning_like(event) and not options.include_reasoning:
            continue

        ts = parse_timestamp(event)

        for fp in detect_files(event):
            files_seen.append(fp)

        call = detect_function_call(event)
        if call:
            name, args = call
            args = redact(short(args, options.max_message_chars), options.redact)
            tool_calls.append((ts, name, args))
            continue

        out = detect_function_output(event)
        if out:
            out = redact(short(out, options.max_tool_output_chars), options.redact)
            tool_outputs.append((ts, out))
            continue

        role = detect_role(event)
        text = detect_message_text(event)
        if role and text:
            text = clean_message_text_for_handoff(text)
            if not text or is_wrapper_text(text):
                continue
            text = redact(short(text, options.max_message_chars), options.redact)
            messages.append((ts, role, text))

    files_ranked = rank_files(files_seen, include_system_files=options.include_system_files)

    user_turns = [(ts, txt) for ts, role, txt in messages if role == "user"]
    assistant_turns = [(ts, txt) for ts, role, txt in messages if role == "assistant"]

    md: List[str] = []
    md.append("# Codex Session Export")
    md.append("")
    md.append("## Metadata")
    md.append(f"- Source file: `{info.path}`")
    md.append(f"- Session ID: `{info.session_id}`")
    md.append(f"- SHA256-12: `{info.sha12}`")
    md.append(f"- Size: `{info.size}` bytes")
    md.append(f"- Modified: `{info.mtime_iso}`")
    md.append(f"- Working directory: `{info.cwd or 'unknown'}`")
    if timestamps:
        md.append(f"- Transcript time range: `{timestamps[0]}` → `{timestamps[-1]}`")
    md.append(f"- Parsed JSONL events: `{len(events)}`")
    md.append("")

    md.append("## Handoff Extraction Notes")
    md.append("- This file was generated from a local Codex JSONL session.")
    md.append("- Secret-like values are redacted by default.")
    md.append("- Reasoning/analysis-like events are omitted by default.")
    md.append(f"- Export mode: `{options.mode}`.")
    md.append("- Tool outputs are omitted in `digest` mode unless explicitly enabled.")
    md.append("")

    md.append("## Session Digest")
    md.append(f"- User turns: `{len(user_turns)}`")
    md.append(f"- Assistant turns: `{len(assistant_turns)}`")
    md.append(f"- Tool/command calls: `{len(tool_calls)}`")
    md.append(f"- Tool outputs captured: `{len(tool_outputs)}`")
    first_user = first_real_turn(user_turns)
    last_user = last_real_turn(user_turns)
    last_assistant = last_real_turn(assistant_turns)
    if first_user:
        md.append(f"- First real user request: {short_line(first_user[1])}")
    if last_user:
        md.append(f"- Last real user request: {short_line(last_user[1])}")
    if last_assistant:
        md.append(f"- Last assistant update: {short_line(last_assistant[1])}")
    md.append("")

    if files_ranked:
        md.append("## Important Files Mentioned or Touched")
        for fp, count in files_ranked[: options.max_files]:
            md.append(f"- `{fp}`  _(seen {count}x)_")
        if len(files_ranked) > options.max_files:
            md.append(f"- ... plus {len(files_ranked) - options.max_files} more low-priority/noisy paths")
        md.append("")

    if tool_calls:
        cats: Dict[str, int] = {}
        for _, name, args in tool_calls:
            cat = command_category(name, args)
            cats[cat] = cats.get(cat, 0) + 1
        md.append("## Tool / Command Calls")
        md.append("### Command Summary")
        for cat in ["edit", "run", "verify", "fail", "read", "other"]:
            if cats.get(cat):
                md.append(f"- {cat}: `{cats[cat]}`")
        md.append("")
        selected_calls = [c for c in tool_calls if is_important_tool_call(c[1], c[2])]
        if not selected_calls:
            selected_calls = tool_calls[: min(len(tool_calls), options.max_tool_calls)]
        visible_calls = selected_calls[: options.max_tool_calls]
        md.append("### Important Commands")
        for i, (ts, name, args) in enumerate(visible_calls, 1):
            cat = command_category(name, args)
            title = f"### {i}. {name} [{cat}]"
            if ts:
                title += f" `{ts}`"
            md.append(title)
            md.append("")
            lang = "bash" if name in {"shell", "bash", "terminal"} else "json"
            md.append(f"```{lang}")
            md.append(args)
            md.append("```")
            md.append("")
        omitted = len(tool_calls) - len(visible_calls)
        if omitted > 0:
            md.append(f"_Omitted {omitted} low-priority or additional tool calls. Re-run with `--mode full` for archival detail._")
            md.append("")

    if tool_outputs and options.include_tool_outputs:
        md.append("## Tool Outputs")
        visible_outputs = tool_outputs[: options.max_tool_outputs]
        for i, (ts, out) in enumerate(visible_outputs, 1):
            title = f"### Output {i}"
            if ts:
                title += f" `{ts}`"
            md.append(title)
            md.append("")
            md.append("```text")
            md.append(out)
            md.append("```")
            md.append("")
        if len(tool_outputs) > len(visible_outputs):
            md.append(f"_Omitted {len(tool_outputs) - len(visible_outputs)} additional tool outputs. Re-run with `--mode full` if needed._")
            md.append("")

    md.append("## Conversation Transcript")
    if not messages:
        md.append("_No user/assistant/tool messages were extracted. Use `--include-raw` to inspect schema._")
        md.append("")
    else:
        for i, (ts, role, text) in enumerate(messages, 1):
            title = f"### {i}. {role}"
            if ts:
                title += f" `{ts}`"
            md.append(title)
            md.append("")
            md.append(text)
            md.append("")

    if options.include_raw:
        md.append("## Raw JSONL Events")
        for i, event in enumerate(events, 1):
            md.append(f"### Raw Event {i}")
            md.append("")
            md.append("```json")
            raw = redact(safe_json(event, options.max_raw_event_chars), options.redact)
            md.append(raw)
            md.append("```")
            md.append("")

    md.append("## Suggested ChatGPT Task")
    md.append("")
    md.append("> Read this Codex session export and produce a concise session handoff. Distinguish completed work, failed attempts, still-valid decisions, obsolete decisions, touched files, commands/tests run, open issues, and next steps. Do not invent facts not supported by the transcript.")
    md.append("")
    return "\n".join(md)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def command_scan(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser()
    files = find_session_files(root)
    if args.limit:
        files = files[-args.limit:]

    if not files:
        print(f"No session files found under {root}", file=sys.stderr)
        return 1

    infos = [inspect_session(f) for f in files]
    print(f"Found {len(infos)} session files under {root}")
    print()
    for idx, info in enumerate(reversed(infos), 1):
        print(f"[{idx}] {info.mtime_iso}  {info.size:>9} bytes  {info.short_name}")
        print(f"    path: {info.path}")
        if info.cwd:
            print(f"    cwd : {info.cwd}")
        if info.first_user:
            print(f"    first user: {info.first_user}")
        print()
    return 0


def build_render_options_from_args(args: argparse.Namespace) -> RenderOptions:
    options = RenderOptions(
        redact=not args.no_redact,
        include_raw=args.include_raw,
        include_reasoning=args.include_reasoning,
        mode=args.mode,
    )
    apply_mode_preset(options)

    # User overrides. None means "use preset".
    for attr in (
        "max_message_chars",
        "max_tool_output_chars",
        "max_raw_event_chars",
        "max_files",
        "max_tool_calls",
        "max_tool_outputs",
    ):
        value = getattr(args, attr, None)
        if value is not None:
            setattr(options, attr, value)

    if args.include_tool_outputs:
        options.include_tool_outputs = True
    if args.include_system_files:
        options.include_system_files = True
    if args.include_raw:
        options.include_raw = True
    return options


def command_convert(args: argparse.Namespace) -> int:
    path = resolve_session(args.session, Path(args.root).expanduser())
    options = build_render_options_from_args(args)
    md = render_session_markdown(path, options)
    if args.out:
        out = Path(args.out).expanduser()
        write_text(out, md)
        print(f"Wrote {out} ({len(md.encode('utf-8'))} bytes)")
    else:
        print(md)
    return 0


def render_outline_markdown(path: Path, options: RenderOptions) -> str:
    info = inspect_session(path, quick=False)
    events = load_jsonl(path)
    rows: List[Tuple[str, str, str]] = []
    files_seen: List[str] = []
    tool_count = 0

    for event in events:
        if is_reasoning_like(event) and not options.include_reasoning:
            continue
        ts = parse_timestamp(event)
        files_seen.extend(detect_files(event))
        call = detect_function_call(event)
        if call:
            tool_count += 1
            name, args = call
            rows.append((ts, f"tool:{name}", short_line(args, 220)))
            continue
        role = detect_role(event)
        text = detect_message_text(event)
        if role in {"user", "assistant"} and text:
            rows.append((ts, role, short_line(redact(text, options.redact), 260)))

    files_ranked = rank_files(files_seen, include_system_files=options.include_system_files)

    md: List[str] = []
    md.append("# Codex Session Outline")
    md.append("")
    md.append("## Metadata")
    md.append(f"- Source file: `{info.path}`")
    md.append(f"- Session ID: `{info.session_id}`")
    md.append(f"- Size: `{info.size}` bytes")
    md.append(f"- Modified: `{info.mtime_iso}`")
    md.append(f"- Working directory: `{info.cwd or 'unknown'}`")
    md.append(f"- Timeline rows: `{len(rows)}`")
    md.append(f"- Tool calls: `{tool_count}`")
    md.append("")
    if files_ranked:
        md.append("## Top Files")
        for fp, count in files_ranked[: min(options.max_files, 40)]:
            md.append(f"- `{fp}` _(seen {count}x)_")
        md.append("")
    md.append("## Timeline")
    md.append("")
    md.append("| # | Time | Type | Content |")
    md.append("|---:|---|---|---|")
    for i, (ts, typ, content) in enumerate(rows[:300], 1):
        safe = content.replace("|", "\\|")
        md.append(f"| {i} | `{ts}` | `{typ}` | {safe} |")
    if len(rows) > 300:
        md.append(f"| ... | ... | ... | Omitted {len(rows) - 300} additional rows. |")
    md.append("")
    return "\n".join(md)


def command_outline(args: argparse.Namespace) -> int:
    path = resolve_session(args.session, Path(args.root).expanduser())
    options = build_render_options_from_args(args)
    md = render_outline_markdown(path, options)
    if args.out:
        out = Path(args.out).expanduser()
        write_text(out, md)
        print(f"Wrote {out} ({len(md.encode('utf-8'))} bytes)")
    else:
        print(md)
    return 0


def safe_filename(info: SessionInfo) -> str:
    dt = _dt.datetime.fromtimestamp(info.mtime).strftime("%Y%m%d-%H%M%S")
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", info.short_name)[:80]
    return f"{dt}-{base}.md"


def command_batch(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser()
    outdir = Path(args.outdir).expanduser()
    files = find_session_files(root)
    if args.limit:
        files = files[-args.limit:]
    if args.since:
        since_dt = _dt.datetime.fromisoformat(args.since)
        files = [f for f in files if _dt.datetime.fromtimestamp(f.stat().st_mtime) >= since_dt]

    if not files:
        print(f"No session files found under {root}", file=sys.stderr)
        return 1

    options = build_render_options_from_args(args)

    outdir.mkdir(parents=True, exist_ok=True)
    index_lines = ["# Codex Session Export Index", ""]
    for f in files:
        info = inspect_session(f)
        md = render_session_markdown(f, options)
        out = outdir / safe_filename(info)
        write_text(out, md)
        out_size = out.stat().st_size if out.exists() else 0
        print(f"Wrote {out} ({out_size} bytes)")
        index_lines.append(f"- [{out.name}]({out.name}) — `{info.mtime_iso}` — source `{info.size}` bytes — export `{out_size}` bytes — `{info.path}`")
        if info.first_user:
            index_lines.append(f"  - first user: {info.first_user}")
    write_text(outdir / "INDEX.md", "\n".join(index_lines) + "\n")
    print(f"Wrote {outdir / 'INDEX.md'}")
    return 0


CHATGPT_PROJECT_PROMPT = """# Task: Build a Research Workspace Handoff or History Map from a Doc-First Codex Evidence Pack

You will receive a bundled input generated from local coding-agent session history and project/workspace files. The bundle may contain WORKSPACE_MAP.md, DOCS_AUTHORITY.md, HISTORY_TIMELINE.md, SESSION_CLUSTERING.md, PACK_SUMMARY.md, selected current/focus documents, PROJECT_SNAPSHOT.md, and selected Codex session digests.

Your job is not to summarize everything. Your job is to produce the requested artifact while respecting source priority and uncertainty.

Source-of-truth rules:
1. Prefer DOCS_AUTHORITY.md and included revised Markdown documents over session transcripts.
2. Prefer newer revised documents over older drafts when they conflict.
3. Treat session digests as supporting evidence explaining how files were produced, not as the final source of truth.
4. Treat archive/superseded/old/deprecated folders as historical unless DOCS_AUTHORITY.md marks them current.
5. If sessions conflict with current documents, prefer current documents and mark the conflict explicitly.
6. Do not invent file paths, commands, test results, APIs, or implementation details.
7. Preserve exact file paths and command names when available.
8. Mark uncertain claims as "unconfirmed".
9. Avoid long transcript retelling.

Recommended two-phase workflow:

Phase 1 — History Map / Triage:
- Build an evolution map of the workspace or workstream.
- Identify workstreams, versioned branches, current evidence, superseded material, duplicate/background sessions, and unresolved questions.
- End with a clear "Needs human confirmation" section.

Phase 2 — Final Report / Handoff:
- After the human confirms which branches matter, generate a final report or handoff Markdown.
- Use the confirmed scope, not every file in the evidence pack.
- Include concrete next steps and reading order for the next human/AI agent.

When the pack goal is `workspace-history`, output Phase 1 first unless the user explicitly asks for the final report. Focus on:
- how the workspace evolved across versioned workstreams;
- which branches are upstream evidence, current synthesis, or superseded;
- which revised Markdown files are the most trustworthy current summaries;
- which session clusters are representative and which are duplicates/background;
- what should be organized next.

Suggested Phase 1 output format:

# Research Workspace History Map

## 1. Scope and Reading Rules
## 2. Source-of-Truth Documents
## 3. Workspace Evolution Timeline
## 4. Workstream Map
## 5. Version/Branch Evolution
## 6. Superseded or Historical Material
## 7. Representative Session Clusters
## 8. Current Reliable Conclusions
## 9. Open Questions / Unconfirmed Items
## 10. Needs Human Confirmation
## 11. Recommended Final Report Scope
## 12. Evidence Index

Suggested Phase 2 output format:

# Final Research Workspace Report / Handoff

## 1. Confirmed Goal and Scope
## 2. Current Source of Truth
## 3. Historical Evolution Summary
## 4. Current State
## 5. Valid Decisions and Reusable Evidence
## 6. Superseded Material to Avoid
## 7. Known Problems / Risks
## 8. Commands and Verification
## 9. Next Steps
## 10. Instructions for the Next AI Agent
## 11. Evidence Index
"""


PHASE1_HISTORY_PROMPT = """# Phase 1 Prompt: Build a Workspace History Map

Use this prompt after uploading CHATGPT_HANDOFF_INPUT.md.

Please analyze the uploaded evidence pack and produce a Phase 1 history map, not a final handoff yet.

Rules:
- Treat the workspace as a messy, multi-branch research/code workspace.
- Use DOCS_AUTHORITY.md and revised Markdown files as higher-priority evidence than session digests.
- Use SESSION_CLUSTERING.md to avoid overweighting duplicate or resumed sessions.
- Do not assume the highest-frequency session cluster is the only main line.
- Do not invent results, paths, commands, or validation outcomes.
- Mark uncertain points as unconfirmed.

Output:
1. Workspace evolution timeline.
2. Workstream map and role of each branch.
3. Version/branch evolution: what each major stage solved and what later replaced it.
4. Current reliable documents and conclusions.
5. Superseded or risky material.
6. Duplicate/background session clusters.
7. Questions requiring human confirmation.
8. Recommended scope for the final report.
"""


PHASE2_FINAL_PROMPT = """# Phase 2 Prompt: Generate the Final Report / Handoff

Use this after Phase 1 has been reviewed by a human.

Please generate the final report/handoff Markdown using the confirmed scope and corrections from the Phase 1 review.

Rules:
- Prefer confirmed branches and current revised documents.
- Keep historical branches only when they explain current decisions or reusable evidence.
- Do not re-expand every session transcript.
- Preserve exact file paths and commands when available.
- Include an Evidence Index mapping major claims to documents or session clusters.

Output:
1. Confirmed goal and scope.
2. Current source-of-truth documents.
3. Historical evolution summary.
4. Current state and reusable outputs.
5. Decisions still valid.
6. Superseded material to avoid.
7. Known risks and unconfirmed assumptions.
8. Next actions.
9. Instructions for the next human/AI agent.
10. Evidence index.
"""


def command_bundle(args: argparse.Namespace) -> int:
    inputs: List[Path] = []
    for pattern in args.inputs:
        expanded = [Path(p) for p in glob.glob(pattern)]
        if expanded:
            inputs.extend(expanded)
        else:
            p = Path(pattern)
            if p.exists():
                inputs.append(p)

    # Deduplicate, preserve order.
    seen = set()
    unique_inputs = []
    for p in inputs:
        rp = str(p.expanduser().resolve())
        if rp not in seen:
            seen.add(rp)
            unique_inputs.append(Path(rp))

    if not unique_inputs:
        print("No input markdown files matched.", file=sys.stderr)
        return 1

    parts: List[str] = []
    if args.with_prompt:
        parts.append(CHATGPT_PROJECT_PROMPT)
        parts.append("\n---\n")

    for i, path in enumerate(unique_inputs, 1):
        text = path.read_text(encoding="utf-8", errors="replace")
        if args.max_file_chars and len(text) > args.max_file_chars:
            text = text[:args.max_file_chars] + f"\n\n...[file truncated {len(text) - args.max_file_chars} chars]...\n"
        parts.append(f"# Input File {i}: `{path.name}`\n")
        parts.append(text)
        parts.append("\n\n---\n\n")

    result = "\n".join(parts)
    out = Path(args.out).expanduser()
    write_text(out, result)
    print(f"Wrote {out}")
    return 0


def run_cmd(cmd: Sequence[str], cwd: Path) -> str:
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
        return proc.stdout.strip()
    except FileNotFoundError:
        return f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return f"Command timed out: {' '.join(cmd)}"



def find_git_root(path: Path) -> Optional[Path]:
    """Return a valid Git root using `git rev-parse`, or None.

    Do not trust the mere existence of a `.git` directory. Some shared servers
    contain empty or broken `.git` directories in home folders, which cause
    false positives. Git itself is the source of truth.
    """
    path = path.expanduser().resolve()
    cwd = path if path.is_dir() else path.parent
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    if not out:
        return None
    root = Path(out).expanduser().resolve()
    try:
        chk = subprocess.run(
            ["git", "-C", str(root), "status", "--short"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if chk.returncode != 0:
        return None
    return root


def safe_rel(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except Exception:
        return str(path)


def is_archive_like_path(path: str) -> bool:
    p = path.lower().replace("\\", "/")
    return any(part in p for part in ["/archive", "archive_", "_superseded", "superseded", "/old", "deprecated"])


def split_patterns(value: Optional[str]) -> List[str]:
    if not value:
        return []
    parts: List[str] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


def session_raw_text(path: Path, max_chars: int = 0) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if max_chars and len(text) > max_chars:
        return text[:max_chars]
    return text


def classify_session_cluster(raw_lower: str, focus_label: str = "") -> str:
    """Coarse session cluster label for messy research workspaces."""
    fl = (focus_label or "").lower()
    if fl and fl in raw_lower:
        if "manuscript_v3" in raw_lower or "handoff_for_codex_review" in raw_lower:
            return "focus-manuscript-integration"
        return f"focus-{fl}"
    checks = [
        ("immune_circrna_paper_v1", "focus-manuscript-integration"),
        ("manuscript_v3", "focus-manuscript-integration"),
        ("peaks_uep_v16", "v16-paper-like-circRNA-translation"),
        ("peaks_uep_v15", "v15-full-length-circRNA-translation"),
        ("peaks_uep_v14", "v14-HLA-pMHC-immunogenicity"),
        ("netmhcpan", "v14-HLA-pMHC-immunogenicity"),
        ("bigmhc", "v14-HLA-pMHC-immunogenicity"),
        ("peaks_uep_v13", "v13-quality-boundary-overlap"),
        ("peaks_uep_v12", "v12-overlap-geometry"),
        ("peaks_uep_v11", "v11-tryptic-vs-HLA-observability"),
        ("peaks_uep_v10", "v10-article-like-cUEP-matching"),
        ("peaks_uep_v9", "v9-circORF-substring-rescue"),
        ("validation_pack_v4", "v4-DIA-NN-validation"),
        ("dia-nn", "v4-DIA-NN-validation"),
        ("diann", "v4-DIA-NN-validation"),
        ("tcr_circrna", "TCR-circRNA-BSJ-correlation"),
        ("tcr_pmhc", "TCR-pMHC-covariance"),
        ("tcr_trust4", "TRUST4-TCR-evidence"),
        ("trust4", "TRUST4-TCR-evidence"),
        ("translation_refs", "workspace-general"),
    ]
    for needle, label in checks:
        if needle in raw_lower:
            return label
    return "unclassified"


def selected_rows_by_cluster(rows: List[Dict[str, Any]], limit: int, history_depth: str = "balanced") -> List[Path]:
    """Soft-deduplicate selected sessions for workspace-history.

    Keep representative sessions per cluster while preserving coverage. Related
    non-selected sessions remain visible in SESSION_CLUSTERING.md.
    """
    candidates = [r for r in rows if r.get("score", 0) > 0]
    candidates = sorted(candidates, key=lambda r: (r.get("score", 0), r.get("mtime", 0)), reverse=True)
    if history_depth == "complete":
        return sorted([Path(r["path"]) for r in candidates[:limit]], key=lambda p: p.stat().st_mtime)
    per_cluster = 1 if history_depth == "light" else 2
    counts: Dict[str, int] = {}
    selected: List[Dict[str, Any]] = []
    for r in candidates:
        cl = r.get("cluster") or "unclassified"
        if counts.get(cl, 0) >= per_cluster:
            continue
        selected.append(r)
        counts[cl] = counts.get(cl, 0) + 1
        if len(selected) >= limit:
            break
    if len(selected) < min(limit, len(candidates)):
        already = {r["path"] for r in selected}
        for r in candidates:
            if r["path"] not in already:
                selected.append(r)
                already.add(r["path"])
            if len(selected) >= limit:
                break
    return sorted([Path(r["path"]) for r in selected[:limit]], key=lambda p: p.stat().st_mtime)


def score_session_for_paths(
    path: Path,
    focus_dir: Optional[Path] = None,
    workspace_root: Optional[Path] = None,
    extra_keywords: Optional[Sequence[str]] = None,
    max_scan_chars: int = 0,
    focus_label: str = "",
) -> Dict[str, Any]:
    """Heuristic score for whether a session belongs to a messy research workspace/focus branch."""
    info = inspect_session(path)
    raw = session_raw_text(path, max_scan_chars)
    raw_lower = raw.lower()
    cwd_lower = (info.cwd or "").lower()
    cluster = classify_session_cluster(raw_lower, focus_label=focus_label)

    score = 0
    reasons: List[str] = []

    def add(points: int, reason: str) -> None:
        nonlocal score
        score += points
        reasons.append(reason)

    focus_hit_count = 0
    workspace_hit_count = 0
    keyword_hit_count = 0

    focus_terms: List[str] = []
    if focus_dir:
        f = focus_dir.expanduser().resolve()
        focus_terms.extend([str(f), f.name])
        if workspace_root:
            try:
                focus_terms.append(str(f.relative_to(workspace_root.expanduser().resolve())))
            except Exception:
                pass

    workspace_terms: List[str] = []
    if workspace_root:
        w = workspace_root.expanduser().resolve()
        workspace_terms.extend([str(w), w.name])

    extra_terms = [x for x in (extra_keywords or []) if x]

    for term in focus_terms:
        t = term.lower()
        if not t:
            continue
        if t in cwd_lower:
            focus_hit_count += 1
            add(120, f"cwd contains focus term: {term}")
        n = raw_lower.count(t)
        if n:
            focus_hit_count += n
            add(min(120, 8 * n), f"raw contains focus term {n}x: {term}")

    for term in workspace_terms:
        t = term.lower()
        if not t:
            continue
        if t in cwd_lower:
            workspace_hit_count += 1
            add(35, f"cwd contains workspace term: {term}")
        n = raw_lower.count(t)
        if n:
            workspace_hit_count += n
            add(min(80, 2 * n), f"raw contains workspace term {n}x: {term}")

    for term in extra_terms:
        t = term.lower()
        n = raw_lower.count(t)
        if n:
            keyword_hit_count += n
            add(min(60, 6 * n), f"raw contains keyword {n}x: {term}")

    # A session that edited or ran files under the focus gets a strong boost.
    if focus_dir:
        ftxt = str(focus_dir.expanduser().resolve()).lower()
        if "apply_patch" in raw_lower and ftxt in raw_lower:
            add(80, "apply_patch near focus path")
        if any(cmd in raw_lower for cmd in ["python ", "python3 ", "rscript ", "bash ", "sh "] ) and ftxt in raw_lower:
            add(35, "run/verify command mentions focus path")

    return {
        "path": str(path),
        "session_id": info.session_id,
        "mtime": info.mtime,
        "mtime_iso": info.mtime_iso,
        "cwd": info.cwd,
        "size": info.size,
        "score": score,
        "focus_hits": focus_hit_count,
        "workspace_hits": workspace_hit_count,
        "keyword_hits": keyword_hit_count,
        "cluster": cluster,
        "reasons": "; ".join(reasons[:8]),
        "first_user": info.first_user,
    }


def write_session_selection_report(outdir: Path, rows: List[Dict[str, Any]], selected: List[Path]) -> None:
    selected_set = {str(p) for p in selected}
    tsv_lines = ["selected\tscore\tfocus_hits\tworkspace_hits\tkeyword_hits\tcluster\tmtime\tsize\tcwd\tsession_id\tpath\treasons"]
    md_lines = [
        "# Session Selection Report",
        "",
        "This is a heuristic ranking. For workspace-history, broad selection is expected; use SESSION_CLUSTERING.md to distinguish representative sessions from background/duplicates.",
        "",
        "| Selected | Score | Focus | Workspace | Keyword | Cluster | Modified | Size | CWD | Session ID | Reasons |",
        "|---|---:|---:|---:|---:|---|---|---:|---|---|---|",
    ]
    for r in sorted(rows, key=lambda x: (x.get("score", 0), x.get("mtime", 0)), reverse=True):
        sel = "yes" if r["path"] in selected_set else "no"
        cluster = str(r.get("cluster", "unclassified"))
        tsv_lines.append("\t".join([
            sel,
            str(r.get("score", 0)),
            str(r.get("focus_hits", 0)),
            str(r.get("workspace_hits", 0)),
            str(r.get("keyword_hits", 0)),
            cluster.replace("\t", " "),
            r["mtime_iso"],
            str(r["size"]),
            str(r["cwd"]).replace("\t", " "),
            r["session_id"],
            r["path"],
            str(r["reasons"]).replace("\t", " "),
        ]))
        reasons = str(r["reasons"]).replace("|", "\\|")
        cwd = str(r["cwd"]).replace("|", "\\|")
        md_lines.append(f"| {sel} | {r.get('score', 0)} | {r.get('focus_hits', 0)} | {r.get('workspace_hits', 0)} | {r.get('keyword_hits', 0)} | `{cluster}` | `{r['mtime_iso']}` | {r['size']} | `{cwd}` | `{r['session_id']}` | {reasons} |")
    write_text(outdir / "SESSION_SELECTION.tsv", "\n".join(tsv_lines) + "\n")
    write_text(outdir / "SESSION_SELECTION.md", "\n".join(md_lines) + "\n")


def write_session_clustering_report(outdir: Path, rows: List[Dict[str, Any]], selected: List[Path]) -> str:
    selected_set = {str(p) for p in selected}
    clusters: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        if r.get("score", 0) <= 0:
            continue
        clusters.setdefault(str(r.get("cluster") or "unclassified"), []).append(r)
    lines: List[str] = [
        "# Session Clustering Report",
        "",
        "Purpose: keep historical evidence broad while avoiding transcript overload. Representative selected sessions are bundled; related non-selected sessions remain indexed for traceability.",
        "",
    ]
    for cluster, items in sorted(clusters.items(), key=lambda kv: max(x.get("score", 0) for x in kv[1]), reverse=True):
        items = sorted(items, key=lambda r: (r.get("score", 0), r.get("mtime", 0)), reverse=True)
        selected_items = [r for r in items if r["path"] in selected_set]
        lines.append(f"## {cluster}")
        lines.append("")
        lines.append(f"- Sessions indexed: `{len(items)}`")
        lines.append(f"- Selected representative sessions: `{len(selected_items)}`")
        if selected_items:
            lines.append("- Representatives:")
            for r in selected_items[:5]:
                lines.append(f"  - `{r['session_id']}` — score `{r.get('score', 0)}` — `{r['mtime_iso']}` — cwd `{r.get('cwd')}`")
        related = [r for r in items if r["path"] not in selected_set]
        if related:
            lines.append("- Related / duplicate / background sessions:")
            for r in related[:8]:
                lines.append(f"  - `{r['session_id']}` — score `{r.get('score', 0)}` — `{r['mtime_iso']}`")
            if len(related) > 8:
                lines.append(f"  - ... plus `{len(related) - 8}` more")
        lines.append("")
    out = outdir / "SESSION_CLUSTERING.md"
    write_text(out, "\n".join(lines) + "\n")
    return str(out)


def write_history_timeline(outdir: Path, rows: List[Dict[str, Any]], doc_authority_path: Optional[str] = None) -> str:
    items = [r for r in rows if r.get("score", 0) > 0]
    items = sorted(items, key=lambda r: r.get("mtime", 0))
    lines: List[str] = [
        "# History Timeline",
        "",
        "This timeline is generated from Codex session metadata and should be used as an orientation aid, not as the final source of truth.",
        "",
    ]
    if doc_authority_path:
        lines.append(f"- Source-of-truth guide: `{doc_authority_path}`")
        lines.append("")
    lines.append("| Time | Cluster | Score | Session ID | CWD | Notes |")
    lines.append("|---|---|---:|---|---|---|")
    for r in items:
        reasons = str(r.get("reasons", "")).replace("|", "\\|")
        cwd = str(r.get("cwd", "")).replace("|", "\\|")
        lines.append(f"| `{r['mtime_iso']}` | `{r.get('cluster', 'unclassified')}` | {r.get('score', 0)} | `{r['session_id']}` | `{cwd}` | {reasons[:220]} |")
    out = outdir / "HISTORY_TIMELINE.md"
    write_text(out, "\n".join(lines) + "\n")
    return str(out)


def select_sessions(
    root: Path,
    limit: int,
    since: Optional[str] = None,
    focus_dir: Optional[Path] = None,
    workspace_root: Optional[Path] = None,
    filter_focus: bool = False,
    filter_workspace: bool = False,
    min_score: int = 1,
    extra_keywords: Optional[Sequence[str]] = None,
    focus_label: str = "",
) -> Tuple[List[Path], List[Dict[str, Any]]]:
    files = find_session_files(root)
    if since:
        since_dt = _dt.datetime.fromisoformat(since)
        files = [f for f in files if _dt.datetime.fromtimestamp(f.stat().st_mtime) >= since_dt]

    if filter_focus or filter_workspace or extra_keywords:
        rows = [score_session_for_paths(f, focus_dir=focus_dir, workspace_root=workspace_root, extra_keywords=extra_keywords, focus_label=focus_label) for f in files]
        filtered = [r for r in rows if r["score"] >= min_score]
        if filter_focus:
            # Strict doc/workstream handoff mode: workspace hits alone are not enough.
            filtered = [r for r in filtered if r.get("focus_hits", 0) > 0 or (extra_keywords and r.get("keyword_hits", 0) > 0)]
        elif filter_workspace:
            filtered = [r for r in filtered if r.get("workspace_hits", 0) > 0 or (extra_keywords and r.get("keyword_hits", 0) > 0)]
        # Prefer relevance first, then recency. This is better for messy workspaces than pure latest-N.
        filtered = sorted(filtered, key=lambda r: (r["score"], r["mtime"]), reverse=True)
        selected = [Path(r["path"]) for r in filtered[:limit]]
        # Preserve chronological order in generated digest directory.
        selected = sorted(selected, key=lambda p: p.stat().st_mtime)
        return selected, rows

    selected = files[-limit:] if limit else files
    rows = []
    for f in selected:
        info = inspect_session(f)
        rows.append({
            "path": str(f),
            "session_id": info.session_id,
            "mtime": info.mtime,
            "mtime_iso": info.mtime_iso,
            "cwd": info.cwd,
            "size": info.size,
            "score": 0,
            "focus_hits": 0,
            "workspace_hits": 0,
            "keyword_hits": 0,
            "cluster": "recency-selected",
            "reasons": "selected by recency",
            "first_user": info.first_user,
        })
    return selected, rows


def workspace_dir_score(path: Path) -> int:
    name = path.name.lower()
    score = 0
    if re.search(r"(?:^|[_-])v\d+", name):
        score += 20
    if any(x in name for x in ["manuscript", "paper", "final", "publication", "candidate", "immune", "circ", "hla", "tcr", "translation", "peaks"]):
        score += 10
    if any(x in name for x in ["archive", "superseded", "old", "deprecated"]):
        score -= 30
    return score


def command_workspace(args: argparse.Namespace) -> int:
    root = Path(args.workspace_root).expanduser().resolve()
    if not root.exists():
        print(f"Workspace root does not exist: {root}", file=sys.stderr)
        return 1
    out = Path(args.out).expanduser()
    max_depth = args.max_depth
    max_dirs = args.max_dirs
    exclude_names = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".DS_Store"}

    dir_rows: List[Tuple[int, str, int, int, int, float]] = []
    for cur, dirs, files in os.walk(root):
        curp = Path(cur)
        rel = curp.relative_to(root)
        depth = len(rel.parts)
        dirs[:] = [d for d in dirs if d not in exclude_names and depth < max_depth]
        if depth == 0:
            continue
        file_count = sum(1 for f in files if f not in exclude_names)
        md_count = sum(1 for f in files if f.lower().endswith(".md"))
        code_count = sum(1 for f in files if re.search(r"\.(py|r|R|sh|ipynb)$", f))
        try:
            latest = max((curp / f).stat().st_mtime for f in files) if files else curp.stat().st_mtime
        except Exception:
            latest = curp.stat().st_mtime
        score = workspace_dir_score(curp) + min(file_count, 30) + md_count * 3 + code_count * 2
        dir_rows.append((score, str(rel), file_count, md_count, code_count, latest))
    dir_rows = sorted(dir_rows, key=lambda r: (r[0], r[5]), reverse=True)[:max_dirs]

    md: List[str] = []
    md.append("# Workspace Map")
    md.append("")
    md.append(f"- Workspace root: `{root}`")
    md.append(f"- Generated at: `{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`")
    md.append("- Purpose: help ChatGPT distinguish current focus branches from historical experiment branches.")
    md.append("")
    md.append("## Workstreams Detected")
    md.append("")
    md.append("| Score | Directory | Likely Role | Likely Status | Files | Markdown | Code | Latest Modified |")
    md.append("|---:|---|---|---|---:|---:|---:|---|")
    for score, rel, file_count, md_count, code_count, latest in dir_rows:
        ts = _dt.datetime.fromtimestamp(latest).strftime('%Y-%m-%d %H:%M:%S')
        rel_low = rel.lower()
        if "manuscript" in rel_low or "paper" in rel_low:
            role = "paper/manuscript branch"
        elif "tcr" in rel_low:
            role = "TCR evidence branch"
        elif "hla" in rel_low or "pmhc" in rel_low:
            role = "HLA/pMHC evidence branch"
        elif "peaks" in rel_low or "uep" in rel_low:
            role = "MS/UEP evidence branch"
        elif "circ" in rel_low or "translation" in rel_low:
            role = "circRNA translation branch"
        else:
            role = "analysis/support branch"
        if is_archive_like_path(rel):
            status = "historical/superseded"
        elif re.search(r"v1[0-9]|v2[0-9]|final|publication|manuscript", rel_low):
            status = "likely recent/high-priority"
        else:
            status = "unknown; verify against DOCS_AUTHORITY"
        md.append(f"| {score} | `{rel}` | {role} | {status} | {file_count} | {md_count} | {code_count} | `{ts}` |")
    md.append("")
    md.append("## Interpretation Notes")
    md.append("- Higher score means the directory looks more likely to be a meaningful analysis branch, not necessarily the current branch.")
    md.append("- Versioned directories such as `*_v12_*`, `*_v16_*` are historical/evolution branches unless a focus directory or current manuscript points to them.")
    md.append("- Use this map as context; do not treat every listed directory as current work.")
    md.append("")
    write_text(out, "\n".join(md))
    print(f"Wrote {out}")
    return 0



DOC_TYPE_PATTERNS = [
    ("handoff", re.compile(r"handoff", re.I)),
    ("logic", re.compile(r"logic", re.I)),
    ("readme", re.compile(r"readme", re.I)),
    ("results", re.compile(r"results", re.I)),
    ("methods", re.compile(r"methods", re.I)),
    ("abstract", re.compile(r"abstract", re.I)),
    ("alignment", re.compile(r"alignment", re.I)),
    ("audit", re.compile(r"audit|self_audit", re.I)),
]


def doc_type(path: Path) -> str:
    name = path.name
    for typ, pat in DOC_TYPE_PATTERNS:
        if pat.search(name):
            return typ
    return "other"


def doc_version_score(path: Path) -> int:
    text = str(path).lower()
    nums = [int(x) for x in re.findall(r"(?:^|[_-])v(\d+)(?:[_\.-]|$)", text)]
    score = max(nums) * 6 if nums else 0
    if "latest" in text or "final" in text or "publication" in text or "manuscript_v3" in text:
        score += 35
    if "revised" in text or "revise" in text or "review" in text:
        score += 10
    return score


def doc_scope(path: Path, focus: Optional[Path], workspace: Optional[Path]) -> str:
    rp = path.resolve()
    if focus:
        try:
            rel = rp.relative_to(focus.resolve())
            if "manuscript" in rel.parts:
                return "focus-manuscript-doc"
            return "focus-doc"
        except Exception:
            pass
    if workspace:
        try:
            rp.relative_to(workspace.resolve())
            return "workspace-doc"
        except Exception:
            pass
    return "external-doc"


def doc_authority_score(path: Path, focus: Optional[Path], workspace: Optional[Path]) -> int:
    score = 0
    scope = doc_scope(path, focus, workspace)
    typ = doc_type(path)
    if scope == "focus-manuscript-doc":
        score += 130
    elif scope == "focus-doc":
        score += 100
    elif scope == "workspace-doc":
        score += 35
    else:
        score += 10
    score += {
        "handoff": 55,
        "logic": 50,
        "readme": 35,
        "results": 32,
        "methods": 30,
        "abstract": 28,
        "alignment": 25,
        "audit": 22,
        "other": 5,
    }.get(typ, 5)
    score += doc_version_score(path)
    if is_archive_like_path(str(path)):
        score -= 80
    if re.search(r"(?:^|[_-])v1(?:[_\.-]|$)|(?:^|[_-])v2(?:[_\.-]|$)", str(path).lower()):
        score -= 10
    try:
        # Recent small boost, bounded.
        age_days = max(0, (_dt.datetime.now().timestamp() - path.stat().st_mtime) / 86400)
        score += max(0, int(20 - age_days / 7))
    except Exception:
        pass
    return score


def doc_family_key(path: Path) -> str:
    name = path.name.lower()
    name = re.sub(r"(?:^|[_-])v\d+(?=[_\.-]|$)", "_vX", name)
    name = re.sub(r"\d{4}-\d{2}-\d{2}", "DATE", name)
    name = re.sub(r"[_-]+", "_", name)
    typ = doc_type(path)
    return f"{typ}:{name}"


def sha12_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]

def collect_doc_inputs(
    project: Optional[Path],
    workspace_root: Optional[Path],
    include_docs: Sequence[str],
    auto_docs: bool,
    outdir: Path,
    max_doc_chars: int,
    focus: Optional[str] = None,
    max_docs: int = 18,
    doc_first: bool = True,
) -> List[str]:
    bases: List[Path] = []
    if project:
        bases.append(project.expanduser().resolve())
    if workspace_root:
        w = workspace_root.expanduser().resolve()
        if w not in bases:
            bases.append(w)

    patterns: List[str] = []
    if auto_docs:
        # Focus docs first. Workspace-level docs are included only if explicitly matched by include_docs
        # or if they look like high-level handoffs/readmes near the workspace root.
        patterns.extend([
            "README*.md",
            "LOGIC*.md",
            "*HANDOFF*.md",
            "*handoff*.md",
            "*alignment*.md",
            "*audit*.md",
            "self_audit*.md",
            "manuscript*/README*.md",
            "manuscript*/LOGIC*.md",
            "manuscript*/*HANDOFF*.md",
            "manuscript*/*audit*.md",
            "manuscript*/Abstract*.md",
            "manuscript*/Methods*.md",
            "manuscript*/Results*.md",
            "manuscript*/professor_alignment*.md",
        ])
        if workspace_root and not project:
            patterns.extend(["*/README*.md", "*/*HANDOFF*.md", "*/*handoff*.md", "*/LOGIC*.md"])
    patterns.extend(include_docs)

    matched: List[Path] = []
    for pat in patterns:
        p = Path(pat).expanduser()
        if p.is_absolute():
            matched.extend(Path(x) for x in glob.glob(str(p), recursive=True))
        else:
            for b in bases:
                matched.extend(Path(x) for x in glob.glob(str(b / pat), recursive=True))

    records: List[Dict[str, Any]] = []
    seen_paths = set()
    seen_hashes = set()
    for p in matched:
        try:
            rp = p.resolve()
        except Exception:
            continue
        if not rp.is_file() or rp.suffix.lower() != ".md":
            continue
        if str(rp) in seen_paths:
            continue
        seen_paths.add(str(rp))
        try:
            raw_text = rp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        h = sha12_text(raw_text)
        exact_duplicate = h in seen_hashes
        if not exact_duplicate:
            seen_hashes.add(h)
        records.append({
            "path": rp,
            "score": doc_authority_score(rp, project, workspace_root),
            "scope": doc_scope(rp, project, workspace_root),
            "type": doc_type(rp),
            "family": doc_family_key(rp),
            "mtime": rp.stat().st_mtime,
            "size": rp.stat().st_size,
            "hash": h,
            "exact_duplicate": exact_duplicate,
        })

    # Keep the highest-authority member in each family; retain an index of superseded/omitted docs.
    best_by_family: Dict[str, Dict[str, Any]] = {}
    for r in sorted(records, key=lambda x: (x["score"], x["mtime"]), reverse=True):
        if r["exact_duplicate"]:
            continue
        fam = r["family"]
        if fam not in best_by_family:
            best_by_family[fam] = r
        else:
            r["superseded_by"] = str(best_by_family[fam]["path"])

    included_records = sorted(best_by_family.values(), key=lambda x: (x["score"], x["mtime"]), reverse=True)[:max_docs]
    included_set = {str(r["path"]) for r in included_records}

    omitted_records = []
    for r in records:
        if str(r["path"]) not in included_set:
            omitted_records.append(r)

    docs_dir = outdir / "included_docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    out_paths: List[str] = []

    authority_lines = [
        "# Document Authority Ranking",
        "",
        "This file is the primary guide for ChatGPT. Prefer these documents over session transcripts when there are conflicts.",
        "",
        f"- Focus/project path: `{project.resolve() if project else 'not set'}`",
        f"- Workspace root: `{workspace_root.resolve() if workspace_root else 'not set'}`",
        f"- Focus label: `{focus or (project.name if project else 'not set')}`",
        "",
        "## Authoritative Current Docs",
        "",
        "Focus-docs are the primary source of truth for current-handoff. Workspace-docs, if present, are background/context unless they are explicitly named by the user.",
        "",
        "| Rank | Score | Scope | Type | Modified | Size | Source |",
        "|---:|---:|---|---|---|---:|---|",
    ]
    index = ["# Included Project Documents", "", "Documents are ranked in `DOCS_AUTHORITY.md`. Session digests are supporting evidence, not the primary source of truth.", ""]
    for i, r in enumerate(included_records, 1):
        p = r["path"]
        text = p.read_text(encoding="utf-8", errors="replace")
        truncated = False
        if max_doc_chars and len(text) > max_doc_chars:
            text = text[:max_doc_chars] + f"\n\n...[document truncated {len(text) - max_doc_chars} chars]...\n"
            truncated = True
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", p.name)
        out = docs_dir / f"{i:02d}_{r['scope']}_{r['type']}_{safe_name}"
        wrapped = (
            f"# Included Document: `{p}`\n\n"
            f"- Authority score: `{r['score']}`\n"
            f"- Scope: `{r['scope']}`\n"
            f"- Type: `{r['type']}`\n"
            f"- SHA256-12: `{r['hash']}`\n\n"
            + text
        )
        write_text(out, wrapped)
        out_paths.append(str(out))
        ts = _dt.datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
        source = str(p).replace("|", "\\|")
        authority_lines.append(f"| {i} | {r['score']} | `{r['scope']}` | `{r['type']}` | `{ts}` | {r['size']} | `{source}` |")
        index.append(f"- [{out.name}]({out.name}) — score `{r['score']}` — `{r['scope']}` / `{r['type']}` — source `{p}`" + (" — truncated" if truncated else ""))

    authority_lines.extend([
        "",
        "## Superseded / Omitted Candidate Docs",
        "",
        "These may be useful for history, but should not override the authoritative docs above.",
        "",
        "| Score | Scope | Type | Reason | Source |",
        "|---:|---|---|---|---|",
    ])
    for r in sorted(omitted_records, key=lambda x: (x["score"], x["mtime"]), reverse=True)[:120]:
        reason = "exact duplicate of another seen document with the same SHA256" if r.get("exact_duplicate") else "lower authority or max-doc cap"
        if r.get("superseded_by"):
            reason = f"superseded by {r['superseded_by']}"
        source = str(r["path"]).replace("|", "\\|")
        authority_lines.append(f"| {r['score']} | `{r['scope']}` | `{r['type']}` | {reason} | `{source}` |")

    authority_lines.extend([
        "",
        "## Scope Interpretation",
        "",
        "- `focus-doc` = documents inside the focus/workstream directory; these are preferred for current-handoff.",
        "- `workspace-doc` = broader workspace context; useful for history/evolution but should not override newer focus-docs.",
        "- Superseded, duplicate, archive, old, and lower-version documents are history unless explicitly promoted above.",
        "",
        "## Conflict Resolution Rule",
        "",
        "When this file conflicts with session digests, prefer the highest-ranked authoritative docs. Treat sessions as provenance/evidence, not final conclusions.",
        "",
    ])
    authority_path = outdir / "DOCS_AUTHORITY.md"
    write_text(authority_path, "\n".join(authority_lines) + "\n")
    write_text(docs_dir / "INDEX.md", "\n".join(index) + "\n")
    return [str(authority_path)] + out_paths

def batch_selected_sessions(files: Sequence[Path], outdir: Path, options: RenderOptions) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    index_lines = ["# Codex Session Export Index", ""]
    for f in files:
        info = inspect_session(f)
        md = render_session_markdown(f, options)
        out = outdir / safe_filename(info)
        write_text(out, md)
        out_size = out.stat().st_size if out.exists() else 0
        print(f"Wrote {out} ({out_size} bytes)")
        index_lines.append(f"- [{out.name}]({out.name}) — `{info.mtime_iso}` — source `{info.size}` bytes — export `{out_size}` bytes — cwd `{info.cwd}` — `{info.path}`")
        if info.first_user:
            index_lines.append(f"  - first user: {info.first_user}")
    write_text(outdir / "INDEX.md", "\n".join(index_lines) + "\n")
    print(f"Wrote {outdir / 'INDEX.md'}")

def command_snapshot(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    if not project.exists():
        print(f"Project path does not exist: {project}", file=sys.stderr)
        return 1

    max_depth = args.max_depth
    exclude_names = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".DS_Store"}

    files: List[str] = []
    for root, dirs, filenames in os.walk(project):
        root_path = Path(root)
        rel_root = root_path.relative_to(project)
        depth = len(rel_root.parts)
        dirs[:] = [d for d in dirs if d not in exclude_names and depth < max_depth]
        for name in filenames:
            if name in exclude_names:
                continue
            p = root_path / name
            rel = p.relative_to(project)
            if len(rel.parts) <= max_depth + 1:
                files.append(str(rel))

    files.sort()
    git_root = find_git_root(project)

    md: List[str] = []
    md.append("# Project Snapshot")
    md.append("")
    md.append(f"- Project path: `{project}`")
    md.append(f"- Generated at: `{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`")
    if git_root:
        md.append("- Git repository: detected")
        md.append(f"- Git root: `{git_root}`")
    else:
        md.append("- Git repository: not detected")
        md.append("- Git status: skipped")
        md.append("- Recent commits: skipped")
    md.append("")
    md.append("## Git Status")
    md.append("```text")
    if git_root:
        md.append(run_cmd(["git", "status", "--short"], git_root) or "(clean)")
    else:
        md.append("(skipped: valid git repository not detected by `git rev-parse --show-toplevel`)")
    md.append("```")
    md.append("")
    md.append("## Recent Commits")
    md.append("```text")
    if git_root:
        md.append(run_cmd(["git", "log", "--oneline", "-20"], git_root) or "(no git log)")
    else:
        md.append("(skipped: valid git repository not detected)")
    md.append("```")
    md.append("")
    md.append("## Files")
    md.append("```text")
    md.append("\n".join(files[: args.max_files]))
    if len(files) > args.max_files:
        md.append(f"... plus {len(files) - args.max_files} more")
    md.append("```")
    md.append("")
    md.append("## Suggested Use")
    md.append("Upload this file together with selected session digests and key project documents when asking ChatGPT to create a project-level handoff.")
    md.append("")

    out = Path(args.out).expanduser()
    write_text(out, "\n".join(md))
    print(f"Wrote {out}")
    return 0




def write_prompt_guides(base: Path, goal: str) -> List[str]:
    """Write reusable prompt files for the two-stage GPT workflow."""
    paths: List[str] = []
    p1 = base / "PROMPT_PHASE1_HISTORY_MAP.md"
    p2 = base / "PROMPT_PHASE2_FINAL_REPORT.md"
    write_text(p1, PHASE1_HISTORY_PROMPT)
    write_text(p2, PHASE2_FINAL_PROMPT)
    if goal == "workspace-history":
        paths.append(str(p1))
        # Phase 2 prompt is useful but usually should not drive the first GPT response.
        # Keep it as a file in the pack directory; do not bundle it by default.
    return paths


def write_pack_summary(
    base: Path,
    args: argparse.Namespace,
    selected: Sequence[Path],
    selection_rows: Sequence[Dict[str, Any]],
    doc_inputs: Sequence[str],
    bundle_inputs: Sequence[str],
) -> str:
    """Write a short orientation file that helps GPT and humans read the pack."""
    clusters: Dict[str, int] = {}
    for row in selection_rows:
        if row.get("selected"):
            clusters[row.get("cluster") or "unclassified"] = clusters.get(row.get("cluster") or "unclassified", 0) + 1

    selected_ids = {str(p) for p in selected}
    selected_rows = [r for r in selection_rows if r.get("path") in selected_ids]
    biggest_cluster = "none"
    if clusters:
        biggest_cluster = max(clusters.items(), key=lambda kv: kv[1])[0]

    md: List[str] = []
    md.append("# Pack Summary")
    md.append("")
    md.append("This file is an orientation aid for humans and GPT. It describes how to interpret the evidence pack.")
    md.append("")
    md.append("## Pack Goal")
    md.append(f"- Goal: `{args.goal}`")
    md.append(f"- History depth: `{getattr(args, 'history_depth', 'n/a')}`")
    md.append(f"- Workspace root: `{args.workspace_root or 'not set'}`")
    md.append(f"- Focus/project: `{args.focus or args.project or 'not set'}`")
    md.append("")
    md.append("## Included Evidence")
    md.append(f"- Selected sessions: `{len(selected)}`")
    md.append(f"- Document inputs: `{max(0, len(doc_inputs) - 1)}` plus DOCS_AUTHORITY when present")
    md.append(f"- Bundle input files: `{len(bundle_inputs)}`")
    md.append(f"- Largest selected cluster: `{biggest_cluster}`")
    md.append("")
    if selected_rows:
        md.append("## Selected Session Clusters")
        for cluster, count in sorted(clusters.items(), key=lambda kv: (-kv[1], kv[0])):
            md.append(f"- `{cluster}`: {count} selected session(s)")
        md.append("")
    md.append("## Reading Guidance")
    if args.goal == "workspace-history":
        md.append("- This is a broad historical/evolution pack. Broad session selection is expected.")
        md.append("- Start with WORKSPACE_MAP.md, DOCS_AUTHORITY.md, HISTORY_TIMELINE.md, and SESSION_CLUSTERING.md.")
        md.append("- Use session digests as provenance, not as final truth.")
        md.append("- First ask GPT for a Phase 1 history map and a human-confirmation checklist; generate the final report only after review.")
    else:
        md.append("- This is a focused handoff pack. Start with DOCS_AUTHORITY.md and current/focus documents.")
        md.append("- Use session digests only to understand how the current files were produced.")
    md.append("")
    out = base / "PACK_SUMMARY.md"
    write_text(out, "\n".join(md) + "\n")
    return str(out)


def build_pack_namespace_from_common(args: argparse.Namespace, *, goal: str, workspace_root: Optional[str], focus: Optional[str]) -> argparse.Namespace:
    """Build a full command_pack namespace for convenience wrappers."""
    return argparse.Namespace(
        root=args.root,
        outdir=args.outdir,
        limit=args.limit,
        since=getattr(args, "since", None),
        project=None,
        focus=focus,
        goal=goal,
        history_depth=getattr(args, "history_depth", "balanced"),
        workspace_root=workspace_root,
        filter_project=(goal != "workspace-history"),
        filter_workspace=(goal == "workspace-history"),
        min_score=getattr(args, "min_score", 1),
        keywords=getattr(args, "keywords", "") or (DEFAULT_HISTORY_KEYWORDS if goal == "workspace-history" else ""),
        include_docs=getattr(args, "include_docs", ""),
        auto_docs=True,
        doc_first=True,
        max_docs=getattr(args, "max_docs", 18),
        max_doc_chars=getattr(args, "max_doc_chars", 50000),
        max_depth=getattr(args, "max_depth", 3),
        max_files=getattr(args, "max_files", 2000),
        workspace_depth=getattr(args, "workspace_depth", 2),
        max_workspace_dirs=getattr(args, "max_workspace_dirs", 120),
        max_file_chars=getattr(args, "max_file_chars", 60000),
        max_message_chars=getattr(args, "max_message_chars", None),
        max_digest_files=getattr(args, "max_digest_files", None),
        max_tool_calls=getattr(args, "max_tool_calls", None),
        no_redact=getattr(args, "no_redact", False),
    )


def command_history(args: argparse.Namespace) -> int:
    """Convenience command for broad workspace-history packs."""
    ns = build_pack_namespace_from_common(args, goal="workspace-history", workspace_root=args.workspace_root, focus=None)
    return command_pack(ns)


def command_handoff(args: argparse.Namespace) -> int:
    """Convenience command for focused current handoff packs."""
    ns = build_pack_namespace_from_common(args, goal="current-handoff", workspace_root=args.workspace_root, focus=args.focus)
    return command_pack(ns)
def phase1_next_message(goal: str = "workspace-history") -> str:
    if goal == "workspace-history":
        return """I uploaded a codex-session-handoff evidence pack.

Please run Phase 1 only. Do not write the final report yet.

Task: build a research/code workspace history map from the uploaded pack.

Use the pack's WORKSPACE_MAP, DOCS_AUTHORITY, HISTORY_TIMELINE, SESSION_CLUSTERING, included docs, and selected session digests. Treat session digests as supporting evidence, not final truth.

Please output:
1. The main workstreams detected in the workspace.
2. The likely historical evolution across versions, branches, and experiments.
3. Which revised Markdown documents appear to be the most trustworthy current summaries.
4. Which branches, files, or claims appear superseded, historical, or uncertain.
5. Which session clusters are representative evidence and which look like duplicates/background.
6. A concise list of questions I should manually confirm before you write the final report.

Do not invent files, commands, results, or conclusions. Mark uncertain claims as "unconfirmed".
"""
    return """I uploaded a codex-session-handoff evidence pack.

Please run Phase 1 only. Do not write the final handoff yet.

Task: inspect the uploaded pack and produce a source-of-truth map for the current handoff.

Use DOCS_AUTHORITY and included revised docs before session digests. Treat session digests as supporting evidence only.

Please output:
1. The current scope of this handoff.
2. The documents/files that should be trusted first.
3. What appears completed and usable.
4. What appears superseded or historical.
5. Important unresolved risks.
6. Questions I should manually confirm before you write the final handoff.

Do not invent files, commands, results, or conclusions. Mark uncertain claims as "unconfirmed".
"""


def phase2_next_message(goal: str = "workspace-history") -> str:
    if goal == "workspace-history":
        return """Here are my confirmations and corrections for Phase 1:

[Paste your corrections here. For example:
- Treat these workstreams as important: ...
- Ignore or down-rank these branches: ...
- This conclusion is outdated: ...
- The final report should focus on: ...
]

Please run Phase 2 now.

Using the uploaded evidence pack, your Phase 1 analysis, and my confirmations above, generate the final Markdown report.

The report should include:
1. Overall workspace goal and scope.
2. Historical evolution map.
3. Main workstreams and their roles.
4. Current most trustworthy documents and results.
5. Superseded or historical materials that should not override current summaries.
6. Known problems, weak evidence, and unresolved questions.
7. Recommended cleanup / organization / next research steps.
8. Reading order for the next human or AI agent.
9. Evidence index mapping major claims back to files, docs, or session clusters.

Do not invent unsupported details. Preserve exact paths when available.
"""
    return """Here are my confirmations and corrections for Phase 1:

[Paste your corrections here. For example:
- Trust these documents first: ...
- Ignore or down-rank these old branches: ...
- This issue is still unresolved: ...
- The final handoff should focus on: ...
]

Please run Phase 2 now.

Using the uploaded evidence pack, your Phase 1 analysis, and my confirmations above, generate the final Markdown handoff.

The handoff should include:
1. Goal and scope.
2. Current source of truth.
3. Current state.
4. Repository/workspace map.
5. Key valid decisions.
6. Superseded or historical materials.
7. Completed work and evidence.
8. Known problems and risks.
9. Important commands and verification.
10. Next steps.
11. Instructions for the next AI agent.
12. Evidence index.

Do not invent unsupported details. Preserve exact paths when available.
"""


def write_phase_prompt_files(outdir: Path, goal: str) -> None:
    phase1 = phase1_next_message(goal)
    phase2 = phase2_next_message(goal)
    write_text(outdir / "NEXT_MESSAGE_PHASE1.txt", phase1)
    write_text(outdir / "NEXT_MESSAGE_PHASE2.txt", phase2)
    # Keep legacy names too, because they describe the prompt purpose.
    write_text(outdir / "PROMPT_PHASE1_HISTORY_MAP.md", "# Phase 1 Message to Paste into ChatGPT\n\n" + phase1)
    write_text(outdir / "PROMPT_PHASE2_FINAL_REPORT.md", "# Phase 2 Message to Paste into ChatGPT\n\n" + phase2)

def command_pack(args: argparse.Namespace) -> int:
    """One-command workflow: select sessions, digest, snapshot, workspace map, docs, bundle."""
    base = Path(args.outdir).expanduser()
    digest_dir = base / "session_digests"
    base.mkdir(parents=True, exist_ok=True)
    digest_dir.mkdir(parents=True, exist_ok=True)

    focus_arg = args.focus or args.project
    project = None
    focus_label = focus_arg or ""
    if focus_arg:
        candidate = Path(focus_arg).expanduser()
        if candidate.exists():
            project = candidate.resolve()
            focus_label = project.name
    workspace_root = Path(args.workspace_root).expanduser().resolve() if args.workspace_root else None
    keywords = split_patterns(args.keywords)
    if focus_arg and not project:
        keywords.append(focus_arg)

    selected, selection_rows = select_sessions(
        Path(args.root).expanduser(),
        limit=args.limit,
        since=args.since,
        focus_dir=project,
        workspace_root=workspace_root,
        filter_focus=args.filter_project,
        filter_workspace=args.filter_workspace,
        min_score=args.min_score,
        extra_keywords=keywords,
        focus_label=focus_label,
    )
    if args.goal == "workspace-history" and selection_rows:
        selected = selected_rows_by_cluster(selection_rows, args.limit, args.history_depth)
    if not selected:
        print("No sessions selected. Try lowering --min-score, increasing -n, or disabling filters.", file=sys.stderr)
        write_session_selection_report(base, selection_rows, selected)
        return 1

    write_session_selection_report(base, selection_rows, selected)
    clustering_path = write_session_clustering_report(base, selection_rows, selected)

    render_args = argparse.Namespace(
        no_redact=args.no_redact,
        include_raw=False,
        include_reasoning=False,
        include_tool_outputs=False,
        include_system_files=False,
        mode="digest",
        max_message_chars=args.max_message_chars or 1200,
        max_tool_output_chars=None,
        max_raw_event_chars=None,
        max_files=args.max_digest_files or 45,
        max_tool_calls=args.max_tool_calls or 35,
        max_tool_outputs=None,
    )
    options = build_render_options_from_args(render_args)
    batch_selected_sessions(selected, digest_dir, options)

    inputs: List[str] = []
    if workspace_root:
        workspace_map = base / "WORKSPACE_MAP.md"
        ws_args = argparse.Namespace(
            workspace_root=str(workspace_root),
            out=str(workspace_map),
            max_depth=args.workspace_depth,
            max_dirs=args.max_workspace_dirs,
        )
        command_workspace(ws_args)
        inputs.append(str(workspace_map))

    if project:
        snap = base / "PROJECT_SNAPSHOT.md"
        snap_args = argparse.Namespace(
            project=str(project),
            out=str(snap),
            max_depth=args.max_depth,
            max_files=args.max_files,
        )
        command_snapshot(snap_args)
        inputs.append(str(snap))

    doc_inputs = collect_doc_inputs(
        project=project,
        workspace_root=workspace_root,
        include_docs=split_patterns(args.include_docs),
        auto_docs=args.auto_docs,
        outdir=base,
        max_doc_chars=args.max_doc_chars,
        focus=focus_label or (project.name if project else None),
        max_docs=args.max_docs,
        doc_first=args.doc_first,
    )
    prompt_inputs = write_prompt_guides(base, args.goal)

    # Doc-first order: summary and prompt first, then authority ranking and revised docs, then session evidence.
    if args.goal == "workspace-history":
        timeline_path = write_history_timeline(base, selection_rows, doc_authority_path=doc_inputs[0] if doc_inputs else None)
        evidence_inputs = prompt_inputs + doc_inputs + [timeline_path, clustering_path]
    else:
        evidence_inputs = doc_inputs + [clustering_path]

    evidence_inputs.extend(str(p) for p in sorted(digest_dir.glob("*.md")) if p.name != "INDEX.md")
    pack_summary = write_pack_summary(base, args, selected, selection_rows, doc_inputs, evidence_inputs)
    inputs.append(pack_summary)
    inputs.extend(evidence_inputs)
    bundle = base / "CHATGPT_HANDOFF_INPUT.md"
    bundle_args = argparse.Namespace(
        inputs=inputs,
        out=str(bundle),
        with_prompt=True,
        max_file_chars=args.max_file_chars,
    )
    command_bundle(bundle_args)

    print()
    write_phase_prompt_files(base, getattr(args, "goal", "workspace-history"))
    print(f"Pack complete: {base}")
    print(f"- Pack summary: {base / 'PACK_SUMMARY.md'}")
    print(f"- Session selection: {base / 'SESSION_SELECTION.md'}")
    print(f"- Session digests: {digest_dir}")
    if workspace_root:
        print(f"- Workspace map: {base / 'WORKSPACE_MAP.md'}")
    if project:
        print(f"- Project snapshot: {base / 'PROJECT_SNAPSHOT.md'}")
    if doc_inputs:
        print(f"- Included docs: {base / 'included_docs'}")
    print(f"- Session clustering: {base / 'SESSION_CLUSTERING.md'}")
    if args.goal == "workspace-history":
        print(f"- History timeline: {base / 'HISTORY_TIMELINE.md'}")
    print(f"- Goal: {args.goal}")
    print(f"- History depth: {args.history_depth}")
    if args.goal == "workspace-history":
        print(f"- Phase 1 prompt: {base / 'PROMPT_PHASE1_HISTORY_MAP.md'}")
        print(f"- Phase 2 prompt: {base / 'PROMPT_PHASE2_FINAL_REPORT.md'}")
    print(f"- ChatGPT input: {bundle}")
    print(f"- Next message phase 1: {base / 'NEXT_MESSAGE_PHASE1.txt'}")
    print(f"- Next message phase 2: {base / 'NEXT_MESSAGE_PHASE2.txt'}")
    return 0


def command_prompt(args: argparse.Namespace) -> int:
    if args.out:
        out = Path(args.out).expanduser()
        write_text(out, CHATGPT_PROJECT_PROMPT)
        print(f"Wrote {out}")
    else:
        print(CHATGPT_PROJECT_PROMPT)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex_handoff.py",
        description="Convert local Codex CLI JSONL sessions into Markdown handoff files.",
    )
    parser.add_argument("--root", default=str(DEFAULT_SESSIONS_DIR), help="Codex sessions root. Default: ~/.codex/sessions")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="List local Codex session files.")
    p.add_argument("-n", "--limit", type=int, default=30, help="Show latest N sessions. Default: 30")
    p.set_defaults(func=command_scan)

    p = sub.add_parser("convert", help="Convert one session to Markdown.")
    p.add_argument("session", help="'latest', a path, or a unique substring/session id")
    p.add_argument("-o", "--out", help="Output Markdown path. If omitted, print to stdout.")
    add_render_args(p)
    p.set_defaults(func=command_convert)

    p = sub.add_parser("outline", help="Create a very small timeline/index for one session.")
    p.add_argument("session", help="'latest', a path, or a unique substring/session id")
    p.add_argument("-o", "--out", help="Output Markdown path. If omitted, print to stdout.")
    add_render_args(p)
    p.set_defaults(func=command_outline)

    p = sub.add_parser("batch", help="Convert many sessions to Markdown files.")
    p.add_argument("-o", "--outdir", default="./codex_session_exports", help="Output directory.")
    p.add_argument("-n", "--limit", type=int, default=20, help="Convert latest N sessions. Default: 20")
    p.add_argument("--since", help="Only convert sessions modified after YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")
    add_render_args(p)
    p.set_defaults(func=command_batch)

    p = sub.add_parser("bundle", help="Bundle Markdown exports into one ChatGPT input file.")
    p.add_argument("inputs", nargs="+", help="Input Markdown files or glob patterns, e.g. './exports/*.md'")
    p.add_argument("-o", "--out", default="./CHATGPT_HANDOFF_INPUT.md", help="Output bundled Markdown file.")
    p.add_argument("--with-prompt", action="store_true", help="Prepend a project-level handoff prompt.")
    p.add_argument("--max-file-chars", type=int, default=120000, help="Truncate each input file to this many chars. 0 disables.")
    p.set_defaults(func=command_bundle)

    p = sub.add_parser("snapshot", help="Create a lightweight project snapshot Markdown.")
    p.add_argument("project", help="Project directory.")
    p.add_argument("-o", "--out", default="./PROJECT_SNAPSHOT.md", help="Output Markdown path.")
    p.add_argument("--max-depth", type=int, default=3, help="Directory walk depth. Default: 3")
    p.add_argument("--max-files", type=int, default=2000, help="Max files to list. Default: 2000")
    p.set_defaults(func=command_snapshot)

    p = sub.add_parser("workspace", help="Create a map of a messy research workspace with many versioned directories.")
    p.add_argument("workspace_root", help="Workspace root, e.g. /home/pcc/project/.../translation_refs")
    p.add_argument("-o", "--out", default="./WORKSPACE_MAP.md", help="Output Markdown path.")
    p.add_argument("--max-depth", type=int, default=2, help="Directory walk depth. Default: 2")
    p.add_argument("--max-dirs", type=int, default=120, help="Max directories to list. Default: 120")
    p.set_defaults(func=command_workspace)

    p = sub.add_parser("history", help="Shortcut: build a broad workspace-history evidence pack.")
    p.add_argument("workspace_root", help="Workspace root, e.g. /path/to/research_workspace")
    p.add_argument("-o", "--outdir", default="./codex_handoff_history", help="Output directory.")
    p.add_argument("-n", "--limit", type=int, default=12, help="Max representative sessions. Default: 12")
    p.add_argument("--since", help="Only include sessions modified after YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")
    p.add_argument("--history-depth", choices=["light", "balanced", "complete"], default="balanced", help="Representative sessions per cluster. Default: balanced")
    p.add_argument("--keywords", default=DEFAULT_HISTORY_KEYWORDS, help="Comma-separated keywords. Default: common version/research terms.")
    p.add_argument("--include-docs", default="", help="Comma-separated doc globs to include.")
    p.add_argument("--max-docs", type=int, default=18, help="Max ranked Markdown docs to include. Default: 18")
    p.add_argument("--max-doc-chars", type=int, default=50000, help="Max chars per included document. Default: 50000")
    p.add_argument("--workspace-depth", type=int, default=2, help="Workspace map directory depth. Default: 2")
    p.add_argument("--max-workspace-dirs", type=int, default=120, help="Max dirs in WORKSPACE_MAP.md. Default: 120")
    p.add_argument("--max-file-chars", type=int, default=60000, help="Per-file cap inside bundled ChatGPT input. Default: 60000")
    p.add_argument("--max-message-chars", type=int, default=None, help="Override max chars per message in session digests.")
    p.add_argument("--max-digest-files", type=int, default=None, help="Override max files listed in each session digest.")
    p.add_argument("--max-tool-calls", type=int, default=None, help="Override max tool calls listed in each session digest.")
    p.add_argument("--min-score", type=int, default=1, help="Minimum session relevance score. Default: 1")
    p.add_argument("--no-redact", action="store_true", help="Disable secret-like value redaction.")
    p.set_defaults(func=command_history)

    p = sub.add_parser("handoff", help="Shortcut: build a focused current-handoff evidence pack.")
    p.add_argument("focus", help="Focus/workstream directory or label.")
    p.add_argument("-o", "--outdir", default="./codex_handoff_current", help="Output directory.")
    p.add_argument("-n", "--limit", type=int, default=5, help="Max selected sessions. Default: 5")
    p.add_argument("--workspace-root", help="Optional messy workspace root for WORKSPACE_MAP.md.")
    p.add_argument("--since", help="Only include sessions modified after YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")
    p.add_argument("--keywords", default="", help="Comma-separated extra keywords for session selection.")
    p.add_argument("--include-docs", default="", help="Comma-separated doc globs to include.")
    p.add_argument("--max-docs", type=int, default=18, help="Max ranked Markdown docs to include. Default: 18")
    p.add_argument("--max-doc-chars", type=int, default=50000, help="Max chars per included document. Default: 50000")
    p.add_argument("--max-depth", type=int, default=3, help="Project snapshot directory walk depth. Default: 3")
    p.add_argument("--max-files", type=int, default=2000, help="Max project files to list. Default: 2000")
    p.add_argument("--workspace-depth", type=int, default=2, help="Workspace map directory depth. Default: 2")
    p.add_argument("--max-workspace-dirs", type=int, default=120, help="Max dirs in WORKSPACE_MAP.md. Default: 120")
    p.add_argument("--max-file-chars", type=int, default=60000, help="Per-file cap inside bundled ChatGPT input. Default: 60000")
    p.add_argument("--max-message-chars", type=int, default=None, help="Override max chars per message in session digests.")
    p.add_argument("--max-digest-files", type=int, default=None, help="Override max files listed in each session digest.")
    p.add_argument("--max-tool-calls", type=int, default=None, help="Override max tool calls listed in each session digest.")
    p.add_argument("--min-score", type=int, default=1, help="Minimum session relevance score. Default: 1")
    p.add_argument("--no-redact", action="store_true", help="Disable secret-like value redaction.")
    p.set_defaults(func=command_handoff)

    p = sub.add_parser("pack", help="Practical one-command workflow: select sessions, snapshot focus project, map workspace, include key docs, and bundle for ChatGPT.")
    p.add_argument("-o", "--outdir", default="./codex_handoff_pack", help="Output directory.")
    p.add_argument("-n", "--limit", type=int, default=10, help="Use top N selected sessions. Default: 10")
    p.add_argument("--since", help="Only include sessions modified after YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")
    p.add_argument("--project", help="Backward-compatible alias for --focus when --focus is not set.")
    p.add_argument("--focus", help="Current workstream/focus directory or label path. Prefer this over --project for messy research workspaces.")
    p.add_argument("--goal", choices=["current-handoff", "workspace-history", "audit", "continue-coding"], default="current-handoff", help="Purpose of the pack. Use workspace-history for broad evolution maps. Default: current-handoff")
    p.add_argument("--history-depth", choices=["light", "balanced", "complete"], default="balanced", help="For workspace-history: representative sessions per cluster. light=1, balanced=up to 2, complete=top-N by score. Default: balanced")
    p.add_argument("--workspace-root", help="Messy research workspace root, e.g. .../translation_refs. Generates WORKSPACE_MAP.md and can score sessions.")
    p.add_argument("--filter-project", action="store_true", help="Select sessions by relevance to --focus/--project instead of pure recency. Requires focus hits or keyword hits.")
    p.add_argument("--filter-sessions-by-focus", dest="filter_project", action="store_true", help="Alias for --filter-project.")
    p.add_argument("--filter-workspace", action="store_true", help="Select sessions by relevance to --workspace-root instead of pure recency.")
    p.add_argument("--min-score", type=int, default=1, help="Minimum session relevance score when filtering. Default: 1")
    p.add_argument("--keywords", default="", help="Comma-separated extra keywords for session selection, e.g. v16,manuscript_v3,GSEA")
    p.add_argument("--include-docs", default="", help="Comma-separated doc globs to include, relative to --project and --workspace-root.")
    p.add_argument("--auto-docs", action="store_true", help="Automatically include README/LOGIC/HANDOFF/audit/alignment/manuscript docs from focus project.")
    p.add_argument("--doc-first", action="store_true", default=True, help="Rank revised Markdown docs as primary evidence before sessions. Default: enabled.")
    p.add_argument("--max-docs", type=int, default=18, help="Max authoritative Markdown docs to include. Default: 18")
    p.add_argument("--max-doc-chars", type=int, default=50000, help="Max chars per included project document. Default: 50000")
    p.add_argument("--max-depth", type=int, default=3, help="Project snapshot directory walk depth. Default: 3")
    p.add_argument("--max-files", type=int, default=2000, help="Max project files to list. Default: 2000")
    p.add_argument("--workspace-depth", type=int, default=2, help="Workspace map directory depth. Default: 2")
    p.add_argument("--max-workspace-dirs", type=int, default=120, help="Max dirs in WORKSPACE_MAP.md. Default: 120")
    p.add_argument("--max-file-chars", type=int, default=60000, help="Per-file cap inside bundled ChatGPT input. Default: 60000")
    p.add_argument("--max-message-chars", type=int, default=None, help="Override max chars per message in session digests.")
    p.add_argument("--max-digest-files", type=int, default=None, help="Override max files listed in each session digest.")
    p.add_argument("--max-tool-calls", type=int, default=None, help="Override max tool calls listed in each session digest.")
    p.add_argument("--no-redact", action="store_true", help="Disable secret-like value redaction.")
    p.set_defaults(func=command_pack)

    p = sub.add_parser("prompt", help="Print or write the ChatGPT handoff prompt.")
    p.add_argument("-o", "--out", help="Output prompt path.")
    p.set_defaults(func=command_prompt)

    return parser


def add_render_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--mode", choices=sorted(MODE_PRESETS), default="digest",
                   help="Export detail level. digest is best for ChatGPT handoff input; full is archival.")
    p.add_argument("--no-redact", action="store_true", help="Disable secret-like value redaction.")
    p.add_argument("--include-raw", action="store_true", help="Append raw JSON events for debugging.")
    p.add_argument("--include-reasoning", action="store_true", help="Include reasoning/analysis-like events. Not recommended.")
    p.add_argument("--include-tool-outputs", action="store_true", help="Include tool outputs even in digest mode.")
    p.add_argument("--include-system-files", action="store_true", help="Keep noisy system paths such as .codex skills and VS Code server files.")
    p.add_argument("--max-message-chars", type=int, default=None, help="Override max chars per message/tool call.")
    p.add_argument("--max-tool-output-chars", type=int, default=None, help="Override max chars per tool output.")
    p.add_argument("--max-raw-event-chars", type=int, default=None, help="Override max chars per raw event when --include-raw is set.")
    p.add_argument("--max-files", type=int, default=None, help="Override max ranked files to show.")
    p.add_argument("--max-tool-calls", type=int, default=None, help="Override max tool calls to show.")
    p.add_argument("--max-tool-outputs", type=int, default=None, help="Override max tool outputs to show.")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
