from __future__ import annotations

import math
import re
from dataclasses import dataclass
from itertools import pairwise
from typing import Iterable, Literal


class LsvSweepSplitError(ValueError):
    """Raised when Mixed Mode LSV channels cannot be paired or split safely."""


@dataclass(frozen=True, slots=True)
class LsvSweepSeries:
    sweep_number: int
    x_values: tuple[float, ...]
    currents_ua: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class LsvCurveData:
    """One PalmSens curve, retaining its title for channel identification."""

    title: str
    x_values: tuple[float, ...]
    y_values: tuple[float, ...]


LsvMixedChannelKind = Literal["potential", "current"]


def mixed_mode_lsv_channel_kind(title: str) -> LsvMixedChannelKind | None:
    """Identify PalmSens Mixed Mode ``E vs t`` and ``i vs t`` curves."""

    normalized = " ".join(title.casefold().split())
    if "potential" in normalized or re.search(
        r"(?:^|\s)e\s+vs\s+t(?:\s|$)", normalized
    ):
        return "potential"
    if "current" in normalized or re.search(r"(?:^|\s)i\s+vs\s+t(?:\s|$)", normalized):
        return "current"
    return None


def extract_mixed_mode_lsv_sweeps(
    curves: Iterable[LsvCurveData],
    *,
    begin_potential_v: float | None = None,
    end_potential_v: float | None = None,
    expected_sweep_count: int | None = None,
) -> tuple[LsvSweepSeries, ...] | None:
    """Pair Mixed Mode potential/current time channels and split the sweeps.

    Returns ``None`` when the collection is not a recognizable Mixed Mode
    channel pair, allowing ordinary LSV curves to use their normal E/I data.
    """

    potential_curve: LsvCurveData | None = None
    current_curve: LsvCurveData | None = None

    for curve in curves:
        kind = mixed_mode_lsv_channel_kind(curve.title)
        if kind == "potential":
            if potential_curve is not None:
                raise LsvSweepSplitError("Multiple Mixed Mode potential channels found")
            potential_curve = curve
        elif kind == "current":
            if current_curve is not None:
                raise LsvSweepSplitError("Multiple Mixed Mode current channels found")
            current_curve = curve

    if potential_curve is None and current_curve is None:
        return None
    if potential_curve is None or current_curve is None:
        raise LsvSweepSplitError(
            "Mixed Mode LSV requires both E vs t and i vs t curves"
        )

    _validate_time_alignment(potential_curve, current_curve)
    return split_lsv_values(
        potential_curve.y_values,
        current_curve.y_values,
        begin_potential_v=begin_potential_v,
        end_potential_v=end_potential_v,
        expected_sweep_count=expected_sweep_count,
    )


def is_lsv_sweep_reset(
    previous_potential_v: float,
    current_potential_v: float,
    *,
    begin_potential_v: float,
    end_potential_v: float,
) -> bool:
    """Return whether two consecutive potential points cross a sweep boundary."""

    sweep_span_v = abs(end_potential_v - begin_potential_v)
    if sweep_span_v == 0:
        raise ValueError("LSV begin and end potentials must differ")

    direction = 1.0 if end_potential_v > begin_potential_v else -1.0
    signed_delta_v = direction * (current_potential_v - previous_potential_v)
    reset_threshold_v = sweep_span_v * 0.5
    endpoint_tolerance_v = sweep_span_v * 0.25

    previous_near_end = (
        abs(previous_potential_v - end_potential_v) <= endpoint_tolerance_v
    )
    current_near_begin = (
        abs(current_potential_v - begin_potential_v) <= endpoint_tolerance_v
    )
    return (
        signed_delta_v <= -reset_threshold_v
        and previous_near_end
        and current_near_begin
    )


