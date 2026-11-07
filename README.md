# Autonomous Flow

A PySide6 desktop workspace for Runze fluidics and EmStat electrochemical experiments. The experiment engine runs independently of the UI; the PalmSens SDK runs in a separate worker.

## Install and launch

Use Python 3.12 or newer. In VS Code, select your project `.venv` with **Python: Select Interpreter**, then use its integrated terminal:

```sh
python -m pip install -e .
python app.py --simulate
```

Simulation uses a separate `simulation/` data folder and generated curves. To control instruments:

```sh
python app.py
```

The equivalent installed command is `autonomous-flow`. A regular wheel installation uses `~/.autonomous-flow/`; a source checkout uses the project directory. Choose another workspace with `--data-dir /path/to/workspace`. Back up the production `data/` and `measurements/` folders before deploying changes to an active laboratory installation.

Real EmStat operations require the .NET runtime matching the installed PyPalmSens SDK; the currently pinned PyPalmSens 1.10 package requests .NET 9 on macOS and Linux. A virtual environment does not install .NET. The loader detects `/usr/local/share/dotnet` on macOS; for another location set `DOTNET_ROOT`. Python and the runtime must have matching architecture. Missing SDK/runtime errors appear when you request a real instrument operation; simulation and desktop startup do not load the SDK.

## First experiment

1. Open **Instruments and sources**, choose a serial device path, and connect the pump and EmStat. Initialize hardware if its status requires it.
2. Record your prepared stock solutions, assign bottles to selector ports, and save the source assignments. Confirm tube contents after inspecting the real apparatus.
3. Open **Plan experiments**. Choose CA, CV, or LSV and stopped or continuous flow. Add experiment rows and enter stock fractions from 0 to 1; water balances the recipe.
4. Review the delivery plan, including requested volume, rounded nominal volume, syringe steps, and stroke count. Start the reviewed queue.
5. Monitor live curves and the icon-based protocol timeline. Each step shows its liquid, route and status; brief steps remain in the history. The reviewed recipe has a graphical overview, and Help includes the full icon key. **Pause after this stage** preserves a resumable queue. **Stop instruments** requests an immediate abort and may require recovery.
6. Inspect saved results, objective metrics and raw curves. Reuse a measurement protocol or export a loaded curve as CSV. Follow ordered source maintenance when needed.

Closing the UI during an operation requests a stage-boundary pause and detaches. Launch again with the same mode and data directory to reconnect. An engine crash or restart does not silently continue physical operations; inspect the restored checkpoint and recover. Only one UI controls a workspace, and physical device leases prevent separate engines from sharing the same serial path or default PalmSens instrument.

## Legacy browser UI

```sh
python -m pip install -e '.[web]'
python app.py --ui nicegui --simulate
```

NiceGUI remains available for transition and sends workflow commands through the same engine. The Qt UI is the default. NiceGUI is an optional dependency.

## Development checks

```sh
python -m pip install -e '.[dev,web]'
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -q
pyright --pythonpath .venv/bin/python engine adapters ui app.py
```

Tests include an isolated simulated engine, Qt planning/run/results, pause/resume, exclusive ownership, reconnect, command deduplication, interrupted motion, and step rounding. They use local loopback IPC, not external services. The test environment requires the optional browser dependency because existing legacy-panel tests import NiceGUI.

See [migration and architecture](docs/migration/README.md) and [physical validation](docs/migration/hardware-validation.md). Software checks do not establish physical delivery accuracy.
