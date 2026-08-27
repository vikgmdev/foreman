#!/usr/bin/env python3
"""
foreman — measure where your Claude Code tokens actually go, then cut the part
that matters.

Reads the session transcripts Claude Code already writes locally
(~/.claude*/projects/*/*.jsonl). No API calls, no telemetry, nothing leaves
your machine. Zero dependencies, Python 3.9+.

Usage:
  foreman audit    [--days 7] [--deep]     decompose real spend
  foreman snapshot [--tag NAME]            freeze a baseline
  foreman compare  [--tag NAME]            current window vs a baseline
  foreman ls                               list saved snapshots
  foreman hook install|uninstall|status    manage the context-sentinel hook
  foreman restart [--go]                   recycle idle tmux sessions in place
  foreman session <id|project>             one session's card: context, cost, compactions
  foreman savings                          $ actually saved by sentinel-triggered compactions
  foreman watch [--go]                     sweep: compact calm idle tmux sessions, trim dead fat transcripts
  foreman watch --install                  run that sweep every 15 min (systemd user timer / cron)
  foreman voice build|install              distill YOUR writing style from your own messages; install it as a skill
  foreman update                           update foreman itself
  foreman version

Why: most "token saver" tools optimize output — typically ~10% of real agentic
spend. The dominant cost is resident_context x loop_iterations. Measure yours
before believing anyone's 97% screenshot, including ours.
"""
import argparse
import datetime
import glob
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
from collections import Counter

__version__ = "0.4.0"

FOREMAN_HOME = os.path.dirname(os.path.abspath(__file__))
SENTINEL = os.path.join(FOREMAN_HOME, "hooks", "context_sentinel.py")

# $/Mtok: (input, cache_write_5m, cache_read, output). Override via FOREMAN_PRICES
# (JSON, same shape) when prices change — these are Aug 2026 Anthropic list prices.
PRICES = {
    "opus": (15.0, 18.75, 1.5, 75.0),
    "sonnet": (3.0, 3.75, 0.3, 15.0),
    "haiku": (0.8, 1.0, 0.08, 4.0),
}
if os.environ.get("FOREMAN_PRICES"):
    PRICES.update({k: tuple(v) for k, v in json.loads(os.environ["FOREMAN_PRICES"]).items()})

TTL_S = 300  # 5-minute default prompt-cache TTL; gaps beyond this = cold wake
STATE_DIR = os.path.expanduser(os.environ.get("FOREMAN_STATE", "~/.local/state/foreman"))
SYS_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


# ───────────────────────────── shared helpers ──────────────────────────────

def tier(model):
    m = (model or "").lower()
    for k in PRICES:
        if k in m:
            return k
    return "sonnet"


def parse_ts(s):
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def discover_profiles(explicit=None):
    """Claude Code config dirs: ~/.claude plus any CLAUDE_CONFIG_DIR-style
    ~/.claude-* profile that has session data or settings."""
    if explicit:
        return [os.path.expanduser(explicit)]
    home = os.path.expanduser("~")
    outs = []
    for d in sorted(glob.glob(os.path.join(home, ".claude*"))):
        if ".bak" in os.path.basename(d):
            continue  # backup copies of a profile are not live profiles
        if os.path.isdir(os.path.join(d, "projects")) or \
           os.path.isfile(os.path.join(d, "settings.json")):
            if os.path.isdir(d):
                outs.append(d)
    return outs or [os.path.join(home, ".claude")]


def iter_transcripts(profiles, cutoff):
    """Yield (path, project) for transcripts modified after cutoff, deduped by
    session id (a profile cloned from another carries identical session files)."""
    seen = set()
    for prof in profiles:
        for f in glob.glob(os.path.join(prof, "projects", "*", "*.jsonl")):
            sid = os.path.basename(f)
            if sid in seen:
                continue
            try:
                if datetime.datetime.fromtimestamp(
                    os.path.getmtime(f), datetime.timezone.utc
                ) < cutoff:
                    continue
            except OSError:
                continue
            seen.add(sid)
            yield f, os.path.basename(os.path.dirname(f))


# ───────────────────────────── audit machinery ─────────────────────────────

