# Switchboard

**The agent that does the after-call paperwork the receptionist used to do.**

A customer calls a service business and describes a job. The instant the call ends, Switchboard runs a **gated multi-step DAG** — Claude reads the messy spoken transcript, extracts a structured record, dedupes against existing customers, and (behind a hard gate on the irreversible write) files a clean, dated, queryable **job record into Notion** with a follow-up scheduled so the lead never goes cold.

The trigger is a **live phone call** — a real-world event, not text already inside a computer. The output is **structured data the business keeps and queries** — a real Notion row, not a chat transcript.

Built for the **AI Agents for Productivity Hackathon** · Notion + Anthropic tracks.

---

## The reusable asset

This repo ships an open **Notion Intake MCP server** (`switchboard/notion_mcp.py`) exposing four tools over the Model Context Protocol:

| Tool | What it does |
|---|---|
| `find_customer` | Dedupe by phone — returns an existing customer record or `None` |
| `create_job_record` | File a fully-populated job row into the Notion Jobs database |
| `schedule_followup` | Set a dated follow-up property so the lead never goes cold |
| `query_jobs` | Filter the Jobs database (by status, urgency, or date) |

Clone the repo, drop in your Notion token + database id, and point it at your own intake (call, email, or form) — working by Monday.

---

## Quick start

```bash
# 1. Install
pip install -e .

# 2. Run the whole loop OFFLINE on a sample call transcript — no keys needed.
#    Claude extraction is stubbed deterministically and Notion is mirrored in
#    local SQLite, so a judge can verify the gated DAG end to end immediately.
python -m switchboard.agent_loop --demo

# 3. Run it GATED — the irreversible Notion write BLOCKS for approval, proving
#    the gate is real, not decorative.
python -m switchboard.agent_loop --demo --autonomy gated
# ...then approve the blocked run:
python -m switchboard.agent_loop --approve <run_id>

# 4. Run the test suite (in-memory, no keys).
python -m switchboard.tests
```

### Going live

```bash
cp .env.example .env          # fill in NOTION_TOKEN, NOTION_DB_ID, ANTHROPIC_API_KEY
python -m switchboard.agent_loop --transcript path/to/call.txt --caller "+17875551234"
```

With keys present, extraction runs on Claude and the record is written to your real Notion database. Without keys, the same code path runs offline against the SQLite mirror — identical record shape, identical DAG.

### Run the MCP server

```bash
python -m switchboard.notion_mcp           # serves the 4 tools over MCP (stdio)
python -m switchboard.notion_mcp --list    # print the tool catalog as JSON
```

The stdio server is built by `notion_mcp.build_mcp_server()` — the single source of truth for the served surface. `python -m switchboard.tests` includes `test_mcp_server_advertises_tools`, which builds that exact server and verifies, over the MCP protocol (not the static `--list` constant), that it advertises all four tools and that a real `call_tool` round-trips to the backend. So the stdio serving path is proven, not assumed.

**Connect it to Claude Desktop** (or any MCP client) — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "switchboard-notion": {
      "command": "python",
      "args": ["-m", "switchboard.notion_mcp"],
      "env": { "NOTION_TOKEN": "secret_...", "NOTION_DB_ID": "..." }
    }
  }
}
```

Omit `env` to run the server against the offline SQLite mirror with no keys — the four tools still appear in the client's tool list and work end to end.

---

## The gated DAG

```
intake → extract → dedupe → [GATE] file → schedule → verify
 safe      safe      safe   irreversible   safe       safe
```

- **SAFE** stages run automatically on `tick`.
- **IRREVERSIBLE** stages (`file` — it writes to the owner's live workspace) **BLOCK** for approval when the run's autonomy is `gated` (the default for anything touching a customer's data). On `--autonomy auto` they run immediately.
- `approve` runs the blocked stage live, then continues the run forward through the remaining safe stages — the same gate shape Switchboard's orchestration engine uses in production.

Every stage transition is logged to a local SQLite `runs` blackboard, so the DAG is fully visible and queryable (and lights up on screen in the demo).

### Watch it run

`viewer/index.html` is a zero-dependency page that polls the `viewer/run.json` snapshot the agent writes on every tick. Open it next to a live run and the DAG lights up stage by stage, the filed record fills in, and the header shows an **instrumented time delta** — `elapsed_seconds` vs. the hand-timed `baseline_seconds` (411s = 6:51), read straight from `run.json` by `renderClock`. The on-screen "6:51 by hand → seconds, 0 typing" number is the agent's own counter, not a desk stopwatch.

```bash
python -m switchboard.agent_loop --demo      # writes viewer/run.json
# then open viewer/index.html in a browser
```

**Already captured:** `demo/` holds a screen recording (`switchboard-demo.mp4`) of this
viewer animating through a real autonomous run and a real gated run (it blocks at the
irreversible Notion write, then approves), plus the three frame screenshots and the
exact `run.json` snapshots behind them. See `demo/README.md` — every asset is
reproducible from the commands above.

---

## Architecture

```
LIVE CALL ─► voice secretary layer ─► call-ended payload ─► Switchboard DAG ─► Notion Intake MCP ─► NOTION JOBS DB
                (reused)                {caller, transcript}    (gated)            (4 tools)         (the kept record)
                                                              Claude extracts +
                                                              plans tool calls
```

- **Reasoning core:** Claude reads the transcript, emits strict JSON, and plans the tool calls. The orchestration is a real gated DAG, not one mega-prompt.
- **Persistence + payoff:** the Notion Intake MCP server writes structured data into a real Notion database.

> Note: Switchboard reuses an existing voice secretary intake layer and gated orchestration engine. This repo is the hackathon's net-new surface — the Notion MCP server, the agent loop, the lane definition, and the offline demo harness.

## License

MIT — Hackathon submission. Clone it, aim it at your own intake.
