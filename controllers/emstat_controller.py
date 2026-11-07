# emstat_controller.py

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal

from common.validation import require_integer, require_non_negative_finite_number
from engine.ports import EmStatInstrument
from palmsens.data_types import Measurement
from palmsens.emstat4s import (
    ChronoamperometrySettings,
    CyclicVoltammetrySettings,
    EmStatDataBatch,
    EmStatMeasurementType,
    LinearSweepVoltammetrySettings,
    MeasurementAbortedError,
)
from palmsens.emstat_session_repository import (
    DEFAULT_MEASUREMENTS_DIRECTORY,
    EmStatSessionRepository,
    SavedEmStatSession,
)
from palmsens.lsv_sweep_series import (
    LsvCurveData,
    extract_mixed_mode_lsv_sweeps,
    is_lsv_sweep_reset,
    looks_like_lsv_potential_channel,
    split_lsv_values,
)

logger = logging.getLogger(__name__)

EmStatMeasurementState = Literal[
    "idle", "measuring", "aborting", "completed", "aborted", "failed"
]
EmStatMeasurementSettings = (
    ChronoamperometrySettings
    | CyclicVoltammetrySettings
    | LinearSweepVoltammetrySettings
)
MeasurementCommandStartedCallback = Callable[[int], None]


@dataclass(frozen=True)
class EmStatSnapshot:
    measurement_id: int
    state: EmStatMeasurementState
    measurement_type: EmStatMeasurementType | None
    settings: EmStatMeasurementSettings | None
    point_count: int
    error_message: str | None

    @property
    def busy(self) -> bool:
        return self.state in ("measuring", "aborting")


@dataclass(frozen=True)
class EmStatSeriesSlice:
    series_id: int
    label: str
    start_index: int
    x_values: tuple[float, ...]
    currents_ua: tuple[float, ...]

    @property
    def next_index(self) -> int:
        return self.start_index + len(self.x_values)


@dataclass(frozen=True)
class EmStatDataSlice:
    measurement_id: int
    data_revision: int
    series: tuple[EmStatSeriesSlice, ...]


@dataclass(frozen=True)
class EmStatLoadedSeries:
    series_id: int
    label: str
    x_values: tuple[float, ...]
    currents_ua: tuple[float, ...]


@dataclass(frozen=True)
class EmStatLoadedMeasurement:
    file_name: str
    measurement_name: str | None
    measurement_type: EmStatMeasurementType
    measurement_title: str
    timestamp: str
    series: tuple[EmStatLoadedSeries, ...]

    @property
    def point_count(self) -> int:
        return sum(len(series.x_values) for series in self.series)


@dataclass
class _EmStatSeriesBuffer:
    label: str
    x_values: list[float]
    currents_ua: list[float]


@dataclass
class _LsvMixedChannelBuffer:
    time_values: list[float]
    values: list[float]


