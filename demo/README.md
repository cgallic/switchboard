# Demo assets

These are real artifacts captured from the running agent in this repo — not mockups.

| File | What it is | How it was produced |
|---|---|---|
| `switchboard-demo.mp4` | Screen capture of the live run-log viewer animating through one full run, then the same run **gated** — `file` stops on its approval gate, then approve finishes the loop. | The exact `viewer/index.html` in this repo, fed the real `run.json` frames the agent writes. |
| `shot-1-auto-done.png` | The fully-autonomous loop complete: every stage green, the Maria Delgado record filed, follow-up dated `2026-06-19`. | `python -m switchboard.agent_loop --demo` -> viewer. |
| `shot-2-gated-blocked.png` | The **gated** run **blocked** at the irreversible `file` write — Status / Follow-up still empty, the orange gate card up. | `--demo --autonomy gated` -> viewer. |
| `shot-3-approved.png` | After `--approve`: the gate clears and the record lands. | `--approve <run_id>` -> viewer. |
| `sample-run-auto-done.json` | The actual `run.json` snapshot behind shot 1 (instrumented `elapsed_seconds` / `baseline_seconds=411`). | Emitted by the agent on its last tick. |
| `sample-run-gated-blocked.json` | The actual `run.json` snapshot behind shot 2, including the `gate` block. | Emitted by the agent when it blocks. |

Reproduce any of them yourself:

```bash
python -m switchboard.agent_loop --demo                  # writes viewer/run.json
# then serve the folder over http and open viewer/index.html — you'll see the
# same frames these screenshots and the video were captured from.
```

The on-screen time delta in the viewer (`6:51 by hand -> 0 typing`) is read straight
from the agent's own `run.json` by `renderClock` — an instrumented in-app counter,
not an overlay. Offline the agent finishes in well under a second, so the typing
cost it returns is `0`; the `6:51` baseline is the hand-timed median it is measured
against.
