# Contributing

Thanks for wanting to help. Two ground rules matter more than anything else here.

## 1. No third-party dependencies. Ever.

Zero dependencies is the whole point of this project — no `pip install`, no Node, no Docker, no database server.
**Pull requests that add an external package will be declined**, even if it makes the code shorter.
If you need a capability, it has to come from the Python standard library.

## 2. Tests must pass

```bash
python test_mcp.py      # 9 checks
python test_panel.py    # 8 checks
```

Both must print `总体: 全部通过` / all PASS before you open a PR. The panel test includes a dependency audit and a real
JS syntax check, so it will catch accidental CDN links or broken script blocks.

## Reporting a bug

Please include:

- Your OS and Python version (`python -V`)
- Which agent(s) you had connected, and the exact tool call that misbehaved
- What you expected vs. what happened
- If retrieval is wrong: the query, and the memory it should have matched

Chinese or English is both fine.

## Sending a pull request

1. Open an issue first for anything larger than a typo, so we don't both waste time.
2. Keep the diff focused — one concern per PR.
3. Run both test files.
4. If you touched `panel.py`, restart the panel and click through the page once; the tests check syntax and structure,
   not whether the UI still makes sense.

## Project layout

| File | Role |
|---|---|
| `hippocampus.py` | Engine: SQLite storage, Chinese retrieval, MCP server, CLI |
| `panel.py` | Single-file web panel (inline HTML/CSS/JS, no CDN) |
| `install_agents.py` | Writes MCP config into each detected agent (with backups) |
| `test_mcp.py` / `test_panel.py` | The test suite |

## Commit messages

Plain and descriptive is enough — say what changed and why. Neither a specific format nor English is required.
