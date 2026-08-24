#!/usr/bin/env python3
"""
fleece — measure where your Claude Code tokens actually go, then prove your savings.

Reads the local session transcripts Claude Code already writes
(~/.claude/projects/*/*.jsonl — no API calls, no telemetry, nothing leaves
your machine) and decomposes real spend into the categories that matter:

  - COLD cache writes  : full-context rewrites after an idle gap (> cache TTL)
  - WARM cache writes  : per-iteration deltas inside a turn
  - cache reads        : the resident context re-read on EVERY agentic iteration
  - input/output       : what you'd naively think you pay for (usually <15%)

Why: most "token saver" tools optimize output — typically <1% of real spend in
agentic use. The dominant cost is resident_context x loop_iterations. Measure
yours before believing anyone's 97% screenshot, including ours.

Commands:
  audit      [--days 7] [--deep]      decompose spend; --deep adds whale autopsy
  snapshot   [--tag NAME]             save current metrics as a named baseline
  compare    [--tag NAME]             current window vs a saved baseline
  ls                                  list saved snapshots

Multi-profile: every ~/.claude* directory containing projects/ is scanned
(CLAUDE_CONFIG_DIR profiles included). Duplicate session ids across profiles
(e.g. a copied profile) are deduped. Restrict with --profile DIR.

Honest-comparison note: raw $/week tracks how much you worked, not how
efficient you were. `compare` therefore leads with NORMALIZED metrics —
$/user-turn, resident context per call — and only then shows totals.

Zero dependencies. Python 3.9+.
"""
import argparse
import datetime
import glob
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict

# $/Mtok: (input, cache_write_5m, cache_read, output). Override via FLEECE_PRICES
# (JSON, same shape) when prices change — these are Aug 2026 Anthropic list prices.
PRICES = {
    "opus": (15.0, 18.75, 1.5, 75.0),
    "sonnet": (3.0, 3.75, 0.3, 15.0),
    "haiku": (0.8, 1.0, 0.08, 4.0),
}
if os.environ.get("FLEECE_PRICES"):
    PRICES.update({k: tuple(v) for k, v in json.loads(os.environ["FLEECE_PRICES"]).items()})

TTL_S = 300  # 5-minute default prompt-cache TTL; gaps beyond this = cold wake
STATE_DIR = os.path.expanduser(os.environ.get("FLEECE_STATE", "~/.local/state/fleece"))
SYS_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


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


def discover_profiles(explicit):
    if explicit:
        return [os.path.expanduser(explicit)]
    home = os.path.expanduser("~")
    outs = []
    for d in sorted(glob.glob(os.path.join(home, ".claude*"))):
        if os.path.isdir(os.path.join(d, "projects")):
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
        "tok": Counter(),  # input/cw_cold/cw_warm/cr/output
        "cost": Counter(),  # same keys, dollars
        "cost_by_model": Counter(),
        "cost_by_project": Counter(),
        "gap_buckets": Counter(),
        "ctx_per_call": [],  # sampled context sizes (input+cr+cw)
        "wake_rewrite": [],  # cw on cold calls = resident rewritten
        "first_ctx": [],  # initial context of fresh sessions (fixed prefix floor)
        "whales": [],  # (cost, calls, max_ctx, project, path)
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
    """Reduce collected metrics to the flat dict snapshots store and compare uses."""
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
    print(f"fleece audit — last {m['window_days']}d across {len(m['profiles'])} profile(s)")
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
            tot = sum(v for k, v in comp.items() if k != "thinking") or 1
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


def cmd_snapshot(m, tag):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"{tag}.json")
    with open(path, "w") as f:
        json.dump(summarize(m), f, indent=2)
    print(f"snapshot '{tag}' saved -> {path}")


def cmd_compare(m, tag):
    path = os.path.join(STATE_DIR, f"{tag}.json")
    if not os.path.exists(path):
        sys.exit(f"no snapshot '{tag}' — run: fleece.py snapshot --tag {tag}")
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

    print(f"fleece compare — '{tag}' ({base['generated'][:10]}) vs now, {cur['window_days']}d windows")
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


def main():
    ap = argparse.ArgumentParser(prog="fleece.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["audit", "snapshot", "compare", "ls"])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--profile", help="scan only this config dir (default: every ~/.claude*)")
    ap.add_argument("--tag", default="baseline")
    ap.add_argument("--deep", action="store_true", help="audit: add whale composition autopsy")
    a = ap.parse_args()

    if a.cmd == "ls":
        os.makedirs(STATE_DIR, exist_ok=True)
        for f in sorted(glob.glob(os.path.join(STATE_DIR, "*.json"))):
            d = json.load(open(f))
            print(f"  {os.path.basename(f)[:-5]:20} {d['generated'][:16]}  "
                  f"{fmt_money(d['total_cost'])} / {d['window_days']}d  "
                  f"$/turn {d['cost_per_wake']:.2f}")
        return

    profiles = discover_profiles(a.profile)
    m = collect(profiles, a.days)
    if a.cmd == "audit":
        print_audit(m, deep=a.deep)
    elif a.cmd == "snapshot":
        cmd_snapshot(m, a.tag)
        print_audit(m)
    elif a.cmd == "compare":
        cmd_compare(m, a.tag)


if __name__ == "__main__":
    main()
