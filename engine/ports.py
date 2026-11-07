"""Instrument interface shared by the real SDK worker and simulator."""

from pathlib import Path
from typing import Protocol

from palmsens.data_types import Measurement
from palmsens.emstat4s import (
    ChronoamperometrySettings,
    CyclicVoltammetrySettings,
    EmStatDataCallback,
    EmStatMeasurementStartingCallback,
    LinearSweepVoltammetrySettings,
)


class EmStatInstrument(Protocol):
    @property
    def connected(self) -> bool: ...
    @property
    def abort_available(self) -> bool: ...
    def connect(self) -> bool: ...
    def disconnect(self) -> bool: ...
    def measure_chronoamperometry(
        self,
        *,
        settings: ChronoamperometrySettings,
        on_data: EmStatDataCallback | None = None,
        on_measurement_starting: EmStatMeasurementStartingCallback | None = None,
        stream_path: str | Path | None = None,
    ) -> Measurement: ...
    def estimate_chronoamperometry_duration_s(
        self, *, settings: ChronoamperometrySettings
    ) -> float: ...
    def measure_cyclic_voltammetry(
        self,
        *,
        settings: CyclicVoltammetrySettings,
        on_data: EmStatDataCallback | None = None,
        on_measurement_starting: EmStatMeasurementStartingCallback | None = None,
        stream_path: str | Path | None = None,
    ) -> Measurement: ...
    def estimate_cyclic_voltammetry_duration_s(
        self, *, settings: CyclicVoltammetrySettings
    ) -> float: ...
    def measure_linear_sweep_voltammetry(
        self,
        *,
        settings: LinearSweepVoltammetrySettings,
        on_data: EmStatDataCallback | None = None,
        on_measurement_starting: EmStatMeasurementStartingCallback | None = None,
        stream_path: str | Path | None = None,
    ) -> Measurement: ...
    def estimate_linear_sweep_voltammetry_duration_s(
        self, *, settings: LinearSweepVoltammetrySettings
    ) -> float: ...
    def abort_measurement(self) -> bool: ...
