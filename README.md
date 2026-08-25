<div align="center">

# 👷 foreman

### Your AI agent sessions have no supervisor. Now they do.

**foreman finds out where your Claude Code tokens actually go — then trims the
dead weight, automatically, at the one moment trimming is free.**

[![Latest tag](https://img.shields.io/github/v/tag/vikgmdev/foreman?label=version&color=blue)](https://github.com/vikgmdev/foreman/tags)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](#)
[![Dependencies: zero](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](#)
[![Privacy](https://img.shields.io/badge/data-never%20leaves%20your%20machine-orange.svg)](#method-notes)

</div>

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/vikgmdev/foreman/main/install.sh | bash
```

That's it. Then:

```bash
foreman audit --deep      # see where your tokens actually go
foreman snapshot          # freeze the "before"
foreman hook install      # enable automatic context trimming
foreman watch --install   # proactive cleanup sweep every 15 min
foreman savings           # $ actually saved so far, attributed per compaction
foreman compare           # days later: prove the savings — or disprove them
```

<sub>Installs to `~/.foreman`, shims `foreman` into `~/.local/bin`. No root, no
package manager, nothing phoned home. Uninstall: `foreman hook uninstall &&
rm -rf ~/.foreman ~/.local/bin/foreman`.</sub>

## What you'll see

```console
$ foreman audit
foreman audit — last 7d across 3 profile(s)
  970 sessions · 70,195 API calls · 3,259 cold wakes

WHERE THE MONEY GOES
  cache_read (agentic loop re-reading context)     $25,227   50.5%
  cache_write WARM (in-turn deltas)                $13,678   27.4%
  cache_write COLD (wake after idle > TTL)          $5,633   11.3%
  output                                            $5,343   10.7%
  input (uncached)                                     $53    0.1%
  TOTAL                                            $49,933

THE NUMBERS THAT PREDICT YOUR NEXT MESSAGE'S COST
  $/user-turn (cold wake)      $15.32
  resident ctx / call (median) 280K   p90 654K
  fresh-session prefix floor   70K

  top-10 sessions = 66.4% of all cost ($33,156)
     $6,313   7,281 calls  max ctx   998K  acme-webapp
     $4,625   4,326 calls  max ctx   998K  data-pipeline
     $3,330   4,854 calls  max ctx   903K  ops-agent
     ...
```

Real numbers from a real heavy multi-project setup. Yours will differ — that's
the point: **measure before believing anyone's screenshot, including this one.**

## Where the money actually goes

Real 7-day decomposition, 970 sessions:

```
cache_read   — context re-read every iteration  ████████████████████░░░░░░░░░░░░░░░░░░░░  50.5%
cache_write  — warm, in-turn deltas             ███████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  27.4%
cache_write  — cold, idle wakes                 █████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  11.3%
output       — what the screenshots optimize    ████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  10.7%
input        — uncached                         ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   0.1%
```

Most "token saver" tools optimize **output** — the 10% slice. The bill is
governed by one product:

```
cost ≈ resident_context × loop_iterations
```

Every tool call the model makes re-reads the session's **entire resident
context**. A 30-tool-call task on a 400K-token session re-reads 400K tokens
thirty times.

### And the resident context is mostly garbage

Autopsy of the 10 most expensive sessions (each ~1M tokens; together 66% of
all spend):

```
tool traffic (stale)  ██████████████████████████████░░░░░░░░  75–80%
dialogue & decisions  ████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  ~20%
```

**~99% of that tool traffic was more than 30 turns old** — command outputs,
file reads and edit payloads from hours ago, re-billed on every iteration.
Dropping it shrinks a 1M session to ~250K **without touching the
conversation**. Realistic savings for long-running multi-project workflows:
**50–70%**. Not 97% — nobody's is 97%.

## The trick: idle time makes cleanup free

> [!TIP]
> The prompt cache lives ~5 minutes. If you run several long-lived sessions
> and touch each occasionally, nearly every message you send lands on a
> **cold cache** — the full-context rewrite happens anyway. **Compacting right
> then costs nothing extra**, and every iteration afterwards runs on a
> fraction of the context.

This timing also kills auto-compact's classic failure mode — firing mid-task
and destroying the plan you were in the middle of. A session idle for an hour
is, by definition, between things.

`foreman hook install` puts this on autopilot: a `UserPromptSubmit` hook (the
**sentinel**) with two triggers:

- **idle tier** — the session is **fat** (>150K resident) *and* **cold**
  (idle >1h): the free moment, the cache is dead anyway.
- **urgent tier** — the session is **huge** (>500K resident), no idle
  required: at that size one more busy turn costs more than the compaction.

When either fires:

| Mode | Behavior |
|---|---|
| `advise` *(default)* | instructs the model to compact surgically first — keep the last 15 turns and every decision verbatim, drop stale tool traffic |
| `auto` | your prompt goes through untouched; once the turn ends and the session's tmux pane has been **continuously calm for 5 min** (`FOREMAN_CALM_S`) — a reading pause is not an absence — foreman types the surgical `/compact` itself and verifies it actually submitted. Never blocks, never races you at the keyboard. Opt-in (`--mode auto`) because it injects keystrokes into your own panes |
| `block` | bounces your prompt back with the exact `/compact` command; nothing happens without you |
| `off` | disabled |

```bash
foreman hook install              # all profiles, advise mode
foreman hook install --mode block # trust nothing
foreman hook status
foreman hook uninstall
```

The hook **fails open** — any error and your prompt proceeds untouched. Every
invocation is logged to `~/.local/state/foreman/sentinel.log`, and every
compaction it causes is measured after the fact: `foreman savings`
cross-references that log with what actually happened in each transcript and
reports **realized** dollars — actual calls that ran on the smaller context —
not projections.

## The sweep: `foreman watch`

The sentinel acts when you message a session. `foreman watch` acts on the
whole fleet without waiting:

- **Live sessions** — fat, idle >1h, sitting in a *verifiably calm* tmux pane:
  the surgical `/compact` is typed in-band by the same deferred watcher the
  sentinel uses. Sessions sharing a cwd are only touched once they're
  **pane-mapped** — the sentinel registers pane↔session↔transcript from
  inside every session it runs in, so the mapping is knowledge, not guesswork.
- **Dead sessions** — no process, fat transcript on disk: stale tool payloads
  are blanked **in the transcript itself** (dialogue, decisions and structure
  intact, `.foreman-bak` backup always), so the next `--resume` rebuilds a
  fraction of the context. No LLM involved — deterministic, free, reversible.

`foreman watch` is a dry-run; `--go` executes; `--install` runs it every
15 minutes as a systemd user timer (or prints the cron line).

> [!NOTE]
> Running sessions capture hooks at startup — that's a Claude Code security
> design (hooks run shell commands; mid-session changes require human review
> via `/hooks`, and there is deliberately no remote reload). `foreman restart`
> handles it: it detects every running session via `/proc` — whatever terminal
> or multiplexer it lives in — shows which ones are on stale hook config, and
> where the harness allows input injection (tmux today) recycles them in place
> with a clean `/exit` + `claude --resume <same session>`, same cwd, same
> profile, nothing lost. Sessions it can't automate get the exact per-session
> recipe (type `/hooks` inside to apply without restarting, or exit + resume).
> It only ever touches sessions idle past `--idle-min` (default 30m), and it
> never force-kills.

## Commands

| Command | What it does |
|---|---|
| `foreman audit [--days N] [--deep]` | full spend decomposition; `--deep` adds whale autopsies |
| `foreman snapshot [--tag NAME]` | freeze current metrics as a baseline |
| `foreman compare [--tag NAME]` | now vs baseline — leads with **normalized** metrics ($/user-turn, ctx/call), because raw totals track how much you worked, not how efficient you got |
| `foreman ls` | list saved snapshots |
| `foreman hook install\|uninstall\|status` | manage the sentinel across every `~/.claude*` profile; `--mode advise\|auto\|block` |
| `foreman session <id\|project>` | one session's card: context curve, cost, compactions |
| `foreman savings` | realized $ saved, attributed per sentinel-triggered compaction |
| `foreman restart [--go]` | find every **running** session (any terminal, any harness), flag the ones on stale hook config, and recycle the recyclable ones in place |
| `foreman watch [--go]` | one proactive sweep: type the surgical `/compact` into fat idle **live** sessions (calm tmux panes only), and trim fat **dead** transcripts on disk — stale tool payloads blanked, dialogue and structure intact, `.foreman-bak` backup always |
| `foreman watch --install` | run that sweep every 15 min (systemd user timer, or prints the cron line) |
| `foreman update` | update foreman itself |

Multi-profile aware (`CLAUDE_CONFIG_DIR` setups included), duplicate sessions
deduped.

## What else actually moves the bill (measured, in order)

1. **Delegate multi-step work to subagents when the parent session is fat** —
   the subagent does its 30 tool calls on a ~40K context instead of the
   parent's 400K. Same work, ~10× cheaper reads, and the parent doesn't grow.
2. **Model tier** — 92% of the audited spend ran on the top-tier model; the
   same context on the mid tier costs 5× less.
3. **Trim the fixed prefix** — fresh sessions started at ~70K tokens *before
   the first message* (system prompt + MCP servers + plugins + CLAUDE.md).
   Disable what each project doesn't use.
4. **Parallelize tool calls** — every merged iteration is one full context
   read saved.
5. **Skip**: output-trimming proxies (the 10% slice), extended 1h cache TTL
   for mostly-idle fleets (raises *all* write prices 1.25×→2×; netted
   **negative** on the audited mix), per-turn history pruning (editing the
   past invalidates the cache prefix — you pay ~12.5× to "save").

## Roadmap

foreman aims to be the **site office for your AI agents** — the boring,
load-bearing tooling that keeps agent fleets on time and within budget:

- `foreman prefix` — startup-context analyzer: what those ~70K tokens are made
  of, per profile, and what to cut
- alerting — flag runaway context growth before it becomes a whale
- Fleet budgets — per-project / per-agent spend tracking and limits
- Support for more agent harnesses beyond Claude Code

## Method notes

<details>
<summary>Pricing, assumptions, and what gets read</summary>

- Prices are Aug-2026 Anthropic list prices, overridable via `FOREMAN_PRICES`
  (JSON: `{"tier": [input, cache_write, cache_read, output]}` in $/Mtok). On a
  subscription the dollars are notional — proportions and savings are real.
- The cold/warm split assumes the default 5-minute prompt-cache TTL (`TTL_S`
  in source).
- Context composition uses a chars/4 approximation; reported *shares* are
  robust to it, absolute token counts less so.
- Transcript trimming (`watch`) saves **disk** unconditionally, but saves
  **resume context** only for tool traffic newer than the session's last
  compact boundary — resume replays from that boundary, not from the top.
  Never-compacted fat sessions (most whales) benefit the most. Verified by
  resuming a trimmed 41.6MB whale: byte-identical rebuilt prompt, zero
  breakage.
- foreman reads `~/.claude*/projects/*/*.jsonl` — the transcripts Claude Code
  already writes — and writes snapshots to `~/.local/state/foreman/`. No
  network calls, no telemetry, nothing leaves your machine.

</details>

## License

[MIT](LICENSE)
