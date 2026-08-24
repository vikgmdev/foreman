# 🐑 fleece

**Stop getting fleeced by your own sessions.**

Long-running Claude Code sessions never stop growing wool. Measured on a real
multi-project setup, the **top-10 longest sessions were 66% of all spend**, each
dragging ~1M tokens of context — and **75–80% of it was fleece**: old command
outputs, old file reads, edit payloads applied hundreds of turns ago. Dead
weight, re-read and re-billed on *every single agentic iteration*.

fleece measures your real spend from the transcripts Claude Code already writes
on your disk, shows you exactly how much wool your sessions are carrying, and
shears it at the one moment shearing is free. Nothing leaves your machine. Zero
dependencies. And it will tell you honestly if your setup doesn't need it.

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
In long sessions, resident context is mostly wool — the *dialogue*, where your
decisions actually live, is only ~20%. Shearing the dead part shrinks a 1M
session to ~250K **without touching the conversation**. Honest expected savings
for long-running multi-project workflows: **50–70%**. Not 97%. Nobody's is 97%.

## The trick: sheep get shorn while they rest

The prompt cache lives ~5 minutes. If you run many sessions and touch each one
occasionally, nearly every message you send lands on a **cold cache** — the
full-context rewrite happens anyway, whatever you do. Which means:

> **Right after a long idle, shearing is free.** The expensive rewrite was
> already going to happen. Shear then, and every iteration afterwards runs on a
> fraction of the context.

This also kills auto-compact's classic failure mode — firing mid-task and
destroying the plan you were in the middle of. A session that has been idle for
an hour is, by definition, between things.

## Usage

```bash
# 1. Weigh the wool (multi-profile aware; dedupes cloned profiles)
python3 fleece.py audit --deep

# 2. Freeze the "before" — this is what makes your savings claim publishable
python3 fleece.py snapshot --tag baseline

# 3. Install the shears (below), work normally for a few days…

# 4. …then prove it — or disprove it
python3 fleece.py compare --tag baseline
```

`compare` leads with **normalized metrics** — $ per user-turn, resident context
per call — because raw weekly totals track how much you worked, not how efficient
you got. Claim savings from the normalized block only.

## The shears

`hooks/context_sentinel.py` is a `UserPromptSubmit` hook implementing the
shorn-while-resting rule: when you message a session that is **woolly** (>150K
resident) *and* **cold** (idle >1h), it intervenes:

- `FLEECE_MODE=advise` (default) — tells the model to shear surgically first:
  *keep the last 15 turns and every decision/state verbatim; drop stale tool
  traffic (durable facts already live in files/memory)*.
- `FLEECE_MODE=block` — bounces your prompt back with the exact `/compact`
  command; nothing happens without you.
- `FLEECE_MODE=off` — disabled.

Install — add to `~/.claude/settings.json` (any profile):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
          "command": "python3 /ABSOLUTE/PATH/fleece/hooks/context_sentinel.py" } ] }
    ]
  }
}
```

Thresholds: `FLEECE_CTX_TOKENS` (150000), `FLEECE_IDLE_S` (3600). The hook fails
open — any error and your prompt proceeds untouched.

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

- Prices: Aug-2026 Anthropic list, overridable via `FLEECE_PRICES`. On a
  subscription the dollars are notional — the proportions and savings are real.
- Cold/warm split assumes the default 5-minute cache TTL (`TTL_S` in source).
- Composition uses a chars/4 approximation; shares are robust to it.
- Reads `~/.claude*/projects/*/*.jsonl` only. No network, no telemetry.

## License

MIT
