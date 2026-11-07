# runze/sy01b.py

import math
from dataclasses import dataclass
from typing import Literal

from common.validation import require_integer_in_range, require_positive_finite_number

from .bus import RunzeBus
from .device import RunzeDevice, RunzeStatus
from .protocol import RawRunzeReply, split_u16_le

_SY01B_FACTORY_PASSWORD = bytes.fromhex("FF EE BB AA")

SY01B_MIN_RESET_STALL_CURRENT = 1
SY01B_MAX_RESET_STALL_CURRENT = 31

SY01B_MIN_DYNAMIC_SPEED_RPM = 1
SY01B_MAX_DYNAMIC_SPEED_RPM = 450

SY01B_MAX_STEPS = 6000
SY01B_STEPS_PER_REV = 400
SY01B_DEFAULT_SYRINGE_VOLUME_ML = 5.0
# An occasional one-step difference in absolute movements observed empirically:
_SY01B_POSITION_TOLERANCE_STEPS = 1
_SY01B_UNKNOWN_POSITION = 0xFFFF

SY01B_DEFAULT_INLET_VALVE_STATE = 1
SY01B_DEFAULT_OUTLET_VALVE_STATE = 3

SY01B_VALVE_STATE_LABELS: dict[int, str] = {1: "IN-C", 2: "IN-OUT", 3: "OUT-C"}

SY01BFluidPort = Literal["inlet", "outlet"]


def syringe_steps_from_volume_ml(volume_ml: float, *, syringe_volume_ml: float) -> int:
    """Convert a positive relative volume to nominal SY01B control steps."""

    require_positive_finite_number(volume_ml, name="volume_ml")
    require_positive_finite_number(syringe_volume_ml, name="syringe_volume_ml")
    if volume_ml > syringe_volume_ml:
        raise ValueError(
            f"volume_ml must not exceed syringe capacity {syringe_volume_ml:g}"
        )
    steps = math.floor(volume_ml * SY01B_MAX_STEPS / syringe_volume_ml + 0.5)
    if steps < 1:
        raise ValueError(f"volume_ml {volume_ml} is too small to move even one step")
    if steps > SY01B_MAX_STEPS:
        raise ValueError(
            f"volume_ml {volume_ml} is too large for step range 1-{SY01B_MAX_STEPS}"
        )
    return steps


def syringe_volume_ml_from_steps(steps: int, *, syringe_volume_ml: float) -> float:
    """Convert a relative SY01B step count to nominal moved volume."""

    require_integer_in_range(steps, name="steps", minimum=0, maximum=SY01B_MAX_STEPS)
    require_positive_finite_number(syringe_volume_ml, name="syringe_volume_ml")
    return steps * syringe_volume_ml / SY01B_MAX_STEPS


@dataclass(frozen=True)
class SY01BFlowRateOptions:
    requested_flow_ml_per_min: float
    lower_rpm: int
    lower_flow_ml_per_min: float
    upper_rpm: int
    upper_flow_ml_per_min: float
    in_range: bool


class UnknownSyringePositionError(RuntimeError):
    """SY01B reports that its syringe position is unknown."""


@dataclass(frozen=True)
class SY01BStatus:
    motor_status: RunzeStatus
    valve_status: RunzeStatus
    valve_state: int
    position: int | None
    volume_ml: float | None


