# gui/runze_controller.py

import logging
import threading
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Literal

from fluidics.runze_fluidics import (
    FlowPhaseHooks,
    RunzeFluidics,
    StoppedFlowHooks,
    WaterDistribution,
)
from fluidics.runze_plumbing import RunzePlumbingConfig, SelectorPortConfig
from runze.bus import RunzeBus
from runze.sv07m import SV07M, SV07MStatus
from runze.sy01b import SY01B, SY01BFlowRateOptions, SY01BFluidPort, SY01BStatus
from runze.types import RunzeDeviceTarget

logger = logging.getLogger(__name__)

SyringePreparationState = Literal["ready", "reset_required"]
SelectorPreparationState = Literal["ready", "home_required"]
SequenceControlState = Literal["unavailable", "stop", "cancel_stop", "resume"]


class SequenceStoppedError(RuntimeError):
    """Raised when a paused multi-step sequence is discarded."""


class OperationStoppedError(RuntimeError):
    """Raised when an active Runze operation is stopped by emergency stop."""


@dataclass(frozen=True)
class RunzeSnapshot:
    syringe: SY01BStatus
    selector: SV07MStatus
    syringe_state: SyringePreparationState
    selector_state: SelectorPreparationState


class RunzeController:
    def __init__(self, *, plumbing: RunzePlumbingConfig | None = None) -> None:
        self._plumbing = plumbing
        self._fluidics: RunzeFluidics | None = None

        self._bus: RunzeBus | None = None
        self._syringe: SY01B | None = None
        self._selector: SV07M | None = None

        self._state_lock = threading.RLock()
        self._active_operation: str | None = None
        self._active_target: RunzeDeviceTarget | None = None
        self._active_sequence_stoppable = False

        self._stop_requested = threading.Event()
        self._sequence_condition = threading.Condition(self._state_lock)
        self._sequence_pause_requested = False
        self._sequence_paused = False
        self._sequence_discard_requested = False

        self._syringe_state: SyringePreparationState | None = None
        self._selector_state: SelectorPreparationState | None = None
        self._last_snapshot: RunzeSnapshot | None = None
        self._before_physical_operation: Callable[[str], None] | None = None
        self._step_observer: Callable[[str, str, str], None] | None = None

    def set_step_observer(self, callback: Callable[[str, str, str], None] | None):
        self._step_observer = callback

    def _notify_step(self, name, target, status):
        if self._step_observer is not None:
            try:
                self._step_observer(name, target, status)
            except Exception:
                logger.exception("Protocol display observer failed")

    def set_before_physical_operation(
        self, callback: Callable[[str], None] | None
    ) -> None:
        """Set the observer called after preflight and before device movement."""

        with self._state_lock:
            self._before_physical_operation = callback

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._bus is not None and self._bus.connected

    @property
    def active_operation(self) -> str | None:
        with self._state_lock:
            return self._active_operation

    @property
    def sequence_control_state(self) -> SequenceControlState:
        with self._state_lock:
            if self._active_operation is None or not self._active_sequence_stoppable:
                return "unavailable"

            if self._sequence_discard_requested:
                return "unavailable"

            if self._sequence_paused:
                return "resume"

            if self._sequence_pause_requested:
                return "cancel_stop"

            return "stop"

    @property
    def active_target(self) -> RunzeDeviceTarget | None:
        with self._state_lock:
            return self._active_target

    @property
    def busy(self) -> bool:
        with self._state_lock:
            return self._active_operation is not None

    @property
    def syringe_state(self) -> SyringePreparationState | None:
        with self._state_lock:
            return self._syringe_state

    @property
    def syringe_ready(self) -> bool:
        with self._state_lock:
            return self._syringe_state == "ready"

    @property
    def selector_state(self) -> SelectorPreparationState | None:
        with self._state_lock:
            return self._selector_state

    @property
    def selector_ready(self) -> bool:
        with self._state_lock:
            return self._selector_state == "ready"

    @property
    def fluidics_available(self) -> bool:
        with self._state_lock:
            return self._fluidics is not None

    @property
    def selector_ports(self) -> tuple[SelectorPortConfig, ...]:
        if self._plumbing is None:
            return ()

        return self._plumbing.selector_ports

    @property
    def plumbing(self) -> RunzePlumbingConfig | None:
        """Return the immutable physical plumbing configuration, if provided."""

        return self._plumbing

    @property
    def liquid_source_ports(self) -> tuple[SelectorPortConfig, ...]:
        if self._plumbing is None:
            return ()

        return self._plumbing.liquid_sources

    @property
    def destination_ports(self) -> tuple[SelectorPortConfig, ...]:
        if self._plumbing is None:
            return ()

        return self._plumbing.destinations

    @property
    def status_known(self) -> bool:
        with self._state_lock:
            return self._last_snapshot is not None

    @property
    def last_snapshot(self) -> RunzeSnapshot | None:
        with self._state_lock:
            return self._last_snapshot

    def connect(self, *, port: str, trace: bool = False) -> RunzeSnapshot:
        logger.info("Connecting Runze devices on %s", port)

        with self._state_lock:
            if self.connected:
                raise RuntimeError("Runze devices are already connected")

            bus = RunzeBus(port=port, trace=trace)

            try:
                syringe = SY01B(bus, address=0x01)
                selector = SV07M(bus, address=0x00)

                self._bus = bus
                self._syringe = syringe
                self._selector = selector

                if self._plumbing is not None:
                    self._fluidics = RunzeFluidics(
                        syringe=syringe,
                        selector=selector,
                        plumbing=self._plumbing,
                        run_step=self._run_sequence_step,
                    )

                initial_position = syringe.query_syringe_position_or_none()

                if initial_position is None:
                    self._syringe_state = "reset_required"

                    logger.warning(
                        "SY01B syringe position is unknown; reset is required"
                    )
                else:
                    logger.info(
                        "Synchronizing SY01B syringe position %s", initial_position
                    )

                    syringe.synchronize_syringe_position(wait=True)
                    self._syringe_state = "ready"

                snapshot = self._read_status(syringe, selector)

                logger.info("Runze devices connected on %s", port)

                return snapshot

            except Exception:
                try:
                    bus.close()
                except Exception:
                    logger.exception(
                        "Failed to close Runze bus after connection failure"
                    )
                finally:
                    self._bus = None
                    self._syringe = None
                    self._selector = None
                    self._fluidics = None

                    self._syringe_state = None
                    self._selector_state = None
                    self._last_snapshot = None

                raise

    def disconnect(self) -> None:
        logger.info("Disconnecting Runze devices")

        with self._state_lock:
            if self._active_operation is not None:
                raise RuntimeError(
                    f"Cannot disconnect while {self._active_operation} is in progress"
                )

            if self._bus is not None:
                try:
                    self._bus.close()
                except Exception:
                    self._last_snapshot = None
                    raise

            self._bus = None
            self._syringe = None
            self._selector = None
            self._fluidics = None

            self._syringe_state = None
            self._selector_state = None
            self._last_snapshot = None

        logger.info("Runze devices disconnected")

    def refresh_status(self) -> RunzeSnapshot:
        operation = "refreshing status"

        syringe, selector = self._begin_operation(
            operation=operation, target=None, requires_known_status=False
        )

        try:
            snapshot = self._read_status(syringe, selector)

            logger.info("Operation completed: %s", operation)

            return snapshot
        except Exception:
            self._invalidate_status()
            raise
        finally:
            self._end_operation()

    # Device methods ---

    def syringe_rpm_options_from_ml_per_min(
        self, flow_ml_per_min: float
    ) -> SY01BFlowRateOptions:
        with self._state_lock:
            syringe, _ = self._require_devices()
            return syringe.rpm_options_from_ml_per_min(flow_ml_per_min)

    def syringe_flow_rate_ml_per_min_from_rpm(self, rpm: int) -> float:
        with self._state_lock:
            syringe, _ = self._require_devices()
            return syringe.flow_rate_ml_per_min_from_rpm(rpm)

    @property
    def syringe_capacity_ml(self) -> float:
        """Configured full-scale SY01B syringe capacity."""

        with self._state_lock:
            syringe, _ = self._require_devices()
            return syringe.syringe_volume_ml

    def syringe_estimated_move_duration_s_from_steps(
        self, steps: int, *, rpm: int
    ) -> float:
        with self._state_lock:
            syringe, _ = self._require_devices()
            return syringe.estimated_move_duration_s_from_steps(steps, rpm=rpm)

    def syringe_estimated_move_duration_s_from_volume_ml(
        self, volume_ml: float, *, rpm: int
    ) -> float:
        with self._state_lock:
            syringe, _ = self._require_devices()
            return syringe.estimated_move_duration_s_from_volume_ml(volume_ml, rpm=rpm)

    def aspirate(
        self, *, volume_ml: float, rpm: int, source: SY01BFluidPort
    ) -> RunzeSnapshot:
        return self._run_operation(
            operation=f"aspirating {volume_ml:g} mL from {source} at {rpm} RPM",
            target="sy01b",
            action=lambda syringe, _: syringe.aspirate_ml(
                volume_ml=volume_ml, rpm=rpm, source=source
            ),
            stopped_message="SY01B aspiration was stopped by the user",
            requires_syringe_ready=True,
        )

    def dispense(
        self, *, volume_ml: float, rpm: int, destination: SY01BFluidPort
    ) -> RunzeSnapshot:
        return self._run_operation(
            operation=f"dispensing {volume_ml:g} mL to {destination} at {rpm} RPM",
            target="sy01b",
            action=lambda syringe, _: syringe.dispense_ml(
                volume_ml=volume_ml, rpm=rpm, destination=destination
            ),
            stopped_message="SY01B dispensing was stopped by the user",
            requires_syringe_ready=True,
        )

    def empty_syringe(self, *, rpm: int, destination: SY01BFluidPort) -> RunzeSnapshot:
        return self._run_operation(
            operation=f"emptying syringe to {destination}",
            target="sy01b",
            action=lambda syringe, _: syringe.empty_syringe(
                rpm=rpm, destination=destination
            ),
            stopped_message=f"Emptying the SY01B syringe to the {destination} "
            "was stopped by the user",
            requires_syringe_ready=True,
        )

    def move_selector_to_port(self, *, port: int) -> RunzeSnapshot:
        return self._run_operation(
            operation=f"moving selector to port {port}",
            target="sv07m",
            action=lambda _, selector: selector.ensure_port(port),
            stopped_message="SV07M movement was stopped by the user",
            requires_selector_ready=True,
        )

    def emergency_stop_sy01b(self) -> bool:
        return self._request_stop(
            target="sy01b", action=lambda syringe, _: syringe.emergency_stop()
        )

    def emergency_stop_sv07m(self) -> bool:
        return self._request_stop(
            target="sv07m", action=lambda _, selector: selector.emergency_stop()
        )

    def emergency_stop_active(self) -> bool:
        with self._state_lock:
            target = self._active_target

            if target == "sy01b":
                return self.emergency_stop_sy01b()

            if target == "sv07m":
                return self.emergency_stop_sv07m()

            raise RuntimeError("No Runze device is currently moving")

    def request_sequence_stop(self) -> bool:
        """Request a running sequence to pause at its next step boundary."""
        with self._sequence_condition:
            if self._active_operation is None or not self._active_sequence_stoppable:
                return False

            if (
                self._sequence_discard_requested
                or self._sequence_pause_requested
                or self._sequence_paused
            ):
                return False

            operation = self._active_operation
            self._sequence_pause_requested = True

        logger.warning("Sequence pause requested during operation %s", operation)

        return True

    def cancel_sequence_stop(self) -> bool:
        """Cancel a pending pause request before the sequence has paused."""
        with self._sequence_condition:
            if (
                self._active_operation is None
                or not self._active_sequence_stoppable
                or self._sequence_paused
                or not self._sequence_pause_requested
            ):
                return False

            operation = self._active_operation
            self._sequence_pause_requested = False

        logger.info("Sequence pause request cancelled during %s", operation)

        return True

    def resume_sequence(self) -> bool:
        """Resume a sequence which is waiting at a step boundary."""
        with self._sequence_condition:
            if (
                self._active_operation is None
                or not self._active_sequence_stoppable
                or not self._sequence_paused
                or not self._sequence_pause_requested
            ):
                return False

            operation = self._active_operation
            self._sequence_pause_requested = False
            self._sequence_paused = False
            self._sequence_condition.notify_all()

        logger.info("Sequence resume requested during %s", operation)

        return True

    def discard_paused_sequence(self) -> bool:
        """Discard a sequence which is waiting at a step boundary."""
        with self._sequence_condition:
            if (
                self._active_operation is None
                or not self._active_sequence_stoppable
                or not self._sequence_paused
            ):
                return False

            operation = self._active_operation
            self._sequence_discard_requested = True
            self._sequence_pause_requested = False
            self._sequence_paused = False
            self._sequence_condition.notify_all()

        logger.warning("Paused sequence discard requested during %s", operation)

        return True

    def reset_syringe(self) -> RunzeSnapshot:
        def reset_and_mark_ready(syringe: SY01B, _: SV07M) -> None:
            syringe.reset_syringe(wait=True)

            with self._state_lock:
                self._syringe_state = "ready"

        return self._run_operation(
            operation="resetting syringe",
            target="sy01b",
            action=reset_and_mark_ready,
            stopped_message="SY01B syringe reset was stopped by the user",
            requires_syringe_ready=False,
        )

    def reset_selector_to_home(self) -> RunzeSnapshot:
        return self._run_operation(
            operation="resetting selector to home",
            target="sv07m",
            action=lambda _, selector: selector.reset_to_home(wait=True),
            stopped_message="SV07M reset to home was stopped by the user",
            requires_selector_ready=False,
        )

    # Device sequence methods

    def prime_all_liquid_source_lines(self, *, rpm: int) -> RunzeSnapshot:
        return self._run_sequence_operation(
            operation="priming all liquid source lines",
            action=lambda fluidics: fluidics.prime_all_liquid_source_lines(rpm=rpm),
            emergency_stopped_message=(
                "Runze source-line priming was stopped by emergency stop"
            ),
        )

    def prime_selected_liquid_source_lines(
        self, *, source_ports: Collection[int], rpm: int, prime_water: bool = True
    ) -> RunzeSnapshot:
        selected_ports = tuple(source_ports)

        return self._run_sequence_operation(
            operation="priming selected liquid source lines",
            action=lambda fluidics: fluidics.prime_selected_liquid_source_lines(
                source_ports=selected_ports, rpm=rpm, prime_water=prime_water
            ),
            emergency_stopped_message=(
                "Runze selected source-line priming was stopped by emergency stop"
            ),
        )

    def flush_system(
        self, *, volume_ml: float, rpm: int, cycles: int = 1
    ) -> RunzeSnapshot:
        return self._run_sequence_operation(
            operation="flushing system with water and purging with air",
            action=lambda fluidics: fluidics.flush_system(
                volume_ml=volume_ml, rpm=rpm, cycles=cycles
            ),
            emergency_stopped_message=(
                "Runze system flush was stopped by emergency stop"
            ),
        )

    def rinse_mixing_vessel(
        self, *, rinse_volume_ml: float, rpm: int, cycles: int = 2
    ) -> RunzeSnapshot:
        return self._run_sequence_operation(
            operation="rinsing mixing vessel with water",
            action=lambda fluidics: fluidics.rinse_mixing_vessel(
                rinse_volume_ml=rinse_volume_ml, rpm=rpm, cycles=cycles
            ),
            emergency_stopped_message=(
                "Runze mixing-vessel rinse was stopped by emergency stop"
            ),
        )

    def rinse_flow_cell_with_water(
        self, *, rinse_through_volume_ml: float, handling_rpm: int, rinsing_rpm: int
    ) -> RunzeSnapshot:
        return self._run_sequence_operation(
            operation="rinsing flow-cell with water",
            action=lambda fluidics: fluidics.rinse_flow_cell_with_water(
                rinse_through_volume_ml=rinse_through_volume_ml,
                handling_rpm=handling_rpm,
                rinsing_rpm=rinsing_rpm,
            ),
            emergency_stopped_message=(
                "Runze flow-cell rinse was stopped by emergency stop"
            ),
        )

    def recover_source_lines_and_clear_flow_cell(
        self, *, source_ports: Collection[int], handling_rpm: int, approach_rpm: int
    ) -> RunzeSnapshot:
        selected_ports = tuple(source_ports)
        return self._run_sequence_operation(
            operation="recovering source lines and clearing the flow-cell route",
            action=lambda fluidics: fluidics.recover_source_lines_and_clear_flow_cell(
                source_ports=selected_ports,
                handling_rpm=handling_rpm,
                approach_rpm=approach_rpm,
            ),
            emergency_stopped_message=(
                "Runze source recovery and flow-cell clearing was stopped by "
                "emergency stop"
            ),
        )

    def rinse_source_lines_with_water(
        self, *, source_ports: Collection[int], additional_volume_ml: float, rpm: int
    ) -> RunzeSnapshot:
        selected_ports = tuple(source_ports)
        return self._run_sequence_operation(
            operation="rinsing source lines with water",
            action=lambda fluidics: fluidics.rinse_source_lines_with_water(
                source_ports=selected_ports,
                additional_volume_ml=additional_volume_ml,
                rpm=rpm,
            ),
            emergency_stopped_message=(
                "Runze source-line water rinse was stopped by emergency stop"
            ),
        )

    def air_empty_source_lines(
        self, *, source_ports: Collection[int], rpm: int
    ) -> RunzeSnapshot:
        selected_ports = tuple(source_ports)
        return self._run_sequence_operation(
            operation="air-emptying all maintained source lines",
            action=lambda fluidics: fluidics.air_empty_source_lines(
                source_ports=selected_ports, rpm=rpm
            ),
            emergency_stopped_message=(
                "Runze source-line air emptying was stopped by emergency stop"
            ),
        )

    def flush_working_destination_routes(
        self, *, rinse_volume_ml: float, rpm: int, flush_cycles: int = 1
    ) -> RunzeSnapshot:
        return self._run_sequence_operation(
            operation="flushing working destination routes with water and air",
            action=lambda fluidics: fluidics.flush_working_destination_routes(
                rinse_volume_ml=rinse_volume_ml, rpm=rpm, flush_cycles=flush_cycles
            ),
            emergency_stopped_message=(
                "Runze working-route flush was stopped by emergency stop"
            ),
        )

    def transfer_liquid_to_route_with_single_aspiration(
        self,
        *,
        volume_ml: float,
        rpm: int,
        source_port: int,
        destination_port: int,
        inlet_aspiration_rpm: int | None = None,
    ) -> RunzeSnapshot:
        return self._run_sequence_operation(
            operation="transferring liquid with one source aspiration",
            action=lambda fluidics: (
                fluidics.transfer_liquid_to_route_with_single_aspiration(
                    source_port=source_port,
                    destination_port=destination_port,
                    volume_ml=volume_ml,
                    rpm=rpm,
                    inlet_aspiration_rpm=inlet_aspiration_rpm,
                )
            ),
            emergency_stopped_message="Liquid transfer was stopped by emergency stop",
        )

    def transfer_liquid_to_route_with_common_line_strokes(
        self,
        *,
        volume_ml: float,
        rpm: int,
        source_port: int,
        destination_port: int,
        inlet_aspiration_rpm: int | None = None,
    ) -> RunzeSnapshot:
        return self._run_sequence_operation(
            operation="transferring liquid with common-line strokes",
            action=lambda fluidics: (
                fluidics.transfer_liquid_to_route_with_common_line_strokes(
                    source_port=source_port,
                    destination_port=destination_port,
                    volume_ml=volume_ml,
                    rpm=rpm,
                    inlet_aspiration_rpm=inlet_aspiration_rpm,
                )
            ),
            emergency_stopped_message=(
                "Common-line-stroke liquid transfer was stopped by emergency stop"
            ),
        )

    def prepare_mixture_with_single_aspiration_transfers(
        self,
        *,
        source_fractions: Mapping[str, float],
        total_volume_ml: float,
        rpm: int,
        inlet_aspiration_rpm: int | None = None,
        water_distribution: WaterDistribution = "single",
    ) -> RunzeSnapshot:
        fractions = dict(source_fractions)

        return self._run_sequence_operation(
            operation="preparing mixture with single-aspiration transfers",
            action=lambda fluidics: (
                fluidics.prepare_mixture_with_single_aspiration_transfers(
                    source_fractions=fractions,
                    total_volume_ml=total_volume_ml,
                    rpm=rpm,
                    inlet_aspiration_rpm=inlet_aspiration_rpm,
                    water_distribution=water_distribution,
                )
            ),
            emergency_stopped_message=(
                "Mixture preparation was stopped by emergency stop"
            ),
        )

    def prepare_mixture_with_common_line_stroke_transfers(
        self,
        *,
        source_fractions: Mapping[str, float],
        total_volume_ml: float,
        rpm: int,
        inlet_aspiration_rpm: int | None = None,
        water_distribution: WaterDistribution = "single",
    ) -> RunzeSnapshot:
        fractions = dict(source_fractions)

        return self._run_sequence_operation(
            operation="preparing mixture with common-line-stroke transfers",
            action=lambda fluidics: (
                fluidics.prepare_mixture_with_common_line_stroke_transfers(
                    source_fractions=fractions,
                    total_volume_ml=total_volume_ml,
                    rpm=rpm,
                    inlet_aspiration_rpm=inlet_aspiration_rpm,
                    water_distribution=water_distribution,
                )
            ),
            emergency_stopped_message=(
                "Common-line-stroke mixture preparation was stopped by emergency stop"
            ),
        )

    def fill_flow_cell_from_mixing_vessel(
        self,
        *,
        volume_ml: float,
        rpm: int,
        flow_rpm: int,
        approach_rpm: int | None = None,
        flow_hooks: FlowPhaseHooks | None = None,
    ) -> RunzeSnapshot:
        return self._run_sequence_operation(
            operation="filling flow-cell from mixing vessel",
            action=lambda fluidics: fluidics.fill_flow_cell_from_mixing_vessel(
                volume_ml=volume_ml,
                rpm=rpm,
                flow_rpm=flow_rpm,
                approach_rpm=approach_rpm,
                flow_hooks=flow_hooks,
            ),
            emergency_stopped_message=(
                "Flow-cell filling was stopped by emergency stop"
            ),
        )

    def run_stopped_flow_series_from_mixing_vessel(
        self,
        *,
        measurement_count: int,
        between_measurement_displacement_volume_ml: float | None,
        rinse_through_volume_ml: float,
        rpm: int,
        approach_rpm: int,
        hooks: StoppedFlowHooks,
    ) -> RunzeSnapshot:
        return self._run_sequence_operation(
            operation="running stopped-flow series from mixing vessel",
            action=lambda fluidics: fluidics.run_stopped_flow_series_from_mixing_vessel(
                measurement_count=measurement_count,
                between_measurement_displacement_volume_ml=(
                    between_measurement_displacement_volume_ml
                ),
                rinse_through_volume_ml=rinse_through_volume_ml,
                rpm=rpm,
                approach_rpm=approach_rpm,
                hooks=hooks,
            ),
            emergency_stopped_message=(
                "Stopped-flow series was stopped by emergency stop"
            ),
        )

    def pass_mixing_plug_through_flow_cell(
        self,
        *,
        volume_ml: float,
        rpm: int,
        flow_rpm: int,
        approach_rpm: int | None = None,
        flow_hooks: FlowPhaseHooks | None = None,
    ) -> RunzeSnapshot:
        return self._run_sequence_operation(
            operation="passing mixing-vessel plug through flow-cell",
            action=lambda fluidics: fluidics.pass_mixing_plug_through_flow_cell(
                volume_ml=volume_ml,
                rpm=rpm,
                flow_rpm=flow_rpm,
                approach_rpm=approach_rpm,
                flow_hooks=flow_hooks,
            ),
            emergency_stopped_message=(
                "Flow-cell plug run was stopped by emergency stop"
            ),
        )

    def drain_mixing_vessel_to_outlet(
        self, *, expected_vessel_volume_ml: float, rpm: int
    ) -> RunzeSnapshot:
        return self._run_sequence_operation(
            operation="draining mixing vessel to outlet",
            action=lambda fluidics: fluidics.drain_mixing_vessel_to_outlet(
                expected_vessel_volume_ml=expected_vessel_volume_ml, rpm=rpm
            ),
            emergency_stopped_message=(
                "Mixing-vessel drain was stopped by emergency stop"
            ),
        )

    # Private helpers ---

    def _request_stop(
        self, *, target: RunzeDeviceTarget, action: Callable[[SY01B, SV07M], object]
    ) -> bool:
        with self._state_lock:
            syringe, selector = self._require_devices()

            if self._active_operation is None:
                raise RuntimeError("No operation is currently in progress")

            if self._active_target != target:
                raise RuntimeError(
                    f"Cannot stop {target}; active operation "
                    f"{self._active_operation!r} targets "
                    f"{self._active_target or 'neither device'}"
                )

            if self._stop_requested.is_set():
                return False

            self._stop_requested.set()

            logger.warning(
                "Stop requested for %s during operation %s",
                target,
                self._active_operation,
            )

            try:
                action(syringe, selector)
            except Exception:
                self._stop_requested.clear()
                raise

            logger.warning("Stop command sent to %s", target)

            return True

    def _begin_operation(
        self,
        *,
        operation: str,
        target: RunzeDeviceTarget | None,
        requires_syringe_ready: bool = False,
        requires_selector_ready: bool = False,
        sequence_stoppable: bool = False,
        requires_known_status: bool = True,
    ) -> tuple[SY01B, SV07M]:
        with self._state_lock:
            if self._active_operation is not None:
                raise RuntimeError(
                    f"Cannot start operation {operation}; "
                    f"{self._active_operation} is already in progress"
                )

            syringe, selector = self._require_devices()

            if requires_known_status and self._last_snapshot is None:
                raise RuntimeError(
                    "Runze device status is unknown; refresh status before "
                    "starting an operation"
                )

            if requires_known_status and self._last_snapshot is not None:
                snapshot = self._last_snapshot
                statuses = (
                    snapshot.syringe.motor_status,
                    snapshot.syringe.valve_status,
                    snapshot.selector.motor_status,
                )
                if any(status.name != "OK" for status in statuses):
                    raise RuntimeError(
                        "Hardware is moving or faulted; wait for idle or recover before a new movement"
                    )

            if requires_syringe_ready and self._syringe_state != "ready":
                raise RuntimeError(
                    "SY01B syringe is not ready; syringe reset is required"
                )

            if requires_selector_ready and self._selector_state != "ready":
                raise RuntimeError(
                    "SV07M is not aligned with a valid port; reset to home is required"
                )

            self._stop_requested.clear()
            self._sequence_pause_requested = False
            self._sequence_paused = False
            self._sequence_discard_requested = False

            self._active_operation = operation
            self._active_target = target
            self._active_sequence_stoppable = sequence_stoppable

            logger.info("Starting operation: %s", operation)

            return syringe, selector

    def _end_operation(self) -> None:
        with self._sequence_condition:
            self._active_operation = None
            self._active_target = None
            self._active_sequence_stoppable = False

            self._stop_requested.clear()
            self._sequence_pause_requested = False
            self._sequence_paused = False
            self._sequence_discard_requested = False
            self._sequence_condition.notify_all()

    def _invalidate_status(self) -> None:
        with self._state_lock:
            self._last_snapshot = None

    def _run_operation(
        self,
        *,
        operation: str,
        target: RunzeDeviceTarget,
        action: Callable[[SY01B, SV07M], object],
        stopped_message: str,
        requires_syringe_ready: bool = False,
        requires_selector_ready: bool = False,
    ) -> RunzeSnapshot:
        syringe, selector = self._begin_operation(
            operation=operation,
            target=target,
            requires_syringe_ready=requires_syringe_ready,
            requires_selector_ready=requires_selector_ready,
        )

        try:
            try:
                self._notify_before_physical_operation(operation)
                action(syringe, selector)

                if self._stop_requested.is_set():
                    raise OperationStoppedError(stopped_message)

                snapshot = self._read_status(syringe, selector)

                if self._stop_requested.is_set():
                    raise OperationStoppedError(stopped_message)

                logger.info("Operation completed: %s", operation)

                return snapshot

            except OperationStoppedError:
                self._invalidate_status()
                logger.warning("Operation stopped: %s", operation)
                raise

            except Exception as error:
                self._invalidate_status()

                if self._stop_requested.is_set():
                    logger.warning("Operation stopped: %s", operation)
                    raise OperationStoppedError(stopped_message) from error
                raise

        finally:
            self._end_operation()

    def _wait_at_sequence_boundary_locked(
        self, *, operation: str, boundary: str
    ) -> None:
        """Wait for resume while holding the sequence condition lock."""

        if self._stop_requested.is_set():
            raise OperationStoppedError(f"{operation} was stopped by emergency stop")

        if not self._sequence_pause_requested:
            return

        self._sequence_paused = True
        self._active_target = None
        self._sequence_condition.notify_all()

        logger.warning("Sequence paused during %s %s", operation, boundary)

        while (
            self._sequence_pause_requested
            and not self._sequence_discard_requested
            and not self._stop_requested.is_set()
        ):
            self._sequence_condition.wait()

        self._sequence_paused = False

        if self._stop_requested.is_set():
            raise OperationStoppedError(f"{operation} was stopped by emergency stop")

        if self._sequence_discard_requested:
            raise SequenceStoppedError(
                f"{operation} was discarded while paused {boundary}"
            )

        logger.info("Sequence resumed during %s %s", operation, boundary)

    def _run_sequence_step(
        self, *, name: str, target: RunzeDeviceTarget, action: Callable[[], object]
    ) -> None:
        """Run one physical step, pausing safely at either surrounding boundary."""

        with self._sequence_condition:
            operation = self._active_operation

            if operation is None or not self._active_sequence_stoppable:
                raise RuntimeError("No stoppable Runze sequence is active")

            self._wait_at_sequence_boundary_locked(
                operation=operation, boundary=f"before step: {name}"
            )

            self._active_target = target

        logger.debug(
            "Starting sequence step: %s; operation=%s; target=%s",
            name,
            operation,
            target,
        )

        self._notify_step(name, target, "started")
        try:
            action()

        except Exception as error:
            self._notify_step(name, target, "failed")
            if self._stop_requested.is_set():
                raise OperationStoppedError(
                    f"{operation} was stopped by emergency stop"
                ) from error

            logger.debug(
                "Sequence step failed: %s; operation=%s; target=%s",
                name,
                operation,
                target,
            )
            raise

        finally:
            with self._state_lock:
                self._active_target = None

        if self._stop_requested.is_set():
            self._notify_step(name, target, "aborted")
            raise OperationStoppedError(f"{operation} was stopped by emergency stop")

        self._notify_step(name, target, "completed")
        logger.debug(
            "Sequence step completed: %s; operation=%s; target=%s",
            name,
            operation,
            target,
        )

        with self._sequence_condition:
            self._wait_at_sequence_boundary_locked(
                operation=operation, boundary=f"after step: {name}"
            )

    def _run_sequence_operation(
        self,
        *,
        operation: str,
        action: Callable[[RunzeFluidics], object],
        emergency_stopped_message: str,
        requires_empty_syringe: bool = True,
        requires_syringe_ready: bool = True,
        requires_selector_ready: bool = True,
    ) -> RunzeSnapshot:
        """Run one complete multi-step fluidics sequence."""

        syringe, selector = self._begin_operation(
            operation=operation,
            target=None,
            requires_syringe_ready=requires_syringe_ready,
            requires_selector_ready=requires_selector_ready,
            sequence_stoppable=True,
        )

        try:
            fluidics = self._require_fluidics()

            if requires_empty_syringe:
                self._require_empty_syringe(syringe)

            self._notify_before_physical_operation(operation)
            action(fluidics)

            with self._sequence_condition:
                if self._stop_requested.is_set():
                    raise OperationStoppedError(emergency_stopped_message)

                self._wait_at_sequence_boundary_locked(
                    operation=operation, boundary="after the final step"
                )

                self._active_sequence_stoppable = False

            snapshot = self._read_status(syringe, selector)

            if self._stop_requested.is_set():
                raise OperationStoppedError(emergency_stopped_message)

            logger.info("Operation completed: %s", operation)
            return snapshot

        except SequenceStoppedError:
            self._invalidate_status()
            logger.warning("Sequence discarded: %s", operation)
            raise
        except OperationStoppedError:
            self._invalidate_status()
            logger.warning("Operation stopped: %s", operation)
            raise
        except Exception as error:
            self._invalidate_status()

            if self._stop_requested.is_set():
                logger.warning("Operation stopped: %s", operation)
                raise OperationStoppedError(emergency_stopped_message) from error
            raise

        finally:
            self._end_operation()

    def _notify_before_physical_operation(self, operation: str) -> None:
        with self._state_lock:
            callback = self._before_physical_operation
        if callback is not None:
            callback(operation)

    def _read_status(self, syringe: SY01B, selector: SV07M) -> RunzeSnapshot:
        syringe_status = syringe.read_status()
        selector_status = selector.read_status()

        with self._state_lock:
            if syringe_status.position is None:
                self._syringe_state = "reset_required"

            if selector_status.current_port is None:
                self._selector_state = "home_required"
            else:
                self._selector_state = "ready"

            syringe_state = self._syringe_state
            selector_state = self._selector_state

        if syringe_state is None:
            raise RuntimeError("SY01B preparation state has not been initialized")

        if selector_state is None:
            raise RuntimeError("SV07M preparation state has not been initialized")

        snapshot = RunzeSnapshot(
            syringe=syringe_status,
            selector=selector_status,
            syringe_state=syringe_state,
            selector_state=selector_state,
        )

        with self._state_lock:
            self._last_snapshot = snapshot

        return snapshot

    def _require_devices(self) -> tuple[SY01B, SV07M]:
        if not self.connected or self._syringe is None or self._selector is None:
            raise RuntimeError("Runze devices are not connected")

        return self._syringe, self._selector

    def _require_fluidics(self) -> RunzeFluidics:
        with self._state_lock:
            fluidics = self._fluidics

        if fluidics is None:
            raise RuntimeError("Runze fluidics is not connected")

        return fluidics

    def _require_empty_syringe(self, syringe: SY01B) -> None:
        position = syringe.query_syringe_position()

        if position != 0:
            raise RuntimeError(
                "Syringe must be at position 0 before starting a "
                f"fluidics sequence; current position is {position}"
            )
