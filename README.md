# 🦪 barnacle

**Barnacles grow on whales. Yours are growing on your Claude Code sessions.**

Your longest-running sessions — the ones alive for days in tmux, one per project —
are whales. Measured on a real multi-project setup, the **top-10 whale sessions
were 66% of all spend**, each dragging ~1M tokens of context… and **75–80% of that
context was stale tool traffic**: old command outputs, old file reads, edit
payloads applied hundreds of turns ago. Barnacles. Dead weight, re-read and
re-billed on *every single agentic iteration*.

barnacle finds them and scrapes them off. Nothing leaves your machine, zero
dependencies, and it will tell you honestly if your setup doesn't need it.

## Why every other "token saver" misses

Most tools optimize *output* — but output is ~10% of real agentic spend. Reading
the transcripts Claude Code already writes locally, real cost decomposes as:

| Component | Share | What it is |
|---|---|---|
| `cache_read` | **50.5%** | resident context re-read on **every** loop iteration |
| `cache_write` warm | 27.4% | per-iteration deltas |
| `cache_write` cold | 11.3% | full-context rewrite when waking an idle session |
| output | 10.7% | what the viral screenshots optimize |
| uncached input | 0.1% | |

The bill is governed by one product: **`resident_context × loop_iterations`**.
And in whales, resident context is mostly barnacles — the *dialogue*, where your
decisions actually live, is only ~20%. Scraping stale tool traffic shrinks a 1M
whale to ~250K **without touching the conversation**. Honest expected savings for
long-running multi-project workflows: **50–70%**. Not 97% — nobody's is 97%.

## The trick: whales get scraped while they sleep

The prompt cache lives ~5 minutes. If you touch each of your many sessions
occasionally, nearly every message you send lands on a **cold cache** — the full
rewrite happens anyway. Which means:

> **Right after a long idle, compaction is free.** The expensive rewrite was
> already going to happen; do the cleaning then, and every iteration afterwards
> runs on a fraction of the context.

This also kills auto-compact's classic failure mode — firing mid-task and
destroying the plan you were in the middle of. A session idle for an hour is,
by definition, between things.

## Usage

```bash
# 1. Meet your whales (multi-profile aware; dedupes cloned profiles)
python3 barnacle.py audit --deep

# 2. Freeze the "before" — this is what makes your savings claim publishable
python3 barnacle.py snapshot --tag baseline

# 3. Install the scraper (below), work normally for a few days…

# 4. …then prove it, with workload-independent metrics
python3 barnacle.py compare --tag baseline
```

`compare` leads with **normalized numbers** — $ per user-turn, resident context
per call — because raw weekly totals track how much you worked, not how efficient
you got. Claim savings from the normalized block only.

## The scraper

`hooks/context_sentinel.py` is a `UserPromptSubmit` hook implementing the
whales-get-scraped-while-they-sleep rule: when you message a session that is
**fat** (>150K resident) *and* **cold** (idle >1h), it intervenes:

- `BARNACLE_MODE=advise` (default) — tells the model to surgically compact first:
  *keep the last 15 turns and every decision/state verbatim; drop stale tool
  traffic (durable facts already live in files/memory)*.
- `BARNACLE_MODE=block` — bounces your prompt back with the exact `/compact`
  command; nothing happens without you.
- `BARNACLE_MODE=off` — disabled.

Install — add to `~/.claude/settings.json` (any profile):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
          "command": "python3 /ABSOLUTE/PATH/barnacle/hooks/context_sentinel.py" } ] }
    ]
  }
}
```

Thresholds: `BARNACLE_CTX_TOKENS` (150000), `BARNACLE_IDLE_S` (3600). The hook
fails open — any error and your prompt proceeds untouched.

## What else actually moves the bill (measured, in order)

1. **Delegate multi-step work to subagents when the parent is fat.** The subagent
   runs its 30 tool calls on a ~40K context instead of your 400K — same work,
   ~10× cheaper reads — and the parent doesn't grow.
2. **Model tier.** 92% of the audited spend was Opus; the same context on Sonnet
   is 5× cheaper. Route execution-heavy work down.
3. **Trim the fixed prefix.** Fresh sessions started at ~70K tokens *before the
   first message* (system prompt + MCP servers + plugins + CLAUDE.md) — re-read
   by every iteration everywhere. Disable unused MCP/plugins per project.
4. **Parallelize tool calls.** Every merged iteration is one full context read
   saved.
5. **Skip**: output-trimming proxies (attack the 10%), extended 1h cache TTL for
   mostly-idle fleets (raises *all* write prices 1.25×→2×; netted **negative** on
   the audited mix), per-turn history pruning (editing the past invalidates the
   cache prefix — you pay 12.5× to "save").

## Method notes

- Prices: Aug-2026 Anthropic list, overridable via `BARNACLE_PRICES`. On a
  subscription the dollars are notional — proportions and savings are real.
- Cold/warm split assumes the default 5-minute cache TTL (`TTL_S` in source).
- Composition uses a chars/4 approximation; shares are robust to it.
- Reads `~/.claude*/projects/*/*.jsonl` only. No network, no telemetry.

## License

MIT