def collect(profiles, days):
    """Single pass over transcripts -> aggregate metrics dict."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    m = {
        "window_days": days,
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "profiles": profiles,
        "sessions": 0,
        "api_calls": 0,
        "wakes": 0,  # first call of a session, or any call after gap > TTL
        "tok": Counter(),
        "cost": Counter(),
        "cost_by_model": Counter(),
        "cost_by_project": Counter(),
        "gap_buckets": Counter(),
        "ctx_per_call": [],
        "wake_rewrite": [],
        "first_ctx": [],
        "whales": [],
    }
    for f, proj in iter_transcripts(profiles, cutoff):
        s_cost = 0.0
        s_calls = 0
        s_maxctx = 0
        prev_t = None
        first = True
        try:
            fh = open(f, errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                msg = d.get("message") or {}
                u = msg.get("usage") or {}
                if not u:
                    continue
                i = u.get("input_tokens", 0) or 0
                cw = u.get("cache_creation_input_tokens", 0) or 0
                cr = u.get("cache_read_input_tokens", 0) or 0
                o = u.get("output_tokens", 0) or 0
                if i + cw + cr + o == 0:
                    continue
                pi, pw, pr, po = PRICES[tier(msg.get("model"))]
                t = parse_ts(d.get("timestamp", ""))
                gap = (t - prev_t).total_seconds() if (t and prev_t) else None
                if t:
                    prev_t = t
                cold = gap is None or gap > TTL_S
                ctx = i + cw + cr
                m["api_calls"] += 1
                s_calls += 1
                s_maxctx = max(s_maxctx, ctx)
                if len(m["ctx_per_call"]) < 200000:
                    m["ctx_per_call"].append(ctx)
                if first:
                    m["first_ctx"].append(ctx)
                    first = False
                m["tok"]["input"] += i
                m["tok"]["cr"] += cr
                m["tok"]["output"] += o
                c_i = i * pi / 1e6
                c_r = cr * pr / 1e6
                c_o = o * po / 1e6
                c_w = cw * pw / 1e6
                m["cost"]["input"] += c_i
                m["cost"]["cr"] += c_r
                m["cost"]["output"] += c_o
                call_cost = c_i + c_r + c_o + c_w
                if cold:
                    m["wakes"] += 1
                    m["tok"]["cw_cold"] += cw
                    m["cost"]["cw_cold"] += c_w
                    if cw:
                        m["wake_rewrite"].append(cw)
                    if gap is not None:
                        for lo, hi, lbl in (
                            (TTL_S, 3600, "5m-1h"),
                            (3600, 21600, "1h-6h"),
                            (21600, 86400, "6h-24h"),
                            (86400, 10**10, ">24h"),
                        ):
                            if lo < gap <= hi:
                                m["gap_buckets"][lbl] += 1
                                break
                else:
                    m["tok"]["cw_warm"] += cw
                    m["cost"]["cw_warm"] += c_w
                m["cost_by_model"][tier(msg.get("model"))] += call_cost
                m["cost_by_project"][proj] += call_cost
                s_cost += call_cost
        if s_calls:
            m["sessions"] += 1
            m["whales"].append((round(s_cost, 2), s_calls, s_maxctx, proj, f))
    m["whales"] = sorted(m["whales"], reverse=True)[:10]
    return m


def summarize(m):
    total = sum(m["cost"].values())
    ctx = m["ctx_per_call"]
    wr = m["wake_rewrite"]
    whale_cost = sum(w[0] for w in m["whales"])
    return {
        "generated": m["generated"],
        "window_days": m["window_days"],
        "sessions": m["sessions"],
        "api_calls": m["api_calls"],
        "wakes": m["wakes"],
        "total_cost": round(total, 2),
        "cost_per_wake": round(total / max(1, m["wakes"]), 3),
        "cost_per_call": round(total / max(1, m["api_calls"]), 4),
        "ctx_per_call_median": int(statistics.median(ctx)) if ctx else 0,
        "ctx_per_call_p90": int(sorted(ctx)[int(len(ctx) * 0.9)]) if ctx else 0,
        "wake_rewrite_median": int(statistics.median(wr)) if wr else 0,
        "first_ctx_median": int(statistics.median(m["first_ctx"])) if m["first_ctx"] else 0,
        "cost_split": {k: round(v, 2) for k, v in m["cost"].items()},
        "cost_by_model": {k: round(v, 2) for k, v in m["cost_by_model"].items()},
        "whale_cost": round(whale_cost, 2),
        "whale_share_pct": round(whale_cost / max(0.01, total) * 100, 1),
        "gap_buckets": dict(m["gap_buckets"]),
    }


def fmt_money(v):
    return f"${v:,.2f}" if v < 100 else f"${v:,.0f}"


def print_audit(m, deep=False):
    s = summarize(m)
    total = s["total_cost"]
    print(f"foreman audit — last {m['window_days']}d across {len(m['profiles'])} profile(s)")
    print(f"  {s['sessions']} sessions · {s['api_calls']:,} API calls · {s['wakes']:,} cold wakes\n")
    print("WHERE THE MONEY GOES (est. list prices; subscriptions burn the same budget)")
    rows = [
        ("cache_read (agentic loop re-reading context)", s["cost_split"].get("cr", 0)),
        ("cache_write WARM (in-turn deltas)", s["cost_split"].get("cw_warm", 0)),
        ("cache_write COLD (wake after idle > TTL)", s["cost_split"].get("cw_cold", 0)),
        ("output", s["cost_split"].get("output", 0)),
        ("input (uncached)", s["cost_split"].get("input", 0)),
    ]
    for lbl, v in rows:
        print(f"  {lbl:46} {fmt_money(v):>10}  {v / max(0.01, total) * 100:5.1f}%")
    print(f"  {'TOTAL':46} {fmt_money(total):>10}\n")
    print("THE NUMBERS THAT PREDICT YOUR NEXT MESSAGE'S COST")
    print(f"  $/user-turn (cold wake)      {fmt_money(s['cost_per_wake'])}")
    print(f"  resident ctx / call (median) {s['ctx_per_call_median'] / 1000:,.0f}K   p90 {s['ctx_per_call_p90'] / 1000:,.0f}K")
    print(f"  rewritten per wake (median)  {s['wake_rewrite_median'] / 1000:,.0f}K")
    print(f"  fresh-session prefix floor   {s['first_ctx_median'] / 1000:,.0f}K")
    print(f"\n  top-10 sessions = {s['whale_share_pct']}% of all cost ({fmt_money(s['whale_cost'])})")
    for c, n, mx, proj, _ in m["whales"]:
        print(f"    {fmt_money(c):>9}  {n:6,} calls  max ctx {mx / 1000:5.0f}K  {proj[:44]}")
    if m["cost_by_model"]:
        print("\n  by model: " + " · ".join(
            f"{k} {fmt_money(v)} ({v / max(0.01, total) * 100:.0f}%)"
            for k, v in m["cost_by_model"].most_common()))
    if deep:
        print("\nWHALE AUTOPSY (composition of the biggest sessions' content)")
        for c, n, mx, proj, path in m["whales"][:5]:
            comp = autopsy(path)
            tot = sum(v for k, v in comp.items() if k not in ("thinking", "stale_pct")) or 1
            print(f"  {proj[:38]:38} tool_traffic {(comp['tool_results'] + comp['tool_inputs']) * 100 // tot}% "
                  f"(results {comp['tool_results'] * 100 // tot}% / inputs {comp['tool_inputs'] * 100 // tot}%) · "
                  f"dialogue {(comp['assistant_text'] + comp['user_text']) * 100 // tot}% · "
                  f"stale tool results {comp['stale_pct']}%")


def autopsy(path):
    """Char-level composition of one transcript (approximates resident makeup)."""
    cat = Counter()
    id2tool = {}
    nturn = 0
    events = []
    with open(path, errors="ignore") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            msg = d.get("message") or {}
            if msg.get("usage"):
                nturn += 1
            content = msg.get("content")
            if d.get("type") == "assistant" and isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text":
                        cat["assistant_text"] += len(b.get("text") or "")
                    elif bt == "thinking":
                        cat["thinking"] += len(b.get("thinking") or "")
                    elif bt == "tool_use":
                        id2tool[b.get("id")] = b.get("name") or "?"
                        cat["tool_inputs"] += len(json.dumps(b.get("input", {}), ensure_ascii=False))
            elif d.get("type") == "user":
                blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_result":
                        c = b.get("content")
                        txt = c if isinstance(c, str) else "".join(
                            (x.get("text") or "") for x in c if isinstance(x, dict)) if isinstance(c, list) else ""
                        body = SYS_REMINDER.sub("", txt)
                        cat["tool_results"] += len(body)
                        events.append((nturn, len(body)))
                    elif b.get("type") == "text":
                        cat["user_text"] += len(SYS_REMINDER.sub("", b.get("text") or ""))
    stale = sum(sz for tn, sz in events if tn < nturn - 30)
    cat["stale_pct"] = stale * 100 // max(1, cat["tool_results"] or 1)
    return cat


# ─────────────────────────── snapshot / compare ────────────────────────────

def cmd_snapshot(m, tag):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"{tag}.json")
    with open(path, "w") as f:
        json.dump(summarize(m), f, indent=2)
    print(f"snapshot '{tag}' saved -> {path}")


def cmd_compare(m, tag):
    path = os.path.join(STATE_DIR, f"{tag}.json")
    if not os.path.exists(path):
        sys.exit(f"no snapshot '{tag}' — run: foreman snapshot --tag {tag}")
    base = json.load(open(path))
    cur = summarize(m)

    def row(label, key, unit="", better="lower"):
        b, c = base.get(key, 0), cur.get(key, 0)
        if not b:
            return
        delta = (c - b) / b * 100
        good = (delta < 0) == (better == "lower")
        mark = "✅" if good and abs(delta) >= 3 else ("⚠️" if abs(delta) >= 3 else "· ")
        print(f"  {mark} {label:34} {b:>12,.2f}{unit} -> {c:>12,.2f}{unit}   {delta:+6.1f}%")

    print(f"foreman compare — '{tag}' ({base['generated'][:10]}) vs now, {cur['window_days']}d windows")
    print("\nNORMALIZED (workload-independent — these are the honest ones)")
    row("$ per user-turn (cold wake)", "cost_per_wake")
    row("$ per API call", "cost_per_call")
    row("resident ctx/call median (tok)", "ctx_per_call_median")
    row("resident ctx/call p90 (tok)", "ctx_per_call_p90")
    row("rewritten per wake median (tok)", "wake_rewrite_median")
    row("fresh-session prefix (tok)", "first_ctx_median")
    print("\nTOTALS (workload-dependent — context, not proof)")
    row("total cost ($)", "total_cost")
    row("API calls", "api_calls", better="n/a")
    row("cold wakes", "wakes", better="n/a")
    row("whale share of cost (%)", "whale_share_pct")
    print("\nCaveat: totals move with how much you worked. Claim savings only from the")
    print("normalized block, ideally over comparable workloads.")


def cmd_ls():
    os.makedirs(STATE_DIR, exist_ok=True)
    snaps = sorted(glob.glob(os.path.join(STATE_DIR, "*.json")))
    if not snaps:
        print("no snapshots yet — run: foreman snapshot")
        return
    for f in snaps:
        d = json.load(open(f))
        print(f"  {os.path.basename(f)[:-5]:24} {d['generated'][:16]}  "
              f"{fmt_money(d['total_cost'])} / {d['window_days']}d  "
              f"$/turn {d['cost_per_wake']:.2f}")


# ────────────────────────────── hook manager ───────────────────────────────

def _hook_command(mode):
    cmd = f"python3 {SENTINEL}"
    if mode and mode != "advise":
        cmd = f"FOREMAN_MODE={mode} {cmd}"
    return cmd


def _load_settings(prof):
    p = os.path.join(prof, "settings.json")
    if os.path.exists(p):
        return p, json.load(open(p))
    return p, {}


def cmd_hook(action, profile=None, mode="advise"):
    profiles = discover_profiles(profile)
    stamp = datetime.date.today().isoformat()
    for prof in profiles:
        p, d = _load_settings(prof)
        groups = d.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
        installed = [
            (g, h) for g in groups for h in g.get("hooks", [])
            if "context_sentinel.py" in h.get("command", "")
        ]
        name = prof.replace(os.path.expanduser("~"), "~")
        if action == "status":
            if installed:
                print(f"  {name:24} ✓ installed  ({installed[0][1]['command']})")
            else:
                print(f"  {name:24} — not installed")
            continue
        if action == "install":
            if not os.path.exists(SENTINEL):
                sys.exit(f"sentinel not found at {SENTINEL}")
            if installed:
                installed[0][1]["command"] = _hook_command(mode)
                print(f"  {name:24} ✓ updated")
            else:
                groups.append({"hooks": [{"type": "command", "command": _hook_command(mode)}]})
                print(f"  {name:24} ✓ installed")
        elif action == "uninstall":
            if not installed:
                print(f"  {name:24} — nothing to remove")
                continue
            for g, h in installed:
                g["hooks"].remove(h)
            d["hooks"]["UserPromptSubmit"] = [g for g in groups if g.get("hooks")]
            if not d["hooks"]["UserPromptSubmit"]:
                del d["hooks"]["UserPromptSubmit"]
            print(f"  {name:24} ✓ removed")
        if os.path.exists(p):
            shutil.copy2(p, p + f".bak.foreman-{stamp}")
        os.makedirs(prof, exist_ok=True)
        json.dump(d, open(p, "w"), indent=2)
        json.load(open(p))  # sanity: still valid JSON
    if action != "status":
        print("\nNote: running sessions capture hooks at startup — restart long-lived")
        print("sessions once for the sentinel to take effect there.")




# ─────────────────────────── session restarter ─────────────────────────────
# Hooks are captured when a session starts — by design (they run arbitrary
# shell, so Claude Code requires human review via /hooks for mid-session
# changes; there is deliberately no remote reload). What foreman can do,
# harness-agnostically:
#   1. DETECT every running claude process (any terminal, any multiplexer),
#      its profile, cwd, session and idle time — via /proc, no tmux required.
#   2. AUTOMATE the recycle where the harness allows injecting input (tmux
#      driver today: clean /exit + `claude --resume <same session>` in place).
#   3. For everything else, print the exact per-session recipe: type /hooks
#      inside it to review & apply without restarting, or exit + resume.

import time


def _proc_table():
    out = subprocess.run(["ps", "-eo", "pid=,ppid=,etimes=,comm="],
                         capture_output=True, text=True).stdout
    kids, info = {}, {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, ppid, et, c = parts
        kids.setdefault(ppid, []).append(pid)
        info[pid] = (ppid, int(et), c)
    return kids, info


def _proc_env(pid):
    try:
        raw = open(f"/proc/{pid}/environ", "rb").read()
        return {k.decode(errors="ignore"): v.decode(errors="ignore")
                for k, v in (x.split(b"=", 1) for x in raw.split(b"\0") if b"=" in x)}
    except Exception:
        return {}


def _is_ancestor(pid):
    cur = os.getpid()
    for _ in range(64):
        if cur == pid:
            return True
        try:
            with open(f"/proc/{cur}/stat") as f:
                cur = int(f.read().split(") ")[-1].split()[1])
        except Exception:
            return False
        if cur <= 1:
            return False
    return False


def _latest_session(profile, cwd):
    proj = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    files = glob.glob(os.path.join(profile, "projects", proj, "*.jsonl"))
    if not files:
        return None, None
    f = max(files, key=os.path.getmtime)
    return os.path.basename(f)[:-6], os.path.getmtime(f)


def _tmux_pane_index(kids):
    """claude pid -> (pane_id, tmux_session) for every tmux pane, if tmux exists."""
    idx = {}
    if not shutil.which("tmux"):
        return idx
    out = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{pane_id}\t#{session_name}\t#{pane_pid}"],
        capture_output=True, text=True).stdout
    for line in out.splitlines():
        try:
            pane, sess, pane_pid = line.split("\t")
        except ValueError:
            continue
        stack = [pane_pid]
        while stack:
            p = stack.pop()
            idx[p] = (pane, sess)
            stack.extend(kids.get(p, []))
    return idx




def _pane_state(pane):
    """What the session's TUI is visibly doing — the truthful per-pane gate.
    busy: mid-task · dialog: a prompt is open · draft: unsent text in the input
    box · calm: empty prompt, safe to /exit · no-tui: nothing recognizable."""
    out = subprocess.run(["tmux", "capture-pane", "-t", pane, "-p"],
                         capture_output=True, text=True).stdout
    if "to interrupt" in out:
        return "busy"
    if "Do you want" in out or "to proceed" in out:
        return "dialog"
    prompt_lines = [l for l in out.splitlines() if l.lstrip().startswith("❯")]
    if not prompt_lines:
        return "no-tui"
    if any(l.lstrip().lstrip("❯").strip() for l in prompt_lines):
        return "draft"
    return "calm"


def cmd_restart(idle_min=30, go=False, target=None, force=False):
    kids, info = _proc_table()
    pane_of = _tmux_pane_index(kids)
    now = time.time()
    rows = []
    for pid, (ppid, etimes, comm) in info.items():
        if comm != "claude":
            continue
        # process state + invocation tell us what kind of "claude" this is
        try:
            state = open(f"/proc/{pid}/stat").read().split(") ")[-1].split()[0]
        except OSError:
            state = "?"
        try:
            argv = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="ignore")
        except OSError:
            argv = ""
        kind = "tui"
        nm = re.search(r"--name[= ]([^ ]+)", argv)
        sname = nm.group(1) if nm else None
        if state == "Z" or not argv.strip():
            kind = "zombie"      # exited but unreaped — there is no session here
        elif " rc" in f" {argv} " or "remote-control" in argv:
            kind = "remote"      # Remote Control client — session lives server-side, no local TUI
        env = _proc_env(pid)
        if not env and kind != "zombie":
            continue  # not ours (other user) — /proc/environ unreadable
        cfg = env.get("CLAUDE_CONFIG_DIR", "") if env else ""
        profile = cfg or os.path.expanduser("~/.claude")
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            if kind != "zombie":
                continue
            cwd = "(defunct)"
        sid, mtime = _latest_session(profile, cwd)
        idle = (now - mtime) / 60 if mtime else None
        started = now - etimes
        settings = os.path.join(profile, "settings.json")
        stale = os.path.exists(settings) and os.path.getmtime(settings) > started
        pane = pane_of.get(pid)
        rows.append({"pid": pid, "cfg": cfg, "profile": profile, "cwd": cwd,
                     "sid": sid, "idle": idle, "stale": stale, "pane": pane,
                     "kind": kind, "name": sname})

    # Sessions sharing one project dir cannot be told apart from outside the
    # process (the transcript is not held open) — resuming by "latest in dir"
    # would risk swapping conversations between panes. Only unambiguous
    # (single-process) projects are auto-recyclable; the rest go manual.
    groups = {}
    for r in rows:
        key = (r["profile"], re.sub(r"[^A-Za-z0-9]", "-", r["cwd"]))
        groups.setdefault(key, []).append(r)
    for key, grp in groups.items():
        for r in grp:
            r["shared"] = len(grp)

    if target:
        rows = [r for r in rows if target in (r["pane"] or ("", ""))[0:2] or
                target == r["pid"] or target in r["cwd"]]
    if not rows:
        print("no running claude sessions found (that this user can inspect).")
        return

    auto, manual, notes = [], [], []
    print(f"{'pid':>8} {'where':20} {'idle':>6} {'hooks':7} {'shr':3} {'profile':14} cwd")
    for r in sorted(rows, key=lambda r: (r["pane"] is None, r["cwd"])):
        where = f"tmux {r['pane'][1][:14]}" if r["pane"] else "other"
        if r["kind"] == "remote":
            where = f"rc   {r['pane'][1][:14]}" if r["pane"] else "rc"
        elif r["kind"] == "zombie":
            where = f"zomb {r['pane'][1][:14]}" if r["pane"] else "zombie"
        prof = os.path.basename(r["cfg"]) if r["cfg"] else ".claude"
        # For shared project dirs the idle we can measure is the DIRECTORY's
        # (any sibling's last write) — a lower bound for this pane, so ≥.
        if r["idle"] is None:
            idle_s = "?"
        elif r["shared"] > 1:
            idle_s = f"≥{r['idle']:.0f}m"
        else:
            idle_s = f"{r['idle']:.0f}m"
        hooks = "STALE" if r["stale"] else "current"
        shared = f"x{r['shared']}" if r["shared"] > 1 else "  "
        print(f"{r['pid']:>8} {where:20} {idle_s:>6} {hooks:7} {shared:3} {prof:14} {r['cwd']}")
        if not r["stale"] and not force:
            continue
        if _is_ancestor(int(r["pid"])):
            continue  # never recycle the session foreman is running inside
        # idle is per-project-dir; with shared cwds one active sibling masks
        # the rest, and the resume target would be a guess. Never guess.
        # Unknown idle (no transcript found) is NOT ready either: there is
        # nothing verifiable to resume — recycling would strand the pane.
        if r["kind"] != "tui":
            continue  # remote-control clients and zombies are not recyclable sessions
        state = _pane_state(r["pane"][0]) if r["pane"] else "no-tui"
        r["pstate"] = state
        ready = (r["idle"] is not None and r["idle"] >= idle_min) or force
        if r["sid"] is None and not force:
            ready = False
        if r["shared"] > 1:
            # A NAMED session is unambiguous (`claude --resume <name>`), so a
            # calm pane makes it auto-recyclable even in a shared dir — the
            # pane's own state outranks the sibling-polluted dir idle.
            if r["name"] and r["pane"] and state == "calm":
                auto.append(r)
            elif r["name"] and r["pane"]:
                notes.append((r, f"named but pane is {state} — send/clear it and re-run"))
            elif ready:
                manual.append(r)
        elif r["pane"] and ready and state == "calm":
            auto.append(r)
        elif r["pane"] and ready:
            notes.append((r, f"idle but pane is {state} — skipped"))
        elif ready:
            manual.append(r)

    if notes and not go:
        print("\npane-state notes:")
        for r, why in notes:
            label = r["name"] or (r["pane"][1] if r["pane"] else r["pid"])
            print(f"  {label}: {why}")
    if not go:
        print(f"\ndry-run: {len(auto)} auto-recyclable (tmux), "
              f"{len(manual)} manual, rest current/busy/self.")
        if manual:
            print("\nmanual sessions — apply WITHOUT restarting by typing /hooks inside")
            print("each one (review & accept). For exited ones:")
            seen_grp = set()
            for r in manual:
                if r["shared"] > 1:
                    key = (r["profile"], r["cwd"])
                    if key in seen_grp:
                        continue
                    seen_grp.add(key)
                    print(f"  {r['shared']} sessions share {r['cwd']} — sessions in a shared")
                    print(f"    project dir can't be told apart from outside; inside each pane:")
                    print(f"    /hooks to apply now, or /exit then `claude --resume` (picker).")
                else:
                    pre = f"CLAUDE_CONFIG_DIR={r['cfg']} " if r["cfg"] else ""
                    res = f"--resume {r['sid']}" if r["sid"] else "--continue"
                    print(f"  pid {r['pid']}: cd {r['cwd']} && {pre}claude {res}")
        if auto:
            print("\nrun with --go to recycle the tmux ones in place.")
        return

    if not auto:
        print(f"\nnothing ready to recycle right now — every candidate is busy "
              f"(idle < {idle_min}m), shared-cwd, remote-control or defunct.")
        print(f"re-run when sessions have settled, or lower the bar: "
              f"foreman restart --go --idle-min 15")
        return
    print(f"\nrecycling {len(auto)} session(s) …")
    for r in auto:
        pane, sess = r["pane"]
        print(f"\nrecycling {pane} ({sess}) ...")
        subprocess.run(["tmux", "send-keys", "-t", pane, "/exit", "Enter"])
        gone = False
        for _ in range(40):
            time.sleep(0.5)
            k2, i2 = _proc_table()
            if r["pid"] not in i2 or i2[r["pid"]][2] != "claude":
                gone = True
                break
        if not gone:
            print("  ✗ did not exit cleanly — skipped (a dialog may be open)")
            continue
        if r["name"]:
            import shlex
            q = shlex.quote(r["name"])
            resume = f"claude --resume {q} --name {q}"
        else:
            resume = f"claude --resume {r['sid']}" if r["sid"] else "claude --continue"
        if r["cfg"]:
            resume = f"CLAUDE_CONFIG_DIR={r['cfg']} {resume}"
        subprocess.run(["tmux", "send-keys", "-t", pane, resume, "Enter"])
        print(f"  ✓ relaunched: {resume}")
    if manual:
        print(f"\n{len(manual)} session(s) (shared-cwd or non-tmux) still need /hooks inside, or a manual resume — run without --go for the recipes.")




# ──────────────────────────── single-session card ──────────────────────────

def _all_transcripts():
    rows = []
    for prof in discover_profiles():
        for f in glob.glob(os.path.join(prof, "projects", "*", "*.jsonl")):
            try:
                rows.append((os.path.getmtime(f), f, prof))
            except OSError:
                pass
    return sorted(rows, reverse=True)


def _list_sessions(limit=15):
    rows = _all_transcripts()
    print(f"most recent sessions ({min(limit, len(rows))} of {len(rows)}) — "
          f"card one with: foreman session <id-prefix|pid|tmux-name>")
    for mt, f, prof in rows[:limit]:
        idle = (time.time() - mt) / 60
        print(f"  {os.path.basename(f)[:12]}…  {idle:7.0f}m idle  "
              f"{os.path.basename(prof):14} {os.path.basename(os.path.dirname(f))[:52]}")


def _transcripts_for_pid(pid):
    """Transcripts of the project dir a running claude pid lives in."""
    env = _proc_env(pid)
    if not env:
        return [], None
    profile = env.get("CLAUDE_CONFIG_DIR", "") or os.path.expanduser("~/.claude")
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return [], None
    proj = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    files = sorted(glob.glob(os.path.join(profile, "projects", proj, "*.jsonl")),
                   key=os.path.getmtime, reverse=True)
    return [(os.path.getmtime(f), f, profile) for f in files], cwd


def _pid_for_tmux_name(name):
    if not shutil.which("tmux"):
        return None
    out = subprocess.run(["tmux", "list-panes", "-a", "-F",
                          "#{session_name}\t#{pane_id}\t#{pane_pid}"],
                         capture_output=True, text=True).stdout
    kids, comm = {}, {}
    k2, i2 = _proc_table()
    for line in out.splitlines():
        try:
            sess, pane, pane_pid = line.split("\t")
        except ValueError:
            continue
        if name in (sess, pane):
            pid = _claude_pid_under(pane_pid, k2, {p: c for p, (_, _, c) in i2.items()})
            if pid:
                return pid
    return None


def _claude_pid_under(pane_pid, kids, comm):
    stack = [str(pane_pid)]
    while stack:
        p = stack.pop()
        if comm.get(p) == "claude":
            return int(p)
        stack.extend(kids.get(p, []))
    return None


def cmd_session(match):
    if not match or match == "ls":
        _list_sessions()
        return
    hits = []
    via = None
    if match.isdigit() and os.path.isdir(f"/proc/{match}"):
        hits, cwd = _transcripts_for_pid(match)
        via = f"pid {match} ({cwd})"
    if not hits:
        pid = _pid_for_tmux_name(match)
        if pid:
            hits, cwd = _transcripts_for_pid(str(pid))
            via = f"tmux '{match}' → pid {pid} ({cwd})"
    if not hits:
        for mt, f, prof in _all_transcripts():
            if (match in os.path.basename(f)
                    or match in os.path.basename(os.path.dirname(f)) or match == f):
                hits.append((mt, f, prof))
    if not hits:
        sys.exit(f"no session matches '{match}' — try `foreman session` (list), a "
                 f"session-id prefix, a running pid, a tmux session name, or a "
                 f"project-dir substring")
    if via:
        print(f"resolved via {via}\n")
    if len(hits) > 1 and via:
        # A pid/tmux name in a SHARED project dir: which transcript is this
        # process's cannot be known from outside — same rule as restart: no guessing.
        print(f"{len(hits)} sessions live in that project dir — a specific pid's own "
              f"transcript can't be identified from outside.")
        print(f"newest first; card one with its id prefix:")
        for mt, f, prof in hits[:8]:
            idle = (time.time() - mt) / 60
            print(f"  {os.path.basename(f)[:12]}…  {idle:7.0f}m idle")
        return
    if len(hits) > 1:
        print(f"{len(hits)} sessions match '{match}' — newest first; narrow with a "
              f"session-id prefix:")
        for mt, f, prof in hits[:8]:
            idle = (time.time() - mt) / 60
            print(f"  {os.path.basename(f)[:12]}…  {idle:7.0f}m idle  "
                  f"{os.path.basename(os.path.dirname(f))[:52]}")
        if len(hits) > 8:
            print(f"  … and {len(hits) - 8} more")
        return
    _session_card(hits[0][1], hits[0][2])


def _session_card(path, prof):
    calls = 0
    cost = 0.0
    ctx_now = 0
    ctx_max = 0
    first_ts = last_ts = None
    last_tier = "opus"
    drops = []
    prev_ctx = 0
    with open(path, errors="ignore") as fh:
        for line in fh:
            if '"usage"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            msg = d.get("message") or {}
            u = msg.get("usage") or {}
            if not u:
                continue
            i = u.get("input_tokens", 0) or 0
            cw = u.get("cache_creation_input_tokens", 0) or 0
            cr = u.get("cache_read_input_tokens", 0) or 0
            o = u.get("output_tokens", 0) or 0
            if i + cw + cr + o == 0:
                continue
            last_tier = tier(msg.get("model"))
            pi, pw, pr, po = PRICES[last_tier]
            cost += (i * pi + cw * pw + cr * pr + o * po) / 1e6
            ctx = i + cw + cr
            calls += 1
            ts = d.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            if prev_ctx > 50_000 and ctx < prev_ctx * 0.7:
                drops.append((ts or "?", prev_ctx, ctx))
            if ctx:
                prev_ctx = ctx
            ctx_now = ctx or ctx_now
            ctx_max = max(ctx_max, ctx)

    comp = autopsy(path)
    ctot = sum(v for k, v in comp.items() if k not in ("thinking", "stale_pct")) or 1
    tool_pct = (comp["tool_results"] + comp["tool_inputs"]) * 100 // ctot
    dia_pct = (comp["assistant_text"] + comp["user_text"]) * 100 // ctot
    pi, pw, pr, po = PRICES[last_tier]
    idle_m = (time.time() - os.path.getmtime(path)) / 60

    print(f"session  {os.path.basename(path)[:-6]}")
    print(f"project  {os.path.basename(os.path.dirname(path))}   profile {os.path.basename(prof)}")
    print(f"activity {calls:,} calls · first {str(first_ts)[:16]} · last {str(last_ts)[:16]} · idle {idle_m:.0f}m")
    print(f"cost     {fmt_money(cost)} total (est., {last_tier})")
    print(f"context  now {ctx_now / 1000:,.0f}K · peak {ctx_max / 1000:,.0f}K")
    print(f"content  tool traffic {tool_pct}% (stale {comp['stale_pct']}%) · dialogue {dia_pct}%")
    print(f"forward  next cold wake rewrite ≈ {fmt_money(ctx_now * pw / 1e6)} · "
          f"each warm iteration reads ≈ {fmt_money(ctx_now * pr / 1e6)}")
    if drops:
        print("\ncontext reductions detected (compaction / clear events):")
        for ts, a, b in drops[-5:]:
            saved_read = (a - b) * pr / 1e6
            print(f"  {str(ts)[:16]}  {a / 1000:,.0f}K → {b / 1000:,.0f}K   "
                  f"(−{(a - b) / 1000:,.0f}K · each later iteration ≈ {fmt_money(saved_read)} cheaper)")
    else:
        print("\nno compaction events detected in this transcript yet.")




# ─────────────────────────── attributed savings ────────────────────────────
# Cross-reference the sentinel's FIRE log with what actually happened next in
# each session's transcript: a context drop shortly after a FIRE is attributed
# to foreman, and the savings REALIZED so far are computed from the calls that
# actually ran on the smaller context afterwards (counterfactual: same calls
# at the pre-drop size). Measured, not projected.

def _parse_sentinel_log():
    logp = os.path.join(STATE_DIR, "sentinel.log")
    fires, passes = [], 0
    if not os.path.exists(logp):
        return fires, passes
    pat = re.compile(r"(\S+) session=(\S+) ctx=(\d+)K idle=(\d+)m -> (\w+)")
    for line in open(logp):
        m = pat.match(line.strip())
        if not m:
            continue
        ts, sid, ctx, idle, dec = m.groups()
        if dec.startswith("FIRE"):
            fires.append({"ts": ts, "sid": sid, "ctx": int(ctx) * 1000})
        elif dec == "pass":
            passes += 1
    return fires, passes


def _find_by_prefix(sid):
    for prof in discover_profiles():
        hits = glob.glob(os.path.join(prof, "projects", "*", sid + "*.jsonl"))
        if hits:
            return max(hits, key=os.path.getmtime)
    return None


def cmd_savings():
    fires, passes = _parse_sentinel_log()
    print(f"foreman savings — attributed to sentinel FIREs "
          f"(log: {os.path.join(STATE_DIR, 'sentinel.log')})")
    print(f"  sentinel invocations: {passes + len(fires):,} ({len(fires)} FIRE, {passes:,} pass)\n")
    if not fires:
        print("no FIRE events yet — savings attribution starts with the first one.")
        print("(a FIRE happens when you message a session that is >150K resident and")
        print(" idle >1h; watch: tail -f ~/.local/state/foreman/sentinel.log)")
        return

    total_shed = 0
    total_saved = 0.0
    attributed = 0
    seen = set()
    for f in fires:
        key = f["sid"]
        path = _find_by_prefix(f["sid"])
        if not path:
            print(f"  {f['ts']}  {f['sid']}: transcript not found (test entry?) — skipped")
            continue
        try:
            fire_t = datetime.datetime.fromisoformat(f["ts"])
        except ValueError:
            continue
        # walk the transcript from the FIRE moment forward
        drop_a = drop_b = None
        saved = 0.0
        prev_ctx = 0
        prev_t = None
        n_after = 0
        with open(path, errors="ignore") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                msg = d.get("message") or {}
                u = msg.get("usage") or {}
                if not u:
                    continue
                t = parse_ts(d.get("timestamp", ""))
                if not t:
                    continue
                tn = t.replace(tzinfo=None)
                ctx = (u.get("input_tokens", 0) or 0) + \
                      (u.get("cache_read_input_tokens", 0) or 0) + \
                      (u.get("cache_creation_input_tokens", 0) or 0)
                pi, pw, pr, po = PRICES[tier(msg.get("model"))]
                if tn < fire_t:
                    prev_ctx = ctx or prev_ctx
                    prev_t = tn
                    continue
                if drop_a is None:
                    # look for the drop within 15 min of the FIRE
                    if (tn - fire_t).total_seconds() > 900 and not (
                            prev_ctx > 150_000 and ctx < prev_ctx * 0.7):
                        break  # no compaction followed this FIRE
                    if prev_ctx > 150_000 and ctx and ctx < prev_ctx * 0.7:
                        drop_a, drop_b = prev_ctx, ctx
                    prev_ctx = ctx or prev_ctx
                    prev_t = tn
                    if drop_a is None:
                        continue
                    continue
                # after the drop: realized savings on every call still below a
                if ctx >= drop_a:
                    break  # context regrew past the old size — stop attributing
                n_after += 1
                saved += (drop_a - ctx) * pr / 1e6
                if prev_t and (tn - prev_t).total_seconds() > TTL_S:
                    saved += (drop_a - ctx) * pw / 1e6  # a cold rewrite that was smaller
                prev_t = tn
        if drop_a:
            key2 = (key, drop_a, drop_b)   # same sid can compact more than once;
            dedupe = " (repeat)" if key2 in seen else ""   # same drop counts once
            if not dedupe:
                attributed += 1
                total_shed += drop_a - drop_b
                total_saved += saved
            seen.add(key2)
            print(f"  {f['ts']}  {f['sid']}: {drop_a // 1000}K → {drop_b // 1000}K "
                  f"(shed {(drop_a - drop_b) // 1000}K) · {n_after} calls since · "
                  f"realized {fmt_money(saved)}{dedupe}")
        else:
            print(f"  {f['ts']}  {f['sid']}: FIRE but no compaction followed "
                  f"(model may lack SlashCommand, or advise was ignored)")
    print(f"\n  attributed compactions: {attributed}/{len(fires)} · "
          f"context shed: {total_shed / 1000:,.0f}K tokens · "
          f"REALIZED savings so far: {fmt_money(total_saved)}")
    print("  (realized = actual calls that ran cheaper than the pre-compaction size;")
    print("   it grows every time those sessions keep working. Re-run anytime.)")


# ──────────────────────────────── watch ────────────────────────────────────
# The two arms of proactive cleanup, from the design discussion:
#   LIVE sessions — context lives in the process's memory; the only way in is
#     in-band: type the surgical /compact into its pane. watch does that for
#     fat sessions whose pane is verifiably calm and whose transcript is idle,
#     reusing the sentinel's deferred-compact watcher (same calm gate, same
#     in-flight marker, same log).
#   DEAD sessions — on --resume Claude Code rebuilds context from the .jsonl,
#     so the transcript can be trimmed surgically on disk: blank the payload
#     of stale tool_result blocks (and Claude Code's duplicate toolUseResult
#     field) while keeping every uuid, pairing and dialogue line intact. No
#     LLM, no summary, deterministic. The cache is long dead (idle > TTL), so
#     the cold write on next resume happens anyway — just over a far smaller
#     context.
# Golden rule inherited from restart: don't guess. Any file that doesn't parse
# cleanly line-by-line is left alone; a backup (.jsonl.foreman-bak) always
# precedes a rewrite; live project dirs are never trimmed.

TRIM_MARK = "[trimmed by foreman]"
TRIM_MIN_BLOCK = 200        # don't bother blanking payloads smaller than this
TRIM_MIN_TOTAL = 200_000    # chars (~50K tokens): below this a file isn't worth it


def _slog(msg):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "sentinel.log"), "a") as f:
            ts = datetime.datetime.now().isoformat(timespec="seconds")
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


def _last_ctx_tail(path, tail_bytes=4_000_000):
    """Resident context from the tail of a transcript (fast on 50MB whales;
    the first partial line just fails json.loads and is skipped)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            data = f.read().decode(errors="ignore")
    except OSError:
        return 0
    ctx = 0
    for line in data.splitlines():
        if '"usage"' not in line:
            continue
        try:
            u = (json.loads(line).get("message") or {}).get("usage") or {}
        except Exception:
            continue
        c = (u.get("input_tokens", 0) or 0) + \
            (u.get("cache_read_input_tokens", 0) or 0) + \
            (u.get("cache_creation_input_tokens", 0) or 0)
        if c:
            ctx = c
    return ctx


