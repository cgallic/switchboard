"""switchboard/lanes.py — the gated DAG: stages, gates, and the lane.

A lane is an ordered list of Stages. Each Stage names a handler, a gate, and a
human description. The gate matrix is the heart of the agentic story:

    SAFE          -> runs automatically on tick (local work / reversible writes)
    IRREVERSIBLE  -> on a 'gated' run, BLOCK and request approval before acting
                     (the Notion write touches the owner's live workspace)
    TERMINAL      -> the run is done

This mirrors the production cmo-os "factory" engine's gate model: safe stages
auto-run, irreversible stages block for approval, and `approve` runs the blocked
stage then continues. The handlers live in agent_loop.py and are injected at
build time so this module stays a pure description of the DAG.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

SAFE = "safe"
IRREVERSIBLE = "irreversible"
TERMINAL = "__terminal__"


@dataclass
class Stage:
    name: str
    gate: str
    desc: str
    handler: Optional[Callable] = None  # set by agent_loop at wire-up; None = terminal


# The one lane Switchboard runs. Order matters.
#
#   intake  -> extract -> dedupe -> [GATE] file -> schedule -> verify
#    safe       safe       safe   irreversible    safe        terminal
#
LANE: list[Stage] = [
    Stage("intake",   SAFE,        "Receive the call-ended payload from the voice secretary layer"),
    Stage("extract",  SAFE,        "Claude reads the spoken transcript -> strict structured record"),
    Stage("dedupe",   SAFE,        "find_customer: new vs. returning (no duplicate rows)"),
    Stage("file",     IRREVERSIBLE,"create_job_record: write the job row into the owner's live Notion DB"),
    Stage("schedule", SAFE,        "schedule_followup: set the dated follow-up so the lead never goes cold"),
    Stage("verify",   TERMINAL,    "query_jobs: confirm the record is filed and queryable"),
]

_BY_NAME = {s.name: s for s in LANE}
_ORDER = [s.name for s in LANE]


def stage_def(name: str) -> Optional[Stage]:
    return _BY_NAME.get(name)


def first_stage() -> str:
    return _ORDER[0]


def next_stage(name: str) -> Optional[str]:
    """Return the stage after `name`, or None if it is the last."""
    try:
        i = _ORDER.index(name)
    except ValueError:
        return None
    return _ORDER[i + 1] if i + 1 < len(_ORDER) else None


def is_terminal(name: str) -> bool:
    s = stage_def(name)
    return bool(s and s.gate == TERMINAL)


def bind_handlers(handlers: dict[str, Callable]) -> None:
    """Attach the runtime handlers from agent_loop to the declared stages."""
    for name, fn in handlers.items():
        s = _BY_NAME.get(name)
        if s is not None:
            s.handler = fn
