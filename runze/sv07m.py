# runze/sv07m.py

from dataclasses import dataclass

from common.validation import require_integer_in_range

from .bus import RunzeBus
from .device import RunzeDevice, RunzeStatus
from .protocol import RawRunzeReply


@dataclass(frozen=True)
class SV07MStatus:
    motor_status: RunzeStatus
    current_port: int | None


class UnknownValvePositionError(RuntimeError):
    """SV07M is not aligned with a valid valve port."""


class SV07M(RunzeDevice):
    device_name = "SV07M"

    def __init__(self, bus: RunzeBus, address: int = 0x00, port_count: int = 10):
        require_integer_in_range(port_count, name="port_count", minimum=1, maximum=10)
        super().__init__(bus, address)
        self.port_count = port_count

    # Queries / status

    def query_address(self) -> RawRunzeReply:
        """Query device address. Use only when exactly one device is connected."""
        return self._send_common(function=0x20)

    def query_rs485_baud_rate(self) -> int:
        reply = self._send_common(function=0x22)
        return self._decode_baud_rate(reply)

    def query_auto_reset(self) -> bool:
        reply = self._send_common(function=0x2E)
        self._require_status(reply, RunzeStatus.OK)

        if reply.value_u16 not in (0, 1):
            raise RuntimeError(
                f"Unexpected auto-reset value {reply.value_u16}.\n"
                f"Reply: {reply.raw.hex(' ')}"
            )

        return bool(reply.value_u16)

    def query_current_port(self) -> int:
        reply = self._send_common(function=0x3E)
        self._require_status(reply, RunzeStatus.OK)

        port = reply.value_u16
        if not 1 <= port <= self.port_count:
            raise RuntimeError(
                f"Unexpected valve position {port}.\nReply: {reply.raw.hex(' ')}"
            )

        return port

    def _query_raw_position(self) -> int:
        reply = self._send_common(function=0x3E)
        self._require_status(reply, RunzeStatus.OK)
        return reply.value_u16

    def query_current_port_or_none(self) -> int | None:
        position = self._query_raw_position()

        if 1 <= position <= self.port_count:
            return position

        return None

    def query_motor_status(self) -> RunzeStatus:
        reply = self._send_common(function=0x4A)
        return self._status(reply)

    def read_status(self) -> SV07MStatus:
        return SV07MStatus(
            motor_status=self.query_motor_status(),
            current_port=self.query_current_port_or_none(),
        )

    # Action commands

    def move_to_port(self, port: int, wait: bool = True) -> RawRunzeReply:
        self._validate_port(port)

        reply = self._send_common(function=0x44, b3=port)
        self._require_status(reply, RunzeStatus.OK, RunzeStatus.TASK_EXECUTING)

        if wait:
            self._confirm_completion(target_port=port)

        return reply

    def reset_to_home(self, wait: bool = True) -> RawRunzeReply:
        reply = self._send_common(function=0x45)
        self._require_status(reply, RunzeStatus.OK, RunzeStatus.TASK_EXECUTING)

        if wait:
            self._confirm_completion(target_port=1)

        return reply

    def emergency_stop(self) -> RawRunzeReply:
        return self._send_common(function=0x49)

    def ensure_port(self, port: int) -> None:
        self._validate_port(port)

        if self.query_current_port() != port:
            self.move_to_port(port, wait=True)

    # Public wait helpers

    def wait_until_idle(
        self, timeout_s: float = 10.0, poll_interval_s: float = 0.1
    ) -> None:
        self._wait_until_idle(
            self.query_motor_status,
            operation="valve motion",
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )

    # Private confirmation / validation helpers

    def _confirm_completion(self, target_port: int) -> None:
        self.wait_until_idle()

        actual_port = self.query_current_port()
        if actual_port != target_port:
            raise RuntimeError(
                f"SV07M stopped at port {actual_port}; expected port {target_port}."
            )

    def _validate_port(self, port: int) -> None:
        require_integer_in_range(port, name="port", minimum=1, maximum=self.port_count)
