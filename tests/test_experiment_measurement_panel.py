import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from gui.emstat_plot_panel import EmStatPlotPanelState
from gui.experiment_measurement_panel import ExperimentMeasurementPanel


class ExperimentMeasurementPanelTests(unittest.TestCase):
    def test_live_measurement_selector_advances_with_controller_progress(self) -> None:
        emstat = MagicMock()
        emstat.snapshot = SimpleNamespace(
            measurement_type=None, state="idle", error_message=None
        )
        emstat.data_since.return_value = SimpleNamespace(
            measurement_id=0, data_revision=0, series=()
        )
        emstat.list_saved_measurements.return_value = ()

        controller = MagicMock()
        controller.busy = True
        controller.emstat_controller = emstat
        controller.list_saved_experiment_runs.return_value = ()
        controller.snapshot = self._snapshot(sequence_number=1)

        panel = ExperimentMeasurementPanel(
            controller,
            state=EmStatPlotPanelState(),
            on_busy_change=lambda _: None,
            on_view_results=lambda _run, _measurement: None,
            on_view_protocol=lambda _: None,
            on_use_protocol=lambda: None,
            on_discard_protocol=lambda: None,
        )
        panel.refresh()

        self.assertEqual(panel.measurement_select.value, 1)
        self.assertNotIn("hidden", panel.measurement_select._classes)

        controller.snapshot = self._snapshot(sequence_number=2)
        panel.refresh()

        self.assertEqual(panel.measurement_select.value, 2)
        self.assertEqual(
            panel.measurement_select.options[1], "1. Measurement — Completed"
        )
        self.assertEqual(panel.measurement_select.options[2], "2. Measurement — Live")
        self.assertEqual(
            panel.measurement_select.options[3], "3. Measurement — Pending"
        )

    @staticmethod
    def _snapshot(*, sequence_number: int) -> SimpleNamespace:
        return SimpleNamespace(
            mixture_id=42,
            measurement_sequence_number=sequence_number,
            measurement_count=3,
            state="measuring",
        )


if __name__ == "__main__":
    unittest.main()
