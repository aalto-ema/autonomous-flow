"""Authoritative command admission and workflow execution, independent of a UI."""

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from adapters.session_repository import PortableSessionRepository
from config.runze_setup import RUNZE_PLUMBING
from controllers.emstat_controller import EmStatController
from controllers.experiment_controller import ExperimentController
from controllers.runze_controller import RunzeController
from engine.protocol_visual import ProtocolTracker
from storage.experiment_repository import ExperimentRepository, SourceConfigurationEntry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EngineSnapshot:
    simulation: bool
    busy: bool
    operation: str | None
    runze_connected: bool
    emstat_connected: bool
    experiment: object
    runze: object
    measurement: object
    error: str | None
    available_actions: tuple[str, ...]
    protocol_steps: tuple = ()


class ExperimentEngine:
    def __init__(self, root: Path, *, simulate=False, minimum_stroke_ml=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        mode_file = self.root / "workspace-mode.json"
        if mode_file.exists():
            if bool(json.loads(mode_file.read_text())["simulation"]) != bool(simulate):
                raise RuntimeError(
                    "This data folder belongs to a different instrument mode. Choose a separate simulation or hardware folder."
                )
        else:
            if simulate and (self.root / "data" / "autonomous_flow.sqlite3").exists():
                raise RuntimeError(
                    "Simulation cannot use an existing unmarked experiment database. Choose a new simulation folder."
                )
            mode_file.write_text(json.dumps({"simulation": bool(simulate)}))
        self.simulation = simulate
        from config.instrument_profile import minimum_stroke_ml as configured_minimum

        self.minimum_stroke_ml = (
            configured_minimum(self.root)
            if minimum_stroke_ml is None
            else minimum_stroke_ml
        )
        self._device_leases = {}
        self._admission = threading.Lock()
        self._operation = None
        self._error = None
        self._pause = threading.Event()
        self._abort = threading.Event()
        self._events = []
        self._event_lock = threading.Lock()
        self.repository = ExperimentRepository(
            self.root / "data" / "autonomous_flow.sqlite3"
        )
        if not self.repository.load_current_source_configuration().entries:
            self.repository.replace_current_source_configuration(
                entries=tuple(
                    SourceConfigurationEntry(p.number, p.key, None, p.key == "water")
                    for p in RUNZE_PLUMBING.liquid_sources
                ),
                expected_revision=0,
            )
        if simulate:
            from adapters.simulated import SimulatedEmStat, SimulatedRunzeController

            self.runze = SimulatedRunzeController(plumbing=RUNZE_PLUMBING)
            instrument = SimulatedEmStat()
        else:
            from adapters.palmsens_worker import PalmSensWorker

            self.runze = RunzeController(plumbing=RUNZE_PLUMBING)
            instrument = PalmSensWorker()
        self.protocol_tracker = ProtocolTracker()
        self.instrument = instrument
        self.emstat = EmStatController(
            instrument,
            session_repository=PortableSessionRepository(
                self.root / "measurements", instrument
            ),
        )
        self.experiment = ExperimentController(
            runze_controller=self.runze,
            emstat_controller=self.emstat,
            repository=self.repository,
        )
        self._refresh_protocol_sources()
        self.runze.set_step_observer(self.protocol_tracker.observe)
        self.emstat.set_step_observer(self.protocol_tracker.observe)
        self.targets = {
            "runze": self.runze,
            "emstat": self.emstat,
            "experiment": self.experiment,
            "repository": self.repository,
            "engine": self,
        }

    def _refresh_protocol_sources(self):
        stocks = {stock.id: stock for stock in self.repository.list_source_stocks()}
        entries = {
            e.selector_port: e for e in self.experiment.source_configuration.entries
        }
        routes = {}
        for route in RUNZE_PLUMBING.selector_ports:
            entry = entries.get(route.number)
            stock = (
                stocks.get(entry.source_stock_id)
                if entry and entry.source_stock_id is not None
                else None
            )
            routes[route.label] = (
                (stock.label + (f" • {stock.batch_name}" if stock.batch_name else ""))
                if stock
                else "Mixture"
                if route.key == "mixing_vessel"
                else route.label
            )
        with self.protocol_tracker.lock:
            self.protocol_tracker.routes = routes

    @property
    def busy(self):
        return (
            self._admission.locked()
            or self.runze.busy
            or self.emstat.busy
            or self.experiment.busy
        )

    def publish(self, kind, message):
        with self._event_lock:
            self._events.append((kind, str(message)))
            self._events = self._events[-500:]
        logger.info("%s: %s", kind, message)

    def events(self):
        with self._event_lock:
            return tuple(self._events)

    def snapshot(self):
        e = self.experiment.snapshot
        actions = (
            ["refresh", "abort", "pause"]
            if self.busy
            else ["connect", "manual", "configure", "plan", "results"]
        )
        if not self.busy and self.runze.connected:
            if e.workflow_phase == "recovery_required":
                actions += ["recover"]
            else:
                actions += [
                    "prime",
                    "prepare",
                    "measure",
                    "clean",
                    "maintenance",
                    "run_queue",
                ]
        return EngineSnapshot(
            self.simulation,
            self.busy,
            self._operation,
            self.runze.connected,
            self.emstat.connected,
            e,
            self.runze.last_snapshot,
            self.emstat.snapshot,
            self._error,
            tuple(actions),
            self.protocol_tracker.snapshot(),
        )

    def invoke(self, target, name, args=(), kwargs=None):
        if target not in self.targets or name.startswith("_"):
            raise ValueError("Unknown command")
        obj = self.targets[target]
        attr = getattr(obj, name)
        if not callable(attr):
            return attr
        read = name.startswith(
            (
                "list_",
                "load_",
                "current_",
                "available_",
                "objective_",
                "source_stock",
                "source_setup",
                "water_substance",
                "settings_fingerprint",
                "syringe_",
            )
        ) or name in {
            "snapshot",
            "events",
            "data_since",
            "source_line_state_editable",
            "source_fraction_for_concentration",
        }
        priority = name in {
            "request_manual_play_pause",
            "cancel_manual_play_pause",
            "request_sequence_stop",
            "cancel_sequence_stop",
            "abort_measurement",
            "emergency_stop",
            "emergency_stop_active",
            "request_pause",
            "abort",
        }
        if read or priority:
            return attr(*args, **(kwargs or {}))
        if not self._admission.acquire(blocking=False):
            raise RuntimeError("Another operation is running; pause or stop it first")
        self._operation = name.replace("_", " ")
        self._error = None
        self.publish("started", self._operation)
        try:
            if self.emstat.busy and target != "emstat" and name != "recover_instrument":
                raise RuntimeError(
                    "Measurement is still active or stopping; wait for recovery"
                )
            if self.experiment.snapshot.workflow_phase == "recovery_required" and (
                name == "run_queue"
                or target == "experiment"
                and name
                in {
                    "prime_required_sources",
                    "prepare_mixture",
                    "run_experiment",
                    "run_flow_cell_measurement",
                    "start_manual_play",
                }
            ):
                raise RuntimeError(
                    "Inspect and complete recovery before starting another experiment"
                )
            if target == "experiment" and name in {
                "prepare_mixture",
                "run_experiment",
                "start_manual_play",
                "build_experiment_plan",
                "build_manual_experiment_plans",
            }:
                if name in {"prepare_mixture", "run_experiment"}:
                    self.validate_transfer_plan((kwargs or {})["plan"])
            direct_visual = target == "runze" and name in {
                "aspirate",
                "dispense",
                "empty_syringe",
                "move_selector_to_port",
                "reset_syringe",
                "reset_selector_to_home",
            }
            visual_name = name.replace("_", " ") + " " + str(kwargs or {})
            if direct_visual:
                self.protocol_tracker.observe(visual_name, "runze", "started")
            lease = None
            if (
                not self.simulation
                and target in {"runze", "emstat"}
                and name == "connect"
                and target not in self._device_leases
            ):
                from common.device_lease import DeviceLease

                identity = (
                    "palmsens-default-instrument"
                    if target == "emstat"
                    else "serial:" + str(Path((kwargs or {})["port"]).resolve())
                )
                lease = DeviceLease(identity)
            try:
                result = attr(*args, **(kwargs or {}))
            except BaseException:
                if lease:
                    lease.close()
                if direct_visual:
                    self.protocol_tracker.observe(visual_name, "runze", "failed")
                raise
            if direct_visual:
                self.protocol_tracker.observe(visual_name, "runze", "completed")
            if lease:
                self._device_leases[target] = lease
            if name == "disconnect" and target in self._device_leases:
                self._device_leases.pop(target).close()
            if name in {"build_experiment_plan", "build_manual_experiment_plans"}:
                for plan in result if isinstance(result, tuple) else (result,):
                    self.validate_transfer_plan(plan)
            if name in {"replace_source_configuration", "create_source_stock"}:
                self._refresh_protocol_sources()
            self.publish("completed", self._operation)
            return result
        except Exception as error:
            self._error = str(error)
            self.publish("failed", error)
            raise
        finally:
            self._operation = None
            self._admission.release()

    def validate_transfer_plan(self, plan):
        for transfer in plan.mixture_transfers:
            minimum_steps = transfer.syringe_steps // transfer.liquid_stroke_count
            minimum_ml = minimum_steps * self.runze.syringe_capacity_ml / 6000
            if minimum_ml + 1e-12 < self.minimum_stroke_ml:
                raise ValueError(
                    f"{transfer.source_config_key}: an individual stroke is {minimum_ml:g} mL, below the configured {self.minimum_stroke_ml:g} mL minimum. Use a more dilute stock or a larger recipe."
                )

    def request_pause(self):
        self._pause.set()
        self.experiment.request_manual_play_pause()
        self.publish("pause", "Pause requested at the next experiment-stage boundary")
        return True

    def abort(self):
        self._abort.set()
        self._pause.set()
        result = self.experiment.emergency_stop()
        if self.runze.busy and self.runze.active_target:
            self.runze.emergency_stop_active()
        if self.emstat.abort_available:
            self.emstat.abort_measurement()
        self.publish(
            "abort",
            "Stop requested; verify instrument and liquid state before continuing",
        )
        return result

    def run_queue(self, *, plans=None, resume=False):
        self._pause.clear()
        self._abort.clear()
        if resume:
            if not self.experiment.resume_manual_play():
                raise RuntimeError("No paused queue can be resumed")
        else:
            if not plans:
                raise ValueError("Review a nonempty experiment queue first")
            for plan in plans:
                self.validate_transfer_plan(plan)
            self.experiment.start_manual_play(plans=plans)
        while not self._abort.is_set():
            if self._pause.is_set():
                self.experiment.request_manual_play_pause()
            stage = self.experiment.manual_play_next_stage()
            if stage is None:
                break
            plan = self.experiment.active_manual_experiment_plan
            if plan is None:
                raise RuntimeError("No active experiment plan")
            prepared = self.experiment.prepared_mixture
            self.protocol_tracker.stage = stage.capitalize()
            self.publish(
                "stage",
                f"Experiment {self.experiment.snapshot.manual_play_plan_index}: {stage}",
            )
            try:
                if stage == "prime":
                    self.experiment.prime_required_sources(rpm=plan.handling_rpm)
                elif stage == "prepare":
                    self.experiment.prepare_mixture(plan=plan)
                elif stage == "measure":
                    if prepared is None:
                        raise RuntimeError("No prepared mixture")
                    self.experiment.run_flow_cell_measurement(prepared_mixture=prepared)
                elif stage == "clean":
                    if prepared is None:
                        raise RuntimeError("No prepared mixture")
                    self.experiment.clean_after_experiment(prepared_mixture=prepared)
                else:
                    raise ValueError("Unknown stage")
                self.experiment.finish_manual_play_stage(stage)
            except BaseException:
                self.experiment.deactivate_manual_play_after_failure()
                raise
        return self.experiment.snapshot

    def recover_instrument(self, *, verified_stopped=False):
        if not verified_stopped:
            raise ValueError(
                "Inspect and confirm the measurement instrument is stopped first"
            )
        if self.runze.busy or self.experiment.busy:
            raise RuntimeError("Wait for fluidics and workflow operations to stop")
        if not hasattr(self.instrument, "recover"):
            return False
        getattr(self.instrument, "recover")()
        return True

    def close(self):
        if self.runze.busy or self.emstat.busy or self.experiment.busy:
            raise RuntimeError(
                "Pause or stop the active operation before closing the engine"
            )
        if self.emstat.connected:
            self.emstat.disconnect()
        if self.runze.connected:
            self.runze.disconnect()
        if hasattr(self.instrument, "close"):
            getattr(self.instrument, "close")()
        for lease in self._device_leases.values():
            lease.close()
        self._device_leases.clear()
