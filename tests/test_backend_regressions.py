import threading
import time
import unittest
from types import SimpleNamespace

from config.runze_setup import RUNZE_PLUMBING
from controllers.experiment_controller import _MeasurementFlowSession
from controllers.runze_controller import RunzeController
from fluidics.runze_fluidics import (
    RunzeFluidics,
    build_mixture_transfer_diagnostics,
    partition_stroke_steps,
)
from runze.device import RunzeStatus
from runze.sy01b import SY01B, SY01BStatus


class BackendRegressionTests(unittest.TestCase):
    def test_preview_matches_execution_at_stroke_boundary(self):
        syringe = object.__new__(SY01B)
        syringe.syringe_volume_ml = 5.0
        fluidics = RunzeFluidics(
            syringe=syringe,
            selector=None,
            plumbing=RUNZE_PLUMBING,
            run_step=lambda **kw: None,
        )
        for volume in (0.5666666666666667, 0.6, 1.1325, 5.01):
            preview = build_mixture_transfer_diagnostics(
                plumbing=RUNZE_PLUMBING,
                syringe_volume_ml=5.0,
                source_fractions={"water": 1.0},
                total_volume_ml=volume,
                water_distribution="single",
                use_common_line_strokes=True,
            )[0]
            execution = fluidics._build_common_line_stroke_transfer_plan(
                source_port=7, destination_port=5, volume_ml=volume
            )
            strokes = partition_stroke_steps(
                execution.liquid_steps, execution.maximum_stroke_steps
            )
            self.assertEqual(preview.liquid_stroke_count, len(strokes))
            self.assertEqual(sum(strokes), preview.syringe_steps)
            self.assertLessEqual(max(strokes) - min(strokes), 1)

    def test_zero_displacement_does_not_verify_one_step(self):
        syringe = object.__new__(SY01B)
        syringe.wait_until_syringe_idle = lambda: None
        syringe.query_syringe_position = lambda: 0
        with self.assertRaises(RuntimeError):
            syringe._confirm_syringe_position(1)

    def test_busy_hardware_blocks_new_motion(self):
        controller = RunzeController()
        controller._bus = SimpleNamespace(connected=True)
        controller._syringe = SimpleNamespace(
            read_status=lambda: SY01BStatus(
                RunzeStatus.MOTOR_BUSY, RunzeStatus.OK, 1, 100, 100 / 1200
            )
        )
        controller._selector = SimpleNamespace(
            read_status=lambda: SimpleNamespace(
                current_port=5, motor_status=RunzeStatus.OK
            )
        )
        controller._syringe_state = "ready"
        controller._read_status(controller._syringe, controller._selector)
        with self.assertRaisesRegex(RuntimeError, "moving or faulted"):
            controller._begin_operation(
                operation="new move", target="sv07m", requires_selector_ready=True
            )
        self.assertFalse(controller.busy)

    def test_start_timeout_does_not_wait_for_stuck_measure_or_abort(self):
        release = threading.Event()
        controller = SimpleNamespace(
            busy=False, abort_measurement=lambda: release.wait(2)
        )
        session = _MeasurementFlowSession(
            controller=controller,
            action=lambda callback: release.wait(2),
            command_start_timeout_s=0.01,
        )
        started = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                session.start()
            self.assertLess(time.monotonic() - started, 0.3)
        finally:
            release.set()

    def test_backend_import_does_not_start_sdk(self):
        import sys

        self.assertNotIn("pypalmsens", sys.modules)


class StorageFailureTests(unittest.TestCase):
    def test_raw_session_save_error_is_not_reported_as_success(self):
        from controllers.emstat_controller import EmStatController

        controller = object.__new__(EmStatController)

        def fail(**kwargs):
            raise OSError("Disk full")

        controller._session_repository = SimpleNamespace(
            save=fail, measurements_directory="test"
        )
        with (
            self.assertLogs("controllers.emstat_controller", level="ERROR"),
            self.assertRaisesRegex(RuntimeError, "could not be saved"),
        ):
            controller._save_session(
                measurement_type="chronoamperometry",
                measurement_name=None,
                measurement=None,
                session_directory=None,
                session_sequence_number=None,
            )