def _is_user_turn(d):
    """A real human prompt line — the unit the keep-window is counted in."""
    if d.get("type") != "user" or d.get("isMeta") or "toolUseResult" in d:
        return False
    c = (d.get("message") or {}).get("content")
    if isinstance(c, str):
        return bool(c.strip())
    if isinstance(c, list):
        return any(isinstance(b, dict) and b.get("type") == "text" for b in c)
    return False


def trim_transcript(path, keep_turns=15, go=False):
    """Blank stale tool payloads in a DEAD session's transcript. Returns
    (chars_shed, context_chars_shed, blocks, reason) — reason set when skipped;
    context_chars_shed counts only tool_result payloads (what resume replays)."""
    try:
        raw = open(path, errors="strict").read().splitlines(True)
    except (OSError, UnicodeDecodeError) as e:
        return 0, 0, 0, f"unreadable ({e.__class__.__name__})"
    parsed = []
    for ln in raw:
        try:
            parsed.append(json.loads(ln))
        except Exception:
            return 0, 0, 0, "line failed to parse — refusing to touch this file"
    user_idx = [i for i, d in enumerate(parsed) if _is_user_turn(d)]
    if len(user_idx) <= keep_turns:
        return 0, 0, 0, f"only {len(user_idx)} user turns (keep window is {keep_turns})"
    cut = user_idx[-keep_turns]
    shed = shed_ctx = blocks = 0   # shed_ctx: tool_result payloads — the part
    changed = {}                   # that gets replayed into context on resume
    for i in range(cut):
        d = parsed[i]
        dirty = False
        c = (d.get("message") or {}).get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    s = json.dumps(b.get("content", ""))
                    if len(s) > TRIM_MIN_BLOCK:
                        b["content"] = f"{TRIM_MARK[:-1]}: {len(s)} chars]"
                        shed += len(s)
                        shed_ctx += len(s)
                        blocks += 1
                        dirty = True
        if "toolUseResult" in d:   # Claude Code's duplicate copy — disk only
            s = json.dumps(d["toolUseResult"])
            if len(s) > TRIM_MIN_BLOCK:
                d["toolUseResult"] = f"{TRIM_MARK[:-1]}: {len(s)} chars]"
                shed += len(s)
                dirty = True
        if dirty:
            changed[i] = d
    if shed < TRIM_MIN_TOTAL:
        return shed, shed_ctx, blocks, "not enough stale payload to be worth a rewrite"
    if not go:
        return shed, shed_ctx, blocks, None
    out_lines = []
    for i, ln in enumerate(raw):
        if i in changed:
            new = json.dumps(changed[i], ensure_ascii=False) + "\n"
            json.loads(new)  # every rewritten line must round-trip
            out_lines.append(new)
        else:
            out_lines.append(ln)
    if len(out_lines) != len(raw):
        return 0, 0, 0, "internal line-count mismatch — aborted"
    tmp = path + ".foreman-tmp"
    with open(tmp, "w") as f:
        f.writelines(out_lines)
    shutil.copy2(path, path + ".foreman-bak")   # backup BEFORE the swap
    os.replace(tmp, path)
    return shed, shed_ctx, blocks, None


