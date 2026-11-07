# Desktop migration and engine boundaries

The migration was implemented in sequence: runtime isolation; backend corrections; UI-independent workflow engine and simulator; process ownership and reconnect; NiceGUI adaptation; PySide6 UI; packaging and operating documentation; regression and physical-validation support.

| Component | Responsibility |
|---|---|
| `app.py` | CLI, frontend selection, simulation/data directory selection |
| `ui/qt/` | Guided screens, background requests, plots, result browsing, user input |
| `gui/legacy.py` | Optional NiceGUI frontend using engine proxies |
| `engine/service.py` | Command admission, queue stage execution, minimum stroke checks, events |
| `engine/process.py` | Independent engine process, exclusive UI lease, authenticated local IPC, command journal |
| `engine/ports.py` | Instrument interface without vendor runtime initialization |
| `controllers/` | Existing experiment planning, checkpoint/recovery rules, instrument state and curve processing |
| `adapters/` | Deterministic simulated instruments, isolated PalmSens SDK worker, portable session loading |
| `fluidics/`, `runze/` | Physical routing and device commands |
| `storage/` | SQLite experiment records, source identity and checkpoints |

The UI sends commands; it never advances the experiment stages itself. The engine owns the prime → prepare → measure → clean loop. All ordinary commands share admission; status, pause and abort can proceed during a workflow. Existing controller validation remains authoritative. The UI provides explanations and blocks obvious unavailable actions, but an engine rejection still protects prerequisites.

The independently launched engine survives UI detach. One controlling UI is admitted per workspace. The journal records IDs before commands execute: a duplicate completed request returns the cached response during the same engine lifetime; after restart, a previous command is rejected rather than replayed. Interrupted commands require inspection. Authentication protects the loopback transport from clients without the workspace token; it is a local trusted-process interface, not an internet API. Losing a UI requests a stage-boundary pause, not a guaranteed physical emergency stop.

The PalmSens worker isolates pythonnet/.NET from both the UI and engine. SDK commands have bounded waits, measurement callbacks cross IPC as Python data, and normal completed sessions retain native vendor bytes. A timeout marks the instrument faulted. Inspect the device before restarting its worker; restarting software cannot prove that the instrument stopped physically. Live plotting renders fewer points for large series while stored data and CSV export retain all values.

## Corrected behavior

- Hardware status indicating motion or a fault blocks a new operation; status refresh remains available.
- Preview and execution use the same conservative common-line stroke limit, including clearance.
- Transfers are partitioned into balanced integer strokes, retaining total commanded steps without a tiny final remainder.
- A one-step request cannot be confirmed by zero displacement. Larger movements allow at most the existing one-step position tolerance.
- A measurement-start timeout no longer waits indefinitely for an unresponsive measurement thread or abort call.
- Requested recipe fractions remain historical recipe data. New `commanded_volume_ml` records use the summed step-rounded transfers. Mixture context stores commanded totals and fractions, with an explicit statement that neither constitutes measured delivery. Historical rows are not rewritten.
- Raw-session save failures are reported as failures rather than successful saved measurements.
- Physical experiment execution is blocked while the checkpoint requires recovery.
- Imports no longer initialize the PalmSens SDK; macOS default .NET path detection addresses the earlier missing `DOTNET_ROOT` startup failure.

The current schema remains version 1; the ERD was regenerated from it. No production experiment records were migrated or overwritten. Simulation has a distinct default folder and clearly marked generated session contents. A persisted workspace mode prevents reopening simulation data as hardware data or injecting simulation into an existing unmarked experiment database.

## Configurable minimum stroke

The default minimum individual liquid stroke is **0.05 mL**, a provisional safeguard. To change the limit after physical characterization, create `instrument-profile.json` in the chosen data directory:

```json
{"minimum_stroke_ml": 0.05}
```

This is not a calibration model. Syringe conversion, routing dead volumes, clearance, reserve volume and fluid properties still require verification on the actual apparatus. Plumbing constants remain in `config/runze_setup.py`.

## Scope and limitations

Manual queues, CA/CV/LSV, stopped/continuous flow, repeated stopped-flow measurements, source stocks, plotting, persistence, objectives, maintenance and manual controls are available in Qt. Bayesian proposal selection was not added by the UI migration.

No physical instrument was moved during implementation. The simulator cannot establish valve sealing, bubble transport, wetting, dead volume, mass delivery, response to cable removal, or effective hardware stopping. Native installers, signing and platform-specific deployment require separate release work; an installable Python wheel is provided by the package build configuration.
