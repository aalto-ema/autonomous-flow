import tempfile
import threading
import time
import unittest
from pathlib import Path

from controllers.experiment_controller import (
    ExperimentCleanupSettings,
    ManualExperimentPlanRequest,
)
from engine.service import ExperimentEngine
from palmsens.emstat4s import ChronoamperometrySettings


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = ExperimentEngine(Path(self.directory.name), simulate=True)
        self.engine.invoke("runze", "connect", kwargs={"port": "simulation"})
        self.engine.invoke("emstat", "connect")
        self.engine.invoke(
            "experiment",
            "set_source_line_state",
            kwargs={"selector_port": 7, "status": "air_filled"},
        )

    def tearDown(self):
        self.engine.close()
        self.directory.cleanup()

    def plans(self, count=1):
        protocol = self.engine.invoke(
            "experiment",
            "estimate_experiment_protocol",
            kwargs={
                "measurement_settings": ChronoamperometrySettings(0.1, 0.5, 1),
                "handling_rpm": 10,
                "approach_rpm": 8,
                "flow_rpm": None,
                "workflow_kind": "stopped_flow",
                "measurement_count": 1,
                "cleanup_settings": ExperimentCleanupSettings(1, 1, 1, 0.1),
            },
        )
        return self.engine.invoke(
            "experiment",
            "build_manual_experiment_plans",
            kwargs={
                "requests": tuple(
                    ManualExperimentPlanRequest({}, f"Test {i}") for i in range(count)
                ),
                "protocol": protocol,
            },
        )

    def test_queue_completes_without_ui_or_sdk(self):
        result = self.engine.invoke(
            "engine", "run_queue", kwargs={"plans": self.plans(2)}
        )
        self.assertEqual(result.manual_play_state, "inactive")
        self.assertEqual(result.workflow_phase, "ready")
        runs = self.engine.repository.list_saved_experiment_runs()
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(r.measurements[0].status == "completed" for r in runs))
        self.assertTrue(
            all(r.mixture_context["commanded_total_volume_ml"] > 0 for r in runs)
        )

    def test_pause_stops_before_next_stage_and_resume_completes(self):
        plans = self.plans(2)
        errors = []

        def run():
            try:
                self.engine.invoke("engine", "run_queue", kwargs={"plans": plans})
            except Exception as e:
                errors.append(e)

        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.monotonic() + 2
        while not self.engine.busy and time.monotonic() < deadline:
            time.sleep(0.002)
        self.engine.request_pause()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertEqual(self.engine.experiment.snapshot.manual_play_state, "paused")
        self.engine.invoke("engine", "run_queue", kwargs={"resume": True})
        self.assertEqual(len(self.engine.repository.list_saved_experiment_runs()), 2)


class ProcessEngineTests(unittest.TestCase):
    def test_exclusive_owner_and_reconnect(self):
        from engine.process import EngineClient

        with tempfile.TemporaryDirectory() as directory:
            client = EngineClient(directory, simulate=True)
            try:
                client.runze.connect(port="simulation")
                self.assertTrue(client.engine.snapshot().runze_connected)
                with self.assertRaisesRegex(RuntimeError, "already controls"):
                    EngineClient(directory, simulate=True)
                client.detach()
                time.sleep(0.3)
                again = EngineClient(directory, simulate=True)
                self.assertTrue(again.engine.snapshot().runze_connected)
                again.close()
                if client._process:
                    client._process.join(5)
                self.assertFalse(client._process.is_alive())
            finally:
                if not client._closed:
                    client.close()

    def test_duplicate_command_does_not_repeat_motion(self):
        from engine.process import EngineClient

        with tempfile.TemporaryDirectory() as directory:
            client = EngineClient(directory, simulate=True)
            try:
                kwargs = {"port": "simulation"}
                first = client.request(
                    "call", "runze", "connect", kwargs=kwargs, identifier="connect-once"
                ).result(5)
                second = client.request(
                    "call", "runze", "connect", kwargs=kwargs, identifier="connect-once"
                ).result(5)
                self.assertEqual(first, second)
                with self.assertRaisesRegex(RuntimeError, "different request"):
                    client.request(
                        "call",
                        "runze",
                        "connect",
                        kwargs={"port": "changed"},
                        identifier="connect-once",
                    ).result(5)
                self.assertEqual(
                    client.request(
                        "call",
                        "runze",
                        "connect",
                        kwargs=kwargs,
                        identifier="connect-once",
                    ).result(5),
                    first,
                )
            finally:
                client.close()


