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

__version__ = "0.4.2"

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
                     "kind": kind})

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

    auto, manual = [], []
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
        ready = (r["idle"] is not None and r["idle"] >= idle_min) or force
        if r["sid"] is None and not force:
            ready = False
        if r["shared"] > 1:
            if ready:
                manual.append(r)
        elif r["pane"] and ready:
            auto.append(r)
        elif ready:
            manual.append(r)

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
        resume = f"claude --resume {r['sid']}" if r["sid"] else "claude --continue"
        if r["cfg"]:
            resume = f"CLAUDE_CONFIG_DIR={r['cfg']} {resume}"
        subprocess.run(["tmux", "send-keys", "-t", pane, resume, "Enter"])
        print(f"  ✓ relaunched: {resume}")
    if manual:
        print(f"\n{len(manual)} non-tmux session(s) still need /hooks or a manual resume (see dry-run).")




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
    hp.add_argument("--mode", choices=["advise", "block"], default="advise")
    rp = sub.add_parser("restart")
    rp.add_argument("--idle-min", type=int, default=30,
                    help="only recycle sessions idle at least this many minutes")
    rp.add_argument("--go", action="store_true", help="execute (default: dry-run)")
    rp.add_argument("--target", help="filter: pid, tmux pane/session, or cwd substring")
    rp.add_argument("--force", action="store_true",
                    help="ignore the idle check (NOT recommended)")
    sp = sub.add_parser("session")
    sp.add_argument("match", nargs="?", help="session-id prefix, project-dir substring, or transcript path")
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
    elif a.cmd == "session":
        cmd_session(a.match)
    elif a.cmd == "restart":
        cmd_restart(a.idle_min, a.go, a.target, a.force)
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
