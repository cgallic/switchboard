"""Switchboard — a gated multi-step agent that turns a live phone call into a
clean, dated, queryable Notion job record.

The trigger is a real-world event (a phone call). The output is structured data
the business keeps and queries (a Notion row). The orchestration is a gated DAG,
not one mega-prompt: safe stages auto-run, the irreversible Notion write blocks
for approval.

Public surface:
    switchboard.notion_mcp   — the reusable Notion Intake MCP server (4 tools)
    switchboard.agent_loop   — the gated DAG orchestrator + CLI
    switchboard.lanes        — the lane/stage/gate definitions
    switchboard.blackboard   — the SQLite run-log (audit trail)
    switchboard.extractor    — transcript -> structured record (Claude or offline stub)
"""

__version__ = "0.1.0"
__all__ = ["notion_mcp", "agent_loop", "lanes", "blackboard", "extractor"]