class JournalTests(unittest.TestCase):
    def test_restart_never_replays_interrupted_command(self):
        from engine.process import CommandJournal

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "journal.sqlite3"
            payload = ("runze", "dispense", (), {"volume_ml": 1})
            first = CommandJournal(path)
            self.assertIsNone(first.begin("motion", payload))
            after = CommandJournal(path)
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                after.begin("motion", payload)

    def test_reused_identifier_cannot_replace_original_result(self):
        from engine.process import CommandJournal

        with tempfile.TemporaryDirectory() as d:
            journal = CommandJournal(Path(d) / "journal.sqlite3")
            journal.begin("one", ("original",))
            journal.finish("one", ("result", 1))
            with self.assertRaisesRegex(RuntimeError, "different request"):
                journal.begin("one", ("changed",))
            self.assertEqual(journal.begin("one", ("original",)), ("result", 1))


class FailureRecoveryTests(EngineTests):
    def test_partial_transfer_failure_blocks_new_queue(self):
        plans = self.plans()
        self.engine.invoke("experiment", "prime_required_sources", kwargs={"rpm": 10})
        self.engine.runze._syringe.fail_next = True
        with self.assertRaisesRegex(RuntimeError, "partial movement"):
            self.engine.invoke("engine", "run_queue", kwargs={"plans": plans})
        self.assertEqual(
            self.engine.experiment.snapshot.workflow_phase, "recovery_required"
        )
        with self.assertRaisesRegex(RuntimeError, "complete recovery"):
            self.engine.invoke("engine", "run_queue", kwargs={"plans": plans})

    def test_three_lsv_sweeps_use_production_curve_extraction(self):
        from palmsens.emstat4s import LinearSweepVoltammetrySettings

        self.engine.invoke(
            "emstat",
            "measure_linear_sweep_voltammetry",
            kwargs={
                "settings": LinearSweepVoltammetrySettings(
                    begin_potential_v=0,
                    end_potential_v=1,
                    step_potential_v=0.01,
                    scan_rate_v_per_s=0.1,
                    sweep_count=3,
                )
            },
        )
        sessions = self.engine.emstat.list_saved_measurements()
        measurement = self.engine.emstat.load_saved_measurement(
            file_name=sessions[0].file_name
        )
        self.assertEqual(len(measurement.series), 3)


class UiExitTests(unittest.TestCase):
    def test_detached_ui_process_exits_and_engine_can_reconnect(self):
        import subprocess
        import sys

        from engine.process import EngineClient

        with tempfile.TemporaryDirectory() as d:
            code = "from engine.process import EngineClient;import sys;c=EngineClient(sys.argv[1],simulate=True);c.runze.connect(port='simulation');c.detach()"
            result = subprocess.run(
                [sys.executable, "-c", code, d],
                timeout=8,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            client = EngineClient(d, simulate=True, start=False)
            try:
                self.assertTrue(client.engine.snapshot().runze_connected)
            finally:
                client.close()


class ModeSeparationTests(unittest.TestCase):
    def test_simulation_folder_cannot_be_reopened_as_hardware(self):
        with tempfile.TemporaryDirectory() as d:
            engine = ExperimentEngine(Path(d), simulate=True)
            engine.close()
            with self.assertRaisesRegex(RuntimeError, "different instrument mode"):
                ExperimentEngine(Path(d), simulate=False)

    def test_device_lease_prevents_another_owner(self):
        from common.device_lease import DeviceLease
        from uuid import uuid4

        identity = uuid4().hex
        lease = DeviceLease(identity)
        try:
            with self.assertRaisesRegex(RuntimeError, "Another engine owns"):
                DeviceLease(identity)
        finally:
            lease.close()
        again = DeviceLease(identity)
        again.close()