class SY01B(RunzeDevice):
    device_name = "SY01B"

    def __init__(
        self,
        bus: RunzeBus,
        address: int = 0x01,
        *,
        syringe_volume_ml: float = SY01B_DEFAULT_SYRINGE_VOLUME_ML,
        inlet_valve_state: int = SY01B_DEFAULT_INLET_VALVE_STATE,
        outlet_valve_state: int = SY01B_DEFAULT_OUTLET_VALVE_STATE,
    ) -> None:
        super().__init__(bus, address)

        require_positive_finite_number(syringe_volume_ml, name="syringe_volume_ml")
        self._validate_syringe_connection_state(
            inlet_valve_state, name="inlet_valve_state"
        )
        self._validate_syringe_connection_state(
            outlet_valve_state, name="outlet_valve_state"
        )

        self.inlet_valve_state = inlet_valve_state
        self.outlet_valve_state = outlet_valve_state
        self.syringe_volume_ml = float(syringe_volume_ml)

    # Queries / status

    def query_address(self) -> RawRunzeReply:
        """Query device address. Use only when exactly one device is connected."""
        return self._send_common(function=0x20)

    def query_rs485_baud_rate(self) -> int:
        reply = self._send_common(function=0x22)
        return self._decode_baud_rate(reply)

    def query_auto_reset(self) -> bool:
        reply = self._send_common(function=0x2E)

        # Noticed exception: B2 is the setting value, not a status code
        if reply.b2 not in (0x00, 0x01):
            raise RuntimeError(
                f"Unexpected SY01B auto-reset value.\nReply: {reply.raw.hex(' ')}"
            )

        return bool(reply.b2)

    def query_valve_state(self) -> int:
        reply = self._send_common(function=0xAE)
        self._require_status(reply, RunzeStatus.OK)

        state = reply.value_u16
        if not 1 <= state <= 3:
            raise RuntimeError(
                f"Unexpected SY01B MY-3 valve state {state}.\n"
                f"Reply: {reply.raw.hex(' ')}"
            )

        return state

    def query_motor_status(self) -> RunzeStatus:
        reply = self._send_common(function=0x4A)
        return self._status(reply)

    def query_valve_status(self) -> RunzeStatus:
        reply = self._send_common(function=0x4D)
        return self._status(reply)

    def query_syringe_position_or_none(self) -> int | None:
        """Return the syringe position, or None if SY01B reports 0xFFFF."""
        reply = self._send_common(function=0x66)
        self._require_status(reply, RunzeStatus.OK)

        position = reply.value_u16

        if position == _SY01B_UNKNOWN_POSITION:
            return None

        if not 0 <= position <= SY01B_MAX_STEPS:
            raise RuntimeError(
                f"Unexpected SY01B syringe position {position}.\n"
                f"Reply: {reply.raw.hex(' ')}"
            )

        return position

    def query_syringe_position(self) -> int:
        """Return a known syringe position.

        Raise UnknownSyringePositionError when SY01B reports 0xFFFF.
        """
        position = self.query_syringe_position_or_none()

        if position is None:
            raise UnknownSyringePositionError(
                "SY01B syringe position is unknown; syringe reset is required"
            )

        return position

    def query_syringe_volume_ml(self) -> float:
        """Return the nominal syringe content volume from the current position."""
        return self.volume_ml_from_position(self.query_syringe_position())

    def read_status(self) -> SY01BStatus:
        position = self.query_syringe_position_or_none()

        return SY01BStatus(
            motor_status=self.query_motor_status(),
            valve_status=self.query_valve_status(),
            valve_state=self.query_valve_state(),
            position=position,
            volume_ml=(
                None if position is None else self.volume_ml_from_position(position)
            ),
        )

    # Factory / config commands ---

    def set_device_address(self, new_address: int) -> RawRunzeReply:
        self._validate_address(new_address)

        parameters = bytes([new_address, 0x00, 0x00, 0x00])
        reply = self._send_factory(
            function=0x00, password=_SY01B_FACTORY_PASSWORD, parameters=parameters
        )
        self._require_status(reply, RunzeStatus.OK)

        self.address = new_address
        return reply

    def set_reset_stall_current(self, current: int) -> RawRunzeReply:
        if (
            not SY01B_MIN_RESET_STALL_CURRENT
            <= current
            <= SY01B_MAX_RESET_STALL_CURRENT
        ):
            raise ValueError(
                "Reset stall current must be in range "
                f"{SY01B_MIN_RESET_STALL_CURRENT}-{SY01B_MAX_RESET_STALL_CURRENT}"
            )

        parameters = bytes([current, 0x00, 0x00, 0x00])
        reply = self._send_factory(
            function=0x1D, password=_SY01B_FACTORY_PASSWORD, parameters=parameters
        )
        self._require_status(reply, RunzeStatus.OK)
        # Manufacturer states B3 echoes saved value, but experiment doesn't support that
        return reply

    # Volume and flow conversion helpers ---

    @property
    def steps_per_ml(self) -> float:
        return SY01B_MAX_STEPS / self.syringe_volume_ml

    @property
    def ml_per_step(self) -> float:
        return self.syringe_volume_ml / SY01B_MAX_STEPS

    def position_from_volume_ml(self, volume_ml: float) -> int:
        """Convert nominal syringe content volume to absolute position."""
        if not isinstance(volume_ml, (int, float)) or isinstance(volume_ml, bool):
            raise TypeError("volume_ml must be a number")
        if not 0 <= volume_ml <= self.syringe_volume_ml:
            raise ValueError(f"volume_ml must be in range 0-{self.syringe_volume_ml}")

        return math.floor(volume_ml * self.steps_per_ml + 0.5)

    def volume_ml_from_position(self, position: int) -> float:
        """Convert absolute syringe position to nominal syringe content volume."""
        self._validate_syringe_position(position)
        return position * self.ml_per_step

    def steps_from_volume_ml(self, volume_ml: float) -> int:
        """Convert a positive relative volume to nominal control steps."""
        return syringe_steps_from_volume_ml(
            volume_ml, syringe_volume_ml=self.syringe_volume_ml
        )

    def estimated_move_duration_s_from_steps(self, steps: int, *, rpm: int) -> float:
        """Return the nominal plunger-motion duration for a step distance.

        The estimate excludes valve movement, communication, and polling
        overhead.
        """
        require_integer_in_range(
            steps, name="steps", minimum=0, maximum=SY01B_MAX_STEPS
        )
        self._validate_dynamic_speed_rpm(rpm)

        return steps * 60.0 / (rpm * SY01B_STEPS_PER_REV)

    def estimated_move_duration_s_from_volume_ml(
        self, volume_ml: float, *, rpm: int
    ) -> float:
        """Return the nominal duration after rounding volume to control steps."""
        steps = self.steps_from_volume_ml(volume_ml)
        return self.estimated_move_duration_s_from_steps(steps, rpm=rpm)

    @property
    def ml_per_rev(self) -> float:
        """Nominal flow-rate increment produced by a 1-rpm speed change.
        In other words, volume (ml) per revolution."""
        return SY01B_STEPS_PER_REV * self.ml_per_step

    def flow_rate_ml_per_min_from_rpm(self, rpm: int) -> float:
        """Return the nominal syringe flow rate for an integer rpm setting."""
        self._validate_dynamic_speed_rpm(rpm)
        return rpm * self.ml_per_rev

    def rpm_options_from_ml_per_min(
        self, flow_ml_per_min: float
    ) -> SY01BFlowRateOptions:
        """Return integer RPM options around the requested nominal flow rate."""
        require_positive_finite_number(flow_ml_per_min, name="flow_ml_per_min")

        min_flow = SY01B_MIN_DYNAMIC_SPEED_RPM * self.ml_per_rev
        max_flow = SY01B_MAX_DYNAMIC_SPEED_RPM * self.ml_per_rev

        if flow_ml_per_min < min_flow:
            lower_rpm = upper_rpm = SY01B_MIN_DYNAMIC_SPEED_RPM
            in_range = False
        elif flow_ml_per_min > max_flow:
            lower_rpm = upper_rpm = SY01B_MAX_DYNAMIC_SPEED_RPM
            in_range = False
        else:
            exact_rpm = flow_ml_per_min / self.ml_per_rev
            lower_rpm = math.floor(exact_rpm)
            upper_rpm = math.ceil(exact_rpm)
            in_range = True

        return SY01BFlowRateOptions(
            requested_flow_ml_per_min=float(flow_ml_per_min),
            lower_rpm=lower_rpm,
            lower_flow_ml_per_min=self.flow_rate_ml_per_min_from_rpm(lower_rpm),
            upper_rpm=upper_rpm,
            upper_flow_ml_per_min=self.flow_rate_ml_per_min_from_rpm(upper_rpm),
            in_range=in_range,
        )

    # Valve action commands ---

    def move_valve_to_state(self, state: int, wait: bool = True) -> RawRunzeReply:
        # MY-3: 1 = C-IN, 2 = IN-OUT, 3 = C-OUT.
        # State 2 bypasses the syringe and should not be used during syringe operation.
        self._validate_valve_state(state)

        reply = self._send_common(function=0x44, b3=state)
        self._require_status(reply, RunzeStatus.OK, RunzeStatus.TASK_EXECUTING)

        if wait:
            self._confirm_valve_state(target_state=state)

        return reply

    def connect_syringe_to_inlet(self, wait: bool = True) -> RawRunzeReply:
        return self.move_valve_to_state(self.inlet_valve_state, wait=wait)

    def connect_syringe_to_outlet(self, wait: bool = True) -> RawRunzeReply:
        return self.move_valve_to_state(self.outlet_valve_state, wait=wait)

    def reset_valve(
        self, wait: bool = True
    ) -> RawRunzeReply:  # should be verified before use
        reply = self._send_common(function=0x4C)
        self._require_status(reply, RunzeStatus.OK, RunzeStatus.TASK_EXECUTING)

        if wait:
            self._confirm_valve_state(
                target_state=3
            )  # observed reset state 3 on this device

        return reply

    # Syringe motion commands ---

    def synchronize_syringe_position(self, *, wait: bool = True) -> RawRunzeReply:
        """Synchronize the stored position after power-up.

        The command is rejected if SY01B reports and unknown position.
        """
        self.query_syringe_position()

        reply = self._send_common(function=0x67)
        self._require_status(reply, RunzeStatus.OK, RunzeStatus.TASK_EXECUTING)

        if wait:
            self.wait_until_syringe_idle()

        return reply

    def set_dynamic_speed_rpm(self, rpm: int) -> RawRunzeReply:
        self._validate_dynamic_speed_rpm(rpm)

        b3, b4 = split_u16_le(rpm, name="rpm")
        reply = self._send_common(function=0x4B, b3=b3, b4=b4)
        self._require_status(reply, RunzeStatus.OK)
        return reply

    def move_plunger_cw(self, steps: int, wait: bool = True) -> RawRunzeReply:
        """Command clockwise syringe-motor motion by 1-6000 control steps."""
        self._validate_move_steps(steps)
        b3, b4 = split_u16_le(steps, name="steps")

        reply = self._send_common(function=0x42, b3=b3, b4=b4)
        self._require_status(reply, RunzeStatus.OK, RunzeStatus.TASK_EXECUTING)

        if wait:
            self.wait_until_syringe_idle()

        return reply

    def move_plunger_ccw(self, steps: int, wait: bool = True) -> RawRunzeReply:
        """Command counterclockwise syringe-motor motion by 1-6000 control steps."""
        self._validate_move_steps(steps)
        b3, b4 = split_u16_le(steps, name="steps")

        reply = self._send_common(function=0x43, b3=b3, b4=b4)
        self._require_status(reply, RunzeStatus.OK, RunzeStatus.TASK_EXECUTING)

        if wait:
            self.wait_until_syringe_idle()

        return reply

    def move_plunger_to_position(
        self, position: int, wait: bool = True
    ) -> RawRunzeReply:
        """Move the syringe plunger to an absolute position."""
        self._validate_syringe_position(position)
        b3, b4 = split_u16_le(position, name="position")

        start_position = self.query_syringe_position() if wait else None
        reply = self._send_common(function=0x4E, b3=b3, b4=b4)
        self._require_status(reply, RunzeStatus.OK, RunzeStatus.TASK_EXECUTING)

        if wait:
            self._confirm_syringe_position(
                target_position=position,
                tolerance_steps=min(1, abs(position - start_position) // 100),
            )

        return reply

    # High-level volume operations ---

    def move_to_syringe_volume_ml(
        self, target_volume_ml: float, *, rpm: int, wait: bool = True
    ) -> RawRunzeReply:
        """Move to a nominal syringe content volume using an integer rpm setting.

        This only moves the plunger to an absolute volume-derived position. It
        does not switch the valve or infer aspirate/dispense fluid direction.
        """
        target_position = self.position_from_volume_ml(target_volume_ml)

        self.set_dynamic_speed_rpm(rpm)
        return self.move_plunger_to_position(target_position, wait=wait)

    def aspirate_ml(
        self, volume_ml: float, *, rpm: int, source: SY01BFluidPort
    ) -> RawRunzeReply:
        """Aspirate a positive volume from the source port to the syringe."""
        step_delta = self.steps_from_volume_ml(volume_ml)
        current_position = self.query_syringe_position()
        target_position = current_position + step_delta

        self._validate_syringe_position(target_position)

        self._connect_syringe_to(source)
        self.set_dynamic_speed_rpm(rpm)
        return self.move_plunger_to_position(target_position, wait=True)

    def dispense_ml(
        self, volume_ml: float, *, rpm: int, destination: SY01BFluidPort
    ) -> RawRunzeReply:
        """Dispense a positive volume from the syringe to the destination port."""
        step_delta = self.steps_from_volume_ml(volume_ml)
        current_position = self.query_syringe_position()
        target_position = current_position - step_delta

        self._validate_syringe_position(target_position)

        self._connect_syringe_to(destination)
        self.set_dynamic_speed_rpm(rpm)
        return self.move_plunger_to_position(target_position, wait=True)

    def empty_syringe(self, *, rpm: int, destination: SY01BFluidPort) -> RawRunzeReply:
        """Dispense all syringe content to the destination port by moving to
        position 0."""
        self._connect_syringe_to(destination)
        self.set_dynamic_speed_rpm(rpm)
        return self.move_plunger_to_position(0, wait=True)

    # Reset / emergency commands ---

    def reset_syringe(self, wait: bool = True) -> RawRunzeReply:
        """Reset the syringe pump to home.

        The first reset after power-on runs at maximum speed and ignores the
        dynamic-speed setting. The manual says a 0x43 motion must run before
        dynamic speed set with 0x4B takes effect.
        """
        reply = self._send_common(function=0x45)
        self._require_status(reply, RunzeStatus.OK, RunzeStatus.TASK_EXECUTING)

        if wait:
            self._confirm_syringe_position(target_position=0)

        return reply

    def emergency_stop(self) -> RawRunzeReply:  # forced stop of pump and valve
        return self._send_common(function=0x49)

    # Public wait helpers ---

    def wait_until_syringe_idle(
        self, timeout_s: float = 900.0, poll_interval_s: float = 0.1
    ) -> None:
        self._wait_until_idle(
            self.query_motor_status,
            operation="syringe motion",
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )

    def wait_until_valve_idle(
        self, timeout_s: float = 10.0, poll_interval_s: float = 0.1
    ) -> None:
        self._wait_until_idle(
            self.query_valve_status,
            operation="valve motion",
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )

    # Private helpers ---

    def _connect_syringe_to(self, port: SY01BFluidPort) -> None:
        if port == "inlet":
            self.connect_syringe_to_inlet(wait=True)
        elif port == "outlet":
            self.connect_syringe_to_outlet(wait=True)
        else:
            raise ValueError(f"Unsupported SY01B fluid port: {port!r}")

    def _confirm_syringe_position(
        self, target_position: int, *, tolerance_steps: int = 0
    ) -> None:
        self.wait_until_syringe_idle()

        actual_position = self.query_syringe_position()
        if abs(actual_position - target_position) > tolerance_steps:
            raise RuntimeError(
                f"SY01B stopped at syringe position {actual_position}; "
                f"expected {target_position} ± {tolerance_steps} step."
            )

    def _confirm_valve_state(self, target_state: int) -> None:
        self.wait_until_valve_idle()

        actual_state = self.query_valve_state()
        if actual_state != target_state:
            raise RuntimeError(
                f"SY01B valve stopped at state {actual_state}; expected {target_state}."
            )

    @staticmethod
    def _validate_valve_state(state: int) -> None:
        require_integer_in_range(state, name="valve state", minimum=1, maximum=3)

    @staticmethod
    def _validate_syringe_connection_state(state: int, *, name: str) -> None:
        SY01B._validate_valve_state(state)

        if state == 2:
            raise ValueError(f"{name} must be 1 or 3")

    @staticmethod
    def _validate_move_steps(steps: int) -> None:
        require_integer_in_range(
            steps, name="steps", minimum=1, maximum=SY01B_MAX_STEPS
        )

    @staticmethod
    def _validate_syringe_position(position: int) -> None:
        require_integer_in_range(
            position, name="position", minimum=0, maximum=SY01B_MAX_STEPS
        )

    @staticmethod
    def _validate_dynamic_speed_rpm(rpm: int) -> None:
        require_integer_in_range(
            rpm,
            name="rpm",
            minimum=SY01B_MIN_DYNAMIC_SPEED_RPM,
            maximum=SY01B_MAX_DYNAMIC_SPEED_RPM,
        )
