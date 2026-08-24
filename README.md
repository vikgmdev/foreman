# 👷 foreman

**Measure where your Claude Code tokens actually go — then cut the part that matters, automatically.**

foreman reads the session transcripts Claude Code already writes on your disk,
shows you your real cost decomposition, and installs a hook that trims dead
context at the one moment trimming is free. Nothing leaves your machine. Zero
dependencies. Python 3.9+.

```bash
python3 foreman.py audit --deep     # where your money goes
python3 foreman.py snapshot         # freeze a baseline
# … install the hook, work normally for a few days …
python3 foreman.py compare          # measured savings — or proof there were none
```

## The problem

Most token-saver tools optimize *output*. But decomposing real usage from a
heavy multi-project setup (970 sessions, 70K API calls, 7 days), output is a
sideshow:

| Component | Share | What it is |
|---|---|---|
| `cache_read` | **50.5%** | resident context re-read on **every** agentic-loop iteration |
| `cache_write` warm | 27.4% | per-iteration deltas written to cache |
| `cache_write` cold | 11.3% | full-context rewrite when waking a session idle past the cache TTL |
| output | 10.7% | what most tools optimize |
| uncached input | 0.1% | |

The bill is governed by one product:

```
cost ≈ resident_context × loop_iterations
```

Every tool call the model makes re-reads the session's entire resident context.
A 30-tool-call task on a 400K-token session re-reads 400K tokens thirty times.

## What the context is actually made of

Autopsying the 10 most expensive sessions in that dataset (each ~1M tokens
resident; together 66% of all spend):

- **75–80% was tool traffic** — old command outputs, old file reads, edit
  payloads applied hundreds of turns ago
- **~99% of those tool results were more than 30 turns old** — stale by
  construction
- the **dialogue** — where decisions and working state actually live — was
  only **~20%**

That is the target. Dropping stale tool traffic shrinks a 1M-token session to
~250K **without touching the conversation**. Since ~78% of cost scales linearly
with resident size, realistic savings for long-running multi-project workflows
are **50–70%**. Not 97% — nobody's is 97%.

## The key insight: idle time makes cleanup free

The prompt cache lives ~5 minutes. If you run several long-lived sessions and
touch each one occasionally, nearly every message you send lands on a **cold
cache** — the full-context rewrite happens anyway, no matter what you do. So:

> **Right after a long idle, compaction costs nothing extra.** The expensive
> rewrite was already going to happen. Compact then, and every iteration
> afterwards runs on a fraction of the context.

This timing also avoids auto-compact's classic failure mode — firing mid-task
and destroying the plan you were in the middle of. A session that has been idle
for an hour is, by definition, between things.

## The tools

### `foreman.py` — audit, snapshot, compare

- `audit [--days 7] [--deep]` — full decomposition: cold/warm/read/write split,
  cost per user-turn, resident context percentiles, your most expensive
  sessions, and (with `--deep`) what their context is made of.
- `snapshot [--tag NAME]` — freeze current metrics as a baseline.
- `compare [--tag NAME]` — current window vs baseline. Leads with
  **normalized metrics** ($ per user-turn, resident context per call) because
  raw weekly totals track how much you worked, not how efficient you got.
- `ls` — list saved snapshots.

Multi-profile aware: every `~/.claude*` directory containing `projects/` is
scanned (`CLAUDE_CONFIG_DIR` setups included), with duplicate sessions deduped.

### `hooks/context_sentinel.py` — the automatic part

A `UserPromptSubmit` hook. When you message a session that is **fat**
(resident context above `FOREMAN_CTX_TOKENS`, default 150K) *and* **cold**
(idle longer than `FOREMAN_IDLE_S`, default 1h), it intervenes:

- `FOREMAN_MODE=advise` (default) — instructs the model to compact surgically
  before doing anything else: *keep the last 15 turns and every decision, file
  path and piece of working state verbatim; drop stale tool traffic — durable
  facts already live in files/memory.*
- `FOREMAN_MODE=block` — bounces your prompt back with the exact `/compact`
  command to run; nothing happens automatically.
- `FOREMAN_MODE=off` — disabled.

The hook fails open: any error, and your prompt proceeds untouched.

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

Note: running sessions capture hooks at startup — restart long-lived sessions
once after installing.

## What else actually moves the bill (measured, in order)

1. **Delegate multi-step work to subagents when the parent session is fat.**
   A subagent runs its 30 tool calls on a ~40K context instead of the parent's
   400K — same work, ~10× cheaper reads — and the parent doesn't grow.
2. **Model tier.** 92% of the audited spend ran on the top-tier model; the same
   context on the mid tier costs 5× less. Route execution-heavy work down.
3. **Trim the fixed prefix.** Fresh sessions started at ~70K tokens *before the
   first message* (system prompt + MCP servers + plugins + CLAUDE.md), re-read
   by every iteration of every session. Disable unused MCP servers and plugins
   per project; keep CLAUDE.md lean.
4. **Parallelize tool calls.** Every merged iteration is one full context read
   saved.
5. **Skip these**: output-trimming proxies (they attack the ~10% slice),
   extended 1-hour cache TTL for mostly-idle fleets (it raises *all* write
   prices 1.25×→2× and netted **negative** on the audited mix), and per-turn
   history pruning (editing past messages invalidates the cache prefix — you
   pay ~12.5× to "save").

## Method notes

- Prices are Aug-2026 Anthropic list prices, overridable via `FOREMAN_PRICES`
  (JSON: `{"tier": [input, cache_write, cache_read, output]}` in $/Mtok). On a
  subscription the dollars are notional — proportions and savings are real.
- The cold/warm split assumes the default 5-minute cache TTL (`TTL_S` in
  source).
- Context composition uses a chars/4 approximation; the reported *shares* are
  robust to it, absolute token counts less so.
- Reads `~/.claude*/projects/*/*.jsonl` only. No network calls, no telemetry.

## License

MIT
