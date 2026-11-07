import unittest
from types import SimpleNamespace
from typing import Any, cast

from palmsens.data_types import Measurement

from analysis.measurement_objectives import calculate_objective


class _Array:
    def __init__(self, values: list[float]) -> None:
        self._values = values

    def to_list(self) -> list[float]:
        return list(self._values)


def _measurement(*curves: tuple[str, list[float], list[float]]) -> Measurement:
    return cast(
        Any,
        SimpleNamespace(
            curves=tuple(
                SimpleNamespace(
                    title=title, x_array=_Array(x_values), y_array=_Array(y_values)
                )
                for title, x_values, y_values in curves
            )
        ),
    )


class MeasurementObjectiveTests(unittest.TestCase):
    def test_cv_preserves_each_scan_maximum_before_averaging(self) -> None:
        result = calculate_objective(
            _measurement(
                ("Scan 1", [0.0, 0.1], [1.0, 3.0]), ("Scan 2", [0.0, 0.1], [2.0, 6.0])
            ),
            measurement_type="cyclic_voltammetry",
        )

        self.assertEqual(result.value, 4.5)
        self.assertEqual(
            [
                (item.sequence_number, item.kind, item.value)
                for item in result.contributions
            ],
            [(1, "scan", 3.0), (2, "scan", 6.0)],
        )
        self.assertAlmostEqual(cast(float, result.standard_error), 1.5)

    def test_lsv_preserves_detected_sweep_maxima(self) -> None:
        result = calculate_objective(
            _measurement(
                ("LSV", [0.0, 0.5, 1.0, 0.0, 0.5, 1.0], [1.0, 2.0, 4.0, 2.0, 5.0, 3.0])
            ),
            measurement_type="linear_sweep_voltammetry",
            expected_lsv_sweep_count=2,
        )

        self.assertEqual(result.value, 4.5)
        self.assertEqual(
            [(item.kind, item.value) for item in result.contributions],
            [("sweep", 4.0), ("sweep", 5.0)],
        )


if __name__ == "__main__":
    unittest.main()
