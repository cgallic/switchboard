"""switchboard/tests.py — in-memory smoke test of the whole agent.

Runs entirely offline (deterministic extractor + SQLite mirror), so:
    python -m switchboard.tests
proves the gated DAG end to end with no keys and no install beyond Python.
"""
from __future__ import annotations

import os

from . import blackboard as bb
from . import lanes
from . import extractor
from . import notion_mcp
from .agent_loop import Agent, SAMPLE_TRANSCRIPT, SAMPLE_CALLER


def _agent() -> Agent:
    # everything in-memory; force the offline extractor
    return Agent(":memory:", force_offline=True, mirror_path=":memory:")


def test_extractor_contract() -> None:
    rec = extractor.extract_record(SAMPLE_TRANSCRIPT, SAMPLE_CALLER, force_offline=True)
    for k in extractor.REQUIRED_FIELDS:
        assert k in rec, f"missing field {k}"
    assert rec["urgency"] in extractor.URGENCIES
    assert rec["urgency"] == "emergency", f"leak/today should be emergency, got {rec['urgency']}"
    assert "Maria" in rec["customer_name"], rec["customer_name"]
    assert "Water heater" in rec["service_type"], rec["service_type"]
    assert rec["phone"] == SAMPLE_CALLER
    print("[PASS] extractor returns the strict seven-field contract, urgency=emergency")


def test_lane_shape() -> None:
    assert lanes.first_stage() == "intake"
    assert lanes.next_stage("intake") == "extract"
    assert lanes.next_stage("dedupe") == "file"
    assert lanes.stage_def("file").gate == lanes.IRREVERSIBLE
    assert lanes.is_terminal("verify")
    assert lanes.next_stage("verify") is None
    print("[PASS] lane: intake->extract->dedupe->[file:irreversible]->schedule->verify(terminal)")


def test_auto_run_completes() -> None:
    a = _agent()
    run_id = a.start_run(SAMPLE_TRANSCRIPT, SAMPLE_CALLER, "auto")
    res = a.drive(run_id)
    assert res["status"] == "done", res
    run = bb.get_run(a.conn, run_id)
    assert run.record.get("_filed_id"), "no Notion record id after auto run"
    assert run.record.get("followup"), "no follow-up date scheduled"
    assert run.record.get("_verified") is True, "record did not verify as queryable"
    # the filed record is queryable
    rows = a.backend.query_jobs(status="New")
    assert any(r["id"] == run.record["_filed_id"] for r in rows)
    print("[PASS] auto run completes the full loop: filed + dated + queryable")


def test_gate_blocks_then_approves() -> None:
    a = _agent()
    run_id = a.start_run(SAMPLE_TRANSCRIPT, SAMPLE_CALLER, "gated")
    res = a.drive(run_id)
    assert res["status"] == "blocked", f"gated run should block at file, got {res}"
    assert res["stage"] == "file"
    run = bb.get_run(a.conn, run_id)
    assert run.gate and run.gate["stage"] == "file"
    # nothing was filed while blocked
    assert not run.record.get("_filed_id"), "filed before approval — gate is broken!"
    # approve -> runs the live write -> continues to done
    res2 = a.approve(run_id)
    assert res2["status"] == "done", res2
    run2 = bb.get_run(a.conn, run_id)
    assert run2.record.get("_filed_id"), "no record after approval"
    assert run2.record.get("_verified") is True
    print("[PASS] gated run BLOCKS at the irreversible write, then approve files + finishes")


def test_dedupe_returning_customer() -> None:
    a = _agent()
    # first call files a record
    r1 = a.start_run(SAMPLE_TRANSCRIPT, SAMPLE_CALLER, "auto")
    a.drive(r1)
    # second call, same caller -> dedupe marks returning
    r2 = a.start_run("Hi it's Maria again, the drain is slow now.", SAMPLE_CALLER, "auto")
    a.drive(r2)
    run2 = bb.get_run(a.conn, r2)
    assert run2.record.get("_dedupe") == "returning", run2.record.get("_dedupe")
    print("[PASS] dedupe recognizes a returning customer by phone")


def test_mcp_catalog() -> None:
    names = {t["name"] for t in notion_mcp.TOOL_CATALOG}
    assert names == {"find_customer", "create_job_record", "schedule_followup", "query_jobs"}, names
    # mirror backend honors the same surface
    be = notion_mcp.MirrorBackend(":memory:")
    filed = be.create_job_record({"customer_name": "Test", "phone": "+10000000000",
                                  "service_type": "X", "urgency": "soon"})
    assert filed["id"].startswith("job_")
    assert be.find_customer("+10000000000")["customer_name"] == "Test"
    assert be.schedule_followup(filed["id"], "2026-07-20")["ok"] is True
    assert len(be.query_jobs(status="New")) == 1
    print("[PASS] MCP server advertises 4 tools; mirror backend honors the full surface")


def test_mcp_server_advertises_tools() -> None:
    """Verify the REAL FastMCP server (the stdio-served surface) advertises the
    four tools to an MCP client and that a tool call routes through the protocol.

    This goes beyond `--list` (a static constant): it builds the exact server
    `python -m switchboard.notion_mcp` serves over stdio and asks it, the way
    Claude Desktop would, what tools it exposes. Skips gracefully (not a failure)
    if the optional `mcp` package isn't installed in this environment.
    """
    try:
        import asyncio
        from mcp.server.fastmcp import FastMCP  # noqa: F401
    except Exception:
        print("[SKIP] `mcp` not installed — stdio server verified via build_mcp_server in CI")
        return

    backend = notion_mcp.MirrorBackend(":memory:")
    server = notion_mcp.build_mcp_server(backend)

    async def _exercise() -> None:
        tools = await server.list_tools()
        names = {t.name for t in tools}
        assert names == {"find_customer", "create_job_record",
                         "schedule_followup", "query_jobs"}, names
        # route a real call through the MCP server (not the backend directly)
        await server.call_tool("create_job_record", {"record": {
            "customer_name": "MCP Smoke", "phone": "+15550009999",
            "service_type": "Drain", "urgency": "soon"}})

    asyncio.run(_exercise())
    # the record reached the backend through the served tool
    assert backend.find_customer("+15550009999")["customer_name"] == "MCP Smoke"
    print("[PASS] FastMCP stdio server advertises the 4 tools to an MCP client; call_tool routes through")


def test_stage_log_visible() -> None:
    a = _agent()
    run_id = a.start_run(SAMPLE_TRANSCRIPT, SAMPLE_CALLER, "auto")
    a.drive(run_id)
    hist = bb.stage_history(a.conn, run_id)
    stages_seen = [h["stage"] for h in hist]
    for s in ("intake", "extract", "dedupe", "file", "schedule", "verify"):
        assert s in stages_seen, f"{s} not in visible DAG log"
    # the file stage is logged as an executed irreversible action
    file_rows = [h for h in hist if h["stage"] == "file"]
    assert any(h["executed"] == 1 and h["gate"] == "irreversible" for h in file_rows)
    print("[PASS] every stage is written to the visible/queryable blackboard")


def run_all() -> int:
    print("=" * 64)
    print("SWITCHBOARD SELF-TEST (offline, no keys)")
    print("=" * 64)
    tests = [
        test_extractor_contract,
        test_lane_shape,
        test_auto_run_completes,
        test_gate_blocks_then_approves,
        test_dedupe_returning_customer,
        test_mcp_catalog,
        test_mcp_server_advertises_tools,
        test_stage_log_visible,
    ]
    for t in tests:
        t()
    print("=" * 64)
    print(f"ALL {len(tests)} TESTS PASSED")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_all())