def _live_claude_procs(kids, info):
    """(pid, profile, proj_dir, transcript, mtime, is_ancestor) for every
    running claude. Ancestors (the session foreman itself runs inside) are
    included so shared-cwd COUNTS stay truthful, but must never be acted on."""
    rows = []
    for pid, (_ppid, _et, comm) in info.items():
        if comm != "claude":
            continue
        env = _proc_env(pid)
        if not env:
            continue
        profile = env.get("CLAUDE_CONFIG_DIR", "") or os.path.expanduser("~/.claude")
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
        proj = re.sub(r"[^A-Za-z0-9]", "-", cwd)
        files = glob.glob(os.path.join(profile, "projects", proj, "*.jsonl"))
        f = max(files, key=os.path.getmtime) if files else None
        rows.append((pid, profile, proj, f, os.path.getmtime(f) if f else 0,
                     _is_ancestor(int(pid))))
    return rows


def _pane_map():
    """sid -> {pane, transcript, ts} written by the sentinel from inside each
    session — the ground truth that makes shared cwds resolvable."""
    out = {}
    for p in glob.glob(os.path.join(STATE_DIR, "pane-map", "*")):
        try:
            out[os.path.basename(p)] = json.load(open(p))
        except Exception:
            pass
    return out


def cmd_watch(go=False, idle_min=60, ctx_min=150_000, keep_turns=15):
    now = time.time()
    kids, info = _proc_table()
    pane_of = _tmux_pane_index(kids)
    live = _live_claude_procs(kids, info)
    live_projects = {(p, j) for _, p, j, _, _, _ in live}
    procs_in = Counter((p, j) for _, p, j, _, _, _ in live)
    print(f"foreman watch — {'EXECUTING' if go else 'dry-run (--go to execute)'}"
          f"  [fat ≥{ctx_min // 1000}K · idle ≥{idle_min}m · keep last {keep_turns} turns]")

    # resolve pane<->session precisely via the sentinel's pane-map: an entry is
    # trusted only if a claude process lives under that pane NOW and the entry
    # was written during that process's lifetime (pane ids are never reused
    # within a tmux server, so a stale entry simply fails these checks)
    pane2pid = {}
    for pid, (_ppid, _et, comm) in info.items():
        if comm == "claude" and pid in pane_of:
            p = pane_of[pid][0]
            if p not in pane2pid or info[pid][1] > info[pane2pid[p]][1]:
                pane2pid[p] = pid   # oldest claude under the pane = the TUI
    best = {}   # pid -> (ts, sid, pane, transcript); newest entry wins, since
    for sid, e in _pane_map().items():   # /clear re-sids the same process
        pane, tp, ts = e.get("pane"), e.get("transcript"), e.get("ts", 0)
        pid = pane2pid.get(pane)
        if not (pid and tp and os.path.exists(tp)):
            continue
        if ts < now - info[pid][1]:
            continue   # entry predates the process now in that pane — stale
        if pid not in best or ts > best[pid][0]:
            best[pid] = (ts, sid, pane, tp)
    resolved = {tp: (sid, pane, pid) for pid, (ts, sid, pane, tp) in best.items()}
    resolved_pids = set(best)

    # arm 1 — LIVE fat idle sessions in calm tmux panes: in-band /compact
    n_compact = 0

    def live_row(sid, ctx, idle, pane, state, tag):
        nonlocal n_compact
        marker = os.path.join(STATE_DIR, f"compacting-{sid}")
        in_flight = os.path.exists(marker) and (now - os.path.getmtime(marker)) < 180
        if idle < idle_min:
            verdict = f"skip (idle {idle:.0f}m < {idle_min}m)"
        elif not pane:
            verdict = "skip (not in tmux — type the /compact yourself)"
        elif state != "calm":
            verdict = f"skip (pane {state})"
        elif in_flight:
            verdict = "skip (compaction already in flight)"
        else:
            verdict = "compact" if go else "would compact"
            if go:
                open(marker, "w").close()
                subprocess.Popen(
                    [sys.executable, SENTINEL, "--orchestrate", pane, marker, sid],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                _slog(f"session={sid} ctx={ctx // 1000}K idle={idle:.0f}m "
                      f"-> FIRE(watch) mode=auto (watch sweep)")
                n_compact += 1
        print(f"  LIVE {sid}  ctx {ctx // 1000:>4}K  idle {idle:6.0f}m  "
              f"pane {pane or '-':<6} {verdict}{tag}")

    for tp, (sid, pane, pid) in sorted(resolved.items(),
                                       key=lambda kv: -os.path.getmtime(kv[0])):
        ctx = _last_ctx_tail(tp)
        if ctx < ctx_min:
            continue
        idle = max(0.0, (now - os.path.getmtime(tp)) / 60)
        live_row(sid, ctx, idle, pane, _pane_state(pane), "")
    unresolved_shared = Counter()
    for pid, profile, proj, f, mt, is_anc in sorted(live, key=lambda r: -(r[4] or 0)):
        if not f or pid in resolved_pids or is_anc:
            continue   # ancestors count toward ambiguity but are never acted on
        if procs_in[(profile, proj)] > 1:
            unresolved_shared[(profile, proj)] += 1
            continue
        # single process in this cwd — the latest transcript is its session
        ctx = _last_ctx_tail(f)
        if ctx < ctx_min:
            continue
        pane, _sess = pane_of.get(pid, (None, None))
        live_row(os.path.basename(f)[:8], ctx, max(0.0, (now - mt) / 60), pane,
                 _pane_state(pane) if pane else "no-tmux", "")
    for (profile, proj), n in sorted(unresolved_shared.items()):
        print(f"  LIVE {proj[:44]}: {n} unmapped session(s) share this cwd — "
              f"message each once (the sentinel maps pane↔session on any prompt)")

    # arm 2 — DEAD fat transcripts: surgical on-disk trim
    n_trim = 0
    seen = set()
    resolved_sids = {os.path.basename(tp) for tp in resolved}
    for prof in discover_profiles():
        for f in glob.glob(os.path.join(prof, "projects", "*", "*.jsonl")):
            sid = os.path.basename(f)
            if sid in seen:
                continue
            seen.add(sid)
            proj = os.path.basename(os.path.dirname(f))
            if f in resolved or sid in resolved_sids:
                continue   # this transcript IS a running session (any profile)
            if (prof, proj) in live_projects:
                # trim dead siblings only when every running session in this
                # dir is pane-mapped — otherwise one of them might be this file
                n_res = sum(1 for tp in resolved
                            if os.path.dirname(tp) == os.path.dirname(f))
                if n_res < procs_in[(prof, proj)]:
                    continue
            try:
                st = os.stat(f)
            except OSError:
                continue
            if st.st_size < 2_000_000 or (now - st.st_mtime) / 60 < idle_min:
                continue
            ctx = _last_ctx_tail(f)
            if ctx < ctx_min:
                continue
            shed, shed_ctx, blocks, reason = trim_transcript(f, keep_turns, go=go)
            if reason:
                if "refusing" in reason or "aborted" in reason:
                    print(f"  DEAD {sid[:8]}  ctx {ctx // 1000:>4}K  skip ({reason})  {proj[:40]}")
                continue  # too small / too few turns — stay quiet
            tag = "trimmed" if go else "would trim"
            print(f"  DEAD {sid[:8]}  ctx {ctx // 1000:>4}K  "
                  f"{tag} {blocks} tool results: ~{shed_ctx // 4000}K tokens off "
                  f"next resume ({shed // 1000:,}K chars off disk)  {proj[:40]}")
            if go:
                _slog(f"watch: session={sid[:8]} trimmed {shed // 1000}K chars "
                      f"({blocks} tool results, kept last {keep_turns} turns)")
                n_trim += 1
    if go:
        print(f"\n  {n_compact} compaction(s) dispatched · {n_trim} transcript(s) trimmed"
              f" · backups: <transcript>.foreman-bak")


def cmd_watch_install():
    """Run the sweep every 15 min — systemd user timer where available."""
    py = shutil.which("python3") or sys.executable
    exec_line = f"{py} {os.path.join(FOREMAN_HOME, 'foreman.py')} watch --go"
    if shutil.which("systemctl"):
        unit_dir = os.path.expanduser("~/.config/systemd/user")
        os.makedirs(unit_dir, exist_ok=True)
        with open(os.path.join(unit_dir, "foreman-watch.service"), "w") as f:
            # KillMode=process: the sweep spawns DETACHED compaction watchers;
            # the default control-group kill would murder them the moment the
            # oneshot exits. TimeoutStartSec: tail-scanning many transcripts
            # can exceed systemd's 90s default.
            f.write("[Unit]\nDescription=foreman watch — proactive Claude Code "
                    "context cleanup\n\n[Service]\nType=oneshot\n"
                    "KillMode=process\nTimeoutStartSec=15min\n"
                    f"ExecStart={exec_line}\n")
        with open(os.path.join(unit_dir, "foreman-watch.timer"), "w") as f:
            f.write("[Unit]\nDescription=foreman watch every 15 min\n\n[Timer]\n"
                    "OnBootSec=5min\nOnUnitActiveSec=15min\n\n[Install]\n"
                    "WantedBy=timers.target\n")
        subprocess.run(["systemctl", "--user", "daemon-reload"])
        subprocess.run(["systemctl", "--user", "enable", "--now", "foreman-watch.timer"])
        print("installed + started: foreman-watch.timer (every 15 min)")
        print("  status : systemctl --user list-timers foreman-watch.timer")
        print("  log    : tail -f ~/.local/state/foreman/sentinel.log")
        print("  remove : systemctl --user disable --now foreman-watch.timer")
    else:
        print("no systemd — add this cron line (crontab -e):")
        print(f"  */15 * * * * {exec_line}")


# ──────────────────────────────── style ────────────────────────────────────
# One command to put an output-style directive everywhere it has to be:
#   1. always.md   — the sentinel injects it on every prompt, so sessions that
#                    are ALREADY OPEN obey it on their next message. No hook
#                    registration, no restart, no /hooks review.
#   2. CLAUDE.md   — every ~/.claude* profile, so new sessions start with it
#                    even when the hook is off or foreman is uninstalled.
# Both are needed: (1) has reach but depends on the hook, (2) is durable but
# only reaches sessions that have not started yet.

ALWAYS_FILE = os.path.expanduser(
    os.environ.get("FOREMAN_ALWAYS", os.path.join(STATE_DIR, "always.md")))
STYLE_DIR = os.path.join(FOREMAN_HOME, "styles")
STYLE_BEGIN = "<!-- foreman:style:begin -->"
STYLE_END = "<!-- foreman:style:end -->"


def _style_text(name):
    p = os.path.join(STYLE_DIR, f"{name}.md")
    if not os.path.exists(p):
        avail = ", ".join(sorted(
            os.path.splitext(f)[0] for f in os.listdir(STYLE_DIR))) \
            if os.path.isdir(STYLE_DIR) else "none"
        raise SystemExit(f"no style '{name}'. available: {avail}")
    return open(p).read().strip()


def _patch_claude_md(path, block):
    """Replace the foreman-managed block, or any hand-written '## Response
    style' section it supersedes, then append. Everything else is preserved."""
    old = open(path).read() if os.path.exists(path) else ""
    if STYLE_BEGIN in old and STYLE_END in old:
        head, rest = old.split(STYLE_BEGIN, 1)
        tail = rest.split(STYLE_END, 1)[1]
        old = head.rstrip() + "\n" + tail.lstrip("\n")
    lines = old.split("\n")
    out, skipping = [], False
    for ln in lines:
        if ln.startswith("## ") and "response style" in ln.lower():
            skipping = True
            continue
        if skipping and ln.startswith("## "):
            skipping = False
        if not skipping:
            out.append(ln)
    body = "\n".join(out).rstrip()
    with open(path, "w") as f:
        f.write(f"{body}\n\n{STYLE_BEGIN}\n## Response style (managed by "
                f"`foreman style`)\n\n{block}\n{STYLE_END}\n")


def cmd_style(action, name="terse"):
    if action == "show":
        print(_style_text(name))
        return
    if action == "list":
        for f in sorted(os.listdir(STYLE_DIR)):
            if f.endswith(".md"):
                p = os.path.join(STYLE_DIR, f)
                first = next((l for l in open(p) if l.strip()), "").strip()
                print(f"  {os.path.splitext(f)[0]:10} {first[:78]}")
        return
    if action == "uninstall":
        for p in [ALWAYS_FILE] if os.path.exists(ALWAYS_FILE) else []:
            os.remove(p)
            print(f"  removed {p}")
        for prof in discover_profiles():
            md = os.path.join(prof, "CLAUDE.md")
            if os.path.exists(md) and STYLE_BEGIN in open(md).read():
                _patch_claude_md(md, "")   # strips the block
                txt = open(md).read().split(STYLE_BEGIN)[0].rstrip() + "\n"
                open(md, "w").write(txt)
                print(f"  cleaned {md}")
        return
    block = _style_text(name)
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(ALWAYS_FILE, "w") as f:
        f.write(block + "\n")
    print(f"  running sessions : {ALWAYS_FILE} (applies on their next message)")
    for prof in discover_profiles():
        md = os.path.join(prof, "CLAUDE.md")
        _patch_claude_md(md, block)
        print(f"  new sessions     : {md}")
    print(f"\nstyle '{name}' installed. Undo: foreman style uninstall")
    if os.environ.get("CLAUDE_CODE_OUTPUT_STYLE", "") not in ("", "default"):
        print("NOTE: this session runs a non-default output style that may "
              "conflict — run /output-style default")


# ──────────────────────────────── voice ────────────────────────────────────
# Your transcripts hold thousands of messages YOU typed. `voice build`
# distills them — one cheap headless model call — into a personal style
# profile (LOCAL, never in any repo), and `voice install` drops a tiny skill
# into every profile so the model loads that profile exactly when it is about
# to write something on your behalf, and never pays for it otherwise.

VOICE_FILE = os.path.join(STATE_DIR, "voice.md")
# Corrections the user typed themselves. They outrank the distilled analysis
# (the person knows their own voice better than a sample of it does) and they
# SURVIVE rebuilds — kept in their own file and re-appended on every build.
VOICE_NOTES = os.path.join(STATE_DIR, "voice-notes.md")
NOTES_HEADER = "\n## Corrections from the user — these OVERRIDE everything above\n"

VOICE_INSTRUCTION = """\
You are analyzing WRITING SAMPLES: real messages one person typed to an AI
coding assistant over months (separated by lines containing only ---).
Distill a reusable STYLE PROFILE so a writer can produce text that sounds
exactly like this person. Extract STYLE, never content — do not quote
anything sensitive (names, projects, credentials, business details).

Cover, with short verbatim examples of tics where safe:
- language mix and code-switching habits (which language when, and why)
- typical message length, rhythm, and structure
- capitalization, punctuation, and typo tolerance (they type fast — what
  kinds of typos/abbreviations are characteristic?)
- greetings/closings, or their absence
- directness: how they ask, how they decide, how they push back
- characteristic phrases, connectors and tics (list them verbatim)
- emoji/emoticon usage (or absence)
- formality range: quick command vs discussion vs frustration
- what this person would NEVER write

End with 5 invented example messages in their voice (varied intents).
Output plain markdown, at most ~150 lines. Start with '# voice profile'.
"""

VOICE_SKILL = """\
---
name: voice
description: Use when drafting ANY text that will be sent or published as the user — messages, emails, chat replies, posts, PR/issue text, commit-adjacent prose. Loads their personal writing-style profile so the result sounds like them, not like an AI.
---

Read {voice_file} — the user's distilled writing-style profile — and imitate
it strictly whenever you write something the user will send as their own:
language choice and code-switching, message length, rhythm, directness,
characteristic phrases, vocabulary, emoji policy. Draft in their voice, not
yours; when in doubt, shorter and more direct.

**Voice, not typing errors.** The profile describes how the user types when
moving fast, which includes mistakes. Reproduce the STYLE; write correct
prose. So: keep their brevity, word choice, bluntness and structure — but
spell correctly, use apostrophes ("its" -> "it's", "thats" -> "that's",
"anupams" -> "Anupam's"), capitalize sentences and proper nouns, and keep
grammar clean. Lowercase-everything, dropped apostrophes and typos are
speed artifacts, not their voice. Deliberate stylistic choices (fragments,
one-word replies, technical shorthand, no greeting/closing) ARE their voice
— keep those.

The "Corrections from the user" section at the end of that file OUTRANKS the
analysis above it — it is the user correcting the profile in their own words.

If the file does not exist, tell the user to run: foreman voice build
If the user says a draft does not sound like them, capture the correction so
it sticks: foreman voice tune "<what was wrong, in their words>"
"""


def _collect_voice_samples(sample=400):
    msgs = {}
    seen_sid = set()
    for prof in discover_profiles():
        for f in glob.glob(os.path.join(prof, "projects", "*", "*.jsonl")):
            sid = os.path.basename(f)
            if sid in seen_sid:
                continue
            seen_sid.add(sid)
            try:
                fh = open(f, errors="ignore")
            except OSError:
                continue
            with fh:
                for line in fh:
                    if '"user"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("type") != "user" or d.get("isMeta") or \
                            d.get("isSidechain") or "toolUseResult" in d:
                        continue
                    c = (d.get("message") or {}).get("content")
                    if isinstance(c, list):
                        c = " ".join(b.get("text", "") for b in c
                                     if isinstance(b, dict) and b.get("type") == "text")
                    if not isinstance(c, str):
                        continue
                    t = c.strip()
                    if not (8 <= len(t) <= 800) or t.count("\n") > 10:
                        continue  # too short to carry voice / probably a paste
                    if t.startswith(("<", "/", "!", "Caveat:")):
                        continue  # command wrappers, slash commands, injected
                    if any(m in t for m in ("<system-reminder", "<command-",
                                            "<local-command", "<cross-session",
                                            "tool_result", "```")):
                        continue
                    msgs.setdefault(t, d.get("timestamp") or "")
    rows = sorted(msgs.items(), key=lambda kv: kv[1])
    if len(rows) > sample:
        # Recency-weighted: 60% of the sample from the most recent third. A
        # voice drifts, and the current one is the one worth imitating.
        cut = int(len(rows) * 2 / 3)
        old, new = rows[:cut], rows[cut:]
        n_new = min(len(new), int(sample * 0.6))
        n_old = sample - n_new
        pick = []
        for src, n in ((old, n_old), (new, n_new)):
            if not src or n <= 0:
                continue
            step = len(src) / n
            pick += [src[int(i * step)] for i in range(n)]
        rows = pick
    return [t for t, _ in rows], len(msgs)


def _append_notes(dest_text):
    """Corrections always ride at the end of the profile, verbatim."""
    if not os.path.exists(VOICE_NOTES):
        return dest_text
    notes = open(VOICE_NOTES).read().strip()
    return dest_text.rstrip() + "\n" + NOTES_HEADER + "\n" + notes + "\n" if notes \
        else dest_text


def cmd_voice(action, sample=400, model="sonnet", text=None):
    if action == "tune":
        if not text:
            print('usage: foreman voice tune "shorter, fewer references, more commands"')
            return
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(VOICE_NOTES, "a") as f:
            f.write(f"- {text.strip()}\n")
        if os.path.exists(VOICE_FILE):   # re-stamp the live profile immediately
            body = open(VOICE_FILE).read().split(NOTES_HEADER)[0]
            with open(VOICE_FILE, "w") as f:
                f.write(_append_notes(body))
        print(f"noted. corrections now in effect ({VOICE_NOTES}):\n")
        print(open(VOICE_NOTES).read().rstrip())
        return
    if action == "show":
        if os.path.exists(VOICE_FILE):
            print(open(VOICE_FILE).read())
        else:
            print(f"no profile yet — run: foreman voice build   ({VOICE_FILE})")
        return
    if action == "install":
        if not os.path.exists(VOICE_FILE):
            print("build the profile first: foreman voice build")
            return
        for prof in discover_profiles():
            d = os.path.join(prof, "skills", "voice")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "SKILL.md"), "w") as f:
                f.write(VOICE_SKILL.format(voice_file=VOICE_FILE))
            print(f"  installed skill: {os.path.join(d, 'SKILL.md')}")
        print("running sessions pick it up on restart; new sessions immediately.")
        return
    if action == "uninstall":
        for prof in discover_profiles():
            p = os.path.join(prof, "skills", "voice", "SKILL.md")
            if os.path.exists(p):
                os.remove(p)
                print(f"  removed {p}")
        return
    # build
    if not shutil.which("claude"):
        print("voice build needs the claude CLI on PATH")
        return
    samples, total = _collect_voice_samples(sample)
    if len(samples) < 50:
        print(f"only {len(samples)} usable messages found — not enough to hear a voice")
        return
    print(f"distilling {len(samples)} of {total:,} messages (one {model} call)…")
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env["FOREMAN_MODE"] = "off"
    os.makedirs(STATE_DIR, exist_ok=True)
    r = subprocess.run(["claude", "-p", "--model", model],
                       input=VOICE_INSTRUCTION + "\n\n" + "\n---\n".join(samples),
                       capture_output=True, text=True, timeout=600,
                       cwd=STATE_DIR, env=env)
    out = (r.stdout or "").strip()
    if "# voice profile" not in out or len(out) < 400:
        print("model returned something unusable — profile NOT written:")
        print((out or r.stderr)[:500])
        return
    with open(VOICE_FILE, "w") as f:
        f.write(_append_notes(out + "\n"))
    print(f"wrote {VOICE_FILE} ({len(out.splitlines())} lines"
          f"{' + your corrections' if os.path.exists(VOICE_NOTES) else ''}). Preview:\n")
    print("\n".join(out.splitlines()[:12]))
    print("\nnext: foreman voice install   (adds the skill to every profile)")


