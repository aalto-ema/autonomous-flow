# emstat4s.py

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from palmsens import sdk as ps
from palmsens.data_types import CallbackData, Measurement
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pypalmsens.types import MethodType

from common.validation import (
    require_finite_number,
    require_non_negative_finite_number,
    require_positive_finite_number,
    require_positive_integer,
)

logger = logging.getLogger(__name__)

PowerFrequencyHz = Literal[50, 60]
EmStatMeasurementType = Literal[
    "chronoamperometry", "cyclic_voltammetry", "linear_sweep_voltammetry"
]


class MeasurementAbortedError(RuntimeError):
    def __init__(
        self,
        partial_measurement: Measurement | None,
        *,
        measurement_type: EmStatMeasurementType,
    ) -> None:
        super().__init__(f"{measurement_type.replace('_', ' ').title()} was aborted")
        self.partial_measurement = partial_measurement
        self.measurement_type = measurement_type


@dataclass(frozen=True)
class ChronoamperometrySettings:
    interval_time_s: float
    potential_v: float
    run_time_s: float
    equilibration_time_s: float = 0.0

    def __post_init__(self) -> None:
        require_positive_finite_number(self.interval_time_s, name="interval_time_s")
        require_finite_number(self.potential_v, name="potential_v")
        require_positive_finite_number(self.run_time_s, name="run_time_s")
        require_non_negative_finite_number(
            self.equilibration_time_s, name="equilibration_time_s"
        )


@dataclass(frozen=True)
class CyclicVoltammetrySettings:
    begin_potential_v: float
    vertex1_potential_v: float
    vertex2_potential_v: float
    step_potential_v: float
    scan_rate_v_per_s: float
    scan_count: int
    equilibration_time_s: float = 0.0

    def __post_init__(self) -> None:
        require_finite_number(self.begin_potential_v, name="begin_potential_v")
        require_finite_number(self.vertex1_potential_v, name="vertex1_potential_v")
        require_finite_number(self.vertex2_potential_v, name="vertex2_potential_v")
        require_positive_finite_number(self.step_potential_v, name="step_potential_v")
        require_positive_finite_number(self.scan_rate_v_per_s, name="scan_rate_v_per_s")
        require_positive_integer(self.scan_count, name="scan_count")
        require_non_negative_finite_number(
            self.equilibration_time_s, name="equilibration_time_s"
        )

        if self.vertex1_potential_v == self.vertex2_potential_v:
            raise ValueError("vertex1_potential_v and vertex2_potential_v must differ")

        minimum_vertex_v = min(self.vertex1_potential_v, self.vertex2_potential_v)
        maximum_vertex_v = max(self.vertex1_potential_v, self.vertex2_potential_v)

        if not minimum_vertex_v <= self.begin_potential_v <= maximum_vertex_v:
            raise ValueError(
                "begin_potential_v must be between vertex1_potential_v and "
                "vertex2_potential_v"
            )


@dataclass(frozen=True)
class LinearSweepVoltammetrySettings:
    begin_potential_v: float
    end_potential_v: float
    step_potential_v: float
    scan_rate_v_per_s: float
    sweep_count: int = 1
    equilibration_time_s: float = 0.0

    def __post_init__(self) -> None:
        require_finite_number(self.begin_potential_v, name="begin_potential_v")
        require_finite_number(self.end_potential_v, name="end_potential_v")
        require_positive_finite_number(self.step_potential_v, name="step_potential_v")
        require_positive_finite_number(self.scan_rate_v_per_s, name="scan_rate_v_per_s")
        require_positive_integer(self.sweep_count, name="sweep_count")
        require_non_negative_finite_number(
            self.equilibration_time_s, name="equilibration_time_s"
        )

        if self.begin_potential_v == self.end_potential_v:
            raise ValueError("begin_potential_v and end_potential_v must differ")

    @property
    def sampling_interval_s(self) -> float:
        """Sampling interval implied by step potential and scan rate."""

        return self.step_potential_v / self.scan_rate_v_per_s


@dataclass(frozen=True)
class EmStatDataBatch:
    curve_id: int
    x_values: tuple[float, ...]
    currents_ua: tuple[float, ...]


