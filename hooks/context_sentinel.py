#!/usr/bin/env python3
"""
foreman context sentinel — a UserPromptSubmit hook for Claude Code.

THE INSIGHT THIS AUTOMATES: long-running sessions accumulate stale tool
traffic (75-80% of resident context in measured whales, ~99% of it >30 turns
old). Every agentic iteration re-reads all of it. Compacting surgically keeps
the dialogue and working state while shedding the dead weight.

TIERS (checked when you message a session):
  idle tier    ctx >= FOREMAN_CTX_TOKENS (150K) and idle >= FOREMAN_IDLE_S (1h)
               — the calm, free moment: the cache is cold anyway.
  urgent tier  ctx >= FOREMAN_CTX_URGENT (500K), NO idle required — at this
               size one more busy turn costs more than the compaction pass.

MODES (FOREMAN_MODE):
  auto (default) — fully automatic when the session lives in a tmux pane:
                   the hook BLOCKS your prompt (saving it first), types the
                   surgical /compact into the pane, waits for it to finish,
                   then resends your original message. No model cooperation
                   required. Falls back to advise outside tmux or when a
                   previous attempt is already in flight.
  advise         — inject instructions asking the model to compact first
                   (works everywhere; depends on the model following through).
  block          — bounce the prompt back with the exact /compact to run.
  off            — disabled.

SAFETY (auto mode): the prompt text is persisted to
~/.local/state/foreman/pending-<sid> BEFORE anything else and only deleted
after a successful resend — a failure can never lose what you typed. An
in-flight marker (10 min) prevents block/compact loops. The orchestrator only
types when the pane is verifiably calm, and gives up loudly (log + kept
pending file) rather than guessing. NOTE: auto mode injects keystrokes into
your own tmux panes — read orchestrate() before adopting it, or use
FOREMAN_MODE=advise for a zero-injection default.

Every invocation logs to ~/.local/state/foreman/sentinel.log.
The hook fails open: any error and your prompt proceeds untouched.
"""
import datetime
import json
import os
import subprocess
import sys
import time

CTX_THRESHOLD = int(os.environ.get("FOREMAN_CTX_TOKENS", "150000"))
IDLE_S = int(os.environ.get("FOREMAN_IDLE_S", "3600"))
CTX_URGENT = int(os.environ.get("FOREMAN_CTX_URGENT", "500000"))
IDLE_URGENT_S = int(os.environ.get("FOREMAN_IDLE_URGENT_S", "0"))
# Default is 'advise' — zero keystroke injection, works everywhere. 'auto' is
# powerful but types into your tmux panes, so it is opt-in on purpose:
#   foreman hook install --mode auto   (or export FOREMAN_MODE=auto)
MODE = os.environ.get("FOREMAN_MODE", "advise").lower()
STATE_DIR = os.path.expanduser(os.environ.get("FOREMAN_STATE", "~/.local/state/foreman"))

SURGICAL = (
    "keep the last 15 turns verbatim and every decision, open task, file path and "
    "piece of working state; aggressively drop old tool outputs and old tool inputs "
    "(command results, file reads, edit payloads already applied) — durable facts "
    "are already in files/memory"
)
COMPACT_CMD = f"/compact {SURGICAL}"


def _log(msg):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "sentinel.log"), "a") as f:
            ts = datetime.datetime.now().isoformat(timespec="seconds")
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


def last_state(transcript_path):
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


# ─────────────────────────── auto-compact orchestrator ──────────────────────

def _pane_calm(pane):
    out = subprocess.run(["tmux", "capture-pane", "-t", pane, "-p"],
                         capture_output=True, text=True).stdout
    if "to interrupt" in out:
        return False
    prompts = [l for l in out.splitlines() if l.lstrip().startswith("❯")]
    return bool(prompts) and not any(l.lstrip().lstrip("❯").strip() for l in prompts)