# ──────────────────────────────── update ───────────────────────────────────

def cmd_update():
    if os.path.isdir(os.path.join(FOREMAN_HOME, ".git")) and shutil.which("git"):
        r = subprocess.run(["git", "-C", FOREMAN_HOME, "pull", "--ff-only"],
                           capture_output=True, text=True)
        print(r.stdout.strip() or r.stderr.strip())
    else:
        print("not a git install — re-run the installer:\n"
              "  curl -fsSL https://raw.githubusercontent.com/vikgmdev/foreman/main/install.sh | bash")


# ───────────────────────────────── main ────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        prog="foreman", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    for name in ("audit", "snapshot", "compare"):
        sp = sub.add_parser(name)
        sp.add_argument("--days", type=int, default=7)
        sp.add_argument("--profile", help="scan only this config dir (default: every ~/.claude*)")
        sp.add_argument("--tag", default="baseline")
        if name == "audit":
            sp.add_argument("--deep", action="store_true", help="add whale composition autopsy")
    sub.add_parser("ls")
    hp = sub.add_parser("hook")
    hp.add_argument("action", choices=["install", "uninstall", "status"])
    hp.add_argument("--profile", help="target one config dir (default: every ~/.claude*)")
    hp.add_argument("--mode", choices=["advise", "auto", "block"], default="advise")
    rp = sub.add_parser("restart")
    rp.add_argument("--idle-min", type=int, default=30,
                    help="only recycle sessions idle at least this many minutes")
    rp.add_argument("--go", action="store_true", help="execute (default: dry-run)")
    rp.add_argument("--target", help="filter: pid, tmux pane/session, or cwd substring")
    rp.add_argument("--force", action="store_true",
                    help="ignore the idle check (NOT recommended)")
    sp = sub.add_parser("session")
    sp.add_argument("match", nargs="?", help="session-id prefix, project-dir substring, or transcript path")
    sub.add_parser("savings")
    wp = sub.add_parser("watch")
    wp.add_argument("--go", action="store_true", help="execute (default: dry-run)")
    wp.add_argument("--idle-min", type=int, default=60,
                    help="only touch sessions idle at least this many minutes")
    wp.add_argument("--ctx-min", type=int, default=150_000,
                    help="only touch sessions at least this fat (resident tokens)")
    wp.add_argument("--keep-turns", type=int, default=15,
                    help="trim: protect tool payloads within the last N user turns")
    wp.add_argument("--install", action="store_true",
                    help="install a 15-min systemd user timer (or print the cron line)")
    stp = sub.add_parser("style")
    stp.add_argument("action", choices=["install", "show", "list", "uninstall"])
    stp.add_argument("--name", default="terse", help="which style (see: style list)")
    vp = sub.add_parser("voice")
    vp.add_argument("action", choices=["build", "install", "show", "tune", "uninstall"])
    vp.add_argument("text", nargs="?", help='tune: the correction, in your words')
    vp.add_argument("--sample", type=int, default=400,
                    help="messages fed to the distillation call")
    vp.add_argument("--model", default="sonnet",
                    help="model for the one distillation call")
    sub.add_parser("update")
    sub.add_parser("version")

    a = ap.parse_args()
    if not a.cmd:
        ap.print_help()
        return
    if a.cmd == "version":
        print(f"foreman {__version__}")
    elif a.cmd == "ls":
        cmd_ls()
    elif a.cmd == "hook":
        cmd_hook(a.action, a.profile, a.mode)
    elif a.cmd == "savings":
        cmd_savings()
    elif a.cmd == "session":
        cmd_session(a.match)
    elif a.cmd == "restart":
        cmd_restart(a.idle_min, a.go, a.target, a.force)
    elif a.cmd == "watch":
        cmd_watch_install() if a.install else \
            cmd_watch(a.go, a.idle_min, a.ctx_min, a.keep_turns)
    elif a.cmd == "style":
        cmd_style(a.action, a.name)
    elif a.cmd == "voice":
        cmd_voice(a.action, a.sample, a.model, a.text)
    elif a.cmd == "update":
        cmd_update()
    else:
        m = collect(discover_profiles(a.profile), a.days)
        if a.cmd == "audit":
            print_audit(m, deep=a.deep)
        elif a.cmd == "snapshot":
            cmd_snapshot(m, a.tag)
            print_audit(m)
        elif a.cmd == "compare":
            cmd_compare(m, a.tag)


if __name__ == "__main__":
    main()
