"""switchboard/agent_loop.py — the gated DAG orchestrator (THE AGENT).

Drives a call through the lane:

    intake -> extract -> dedupe -> [GATE] file -> schedule -> verify

Gate matrix (the agentic proof):
  * SAFE stages run automatically.
  * The IRREVERSIBLE `file` stage (it writes to the owner's live Notion
    workspace) BLOCKS for approval when the run's autonomy is 'gated' (default).
    On 'auto' it runs immediately. `approve(run_id)` runs the blocked stage live,
    then continues the run forward through the remaining safe stages.

Every transition is written to the SQLite blackboard so the DAG is visible and
queryable (the demo's run-log viewer polls it).

CLI:
    python -m switchboard.agent_loop --demo                  # offline, full loop, auto
    python -m switchboard.agent_loop --demo --autonomy gated # blocks at `file`
    python -m switchboard.agent_loop --approve <run_id>      # approve + continue
    python -m switchboard.agent_loop --transcript call.txt --caller "+17875551234"
    python -m switchboard.agent_loop --source form --form req.json   # form intake
    python -m switchboard.agent_loop --source email --email msg.eml   # email intake
    python -m switchboard.agent_loop --status <run_id>       # print the run + DAG

Demo determinism: the offline mirror is a *file* so --approve/--status across
processes see the same records. That file is a re-record landmine — a second
`--demo` run would find the first run's row and falsely report "returning
customer." So `--demo` resets the mirror + blackboard to a clean slate before it
runs (override with --keep-demo-state), which makes the dedupe beat deterministic
on camera every take. `--reset-demo` resets without running.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from . import adapters
from . import blackboard as bb
from . import lanes
from . import extractor
from . import notion_mcp

DEFAULT_DB = os.environ.get("SWITCHBOARD_DB", "switchboard.db")
DEFAULT_MIRROR = os.environ.get("SWITCHBOARD_MIRROR", "switchboard_notion_mirror.db")
DEFAULT_RUN_JSON = os.environ.get("SWITCHBOARD_RUN_JSON", "viewer/run.json")
MAX_STEPS = 12  # safety cap so one drive() can never loop forever

# Baseline the demo's live counter ticks against: the hand-timed median from
# build/baseline-timing.md (6:51 = 411s). The viewer shows THIS run's own elapsed
# seconds next to it, so the on-screen delta is instrumented, not a desk stopwatch.
BASELINE_SECONDS = 411

SAMPLE_TRANSCRIPT = (
    "Yeah hi, this is Maria Delgado, my water heater's leaking all over the "
    "garage, there's water everywhere, can someone come out today? I'm on Maple "
    "Street, it's the blue house. How much is that gonna run me?"
)
SAMPLE_CALLER = "+17875550142"


class Agent:
    """Holds the run-time wiring: blackboard + Notion backend + extractor mode."""

    def __init__(self, db_path: str = DEFAULT_DB, *, force_offline: bool = False,
                 mirror_path: str = ":memory:",
                 run_json_path: Optional[str] = None) -> None:
        self.conn = bb.connect(db_path)
        self.force_offline = force_offline
        self.backend = notion_mcp.get_backend(mirror_path)
        # Where to mirror live run state for the viewer (None = don't write).
        self.run_json_path = run_json_path
        # Wall-clock start per run id, so the viewer's counter is THIS run's
        # own elapsed seconds — an instrumented in-app counter, not a desk cam.
        self._started_at: dict[str, float] = {}
        lanes.bind_handlers({
            "intake":   self._intake,
            "extract":  self._extract,
            "dedupe":   self._dedupe,
            "file":     self._file,
            "schedule": self._schedule,
            "verify":   self._verify,
        })

    # ----- stage handlers: each returns (ok, executed, note, record_patch) ----- #

    def _intake(self, run: bb.Run) -> tuple[bool, bool, str, dict]:
        if not (run.transcript and run.transcript.strip()):
            return False, False, "empty transcript — nothing to intake", {}
        return True, False, f"call-ended payload received from {run.caller or 'unknown caller'}", {}

    def _extract(self, run: bb.Run) -> tuple[bool, bool, str, dict]:
        rec = extractor.extract_record(
            run.transcript or "", run.caller, force_offline=self.force_offline
        )
        return True, False, (
            f"extracted: {rec['customer_name'] or '?'} / {rec['service_type']} / "
            f"urgency={rec['urgency']}"
        ), rec

    def _dedupe(self, run: bb.Run) -> tuple[bool, bool, str, dict]:
        phone = run.record.get("phone", "")
        existing = self.backend.find_customer(phone) if phone else None
        patch = {"_dedupe": "returning" if existing else "new"}
        if existing:
            patch["_existing_id"] = existing.get("id", "")
        note = ("returning customer — will link, not duplicate"
                if existing else "new customer")
        return True, False, note, patch

    def _file(self, run: bb.Run) -> tuple[bool, bool, str, dict]:
        # IRREVERSIBLE: writes a row into the owner's live Notion workspace.
        filed = self.backend.create_job_record(run.record)
        return True, True, (
            f"filed Notion record {filed.get('id', '?')} (status New)"
        ), {"_filed_id": filed.get("id", ""), "_filed_url": filed.get("url", ""),
            "status": "New"}

    def _schedule(self, run: bb.Run) -> tuple[bool, bool, str, dict]:
        rid = run.record.get("_filed_id") or run.record.get("_existing_id")
        if not rid:
            return False, False, "no record id to schedule a follow-up on", {}
        when = notion_mcp.default_followup_date(run.record.get("urgency", "routine"))
        res = self.backend.schedule_followup(rid, when, note="auto follow-up so the lead never goes cold")
        if not res.get("ok"):
            return False, False, res.get("error", "follow-up failed"), {}
        return True, True, f"follow-up scheduled for {when}", {"followup": when}

    def _verify(self, run: bb.Run) -> tuple[bool, bool, str, dict]:
        rows = self.backend.query_jobs(status="New")
        rid = run.record.get("_filed_id") or run.record.get("_existing_id")
        found = any(r.get("id") == rid for r in rows)
        note = (f"verified: record is queryable ({len(rows)} New jobs in the DB)"
                if found else "WARNING: record not found on verify query")
        return found, False, note, {"_verified": found}

    # ----- engine ----- #

    def start_run(self, transcript: str, caller: Optional[str], autonomy: str,
                  source: str = "call") -> str:
        run_id = bb.create_run(self.conn, caller=caller, transcript=transcript, autonomy=autonomy)
        self._started_at[run_id] = time.time()
        # stamp provenance so the viewer/log shows which inbound source this was
        bb.update_run(self.conn, run_id, record={"_source": source})
        return run_id

    def start_run_from_payload(self, payload: "adapters.Payload", autonomy: str) -> str:
        """Start a run from a normalized Payload (call / form / email) — the one
        ingestion path every source funnels through."""
        return self.start_run(payload.transcript, payload.caller, autonomy, payload.source)

    # ----- live time-delta instrumentation (the in-app counter) ----- #

    def elapsed(self, run_id: str) -> float:
        """Wall-clock seconds this run has been working — what the viewer shows."""
        start = self._started_at.get(run_id)
        if start is None:
            run = bb.get_run(self.conn, run_id)
            start = run.created_at if run else time.time()
        return max(0.0, time.time() - start)

    def _write_run_json(self, run_id: str) -> None:
        """Snapshot live run state (incl. THIS run's elapsed seconds vs. the
        baseline) to the file the viewer polls. The on-screen 0:48-vs-6:51 delta
        is read straight from here — an instrumented counter, not a stopwatch."""
        if not self.run_json_path:
            return
        run = bb.get_run(self.conn, run_id)
        if not run:
            return
        hist = bb.stage_history(self.conn, run_id)
        reached = [h["stage"] for h in hist]
        notes = {h["stage"]: h["note"] for h in hist}
        clean = {k: v for k, v in run.record.items() if not k.startswith("_")}
        elapsed = self.elapsed(run_id)
        snapshot = {
            "run_id": run.id,
            "status": run.status,
            "stage": run.stage,
            "autonomy": run.autonomy,
            "source": run.record.get("_source", "call"),
            "reached": reached,
            "notes": notes,
            "gate": run.gate,
            "record": {
                "Customer": clean.get("customer_name", ""),
                "Phone": clean.get("phone", ""),
                "Service": clean.get("service_type", ""),
                "Urgency": clean.get("urgency", ""),
                "Address": clean.get("address", ""),
                "Summary": clean.get("job_summary", ""),
                "Quote": clean.get("quote_hint", ""),
                "Status": clean.get("status", ""),
                "Follow-up": clean.get("followup", ""),
            } if clean else None,
            # the instrumented time delta (seconds)
            "elapsed_seconds": round(elapsed, 1),
            "baseline_seconds": BASELINE_SECONDS,
            "saved_seconds": round(max(0.0, BASELINE_SECONDS - elapsed), 1),
        }
        path = Path(self.run_json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        tmp.replace(path)  # atomic so the polling viewer never reads a half file

    def _advance_one(self, run_id: str) -> str:
        """Process the run's current stage once. Returns an outcome token."""
        run = bb.get_run(self.conn, run_id)
        assert run is not None
        stage = lanes.stage_def(run.stage)
        if stage is None:
            bb.update_run(self.conn, run_id, status="failed", error=f"unknown stage {run.stage}")
            return "failed"

        if stage.gate == lanes.TERMINAL:
            ok, _, note, patch = stage.handler(run)  # verify is terminal but still runs
            rec = {**run.record, **patch}
            bb.log_stage(self.conn, run_id, run.stage, "terminal",
                         "done" if ok else "failed", note=note)
            bb.update_run(self.conn, run_id, record=rec,
                          status="done" if ok else "failed",
                          error=None if ok else note)
            return "done" if ok else "failed"

        approved = (run.gate or {}).get("approved") is True
        # GATE: irreversible + gated + not approved -> BLOCK (compute nothing irreversible).
        if stage.gate == lanes.IRREVERSIBLE and run.autonomy == "gated" and not approved:
            gate_req = {
                "stage": run.stage,
                "desc": stage.desc,
                "reason": "writes to the owner's live Notion workspace — needs approval",
                "preview": run.record,
                "approved": False,
            }
            bb.log_stage(self.conn, run_id, run.stage, "irreversible", "blocked",
                         note="awaiting approval before the live write")
            bb.update_run(self.conn, run_id, status="blocked", gate=gate_req)
            return "blocked"

        ok, executed, note, patch = stage.handler(run)
        rec = {**run.record, **patch}
        bb.log_stage(self.conn, run_id, run.stage, stage.gate,
                     "executed" if executed else "advanced", executed=executed, note=note)
        if not ok:
            bb.update_run(self.conn, run_id, record=rec, status="failed", error=note)
            return "failed"

        nxt = lanes.next_stage(run.stage)
        bb.update_run(self.conn, run_id, record=rec, stage=nxt, status="running", clear_gate=True)
        return "advanced"

    def drive(self, run_id: str) -> dict:
        """Run a run forward until it blocks, finishes, or fails."""
        steps = 0
        self._write_run_json(run_id)
        while steps < MAX_STEPS:
            run = bb.get_run(self.conn, run_id)
            if not run or run.status != "running":
                break
            outcome = self._advance_one(run_id)
            steps += 1
            self._write_run_json(run_id)
            if outcome in ("blocked", "done", "failed"):
                break
        run = bb.get_run(self.conn, run_id)
        return {"run_id": run_id, "status": run.status, "stage": run.stage,
                "steps": steps, "elapsed_seconds": round(self.elapsed(run_id), 1),
                "saved_seconds": round(max(0.0, BASELINE_SECONDS - self.elapsed(run_id)), 1)}

    def approve(self, run_id: str) -> dict:
        """Approve the blocked stage, run it live, then continue."""
        run = bb.get_run(self.conn, run_id)
        if not run:
            raise KeyError(run_id)
        if run.status != "blocked":
            return {"run_id": run_id, "status": run.status, "note": "not blocked — nothing to approve"}
        gate = dict(run.gate or {})
        gate["approved"] = True
        bb.update_run(self.conn, run_id, status="running", gate=gate)
        bb.log_stage(self.conn, run_id, run.stage, "irreversible", "approved",
                     note="approved by operator")
        self._write_run_json(run_id)
        return self.drive(run_id)


