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
  advise (default) — inject instructions asking the model to compact first
                     (works everywhere; depends on the model following through).
  auto             — deferred compaction when the session lives in a tmux pane:
                     your prompt goes through UNTOUCHED; a detached watcher
                     waits for the turn to end and the pane to be verifiably
                     calm for FOREMAN_CALM_S (default 5 min — a reading pause
                     is not an absence), then types the surgical /compact
                     itself. Never blocks, never resends, never races you at
                     the keyboard. Falls back to advise outside tmux.
  block            — bounce the prompt back with the exact /compact to run.
  off              — disabled.

SAFETY (auto mode): the watcher only types into a pane that has been calm
(empty prompt, no running turn, no open dialog) continuously for
FOREMAN_CALM_S. Anything else — you typing, a permission dialog, a feedback
prompt — and it keeps waiting, up to 45 min, then gives up silently and
retries on the next trigger. After typing it verifies the command actually
submitted (the slash-command menu can swallow the first Enter) and never
presses Enter more than twice. A heartbeat marker (compacting-<sid>)
prevents stacked watchers.
NOTE: auto mode injects keystrokes into your own tmux panes — read
orchestrate() before adopting it; advise is the zero-injection default.

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

# The watcher's in-flight marker is refreshed every loop; older than this and
# the watcher is presumed dead, so a new trigger may spawn a fresh one.
MARKER_FRESH_S = 180
# The pane must be continuously calm this long before the watcher types — a
# 30s gap is a human reading, not a human gone (learned the hard way).
CALM_S = int(os.environ.get("FOREMAN_CALM_S", "300"))


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


# ─────────────────────────── deferred-compact watcher ───────────────────────

def _pane_calm(pane):
    """True only when the pane is verifiably safe to type into: an empty
    prompt box, no running turn, and no open dialog of any kind."""
    out = subprocess.run(["tmux", "capture-pane", "-t", pane, "-p"],
                         capture_output=True, text=True).stdout
    if not out:
        return False
    if "to interrupt" in out:            # a turn is running
        return False
    for needle in ("Do you want", "to proceed", "How is Claude doing"):
        if needle in out:                # permission / feedback dialog is up
            return False
    prompts = [l for l in out.splitlines() if l.lstrip().startswith("❯")]
    return bool(prompts) and not any(l.lstrip().lstrip("❯").strip() for l in prompts)


def _cmd_stuck(pane):
    """True if our /compact is still sitting unsent in the input box."""
    out = subprocess.run(["tmux", "capture-pane", "-t", pane, "-p"],
                         capture_output=True, text=True).stdout
    return any(l.lstrip().startswith("❯") and "/compact" in l
               for l in out.splitlines())


def orchestrate(pane, marker, sid):
    """Runs DETACHED after the user's prompt went through untouched: wait for
    the turn to finish and the pane to stay CONTINUOUSLY calm for CALM_S
    (default 5 min — a human pausing to read is not a human gone), then type
    the surgical /compact. Deferred-only, by design — the v0.7.0
    block-and-resend approach raced the human at the keyboard and could
    strand messages; letting the turn run and compacting a real gap after it
    is race-free. Gives up silently after 45 min (next trigger retries)."""
    time.sleep(3)  # let the just-submitted turn actually start
    deadline = time.time() + 2700
    need = max(1, CALM_S // 10)
    stable = 0
    while time.time() < deadline:
        try:
            os.utime(marker, None)       # heartbeat: holds the in-flight lock
        except OSError:
            pass
        stable = stable + 1 if _pane_calm(pane) else 0
        if stable >= need:
            break
        time.sleep(10)
    else:
        _log(f"session={sid} auto: no {CALM_S // 60}m-calm gap within 45m — "
             f"skipped, will retry on next trigger")
        _rm(marker)
        return
    subprocess.run(["tmux", "send-keys", "-t", pane, "-l", COMPACT_CMD])
    time.sleep(0.5)
    subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"])
    time.sleep(2)
    if _cmd_stuck(pane):
        # the slash-command autocomplete menu can swallow the first Enter
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"])
        time.sleep(2)
    if _cmd_stuck(pane):
        _log(f"session={sid} auto: /compact stuck in the input box — left as "
             f"typed; press Enter to run it or clear it")
        _rm(marker)
        return
    _log(f"session={sid} auto: /compact typed")
    time.sleep(5)
    end = time.time() + 360
    done = False
    while time.time() < end:
        try:
            os.utime(marker, None)
        except OSError:
            pass
        if _pane_calm(pane):
            done = True
            break
        time.sleep(5)
    _log(f"session={sid} auto: compaction finished" if done else
         f"session={sid} auto: compaction still running after 6m — leaving it be")
    _rm(marker)


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


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
    pane = os.environ.get("TMUX_PANE", "")
    if pane:
        # pane-map: the sentinel runs INSIDE the session, so it is the one
        # component that knows pane<->session<->transcript for certain. Every
        # invocation (even a pass) registers the mapping; `foreman watch`
        # consumes it to act precisely where shared cwds make guessing unsafe.
        try:
            d = os.path.join(STATE_DIR, "pane-map")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, sid), "w") as f:
                json.dump({"pane": pane, "transcript": transcript,
                           "ts": time.time()}, f)
        except Exception:
            pass
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

    if MODE == "auto" and pane:
        marker = os.path.join(STATE_DIR, f"compacting-{sid}")
        try:
            in_flight = os.path.exists(marker) and \
                (time.time() - os.path.getmtime(marker)) < MARKER_FRESH_S
        except OSError:
            in_flight = False
        if in_flight:
            _log(f"session={sid} ctx={ctx_k}K idle={idle / 60:.0f}m -> FIRE({why}) "
                 f"mode=auto (compaction already in flight)")
            return
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            open(marker, "w").close()
            subprocess.Popen(
                [sys.executable, os.path.abspath(__file__),
                 "--orchestrate", pane, marker, sid],
                start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _log(f"session={sid} ctx={ctx_k}K idle={idle / 60:.0f}m -> FIRE({why}) "
                 f"mode=auto (compact scheduled for after this turn)")
            return  # prompt proceeds untouched; compaction happens in the gap
        except Exception:
            pass  # fall through to advise on any orchestration failure

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

    fallback = " (auto unavailable: no tmux pane)" if MODE == "auto" else ""
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
