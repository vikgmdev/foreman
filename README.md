# cc-diet

**Measure where your Claude Code tokens actually go. Then cut the part that's real.**

Most "token saver" tools for Claude Code optimize *output* — and output is typically
**~10% of real spend** (often <1% of token volume). This repo starts from a different
place: it reads the session transcripts Claude Code already writes on your disk,
shows you your true cost decomposition, and then attacks the component that
dominates it. Nothing leaves your machine; there are no dependencies.

## The finding (measured, reproducible)

Auditing one real, heavy multi-project setup — 970 sessions / 70K API calls over
7 days (~$50K at list prices; subscription users burn the same budget) — the spend
decomposed as:

| Component | Share | What it is |
|---|---|---|
| `cache_read` | **50.5%** | the resident context re-read on **every** agentic-loop iteration |
| `cache_write` warm | 27.4% | per-iteration deltas written to cache |
| `cache_write` cold | 11.3% | full-context rewrites when waking a session idle past the cache TTL |
| output | 10.7% | what most tools optimize |
| uncached input | 0.1% | |

So the bill is governed by one product:

```
cost ≈ resident_context × loop_iterations
```

And the resident context of long-running sessions is mostly garbage. Autopsying
the 10 most expensive sessions (each ~1M tokens resident, together 66% of all
cost):

- **75–80% is tool traffic** — old command outputs, file reads, edit payloads
- **~99% of those tool results were >30 turns old**: stale by construction
- the actual **dialogue — where decisions live — is only ~20%**

Killing stale tool traffic shrinks a 1M-token whale to ~250K **without touching
the conversation**. Since ~78% of cost scales linearly with resident size, that
is a ~4× cut on the sessions that dominate the bill — honest expected savings of
**50–70% for long-running multi-project workflows**. Not 97%; nobody's is.

## The second insight: idle makes compaction free

The prompt cache lives ~5 minutes. If you run many long-lived sessions and touch
each occasionally (the tmux-per-project pattern), nearly every message you send
lands on a **cold cache** — the full-context rewrite happens anyway. That means:

> **The moment right after a long idle is a free compaction point.** Cleaning the
> session then costs almost nothing extra, and every subsequent iteration runs on
> a fraction of the context.

Compaction's usual failure mode — firing mid-task and destroying working context —
also disappears: a session idle for an hour is, by definition, between things.

## Usage

```bash
# 1. See where YOUR money goes (multi-profile aware, dedupes cloned profiles)
python3 ccdiet.py audit --deep

# 2. Freeze a baseline before changing anything
python3 ccdiet.py snapshot --tag baseline

# 3. Install the sentinel (below), work normally for a few days…

# 4. …then prove (or disprove) the savings
python3 ccdiet.py compare --tag baseline
```

`compare` leads with **normalized metrics** ($ per user-turn, resident context
per call) because raw weekly totals track how much you worked, not how efficient
you were. Claim savings from the normalized block only.

## The sentinel hook

`hooks/context_sentinel.py` is a `UserPromptSubmit` hook implementing the free-
compaction-point rule: when you message a session that is **fat** (>150K resident)
*and* **cold** (idle >1h), it intervenes:

- `CC_DIET_MODE=advise` (default) — instructs the model to surgically compact
  first: *keep the last 15 turns and all decisions/state verbatim; drop stale
  tool traffic (durable facts are already in files/memory)*.
- `CC_DIET_MODE=block` — bounces your prompt back with the exact `/compact`
  command to run; nothing happens automatically.
- `CC_DIET_MODE=off` — disabled.

Install — add to `~/.claude/settings.json` (any profile):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
          "command": "python3 /ABSOLUTE/PATH/cc-diet/hooks/context_sentinel.py" } ] }
    ]
  }
}
```

Thresholds via env: `CC_DIET_CTX_TOKENS` (150000), `CC_DIET_IDLE_S` (3600).
The hook fails open — any error and your prompt proceeds untouched.

## What else actually moves the bill (measured, in order)

1. **Delegate multi-step work to subagents when the parent session is fat.** A
   subagent does its 30 tool-calls on a ~40K context instead of your 400K one —
   same work, ~10× cheaper reads — and the parent doesn't grow. The fatter the
   session, the stronger the effect.
2. **Model tier**: in the audited setup 92% of spend was Opus. The same context
   on Sonnet costs 5× less; route execution-heavy work accordingly.
3. **Trim the fixed prefix.** A fresh session started at **~70K tokens** before
   the first message (system prompt + tools + MCP servers + plugins + CLAUDE.md)
   — re-read by every iteration of every session. Disable unused MCP servers and
   plugins per project; keep CLAUDE.md lean.
4. **Parallelize tool calls.** Every merged iteration is one full context read
   saved.
5. **Don't bother with**: output-trimming proxies (attack 10%), extended 1-h
   cache TTL for mostly-idle fleets (it raises *all* write prices 1.25×→2×; on
   the audited mix it nets **negative**), or per-turn history pruning (editing
   the past invalidates the cache prefix — you pay 12.5× to "save").

## Method notes

- Prices are Aug-2026 Anthropic list prices, overridable via `CC_DIET_PRICES`.
  Subscription users: dollars are notional but proportions and savings are real.
- Cold/warm split assumes the default 5-minute cache TTL (`TTL_S` in source).
- Token composition uses a chars/4 approximation over transcript content;
  shares are robust to the approximation, absolute counts less so.
- Everything runs locally against `~/.claude*/projects/*/*.jsonl`. No network.

## License

MIT