# --------------------------------------------------------------------------- #
# Pretty printing
# --------------------------------------------------------------------------- #

def _print_run(agent: Agent, run_id: str) -> None:
    run = bb.get_run(agent.conn, run_id)
    if not run:
        print(f"no run {run_id}")
        return
    print(f"\n  RUN {run.id}  [{run.status}]  stage={run.stage}  autonomy={run.autonomy}")
    print("  " + "-" * 64)
    for s in bb.stage_history(agent.conn, run_id):
        mark = {"executed": "*", "blocked": "!", "approved": "+",
                "done": "=", "failed": "x"}.get(s["outcome"], ">")
        print(f"  [{mark}] {s['stage']:<9} {s['gate']:<13} {s['note']}")
    if run.status == "blocked":
        print(f"\n  >> BLOCKED at '{run.stage}' — {(run.gate or {}).get('reason','')}")
        print(f"     approve with:  python -m switchboard.agent_loop --approve {run.id}")
    if run.record:
        clean = {k: v for k, v in run.record.items() if not k.startswith("_")}
        print("\n  FILED RECORD:")
        for k, v in clean.items():
            print(f"     {k:<14} {v}")
    print()


# --------------------------------------------------------------------------- #
# Demo state reset — kill the re-record landmine
# --------------------------------------------------------------------------- #