def orchestrate(pane, pending_file, sid):
    """Runs DETACHED after the hook blocked the prompt: type the surgical
    /compact into the session's own pane, wait for it to finish, resend the
    saved prompt. Gives up loudly (log + pending file kept) on any doubt."""
    time.sleep(1.5)  # let the TUI settle after the block
    deadline = time.time() + 20
    while time.time() < deadline and not _pane_calm(pane):
        time.sleep(1)
    if not _pane_calm(pane):
        _log(f"session={sid} auto: pane never settled — prompt kept at {pending_file}")
        return
    subprocess.run(["tmux", "send-keys", "-t", pane, "-l", COMPACT_CMD])
    time.sleep(0.5)
    subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"])
    _log(f"session={sid} auto: /compact typed, waiting")
    time.sleep(5)
    deadline = time.time() + 360
    while time.time() < deadline:
        if _pane_calm(pane):
            break
        time.sleep(2)
    else:
        _log(f"session={sid} auto: compaction still running after 6m — "
             f"prompt kept at {pending_file}; paste it manually")
        return
    try:
        prompt = open(pending_file, errors="ignore").read()
    except OSError:
        _log(f"session={sid} auto: pending file vanished")
        return
    if prompt.strip():
        # multiline-safe resend via a tmux paste buffer (consumed with -d)
        subprocess.run(["tmux", "load-buffer", "-b", "foreman-resend", pending_file])
        subprocess.run(["tmux", "paste-buffer", "-b", "foreman-resend", "-d", "-t", pane])
        time.sleep(0.5)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"])
    try:
        os.remove(pending_file)
    except OSError:
        pass
    _log(f"session={sid} auto: compaction done, prompt resent")


# ──────────────────────────────── the hook ──────────────────────────────────

def main():
    if MODE == "off":
        _log("mode=off")
        return
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    transcript = payload.get("transcript_path") or ""
    if not transcript or not os.path.exists(transcript):
        _log("no-transcript")
        return
    sid = os.path.basename(transcript)[:8]
    ctx, idle = last_state(transcript)
    scheduled = ctx >= CTX_THRESHOLD and idle >= IDLE_S
    urgent = ctx >= CTX_URGENT and idle >= IDLE_URGENT_S
    if not (scheduled or urgent):
        _log(f"session={sid} ctx={ctx // 1000}K idle={idle / 60:.0f}m -> pass "
             f"(idle-tier {CTX_THRESHOLD // 1000}K/{IDLE_S // 60}m · "
             f"urgent-tier {CTX_URGENT // 1000}K/{IDLE_URGENT_S // 60}m)")
        return
    why = "urgent" if urgent and not scheduled else "idle"
    ctx_k = ctx // 1000

    pane = os.environ.get("TMUX_PANE", "")
    pending = os.path.join(STATE_DIR, f"pending-{sid}")
    in_flight = os.path.exists(pending) and (time.time() - os.path.getmtime(pending)) < 600

    if MODE == "auto" and pane and not in_flight:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(pending, "w") as f:
                f.write(payload.get("prompt") or "")
            subprocess.Popen(
                [sys.executable, os.path.abspath(__file__),
                 "--orchestrate", pane, pending, sid],
                start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _log(f"session={sid} ctx={ctx_k}K idle={idle / 60:.0f}m -> FIRE({why}) mode=auto")
            print(json.dumps({
                "decision": "block",
                "reason": (
                    f"[foreman] This session holds ~{ctx_k}K tokens (mostly stale tool "
                    f"traffic). Auto-compacting now — your message was saved and will be "
                    f"sent automatically when it finishes (~1-2 min)."
                ),
            }))
        except Exception:
            pass  # fall through to advise below on any orchestration failure
        else:
            return

    if MODE == "block":
        _log(f"session={sid} ctx={ctx_k}K idle={idle / 60:.0f}m -> FIRE({why}) mode=block")
        print(json.dumps({
            "decision": "block",
            "reason": (
                f"[foreman] ~{ctx_k}K tokens resident, idle {idle / 3600:.1f}h. "
                f"Compact first, then resend:\n\n  {COMPACT_CMD}\n\n"
                f"(or /clear if between tasks; FOREMAN_MODE=off disables)"
            ),
        }))
        return

    fallback = " (auto unavailable: no tmux pane)" if MODE == "auto" and not pane else \
               " (auto attempt already in flight)" if MODE == "auto" else ""
    _log(f"session={sid} ctx={ctx_k}K idle={idle / 60:.0f}m -> FIRE({why}) mode=advise{fallback}")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                f"[foreman] Resident context is ~{ctx_k}K tokens (in measured long "
                f"sessions 75-80% is stale tool traffic). If a SlashCommand tool is "
                f"available, run `{COMPACT_CMD}` before addressing the user's request. "
                f"If not, finish the request first, then recommend running "
                f"`{COMPACT_CMD}` verbatim in your final message."
            ),
        }
    }))


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--orchestrate":
        try:
            orchestrate(sys.argv[2], sys.argv[3], sys.argv[4])
        except Exception as e:
            _log(f"auto: orchestrator crashed: {e}")
        sys.exit(0)
    try:
        main()
    except Exception:
        pass  # fail open — never break the user's prompt
    sys.exit(0)