EmStatDataCallback = Callable[[EmStatDataBatch], None]
EmStatMeasurementStartingCallback = Callable[[], None]


class _PalmSensCallback(Protocol):
    def __call__(self, data: CallbackData) -> None: ...


class EmStat4S:
    def __init__(self, *, power_frequency_hz: PowerFrequencyHz = 50) -> None:
        if power_frequency_hz not in (50, 60):
            raise ValueError("power_frequency_hz must be 50 or 60")

        self._power_frequency_hz: PowerFrequencyHz = power_frequency_hz

        self._operation_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._persistent_manager: ps.InstrumentManager | None = None
        self._active_manager: ps.InstrumentManager | None = None
        self._abort_requested = False

    @property
    def connected(self) -> bool:
        """Whether an explicitly requested persistent connection is open."""

        with self._state_lock:
            manager = self._persistent_manager

        return manager is not None and manager.is_connected()

    @property
    def abort_available(self) -> bool:
        with self._state_lock:
            return self._active_manager is not None and not self._abort_requested

    def connect(self) -> bool:
        """Open a persistent instrument connection.

        Returns False when a persistent connection is already open. Measurements
        reuse this connection until disconnect is called.
        """

        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError("Another EmStat4 operation is already in progress")

        try:
            with self._state_lock:
                manager = self._persistent_manager

            if manager is not None and manager.is_connected():
                return False

            started_at = time.perf_counter()
            instrument = self._find_instrument()

            logger.info(
                "Opening persistent EmStat4 connection: instrument=%s", instrument.name
            )

            manager = ps.connect(instrument)

            with self._state_lock:
                self._persistent_manager = manager

            logger.info(
                "Persistent EmStat4 connection opened in %.3f s",
                time.perf_counter() - started_at,
            )
            return True
        finally:
            self._operation_lock.release()

    def disconnect(self) -> bool:
        """Close the persistent connection without affecting one-shot use.

        Returns False when no persistent connection is open. Disconnecting is
        rejected while a measurement or another connection operation is active.
        """

        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError("Another EmStat4 operation is already in progress")

        try:
            with self._state_lock:
                manager = self._persistent_manager

                if manager is None:
                    return False

                self._persistent_manager = None

            try:
                manager.disconnect()
            except Exception:
                logger.exception("Failed to close persistent EmStat4 connection")
                raise

            logger.info("Persistent EmStat4 connection closed")
            return True
        finally:
            self._operation_lock.release()

    def measure_chronoamperometry(
        self,
        *,
        settings: ChronoamperometrySettings,
        on_data: EmStatDataCallback | None = None,
        on_measurement_starting: EmStatMeasurementStartingCallback | None = None,
        stream_path: str | Path | None = None,
    ) -> Measurement:
        """Perform one chronoamperometry measurement."""

        return self._measure(
            method=self._build_chronoamperometry_method(settings),
            measurement_type="chronoamperometry",
            settings=settings,
            on_data=on_data,
            on_measurement_starting=on_measurement_starting,
            stream_path=stream_path,
        )

    def estimate_chronoamperometry_duration_s(
        self, *, settings: ChronoamperometrySettings
    ) -> float:
        """Ask the instrument to estimate total chronoamperometry duration."""

        return self._estimate_duration(
            method=self._build_chronoamperometry_method(settings),
            measurement_type="chronoamperometry",
        )

    def measure_cyclic_voltammetry(
        self,
        *,
        settings: CyclicVoltammetrySettings,
        on_data: EmStatDataCallback | None = None,
        on_measurement_starting: EmStatMeasurementStartingCallback | None = None,
        stream_path: str | Path | None = None,
    ) -> Measurement:
        """Perform one cyclic-voltammetry measurement."""

        return self._measure(
            method=self._build_cyclic_voltammetry_method(settings),
            measurement_type="cyclic_voltammetry",
            settings=settings,
            on_data=on_data,
            on_measurement_starting=on_measurement_starting,
            stream_path=stream_path,
        )

    def estimate_cyclic_voltammetry_duration_s(
        self, *, settings: CyclicVoltammetrySettings
    ) -> float:
        """Ask the instrument to estimate total cyclic-voltammetry duration."""

        return self._estimate_duration(
            method=self._build_cyclic_voltammetry_method(settings),
            measurement_type="cyclic_voltammetry",
        )

    def measure_linear_sweep_voltammetry(
        self,
        *,
        settings: LinearSweepVoltammetrySettings,
        on_data: EmStatDataCallback | None = None,
        on_measurement_starting: EmStatMeasurementStartingCallback | None = None,
        stream_path: str | Path | None = None,
    ) -> Measurement:
        """Perform one linear-sweep-voltammetry measurement."""

        return self._measure(
            method=self._build_linear_sweep_voltammetry_method(settings),
            measurement_type="linear_sweep_voltammetry",
            settings=settings,
            on_data=on_data,
            on_measurement_starting=on_measurement_starting,
            stream_path=stream_path,
        )

    def estimate_linear_sweep_voltammetry_duration_s(
        self, *, settings: LinearSweepVoltammetrySettings
    ) -> float:
        """Ask the instrument to estimate total linear-sweep duration."""

        duration_s = self._estimate_duration(
            method=self._build_linear_sweep_voltammetry_method(settings),
            measurement_type="linear_sweep_voltammetry",
        )
        # PyPalmSens 1.10.1 does not include Mixed Mode pretreatment time in
        # the duration returned for repeated sweeps. Ordinary LSV already does.
        if settings.sweep_count > 1:
            duration_s += settings.equilibration_time_s
        return duration_s

    def abort_measurement(self) -> bool:
        """Request abortion of the active measurement.

        Returns False if no measurement is currently active. A successful
        request causes the blocking measurement method to raise
        MeasurementAbortedError after PalmSens finishes aborting.
        """

        with self._state_lock:
            manager = self._active_manager

            if manager is None or self._abort_requested:
                return False

            self._abort_requested = True
            logger.warning("Aborting active EmStat4 measurement")

            try:
                manager.abort()
            except Exception:
                self._abort_requested = False
                raise

            return True

    def _estimate_duration(
        self, *, method: MethodType, measurement_type: EmStatMeasurementType
    ) -> float:
        """Estimate through the persistent manager or a temporary connection."""

        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError("Another EmStat4 operation is already in progress")

        measurement_label = measurement_type.replace("_", " ")

        try:
            with self._state_lock:
                persistent_manager = self._persistent_manager

            if persistent_manager is not None:
                logger.info(
                    "Estimating %s duration: instrument=%s, connection=persistent",
                    measurement_label,
                    persistent_manager.instrument.name,
                )
                return self._estimate_duration_with_manager(
                    manager=persistent_manager,
                    method=method,
                    measurement_type=measurement_type,
                )

            connection_started_at = time.perf_counter()
            instrument = self._find_instrument()

            logger.info(
                "Estimating %s duration: instrument=%s, connection=temporary",
                measurement_label,
                instrument.name,
            )

            with ps.connect(instrument) as manager:
                logger.info(
                    "Temporary EmStat4 connection for duration estimation opened "
                    "in %.3f s",
                    time.perf_counter() - connection_started_at,
                )
                return self._estimate_duration_with_manager(
                    manager=manager, method=method, measurement_type=measurement_type
                )
        finally:
            self._operation_lock.release()

    @staticmethod
    def _estimate_duration_with_manager(
        *,
        manager: ps.InstrumentManager,
        method: MethodType,
        measurement_type: EmStatMeasurementType,
    ) -> float:
        duration_s = float(manager.get_estimated_duration(method))
        require_non_negative_finite_number(duration_s, name="estimated_duration_s")

        logger.info(
            "Estimated %s duration: %.3f s",
            measurement_type.replace("_", " "),
            duration_s,
        )
        return duration_s

    def _measure(
        self,
        *,
        method: MethodType,
        measurement_type: EmStatMeasurementType,
        settings: object,
        on_data: EmStatDataCallback | None,
        on_measurement_starting: EmStatMeasurementStartingCallback | None,
        stream_path: str | Path | None,
    ) -> Measurement:
        """Run the shared callback and abort lifecycle.

        Reuse an explicitly opened persistent connection when present. Otherwise,
        open and close a temporary connection for this measurement.
        """

        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError("Another EmStat4 operation is already in progress")

        measurement_label = measurement_type.replace("_", " ")

        try:
            with self._state_lock:
                self._abort_requested = False
                persistent_manager = self._persistent_manager

            if persistent_manager is not None:
                logger.info(
                    "Starting %s: instrument=%s, connection=persistent, settings=%r",
                    measurement_label,
                    persistent_manager.instrument.name,
                    settings,
                )

                return self._measure_with_manager(
                    manager=persistent_manager,
                    method=method,
                    measurement_type=measurement_type,
                    on_data=on_data,
                    on_measurement_starting=on_measurement_starting,
                    stream_path=stream_path,
                    persistent=True,
                )

            connection_started_at = time.perf_counter()
            instrument = self._find_instrument()

            logger.info(
                "Starting %s: instrument=%s, connection=temporary, settings=%r",
                measurement_label,
                instrument.name,
                settings,
            )

            with ps.connect(instrument) as manager:
                logger.info(
                    "Temporary EmStat4 connection opened in %.3f s",
                    time.perf_counter() - connection_started_at,
                )

                return self._measure_with_manager(
                    manager=manager,
                    method=method,
                    measurement_type=measurement_type,
                    on_data=on_data,
                    on_measurement_starting=on_measurement_starting,
                    stream_path=stream_path,
                    persistent=False,
                )
        finally:
            self._operation_lock.release()

    def _measure_with_manager(
        self,
        *,
        manager: ps.InstrumentManager,
        method: MethodType,
        measurement_type: EmStatMeasurementType,
        on_data: EmStatDataCallback | None,
        on_measurement_starting: EmStatMeasurementStartingCallback | None,
        stream_path: str | Path | None,
        persistent: bool,
    ) -> Measurement:
        measurement_label = measurement_type.replace("_", " ")
        measurement: Measurement | None = None
        measurement_error: Exception | None = None

        with self._state_lock:
            self._active_manager = manager

        try:
            if on_measurement_starting is not None:
                on_measurement_starting()

            measurement = manager.measure(
                method, callback=self._make_callback(on_data), stream=stream_path
            )
        except Exception as error:  # noqa: BLE001 - preserve PalmSens abort result
            measurement_error = error
        finally:
            with self._state_lock:
                abort_requested = self._abort_requested
                self._abort_requested = False

                if self._active_manager is manager:
                    self._active_manager = None

        if abort_requested:
            logger.info("%s aborted", measurement_label.capitalize())
            raise MeasurementAbortedError(
                measurement, measurement_type=measurement_type
            ) from measurement_error

        if measurement_error is not None:
            if persistent:
                self._discard_persistent_manager(manager)

            raise measurement_error

        if measurement is None:
            if persistent:
                self._discard_persistent_manager(manager)

            raise RuntimeError("PalmSens returned no measurement")

        if not self._measurement_contains_data(measurement):
            if persistent:
                self._discard_persistent_manager(manager)

            raise ConnectionError(
                f"{measurement_label.capitalize()} ended without measurement "
                "data. The instrument connection was lost or PalmSens could "
                "not parse the measurement response. Reconnect the EmStat4 "
                "before retrying."
            )

        logger.info("%s completed", measurement_label.capitalize())
        return measurement

    @staticmethod
    def _measurement_contains_data(measurement: Measurement) -> bool:
        """Return whether PalmSens produced at least one complete data point.

        PyPalmSens 1.10.1 can log a communication/parsing error from an asyncio
        callback yet still return an empty ``Measurement`` to the synchronous
        caller. Treating that object as successful would save an empty session
        and leave a disposed persistent manager available for the next command.
        """

        return any(
            bool(curve.x_array.to_list()) and bool(curve.y_array.to_list())
            for curve in measurement.curves
        )

    def _discard_persistent_manager(self, manager: ps.InstrumentManager) -> None:
        """Discard a persistent manager after an unexpected measurement failure."""

        with self._state_lock:
            if self._persistent_manager is not manager:
                return

            self._persistent_manager = None

        try:
            manager.disconnect()
        except Exception as error:  # noqa: BLE001 - PalmSens/.NET exception boundary
            # A PalmSens communication failure may already have disposed the
            # underlying semaphore and connection. The manager has still been
            # removed from application state, so this is cleanup information,
            # not a second measurement failure.
            logger.warning(
                "Persistent EmStat4 manager was already closed or could not be "
                "closed after measurement error: %s",
                error,
            )
        else:
            logger.warning(
                "Persistent EmStat4 connection closed after measurement error"
            )

    def _build_chronoamperometry_method(
        self, settings: ChronoamperometrySettings
    ) -> ps.ChronoAmperometry:
        method = ps.ChronoAmperometry(
            interval_time=settings.interval_time_s,
            potential=settings.potential_v,
            run_time=settings.run_time_s,
            equilibration_time=settings.equilibration_time_s,
        )
        self._configure_method(method)
        return method

    def _build_cyclic_voltammetry_method(
        self, settings: CyclicVoltammetrySettings
    ) -> ps.CyclicVoltammetry:
        method = ps.CyclicVoltammetry(
            equilibration_time=settings.equilibration_time_s,
            begin_potential=settings.begin_potential_v,
            vertex1_potential=settings.vertex1_potential_v,
            vertex2_potential=settings.vertex2_potential_v,
            step_potential=settings.step_potential_v,
            scanrate=settings.scan_rate_v_per_s,
            n_scans=settings.scan_count,
        )
        self._configure_method(method)
        return method

    def _build_linear_sweep_voltammetry_method(
        self, settings: LinearSweepVoltammetrySettings
    ) -> ps.LinearSweepVoltammetry | ps.MixedMode:
        if settings.sweep_count == 1:
            method = ps.LinearSweepVoltammetry(
                equilibration_time=settings.equilibration_time_s,
                begin_potential=settings.begin_potential_v,
                end_potential=settings.end_potential_v,
                step_potential=settings.step_potential_v,
                scanrate=settings.scan_rate_v_per_s,
            )
        else:
            method = ps.MixedMode(
                interval_time=settings.sampling_interval_s,
                cycles=settings.sweep_count,
                stages=[
                    ps.stages.SweepE(
                        begin_potential=settings.begin_potential_v,
                        end_potential=settings.end_potential_v,
                        step_potential=settings.step_potential_v,
                        scanrate=settings.scan_rate_v_per_s,
                    )
                ],
            )
            # MixedMode owns equilibration through its Pretreatment settings;
            # ``equilibration_time`` is not a MixedMode constructor argument.
            method.pretreatment.conditioning_potential = settings.begin_potential_v
            method.pretreatment.conditioning_time = settings.equilibration_time_s
        self._configure_method(method)
        return method

    def _configure_method(
        self,
        method: (
            ps.ChronoAmperometry
            | ps.CyclicVoltammetry
            | ps.LinearSweepVoltammetry
            | ps.MixedMode
        ),
    ) -> None:
        method.general.power_frequency = self._power_frequency_hz

        # Preserve raw current values. Application analysis owns any filtering.
        method.data_processing.smooth_level = -1

    @staticmethod
    def _make_callback(on_data: EmStatDataCallback | None) -> _PalmSensCallback | None:
        if on_data is None:
            return None

        def callback(data: CallbackData) -> None:
            x_values = tuple(float(value) for value in data.x_array[data.start :])
            currents_ua = tuple(float(value) for value in data.y_array[data.start :])

            if x_values:
                on_data(
                    EmStatDataBatch(
                        curve_id=int(data.id),
                        x_values=x_values,
                        currents_ua=currents_ua,
                    )
                )

        return callback

    @staticmethod
    def _find_instrument() -> ps.Instrument:
        instruments = [
            instrument
            for instrument in ps.discover()
            if instrument.name.startswith("EmStat4")
        ]

        if not instruments:
            raise RuntimeError("No EmStat4 instrument was found")

        if len(instruments) > 1:
            names = ", ".join(instrument.name for instrument in instruments)
            raise RuntimeError(f"Multiple EmStat4 instruments were found: {names}")

        return instruments[0]
