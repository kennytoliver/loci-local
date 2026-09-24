<p align="center"><img src="assets/icon.png" width="128" alt="Loci"></p>

<p align="center"><b>a memory palace your agents share.</b><br>One local memory hub shared by all your AI agents.</p>

# Loci

[简体中文](README.md) ｜ English

One SQLite file, shared by every MCP-capable agent you use — no cloud, no account, no dependencies.

You use Claude Code in the morning, Codex after lunch, Cursor in the evening — and every one of them starts from zero, re-asking things you already decided. Loci fixes that: **one SQLite file on your machine, exposed to every MCP-capable agent, with nothing to install but Python.**

```
Claude Code ─┐
Codex ───────┤
Cursor ──────┼──►  Loci         ──►  loci.db   (one file, on your disk)
Windsurf ────┤     (MCP server)
Copilot ─────┘
```

## Why this exists

### The problem: every time you switch harnesses, you start over

If you use more than one AI coding tool (Claude Code, Codex, Cursor, Windsurf, Copilot…), you know the loop:

- In the morning you explain your project's background, your tech choices, and the pitfalls you hit — in product A.
- At noon you want to try B's new model. You switch over and **it knows nothing**. You explain again.
- In the evening you switch back to C for the free quota. You explain a third time.

It bites hardest for:

| Who you are | Where it hurts |
|---|---|
| **Multi-harness users** | Decisions, unwritten conventions and hard-won lessons stay inside the last product. Switching = amnesia. |
| **Model comparison testers** | You want the same task across different models and harnesses, but the context won't move with you. You re-paste the same background every time. |
| **Free-tier hoppers** | You use whichever product has quota today — sometimes three or four switches a day. Rebuilding context each time is what wears you down. |

Worse: even within one product, **a new conversation can lose context entirely**, and there's no way to export it, keep it, or carry it with you.

### What Loci does: takes memory out of the harness

Memory shouldn't belong to a product. It should belong to you. Loci keeps it in a single local SQLite file and exposes it to every agent over standard MCP:

- **Switching no longer resets you**: a new agent calls `memory_context` and immediately gets your standing decisions, preferences and pitfalls.
- **It works in both directions**: whatever one agent writes (say, "remember this…" in Cursor) is searchable from every other agent. This path is tested end to end.
- **Not another cloud service**: no account, no API key, no telemetry. One file, on your own disk.

Three things that make it different:

1. **Chinese retrieval that actually works** — bigram tokenizer + TF-IDF, 5/5 hit rate on Chinese queries.
2. **Standard MCP** — plug into any MCP-capable agent, no per-product adapters.
3. **Zero dependencies, pure Python** — one engine file, one panel file. Clone and run.


## Install

```bash
git clone https://github.com/kennytoliver/loci-local.git
cd loci-local
python install_agents.py --list      # what's installed on this machine
python install_agents.py --all       # connect every agent found
python install_agents.py --verify    # real MCP handshake, lists the 10 tools
```

Then start the panel:

```bash
python panel.py            # http://127.0.0.1:8787
```

Python 3.9+ is the only requirement. Rust-free, Node-free, cloud-free.

## What it does

**10 MCP tools** for agents:

| Tool | Purpose |
|---|---|
| `memory_save` / `memory_search` / `memory_list` / `memory_delete` / `memory_stats` | store and retrieve memories |
| `memory_context` | pinned + recent memories — call once at the start of a conversation |
| `memory_pin` | mark a memory as always-on |
| `memory_handoff` | project hand-off card to paste into the next agent |
| `session_save` / `session_recall` | archive raw conversations, then search the original wording |

**Memory QC (7 checks + health score)** — the part most memory tools skip:

- duplicates (merge), near-synonyms, contradictions (supersede), stale entries, too-short, **over-coarse (a whole log file stored as one memory → one-click split)**, missing metadata
- **0–100 health score** with a four-dimension penalty breakdown
- **Markdown QC report** export
- superseded memories are **marked, never deleted** (you can still answer "what was true back then")

**Automatic scanning** — no agent cooperation required. Loci reads the conversation stores agents already keep on disk:

| Agent | Source |
|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` (same parser; not yet verified on a real machine) |
| Codex | `~/.codex/sessions/**/*.jsonl` (same parser; not yet verified on a real machine) |
| ZCode (Zhipu GLM) | `~/.zcode/cli/db/db.sqlite` (session / message / part tables) — verified end to end |
| Claude Code | `~/.claude/projects/**/*.jsonl` |
| Codex | `~/.codex/sessions/**/*.jsonl` |

Scans run on panel load, deduplicate by fingerprint, and can auto-extract candidate memories for you to approve.

**Cleanup with a safety net** — delete memories *and* their source files: backup first, then move to the system recycle bin. Batched at 10 files, with a size-verified backup that aborts the delete if anything fails.

**Panel** — dark/light theme, master-detail layout, live health dashboard, zero external requests (no CDN, no icon font, no animation library — all motion is hand-written CSS).

## Tests

```bash
python test_mcp.py      # 9 checks: MCP protocol, Chinese retrieval, session layer
python test_panel.py    # 8 checks: JS syntax guard, DOM ids, routes, zero-dependency audit
```

CI runs both on Ubuntu and Windows across Python 3.9 and 3.12.

## Design notes

- **Search**: bigram tokenizer + TF-IDF + zero-dependency boosts (pinned ×1.35, project match ×1.30, tag match ×1.20). No embeddings — keeping the install to zero dependencies mattered more than squeezing out the last few points of recall.
- **Quality checks**: Jaccard **and** containment similarity (calibrated against real data: a pair reading `深圳/广州番禺/佛山` vs `深圳/广州/佛山` scores J=0.54 / C=0.72 — a single 0.66 Jaccard threshold would have missed it).
- **Time**: superseding marks rather than deletes, so the history of a decision survives.
- **Hooks**: Some agents ship plugin-level hook mechanisms, but the event contract is not publicly documented yet. Rather than ship something that silently never fires, Loci writes a plain-language usage convention into `~/.agents/AGENTS.md` instead.

## Contributing

Issues and PRs are welcome. The one hard rule: **no third-party dependencies** — that constraint is the point of this project. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Status

Working daily on Windows across four agents. Pre-1.0: the panel is Chinese-first, an English UI is on the roadmap.

MIT licensed.

## Acknowledgements

With thanks to [MemForge](https://github.com/gitstq/MemForge) (MIT) — it showed that a local-first, single-file memory store could work.
Loci is an independent implementation focused on Chinese retrieval, MCP integration, and cross-harness continuity.
