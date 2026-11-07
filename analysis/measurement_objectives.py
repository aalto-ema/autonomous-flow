from __future__ import annotations

import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from palmsens.data_types import Measurement

from palmsens.lsv_sweep_series import (
    LsvCurveData,
    extract_mixed_mode_lsv_sweeps,
    split_lsv_values,
)

ObjectiveName = str
AnalyzedMeasurementType = Literal[
    "chronoamperometry", "cyclic_voltammetry", "linear_sweep_voltammetry"
]


@dataclass(frozen=True, slots=True)
class ObjectiveDefinition:
    name: ObjectiveName
    label: str
    component_label: str
    unit: str
    analysis_method: str
    analysis_version: str
    supported_measurement_types: tuple[AnalyzedMeasurementType, ...]
    component_calculator: Callable[[tuple[float, ...]], float]


@dataclass(frozen=True, slots=True)
class ObjectiveContribution:
    """One objective value from a logical curve, scan, or sweep."""

    sequence_number: int
    kind: str
    value: float


@dataclass(frozen=True, slots=True)
class ObjectiveResult:
    definition: ObjectiveDefinition
    value: float
    standard_error: float | None
    contributions: tuple[ObjectiveContribution, ...]
    point_count: int

    @property
    def curve_values(self) -> tuple[float, ...]:
        """Compatibility view of the ordered logical-series values."""

        return tuple(item.value for item in self.contributions)


DEFAULT_OBJECTIVE: ObjectiveName = "mean_scan_max_current"


def _maximum_current(current_values: tuple[float, ...]) -> float:
    return max(current_values)


OBJECTIVE_DEFINITIONS: dict[ObjectiveName, ObjectiveDefinition] = {
    "mean_scan_max_current": ObjectiveDefinition(
        name="mean_scan_max_current",
        label="Mean scan maximum current",
        component_label="Maximum current",
        unit="µA",
        analysis_method="logical_series_maxima",
        analysis_version="1.2.0",
        supported_measurement_types=(
            "chronoamperometry",
            "cyclic_voltammetry",
            "linear_sweep_voltammetry",
        ),
        component_calculator=_maximum_current,
    )
}


def calculate_objective(
    measurement: Measurement,
    *,
    measurement_type: AnalyzedMeasurementType,
    objective_name: ObjectiveName = DEFAULT_OBJECTIVE,
    expected_lsv_sweep_count: int | None = None,
) -> ObjectiveResult:
    """Calculate one versioned objective directly from PalmSens curve data.

    The prototype objective takes the maximum raw current from each logical
    series and averages those maxima. CV curves are scans. Mixed Mode LSV
    stores potential-vs-time and current-vs-time as separate channels; they are
    paired by timestamp and then converted into logical sweeps.
    """

    if measurement_type not in (
        "chronoamperometry",
        "cyclic_voltammetry",
        "linear_sweep_voltammetry",
    ):
        raise ValueError(f"Unsupported measurement type: {measurement_type}")

    try:
        definition = OBJECTIVE_DEFINITIONS[objective_name]
    except KeyError as error:
        raise ValueError(f"Unsupported objective: {objective_name}") from error

    curve_maxima: list[float] = []
    point_count = 0

    curve_data = tuple(
        LsvCurveData(
            title=str(curve.title),
            x_values=tuple(float(value) for value in curve.x_array.to_list()),
            y_values=tuple(float(value) for value in curve.y_array.to_list()),
        )
        for curve in measurement.curves
    )

    logical_series: list[tuple[float, ...]] = []
    mixed_mode_series_extracted = False
    if measurement_type == "linear_sweep_voltammetry":
        mixed_mode_sweeps = extract_mixed_mode_lsv_sweeps(
            curve_data, expected_sweep_count=expected_lsv_sweep_count
        )
        if mixed_mode_sweeps is not None:
            logical_series.extend(sweep.currents_ua for sweep in mixed_mode_sweeps)
            mixed_mode_series_extracted = True

    for curve_number, curve in enumerate(curve_data, start=1):
        if mixed_mode_series_extracted:
            break
        x_values = curve.x_values
        currents_ua = curve.y_values
        if len(x_values) != len(currents_ua):
            raise ValueError(f"PalmSens curve {curve_number} has unequal X/Y lengths")

        if measurement_type == "linear_sweep_voltammetry":
            sweeps = split_lsv_values(
                x_values,
                currents_ua,
                expected_sweep_count=(
                    expected_lsv_sweep_count if len(curve_data) == 1 else None
                ),
            )
            logical_series.extend(sweep.currents_ua for sweep in sweeps)
        else:
            logical_series.append(currents_ua)

    if (
        measurement_type == "linear_sweep_voltammetry"
        and expected_lsv_sweep_count is not None
        and len(logical_series) != expected_lsv_sweep_count
    ):
        raise ValueError(
            f"Expected {expected_lsv_sweep_count} LSV sweeps but extracted "
            f"{len(logical_series)}"
        )

    for series_number, raw_current_values in enumerate(logical_series, start=1):
        current_values = tuple(
            value for value in raw_current_values if math.isfinite(value)
        )
        if not current_values:
            raise ValueError(
                f"Logical measurement series {series_number} contains no finite "
                "current values"
            )

        curve_maxima.append(definition.component_calculator(current_values))
        point_count += len(current_values)

    if not curve_maxima:
        raise ValueError("PalmSens measurement contains no curves")

    standard_error = None
    if len(curve_maxima) > 1:
        standard_error = statistics.stdev(curve_maxima) / math.sqrt(len(curve_maxima))

    contribution_kind = {
        "chronoamperometry": "series",
        "cyclic_voltammetry": "scan",
        "linear_sweep_voltammetry": "sweep",
    }[measurement_type]
    return ObjectiveResult(
        definition=definition,
        value=math.fsum(curve_maxima) / len(curve_maxima),
        standard_error=standard_error,
        contributions=tuple(
            ObjectiveContribution(
                sequence_number=index, kind=contribution_kind, value=value
            )
            for index, value in enumerate(curve_maxima, start=1)
        ),
        point_count=point_count,
    )
