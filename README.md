# 👷 foreman

**Your Claude Code sessions have no supervisor. Now they do.**

A foreman's job is simple: make sure the work finishes **on time and within
budget**, and that nobody leaves the job site buried in debris. Long-running
Claude Code sessions fail both without one. Measured on a real multi-project
setup, the **top-10 longest sessions were 66% of all spend**, each dragging ~1M
tokens of context — and **75–80% of it was debris**: old command outputs, old
file reads, edit payloads applied hundreds of turns ago. Re-read, and re-billed,
on *every single agentic iteration*.

foreman walks your job site (the transcripts Claude Code already writes on your
disk), shows you exactly where the budget goes, and orders a cleanup at the one
moment cleanup is free. Nothing leaves your machine. Zero dependencies. And it
will tell you honestly if your site is already clean.

*(Yes, there's a 2011 Ruby process manager named foreman. Different job site.)*

## Why the viral "97% saved" screenshots miss

Most token-saver tools optimize *output* — but output is ~10% of real agentic
spend. Decomposing actual usage:

| Component | Share | What it is |
|---|---|---|
| `cache_read` | **50.5%** | resident context re-read on **every** loop iteration |
| `cache_write` warm | 27.4% | per-iteration deltas |
| `cache_write` cold | 11.3% | full-context rewrite when waking an idle session |
| output | 10.7% | what the screenshots optimize |
| uncached input | 0.1% | |

The bill is governed by one product: **`resident_context × loop_iterations`**.
In long sessions, resident context is mostly debris — the *dialogue*, where your
decisions actually live, is only ~20%. Clearing the debris shrinks a 1M session
to ~250K **without touching the conversation**. Honest expected savings for
long-running multi-project workflows: **50–70%**. Not 97%. Nobody's is 97%.

## The rule: clean the site while the crew is off

The prompt cache lives ~5 minutes. If you run many sessions and touch each one
occasionally, nearly every message you send lands on a **cold cache** — the
full-context rewrite happens anyway, whatever you do. Which means:

> **Right after a long idle, cleanup is free.** The expensive rewrite was
> already going to happen. Clean then, and every iteration afterwards runs on a
> fraction of the context.

This also kills auto-compact's classic failure mode — firing mid-task and
destroying the plan you were in the middle of. A session that has been idle for
an hour is, by definition, between shifts.

## Usage

```bash
# 1. Walk the site (multi-profile aware; dedupes cloned profiles)
python3 foreman.py audit --deep

# 2. Freeze the "before" — this is what makes your savings claim publishable
python3 foreman.py snapshot --tag baseline

# 3. Put the foreman on duty (below), work normally for a few days…

# 4. …then prove it — or disprove it
python3 foreman.py compare --tag baseline
```

`compare` leads with **normalized metrics** — $ per user-turn, resident context
per call — because raw weekly totals track how much you worked, not how efficient
you got. Claim savings from the normalized block only.

## On duty

`hooks/context_sentinel.py` is a `UserPromptSubmit` hook enforcing the
clean-site rule: when you message a session that is **cluttered** (>150K
resident) *and* **cold** (idle >1h), the foreman steps in:

- `FOREMAN_MODE=advise` (default) — tells the model to clean up first,
  surgically: *keep the last 15 turns and every decision/state verbatim; drop
  stale tool traffic (durable facts already live in files/memory)*.
- `FOREMAN_MODE=block` — bounces your prompt back with the exact `/compact`
  command; nothing happens without you.
- `FOREMAN_MODE=off` — off duty.

Install — add to `~/.claude/settings.json` (any profile):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
          "command": "python3 /ABSOLUTE/PATH/foreman/hooks/context_sentinel.py" } ] }
    ]
  }
}
```

Thresholds: `FOREMAN_CTX_TOKENS` (150000), `FOREMAN_IDLE_S` (3600). The hook
fails open — any error and your prompt proceeds untouched.

## What else actually moves the bill (measured, in order)

1. **Delegate multi-step work to subagents when the parent session is fat.** The
   subagent runs its 30 tool calls on a ~40K context instead of your 400K —
   same work, ~10× cheaper reads — and the parent doesn't grow.
2. **Model tier.** 92% of the audited spend was Opus; the same context on Sonnet
   is 5× cheaper. Route execution-heavy work down.
3. **Trim the fixed prefix.** Fresh sessions started at ~70K tokens *before the
   first message* (system prompt + MCP servers + plugins + CLAUDE.md) — re-read
   by every iteration everywhere. Disable unused MCP/plugins per project.
4. **Parallelize tool calls.** Every merged iteration is one full context read
   saved.
5. **Skip**: output-trimming proxies (they attack the 10%), extended 1h cache
   TTL for mostly-idle fleets (raises *all* write prices 1.25×→2×; netted
   **negative** on the audited mix), and per-turn history pruning (editing the
   past invalidates the cache prefix — you pay 12.5× to "save").

## Method notes

- Prices: Aug-2026 Anthropic list, overridable via `FOREMAN_PRICES`. On a
  subscription the dollars are notional — the proportions and savings are real.
- Cold/warm split assumes the default 5-minute cache TTL (`TTL_S` in source).
- Composition uses a chars/4 approximation; shares are robust to it.
- Reads `~/.claude*/projects/*/*.jsonl` only. No network, no telemetry.

## License

MIT