def split_lsv_values(
    x_values: tuple[float, ...],
    currents_ua: tuple[float, ...],
    *,
    begin_potential_v: float | None = None,
    end_potential_v: float | None = None,
    expected_sweep_count: int | None = None,
) -> tuple[LsvSweepSeries, ...]:
    """Split paired potential/current values at potential-reset boundaries."""

    if len(x_values) != len(currents_ua):
        raise LsvSweepSplitError("LSV curve has unequal potential/current lengths")
    if not x_values:
        return ()
    if expected_sweep_count is not None and expected_sweep_count < 1:
        raise ValueError("expected_sweep_count must be positive")
    if not all(math.isfinite(value) for value in x_values):
        raise LsvSweepSplitError("LSV curve contains a non-finite potential")
    if not all(math.isfinite(value) for value in currents_ua):
        raise LsvSweepSplitError("LSV curve contains a non-finite current")

    if (begin_potential_v is None) != (end_potential_v is None):
        raise ValueError("Provide both LSV endpoints or neither")

    if begin_potential_v is None:
        observed_min = min(x_values)
        observed_max = max(x_values)
        observed_span = observed_max - observed_min
        if observed_span == 0:
            if expected_sweep_count not in (None, 1):
                raise LsvSweepSplitError(
                    "Constant-potential data cannot contain multiple LSV sweeps"
                )
            return (LsvSweepSeries(1, x_values, currents_ua),)

        direction = _infer_sweep_direction(x_values, observed_span=observed_span)
        if direction > 0:
            begin_potential_v, end_potential_v = observed_min, observed_max
        else:
            begin_potential_v, end_potential_v = observed_max, observed_min

    assert end_potential_v is not None
    boundaries = [0]
    for index in range(1, len(x_values)):
        if is_lsv_sweep_reset(
            x_values[index - 1],
            x_values[index],
            begin_potential_v=begin_potential_v,
            end_potential_v=end_potential_v,
        ):
            boundaries.append(index)
    boundaries.append(len(x_values))

    series = tuple(
        LsvSweepSeries(
            sweep_number=sweep_number,
            x_values=x_values[start:stop],
            currents_ua=currents_ua[start:stop],
        )
        for sweep_number, (start, stop) in enumerate(pairwise(boundaries), start=1)
        if stop > start
    )

    if expected_sweep_count is not None and len(series) != expected_sweep_count:
        raise LsvSweepSplitError(
            f"Expected {expected_sweep_count} LSV sweeps but detected "
            f"{len(series)} from potential resets"
        )
    return series


def looks_like_lsv_potential_channel(
    values: tuple[float, ...] | list[float],
    *,
    begin_potential_v: float,
    end_potential_v: float,
    step_potential_v: float,
) -> bool:
    """Classify a live Mixed Mode channel from its first acquired values."""

    if len(values) < 2:
        return False
    span_v = abs(end_potential_v - begin_potential_v)
    direction = 1.0 if end_potential_v > begin_potential_v else -1.0
    start_tolerance_v = max(step_potential_v * 3.0, span_v * 0.05, 1e-6)
    step_tolerance_v = max(step_potential_v * 0.75, span_v * 1e-3, 1e-6)
    if abs(values[0] - begin_potential_v) > start_tolerance_v:
        return False

    deltas = [
        direction * (current - previous)
        for previous, current in zip(values, values[1:], strict=False)
    ]
    forward = [delta for delta in deltas if delta >= -step_tolerance_v]
    return bool(forward) and sum(
        abs(delta - step_potential_v) <= step_tolerance_v for delta in forward
    ) >= max(1, len(forward) // 2)


def _validate_time_alignment(
    potential_curve: LsvCurveData, current_curve: LsvCurveData
) -> None:
    if len(potential_curve.x_values) != len(potential_curve.y_values):
        raise LsvSweepSplitError("Mixed Mode potential curve has unequal X/Y lengths")
    if len(current_curve.x_values) != len(current_curve.y_values):
        raise LsvSweepSplitError("Mixed Mode current curve has unequal X/Y lengths")
    if len(potential_curve.x_values) != len(current_curve.x_values):
        raise LsvSweepSplitError(
            "Mixed Mode potential/current channels have different point counts"
        )
    if not potential_curve.x_values:
        return

    time_span_s = max(potential_curve.x_values) - min(potential_curve.x_values)
    tolerance_s = max(1e-6, abs(time_span_s) * 1e-6)
    if any(
        abs(potential_time - current_time) > tolerance_s
        for potential_time, current_time in zip(
            potential_curve.x_values, current_curve.x_values, strict=True
        )
    ):
        raise LsvSweepSplitError(
            "Mixed Mode potential/current channels are not time-aligned"
        )


def _infer_sweep_direction(x_values: tuple[float, ...], *, observed_span: float) -> int:
    minimum_meaningful_delta = observed_span * 1e-9
    for previous, current in zip(x_values, x_values[1:], strict=False):
        delta = current - previous
        if abs(delta) > minimum_meaningful_delta:
            return 1 if delta > 0 else -1
    raise LsvSweepSplitError("Could not infer the LSV sweep direction")
