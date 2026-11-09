"""Export the coding-assistant session for this project as one Markdown transcript.

Usage:
    export_transcripts.py SESSION_DIR SESSION_ID [--out docs/AI_TRANSCRIPT.md] [--env .env]

SESSION_DIR is the assistant's project folder holding `<SESSION_ID>.jsonl`, the session
I worked in from the first message to submission. It is rendered in order: my
messages, the assistant's replies, and its tool calls and results (long text and tool
output are clipped, and the clip is marked).

Two edits only. The Anthropic API key, matched by the literal value of
ANTHROPIC_API_KEY in --env and by the key's shape, becomes [REDACTED-KEY]. Keys of the
local Langfuse stack become [REDACTED-LANGFUSE-KEY], since the repo's secret scan
rejects any key. The export fails (exit 1) if anything key-shaped survives.
Standard library only, like the other repo scripts.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REDACTED = "[REDACTED-KEY]"
KEY_SHAPE = re.compile("sk-" + "ant-" + r"[A-Za-z0-9_\-]{20,}")
LANGFUSE_REDACTED = "[REDACTED-LANGFUSE-KEY]"
LANGFUSE_SHAPE = re.compile(
    r"\b[ps]k-" + "lf-" + r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
TEXT_LIMIT = 4000
TOOL_LIMIT = 1500
HEADER = """# Coding-assistant transcript

The session I worked in with the coding assistant on this take-home, from reading the
brief to submission, in order. Long messages and tool output are clipped where marked.
Two edits only: the Anthropic API key is replaced with `[REDACTED-KEY]` (and rotated
regardless), and the keys of my local Langfuse stack with `[REDACTED-LANGFUSE-KEY]`,
because the repo's secret scan rejects any key.
"""


def env_key(env_path: Path) -> str | None:
    if not env_path.is_file():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == "ANTHROPIC_API_KEY":
            value = value.strip().strip("'\"")
            return value or None
    return None


def redact(text: str, key: str | None) -> str:
    if key:
        text = text.replace(key, REDACTED)
    return LANGFUSE_SHAPE.sub(LANGFUSE_REDACTED, KEY_SHAPE.sub(REDACTED, text))


def clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [clipped: {len(text) - limit} more characters]"


def fence(text: str) -> str:
    ticks = "````" if "```" in text else "```"
    return f"{ticks}\n{text}\n{ticks}"


def block_text(block: object) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        if block.get("type") == "text":
            return str(block.get("text", ""))
        if "content" in block:
            content = block["content"]
            if isinstance(content, list):
                return "\n".join(block_text(c) for c in content)
            return str(content)
    return ""


def details(summary: str, body: str) -> list[str]:
    return [f"<details><summary>{summary}</summary>", "", body, "", "</details>", ""]


def render(lines: list[str]) -> str:
    out = [HEADER]
    for raw in lines:
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = rec.get("type")
        msg = rec.get("message")
        if kind == "summary" and rec.get("summary"):
            out += ["**Context summary**", "", clip(str(rec["summary"]), TEXT_LIMIT), ""]
            continue
        if kind not in ("user", "assistant") or not isinstance(msg, dict):
            continue
        ts = rec.get("timestamp")
        stamp = f" ({ts[:19].replace('T', ' ')} UTC)" if ts else ""
        content = msg.get("content")
        blocks = [content] if isinstance(content, str) else (content or [])
        for block in blocks:
            if isinstance(block, str) or (isinstance(block, dict) and block.get("type") == "text"):
                text = block_text(block)
                if not text.strip():
                    continue
                who = "User" if kind == "user" else "Assistant"
                out += [f"### {who}{stamp}", "", clip(text, TEXT_LIMIT), ""]
            elif not isinstance(block, dict):
                continue
            elif block.get("type") == "thinking" and block.get("thinking"):
                out += details("Assistant thinking", clip(block["thinking"], TEXT_LIMIT))
            elif block.get("type") == "tool_use":
                args = json.dumps(block.get("input", {}), ensure_ascii=False, indent=1)
                name = block.get("name", "?")
                out += [f"**Tool call: {name}**", "", fence(clip(args, TOOL_LIMIT)), ""]
            elif block.get("type") == "tool_result":
                out += details("Tool result", fence(clip(block_text(block), TOOL_LIMIT)))
    return "\n".join(out) + "\n"


def export(session_dir: Path, session_id: str, out: Path, env_path: Path) -> int:
    source = session_dir / f"{session_id}.jsonl"
    if not source.is_file():
        print(f"no session file {source}", file=sys.stderr)
        return 2
    key = env_key(env_path)
    text = redact(source.read_text(encoding="utf-8", errors="replace"), key)
    body = redact(render(text.splitlines()), key)
    if KEY_SHAPE.search(body) or LANGFUSE_SHAPE.search(body) or (key and key in body):
        print(f"key material survived the redaction; {out} not written", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    print(f"exported {source.name} to {out}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("session_id")
    parser.add_argument("--out", type=Path, default=Path("docs/AI_TRANSCRIPT.md"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    return export(args.session_dir, args.session_id, args.out, args.env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
