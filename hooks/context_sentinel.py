#!/usr/bin/env python3
"""
barnacle context sentinel — a UserPromptSubmit hook for Claude Code.

THE INSIGHT THIS AUTOMATES: after an idle gap longer than the prompt-cache TTL
(5 min), your next message pays a full-context rewrite ANYWAY — the cache is
already dead. That makes every post-idle wake a FREE compaction point: cleaning
the session at that exact moment costs almost nothing extra, and everything
after it runs on a fraction of the context. Long-running, mostly-idle sessions
(the multi-project tmux pattern) hit this constantly.

WHAT IT DOES: when you submit a prompt to a session whose resident context
exceeds BARNACLE_CTX_TOKENS *and* that has been idle longer than BARNACLE_IDLE_S,
it intervenes, in one of two modes (BARNACLE_MODE):

  advise (default) : injects context telling the model to surgically compact
                     first (keep the dialogue and recent turns; drop stale tool
                     traffic — measured at 75-80% of long sessions, ~99% stale),
                     then handle your request.
  block            : bounces your prompt back with the exact /compact command to
                     run first. Zero-trust mode: nothing happens automatically.

Config (env, all optional):
  BARNACLE_CTX_TOKENS  resident-context threshold (default 150000)
  BARNACLE_IDLE_S      idle threshold in seconds  (default 3600)
  BARNACLE_MODE        advise | block | off       (default advise)

Install (~/.claude/settings.json):
  "hooks": { "UserPromptSubmit": [ { "hooks": [ { "type": "command",
    "command": "python3 /path/to/barnacle/hooks/context_sentinel.py" } ] } ] }

Stdlib only. Fails open: any error -> exit 0, prompt proceeds untouched.
"""
import datetime
import json
import os
import sys

CTX_THRESHOLD = int(os.environ.get("BARNACLE_CTX_TOKENS", "150000"))
IDLE_S = int(os.environ.get("BARNACLE_IDLE_S", "3600"))
MODE = os.environ.get("BARNACLE_MODE", "advise").lower()

SURGICAL = (
    "keep the last 15 turns verbatim and every decision, open task, file path and "
    "piece of working state; aggressively drop old tool outputs and old tool inputs "
    "(command results, file reads, edit payloads already applied) — durable facts "
    "are already in files/memory"
)


def last_state(transcript_path):
    """(resident_tokens, idle_seconds) from the transcript's last usage entry."""
    ctx = 0
    last_ts = None
    try:
        with open(transcript_path, errors="ignore") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                u = (d.get("message") or {}).get("usage") or {}
                if not u:
                    continue
                c = (u.get("input_tokens", 0) or 0) + \
                    (u.get("cache_read_input_tokens", 0) or 0) + \
                    (u.get("cache_creation_input_tokens", 0) or 0)
                if c:
                    ctx = c
                ts = d.get("timestamp")
                if ts:
                    last_ts = ts
    except OSError:
        return 0, 0
    idle = 0
    if last_ts:
        try:
            t = datetime.datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            idle = (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()
        except Exception:
            pass
    return ctx, idle


def main():
    if MODE == "off":
        return
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    transcript = payload.get("transcript_path") or ""
    if not transcript or not os.path.exists(transcript):
        return
    ctx, idle = last_state(transcript)
    if ctx < CTX_THRESHOLD or idle < IDLE_S:
        return

    ctx_k = ctx // 1000
    idle_h = idle / 3600
    if MODE == "block":
        print(json.dumps({
            "decision": "block",
            "reason": (
                f"[barnacle] This session holds ~{ctx_k}K tokens of context and has been idle "
                f"{idle_h:.1f}h — the prompt cache is cold, so compacting RIGHT NOW is free "
                f"(the full rewrite was going to happen anyway). Run:\n\n"
                f"  /compact {SURGICAL}\n\n"
                f"…or /clear if you are between tasks. Then resend your message. "
                f"(Disable with BARNACLE_MODE=off)"
            ),
        }))
        return
    # advise mode: let the prompt through, but instruct the model to clean first.
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                f"[barnacle sentinel] Resident context is ~{ctx_k}K tokens and this session was "
                f"idle {idle_h:.1f}h (cache cold — compaction at this moment costs nothing extra; "
                f"in measured long sessions 75-80% of context is tool traffic, ~99% of it stale). "
                f"Before addressing the user's request: if a SlashCommand tool is available, run "
                f"`/compact {SURGICAL}`. If it is not available, do NOT interrupt the task — "
                f"finish the user's request first and then, in your final message, recommend "
                f"running `/compact {SURGICAL}` verbatim, briefly noting the session's size."
            ),
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail open — never break the user's prompt
    sys.exit(0)