class EmStatController:
    """Application state and plot data for one EmStat4S measurement at a time."""

    def __init__(
        self,
        emstat: EmStatInstrument,
        *,
        measurements_directory: str | Path | None = None,
        session_repository: EmStatSessionRepository | None = None,
    ) -> None:
        self._emstat = emstat
        self._step_observer: Callable[[str, str, str], None] | None = None

        if measurements_directory is not None and session_repository is not None:
            raise ValueError(
                "Provide measurements_directory or session_repository, not both"
            )

        self._session_repository = (
            session_repository
            if session_repository is not None
            else EmStatSessionRepository(
                measurements_directory
                if measurements_directory is not None
                else DEFAULT_MEASUREMENTS_DIRECTORY
            )
        )

        self._state_lock = threading.RLock()
        self._measurement_condition = threading.Condition(self._state_lock)
        self._measurement_id = 0
        self._command_started_measurement_id: int | None = None
        self._data_revision = 0
        self._state: EmStatMeasurementState = "idle"
        self._measurement_type: EmStatMeasurementType | None = None
        self._settings: EmStatMeasurementSettings | None = None
        self._series: dict[int, _EmStatSeriesBuffer] = {}
        self._lsv_live_sweep_number = 1
        self._lsv_last_potential_v: float | None = None
        self._lsv_mixed_channels: dict[int, _LsvMixedChannelBuffer] = {}
        self._lsv_potential_curve_id: int | None = None
        self._lsv_current_curve_id: int | None = None
        self._lsv_paired_point_count = 0
        self._last_measurement: Measurement | None = None
        self._last_saved_session_path: Path | None = None
        self._error_message: str | None = None

    def set_step_observer(self, callback: Callable[[str, str, str], None] | None):
        self._step_observer = callback

    def _notify_step(self, measurement_type, status):
        if self._step_observer is not None:
            try:
                self._step_observer(
                    "Measure " + measurement_type.replace("_", " "), "emstat", status
                )
            except Exception:
                logger.exception("Protocol display observer failed")

    @property
    def connected(self) -> bool:
        return self._emstat.connected

    @property
    def abort_available(self) -> bool:
        with self._state_lock:
            measuring = self._state == "measuring"

        return measuring and self._emstat.abort_available

    @property
    def busy(self) -> bool:
        with self._state_lock:
            return self._state in ("measuring", "aborting") or getattr(
                self._emstat, "faulted", False
            )

    @property
    def state(self) -> EmStatMeasurementState:
        with self._state_lock:
            return self._state

    @property
    def last_measurement(self) -> Measurement | None:
        with self._state_lock:
            return self._last_measurement

    @property
    def last_saved_session_path(self) -> Path | None:
        """Path saved for the most recently completed measurement, if any."""

        with self._state_lock:
            return self._last_saved_session_path

    @property
    def snapshot(self) -> EmStatSnapshot:
        with self._state_lock:
            return EmStatSnapshot(
                measurement_id=self._measurement_id,
                state=self._state,
                measurement_type=self._measurement_type,
                settings=self._settings,
                point_count=sum(
                    len(series.x_values) for series in self._series.values()
                ),
                error_message=self._error_message,
            )

    def connect(self) -> bool:
        """Open a persistent EmStat connection.

        Returns False when the persistent connection is already open.
        """

        return self._emstat.connect()

    def disconnect(self) -> bool:
        """Close the persistent EmStat connection.

        Returns False when no persistent connection is open. The device layer
        rejects disconnection while a measurement is active.
        """

        return self._emstat.disconnect()

    def estimate_chronoamperometry_duration_s(
        self, *, settings: ChronoamperometrySettings
    ) -> float:
        return self._emstat.estimate_chronoamperometry_duration_s(settings=settings)

    def estimate_cyclic_voltammetry_duration_s(
        self, *, settings: CyclicVoltammetrySettings
    ) -> float:
        return self._emstat.estimate_cyclic_voltammetry_duration_s(settings=settings)

    def estimate_linear_sweep_voltammetry_duration_s(
        self, *, settings: LinearSweepVoltammetrySettings
    ) -> float:
        return self._emstat.estimate_linear_sweep_voltammetry_duration_s(
            settings=settings
        )

    def wait_until_measurement_command_started(
        self, *, measurement_id: int, timeout_s: float
    ) -> bool:
        """Wait until the selected measurement reaches ``manager.measure``.

        This is a command-boundary synchronization point, not a claim that the
        instrument has already produced its first acquired data point.
        """

        measurement_id = require_integer(measurement_id, name="measurement_id")
        if measurement_id <= 0:
            raise ValueError("measurement_id must be positive")
        require_non_negative_finite_number(timeout_s, name="timeout_s")

        with self._measurement_condition:
            self._measurement_condition.wait_for(
                lambda: (
                    self._command_started_measurement_id == measurement_id
                    or self._measurement_id > measurement_id
                    or (
                        self._measurement_id == measurement_id
                        and self._state in ("completed", "aborted", "failed")
                    )
                ),
                timeout=float(timeout_s),
            )
            return self._command_started_measurement_id == measurement_id

    def measure_chronoamperometry(
        self,
        *,
        settings: ChronoamperometrySettings,
        measurement_name: str | None = None,
        stream_path: str | Path | None = None,
        on_command_started: MeasurementCommandStartedCallback | None = None,
        session_directory: str | Path | None = None,
        session_sequence_number: int | None = None,
    ) -> Measurement:
        return self._run_measurement(
            measurement_type="chronoamperometry",
            settings=settings,
            measurement_name=measurement_name,
            session_directory=session_directory,
            session_sequence_number=session_sequence_number,
            action=lambda measurement_id: self._emstat.measure_chronoamperometry(
                settings=settings,
                on_data=partial(self._record_batch, measurement_id),
                on_measurement_starting=partial(
                    self._mark_measurement_command_started,
                    measurement_id,
                    on_command_started,
                ),
                stream_path=stream_path,
            ),
        )

    def measure_cyclic_voltammetry(
        self,
        *,
        settings: CyclicVoltammetrySettings,
        measurement_name: str | None = None,
        stream_path: str | Path | None = None,
        on_command_started: MeasurementCommandStartedCallback | None = None,
        session_directory: str | Path | None = None,
        session_sequence_number: int | None = None,
    ) -> Measurement:
        return self._run_measurement(
            measurement_type="cyclic_voltammetry",
            settings=settings,
            measurement_name=measurement_name,
            session_directory=session_directory,
            session_sequence_number=session_sequence_number,
            action=lambda measurement_id: self._emstat.measure_cyclic_voltammetry(
                settings=settings,
                on_data=partial(self._record_batch, measurement_id),
                on_measurement_starting=partial(
                    self._mark_measurement_command_started,
                    measurement_id,
                    on_command_started,
                ),
                stream_path=stream_path,
            ),
        )

    def measure_linear_sweep_voltammetry(
        self,
        *,
        settings: LinearSweepVoltammetrySettings,
        measurement_name: str | None = None,
        stream_path: str | Path | None = None,
        on_command_started: MeasurementCommandStartedCallback | None = None,
        session_directory: str | Path | None = None,
        session_sequence_number: int | None = None,
    ) -> Measurement:
        return self._run_measurement(
            measurement_type="linear_sweep_voltammetry",
            settings=settings,
            measurement_name=measurement_name,
            session_directory=session_directory,
            session_sequence_number=session_sequence_number,
            action=lambda measurement_id: self._emstat.measure_linear_sweep_voltammetry(
                settings=settings,
                on_data=partial(self._record_batch, measurement_id),
                on_measurement_starting=partial(
                    self._mark_measurement_command_started,
                    measurement_id,
                    on_command_started,
                ),
                stream_path=stream_path,
            ),
        )

    def abort_measurement(self) -> bool:
        """Request abortion of the active measurement.

        Returns False if no measurement is active or an abort has already been
        requested. The blocking measurement method ultimately raises
        MeasurementAbortedError when PalmSens finishes aborting.
        """

        with self._state_lock:
            if self._state != "measuring":
                return False

            self._state = "aborting"

        try:
            requested = self._emstat.abort_measurement()
        except Exception:
            with self._state_lock:
                if self._state == "aborting":
                    self._state = "measuring"

            raise

        if not requested:
            with self._state_lock:
                if self._state == "aborting":
                    self._state = "measuring"

        return requested

    def data_since(
        self,
        *,
        measurement_id: int | None,
        data_revision: int | None,
        start_indices: Mapping[int, int],
    ) -> EmStatDataSlice:
        """Return points not yet consumed by a live plot.

        A changed measurement ID or data revision resets every series to index
        zero. Reconciliation with PalmSens curves increments the revision so a
        completed plot is rebuilt from the authoritative curve collection.
        """

        if measurement_id is not None:
            measurement_id = require_integer(measurement_id, name="measurement_id")
            if measurement_id < 0:
                raise ValueError("measurement_id must be non-negative")

        if data_revision is not None:
            data_revision = require_integer(data_revision, name="data_revision")
            if data_revision < 0:
                raise ValueError("data_revision must be non-negative")

        with self._state_lock:
            reset = (
                measurement_id != self._measurement_id
                or data_revision != self._data_revision
            )
            series_slices: list[EmStatSeriesSlice] = []

            for series_id, series in self._series.items():
                start_index = 0 if reset else start_indices.get(series_id, 0)
                start_index = require_integer(
                    start_index, name=f"start_indices[{series_id}]"
                )

                if not 0 <= start_index <= len(series.x_values):
                    raise ValueError(
                        f"Start index for EmStat series {series_id} is outside "
                        "the available point range"
                    )

                series_slices.append(
                    EmStatSeriesSlice(
                        series_id=series_id,
                        label=series.label,
                        start_index=start_index,
                        x_values=tuple(series.x_values[start_index:]),
                        currents_ua=tuple(series.currents_ua[start_index:]),
                    )
                )

            return EmStatDataSlice(
                measurement_id=self._measurement_id,
                data_revision=self._data_revision,
                series=tuple(series_slices),
            )

    def list_saved_measurements(self) -> tuple[SavedEmStatSession, ...]:
        return self._session_repository.list_sessions()

    def load_saved_measurement(self, *, file_name: str) -> EmStatLoadedMeasurement:
        entry, measurement = self._session_repository.load(file_name)
        series = self._extract_measurement_series(
            measurement=measurement, measurement_type=entry.measurement_type
        )

        if not series:
            raise RuntimeError(f"Saved measurement contains no curves: {file_name}")

        return EmStatLoadedMeasurement(
            file_name=entry.file_name,
            measurement_name=entry.measurement_name,
            measurement_type=entry.measurement_type,
            measurement_title=measurement.title,
            timestamp=measurement.timestamp,
            series=series,
        )

    def load_saved_measurement_session(self, *, file_name: str) -> Measurement:
        """Load the raw PalmSens measurement for deterministic analysis."""

        _, measurement = self._session_repository.load(file_name)
        return measurement

    def _run_measurement(
        self,
        *,
        measurement_type: EmStatMeasurementType,
        settings: EmStatMeasurementSettings,
        measurement_name: str | None,
        session_directory: str | Path | None,
        session_sequence_number: int | None,
        action: Callable[[int], Measurement],
    ) -> Measurement:
        measurement_id = self._begin_measurement(
            measurement_type=measurement_type, settings=settings
        )

        try:
            self._notify_step(measurement_type, "started")
            measurement = action(measurement_id)
            self._reconcile_measurement_series(
                measurement_id=measurement_id,
                measurement_type=measurement_type,
                settings=settings,
                measurement=measurement,
            )
            saved_session_path = self._save_session(
                measurement_type=measurement_type,
                measurement_name=measurement_name,
                measurement=measurement,
                session_directory=session_directory,
                session_sequence_number=session_sequence_number,
            )
        except MeasurementAbortedError as error:
            self._notify_step(measurement_type, "aborted")
            if error.partial_measurement is not None:
                self._reconcile_measurement_series(
                    measurement_id=measurement_id,
                    measurement_type=measurement_type,
                    settings=settings,
                    measurement=error.partial_measurement,
                )

            with self._state_lock:
                if self._measurement_id == measurement_id:
                    self._last_measurement = error.partial_measurement
                    self._last_saved_session_path = None
                    self._state = "aborted"
                    self._error_message = None
                    self._measurement_condition.notify_all()

            raise
        except Exception as error:
            with self._state_lock:
                if self._measurement_id == measurement_id:
                    self._last_measurement = None
                    self._last_saved_session_path = None
                    self._state = "failed"
                    self._error_message = str(error)
                    self._measurement_condition.notify_all()

            self._notify_step(measurement_type, "failed")
            logger.warning("EmStat measurement failed: %s", error)
            raise

        with self._state_lock:
            if self._measurement_id == measurement_id:
                self._last_measurement = measurement
                self._last_saved_session_path = saved_session_path
                self._state = "completed"
                self._error_message = None
                self._measurement_condition.notify_all()

        self._notify_step(measurement_type, "completed")
        return measurement

    def _begin_measurement(
        self,
        *,
        measurement_type: EmStatMeasurementType,
        settings: EmStatMeasurementSettings,
    ) -> int:
        with self._state_lock:
            if self._state in ("measuring", "aborting"):
                raise RuntimeError("An EmStat measurement is already in progress")

            self._measurement_id += 1
            self._data_revision += 1
            self._command_started_measurement_id = None
            self._state = "measuring"
            self._measurement_type = measurement_type
            self._settings = settings
            self._series.clear()
            self._lsv_live_sweep_number = 1
            self._lsv_last_potential_v = None
            self._lsv_mixed_channels.clear()
            self._lsv_potential_curve_id = None
            self._lsv_current_curve_id = None
            self._lsv_paired_point_count = 0
            self._last_measurement = None
            self._last_saved_session_path = None
            self._error_message = None

            self._measurement_condition.notify_all()

            return self._measurement_id

    def _mark_measurement_command_started(
        self,
        measurement_id: int,
        on_command_started: MeasurementCommandStartedCallback | None,
    ) -> None:
        with self._measurement_condition:
            if self._measurement_id != measurement_id:
                logger.warning(
                    "Ignoring command-start notification from stale EmStat "
                    "measurement %s",
                    measurement_id,
                )
                return

            self._command_started_measurement_id = measurement_id
            self._measurement_condition.notify_all()

        if on_command_started is not None:
            on_command_started(measurement_id)

    def _record_batch(self, measurement_id: int, batch: EmStatDataBatch) -> None:
        if len(batch.x_values) != len(batch.currents_ua):
            raise RuntimeError("EmStat callback batch has unequal X/Y lengths")

        with self._state_lock:
            if self._measurement_id != measurement_id:
                logger.warning(
                    "Discarding data from stale EmStat measurement %s", measurement_id
                )
                return

            settings = self._settings
            if (
                self._measurement_type == "linear_sweep_voltammetry"
                and isinstance(settings, LinearSweepVoltammetrySettings)
                and settings.sweep_count > 1
            ):
                self._record_multi_sweep_lsv_batch(batch, settings=settings)
                return

            series = self._series.get(batch.curve_id)

            if series is None:
                series_number = len(self._series) + 1
                label = (
                    f"Scan {series_number}"
                    if self._measurement_type == "cyclic_voltammetry"
                    else "Current"
                )
                series = _EmStatSeriesBuffer(label=label, x_values=[], currents_ua=[])
                self._series[batch.curve_id] = series

            series.x_values.extend(batch.x_values)
            series.currents_ua.extend(batch.currents_ua)

    def _record_multi_sweep_lsv_batch(
        self, batch: EmStatDataBatch, *, settings: LinearSweepVoltammetrySettings
    ) -> None:
        """Pair live ``E vs t`` and ``i vs t`` channels, then split sweeps.

        Mixed Mode emits one callback curve for potential against time and one
        for current against time. Neither callback curve is itself an LSV
        sweep. Called only while ``_state_lock`` is held.
        """

        channel = self._lsv_mixed_channels.get(batch.curve_id)
        if channel is None:
            channel = _LsvMixedChannelBuffer(time_values=[], values=[])
            self._lsv_mixed_channels[batch.curve_id] = channel
        channel.time_values.extend(batch.x_values)
        channel.values.extend(batch.currents_ua)

        self._identify_live_lsv_channels(settings)
        potential_curve_id = self._lsv_potential_curve_id
        current_curve_id = self._lsv_current_curve_id
        if potential_curve_id is None or current_curve_id is None:
            return

        potential_channel = self._lsv_mixed_channels[potential_curve_id]
        current_channel = self._lsv_mixed_channels[current_curve_id]
        available_point_count = min(
            len(potential_channel.values), len(current_channel.values)
        )

        for index in range(self._lsv_paired_point_count, available_point_count):
            potential_v = potential_channel.values[index]
            current_ua = current_channel.values[index]
            previous_potential_v = self._lsv_last_potential_v
            if previous_potential_v is not None and is_lsv_sweep_reset(
                previous_potential_v,
                potential_v,
                begin_potential_v=settings.begin_potential_v,
                end_potential_v=settings.end_potential_v,
            ):
                self._lsv_live_sweep_number += 1

            series_id = self._lsv_live_sweep_number
            series = self._series.get(series_id)
            if series is None:
                series = _EmStatSeriesBuffer(
                    label=f"Sweep {series_id}", x_values=[], currents_ua=[]
                )
                self._series[series_id] = series

            series.x_values.append(potential_v)
            series.currents_ua.append(current_ua)
            self._lsv_last_potential_v = potential_v
        self._lsv_paired_point_count = available_point_count

    def _identify_live_lsv_channels(
        self, settings: LinearSweepVoltammetrySettings
    ) -> None:
        """Identify the potential channel after enough live points arrive."""

        if self._lsv_potential_curve_id is not None:
            return
        if len(self._lsv_mixed_channels) < 2:
            return

        potential_candidates = [
            curve_id
            for curve_id, channel in self._lsv_mixed_channels.items()
            if looks_like_lsv_potential_channel(
                channel.values,
                begin_potential_v=settings.begin_potential_v,
                end_potential_v=settings.end_potential_v,
                step_potential_v=settings.step_potential_v,
            )
        ]
        if len(potential_candidates) != 1:
            return

        potential_curve_id = potential_candidates[0]
        other_curve_ids = [
            curve_id
            for curve_id in self._lsv_mixed_channels
            if curve_id != potential_curve_id
        ]
        if len(other_curve_ids) != 1:
            return

        self._lsv_potential_curve_id = potential_curve_id
        self._lsv_current_curve_id = other_curve_ids[0]

    def _reconcile_measurement_series(
        self,
        *,
        measurement_id: int,
        measurement_type: EmStatMeasurementType,
        settings: EmStatMeasurementSettings,
        measurement: Measurement,
    ) -> None:
        """Replace callback buffers with PalmSens' final curve collection."""

        try:
            extracted_series = self._extract_measurement_series(
                measurement=measurement,
                measurement_type=measurement_type,
                settings=settings,
            )
        except Exception:
            logger.exception(
                "Failed to rebuild %s plot data from PalmSens curves; "
                "retaining callback data",
                measurement_type.replace("_", " "),
            )
            return

        if not extracted_series:
            return

        with self._state_lock:
            if self._measurement_id != measurement_id:
                return

            self._series = {
                series.series_id: _EmStatSeriesBuffer(
                    label=series.label,
                    x_values=list(series.x_values),
                    currents_ua=list(series.currents_ua),
                )
                for series in extracted_series
            }
            self._data_revision += 1

    @staticmethod
    def _extract_measurement_series(
        *,
        measurement: Measurement,
        measurement_type: EmStatMeasurementType,
        settings: EmStatMeasurementSettings | None = None,
    ) -> tuple[EmStatLoadedSeries, ...]:
        extracted_series: list[EmStatLoadedSeries] = []
        curve_data = tuple(
            LsvCurveData(
                title=str(curve.title),
                x_values=tuple(float(value) for value in curve.x_array.to_list()),
                y_values=tuple(float(value) for value in curve.y_array.to_list()),
            )
            for curve in measurement.curves
        )

        lsv_settings = (
            settings if isinstance(settings, LinearSweepVoltammetrySettings) else None
        )
        if measurement_type == "linear_sweep_voltammetry":
            mixed_mode_sweeps = extract_mixed_mode_lsv_sweeps(
                curve_data,
                begin_potential_v=(
                    None if lsv_settings is None else lsv_settings.begin_potential_v
                ),
                end_potential_v=(
                    None if lsv_settings is None else lsv_settings.end_potential_v
                ),
                expected_sweep_count=(
                    None if lsv_settings is None else lsv_settings.sweep_count
                ),
            )
            if mixed_mode_sweeps is not None:
                return tuple(
                    EmStatLoadedSeries(
                        series_id=sweep.sweep_number,
                        label=f"Sweep {sweep.sweep_number}",
                        x_values=sweep.x_values,
                        currents_ua=sweep.currents_ua,
                    )
                    for sweep in mixed_mode_sweeps
                )

        for curve_number, curve in enumerate(curve_data, start=1):
            x_values = curve.x_values
            currents_ua = curve.y_values

            if len(x_values) != len(currents_ua):
                raise RuntimeError(
                    f"PalmSens curve {curve_number} has unequal X/Y lengths"
                )

            if measurement_type == "linear_sweep_voltammetry":
                sweeps = split_lsv_values(
                    x_values,
                    currents_ua,
                    begin_potential_v=(
                        None if lsv_settings is None else lsv_settings.begin_potential_v
                    ),
                    end_potential_v=(
                        None if lsv_settings is None else lsv_settings.end_potential_v
                    ),
                    expected_sweep_count=(
                        lsv_settings.sweep_count
                        if lsv_settings is not None and len(curve_data) == 1
                        else None
                    ),
                )
                for sweep in sweeps:
                    series_number = len(extracted_series) + 1
                    extracted_series.append(
                        EmStatLoadedSeries(
                            series_id=series_number,
                            label=f"Sweep {series_number}",
                            x_values=sweep.x_values,
                            currents_ua=sweep.currents_ua,
                        )
                    )
                continue

            label = (
                f"Scan {curve_number}"
                if measurement_type == "cyclic_voltammetry"
                else "Current"
            )
            extracted_series.append(
                EmStatLoadedSeries(
                    series_id=curve_number,
                    label=label,
                    x_values=x_values,
                    currents_ua=currents_ua,
                )
            )

        if (
            measurement_type == "linear_sweep_voltammetry"
            and isinstance(settings, LinearSweepVoltammetrySettings)
            and len(extracted_series) != settings.sweep_count
        ):
            raise RuntimeError(
                f"Expected {settings.sweep_count} LSV sweeps but extracted "
                f"{len(extracted_series)}"
            )

        return tuple(extracted_series)

    def _save_session(
        self,
        *,
        measurement_type: EmStatMeasurementType,
        measurement_name: str | None,
        measurement: Measurement,
        session_directory: str | Path | None,
        session_sequence_number: int | None,
    ) -> Path | None:
        measurement_label = measurement_type.replace("_", " ")

        try:
            saved_session = self._session_repository.save(
                measurement=measurement,
                measurement_type=measurement_type,
                measurement_name=measurement_name,
                relative_directory=session_directory,
                sequence_number=session_sequence_number,
            )
        except Exception as error:
            logger.exception(
                "Failed to save %s session in %s",
                measurement_label,
                self._session_repository.measurements_directory,
            )
            raise RuntimeError(
                "Measurement finished, but its raw session could not be saved. Check storage before continuing."
            ) from error
        else:
            saved_path = self._session_repository.measurements_directory / Path(
                saved_session.file_name
            )
            logger.info("Saved %s session to %s", measurement_label, saved_path)
            return saved_path