def reset_demo_state(db_path: str = DEFAULT_DB, mirror_path: str = DEFAULT_MIRROR,
                     run_json_path: str = DEFAULT_RUN_JSON) -> None:
    """Delete the persistent blackboard + Notion mirror + viewer snapshot so the
    next demo run starts from a guaranteed-clean slate.

    Without this, a second `--demo` run finds the first run's filed row and the
    dedupe stage falsely reports 'returning customer' on what should be a fresh
    take — a re-record landmine that breaks the on-camera 'new customer' beat.
    `--demo` calls this automatically (disable with --keep-demo-state).
    """
    for p in (db_path, mirror_path, run_json_path):
        try:
            Path(p).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="switchboard.agent_loop",
                                 description="Live call -> gated DAG -> Notion job record.")
    ap.add_argument("--demo", action="store_true",
                    help="Run the full loop offline on a sample transcript (no keys).")
    ap.add_argument("--transcript", help="Path to a call transcript text file.")
    ap.add_argument("--caller", help="Caller phone id, e.g. +17875551234.")
    ap.add_argument("--source", choices=["call", "form", "email"], default="call",
                    help="Which inbound adapter to use (call=transcript, form=JSON, email=.eml).")
    ap.add_argument("--form", metavar="PATH", help="Path to a form-submission JSON file (--source form).")
    ap.add_argument("--email", metavar="PATH", help="Path to a raw .eml file (--source email).")
    ap.add_argument("--autonomy", choices=["gated", "auto", "dryrun"], default="auto",
                    help="gated blocks the Notion write for approval; auto runs it.")
    ap.add_argument("--approve", metavar="RUN_ID", help="Approve a blocked run and continue.")
    ap.add_argument("--status", metavar="RUN_ID", help="Print a run and its DAG history.")
    ap.add_argument("--reset-demo", action="store_true",
                    help="Delete the persistent demo state (blackboard + mirror + run.json) and exit.")
    ap.add_argument("--keep-demo-state", action="store_true",
                    help="Do NOT reset demo state before a --demo run (off by default).")
    ap.add_argument("--db", default=DEFAULT_DB, help="Blackboard SQLite path.")
    ap.add_argument("--no-viewer", action="store_true",
                    help="Do not write the viewer/run.json live snapshot.")
    args = ap.parse_args(argv)

    # Use a single shared mirror file so --approve / --status across processes
    # see the same filed records as the run that created them.
    mirror = os.environ.get("SWITCHBOARD_MIRROR", DEFAULT_MIRROR)
    run_json = None if args.no_viewer else DEFAULT_RUN_JSON

    if args.reset_demo:
        reset_demo_state(args.db, mirror, DEFAULT_RUN_JSON)
        print(json.dumps({"reset": True, "db": args.db, "mirror": mirror,
                          "run_json": DEFAULT_RUN_JSON}, indent=2))
        return 0

    # --demo resets state first so the dedupe beat ('new customer') is deterministic
    # on every take — kills the persistent-mirror re-record landmine.
    if args.demo and not args.keep_demo_state:
        reset_demo_state(args.db, mirror, DEFAULT_RUN_JSON)

    agent = Agent(args.db, force_offline=args.demo, mirror_path=mirror,
                  run_json_path=run_json)

    if args.approve:
        res = agent.approve(args.approve)
        print(json.dumps(res, indent=2))
        _print_run(agent, args.approve)
        return 0

    if args.status:
        _print_run(agent, args.status)
        return 0

    # Build the ingestion Payload from whichever source was requested.
    if args.demo:
        payload = adapters.from_call(SAMPLE_CALLER, SAMPLE_TRANSCRIPT)
    elif args.source == "form":
        if not args.form:
            ap.error("--source form requires --form PATH"); return 2
        payload = adapters.from_form(Path(args.form).read_text(encoding="utf-8"))
    elif args.source == "email":
        if not args.email:
            ap.error("--source email requires --email PATH"); return 2
        payload = adapters.from_email(Path(args.email).read_text(encoding="utf-8"))
    elif args.transcript:
        payload = adapters.from_call(args.caller, Path(args.transcript).read_text(encoding="utf-8"))
    else:
        ap.error("provide --demo, --transcript PATH, --source form|email, or --approve/--status")
        return 2

    run_id = agent.start_run_from_payload(payload, args.autonomy)
    res = agent.drive(run_id)
    print(json.dumps(res, indent=2))
    _print_run(agent, run_id)
    return 0


def _main_console() -> int:
    """console_scripts entry point (reads sys.argv itself)."""
    return _main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
