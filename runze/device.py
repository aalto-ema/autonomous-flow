# runze/device.py

import time
from collections.abc import Callable
from enum import IntEnum

from .bus import RunzeBus
from .protocol import RawRunzeReply, build_common_command, build_factory_command

BAUD_RATE_BY_CODE = {
    0x0000: 9600,
    0x0001: 19200,
    0x0002: 38400,
    0x0003: 57600,
    0x0004: 115200,
}


class RunzeStatus(IntEnum):
    OK = 0x00
    FRAME_ERROR = 0x01
    PARAMETER_ERROR = 0x02
    OPTOCOUPLER_ERROR = 0x03
    MOTOR_BUSY = 0x04
    MOTOR_STALLED = 0x05
    UNKNOWN_POSITION = 0x06
    COMMAND_REJECTED = 0x07
    ILLEGAL_POSITION = 0x08  # documented for SY01B
    TASK_EXECUTING = 0xFE
    UNKNOWN_ERROR = 0xFF


class RunzeDevice:
    device_name = "Runze device"

    def __init__(self, bus: RunzeBus, address: int = 0x00):
        if not isinstance(bus, RunzeBus):
            raise TypeError("bus must be a RunzeBus instance")

        self._validate_address(address)

        self.bus = bus
        self.address = address

    @staticmethod
    def _validate_address(address: int) -> None:
        if not isinstance(address, int) or isinstance(address, bool):
            raise TypeError("address must be an integer")
        if not 0 <= address <= 0x7F:
            raise ValueError("address must be in range 0x00-0x7F")

    @staticmethod
    def _status(reply: RawRunzeReply) -> RunzeStatus:
        try:
            return RunzeStatus(reply.b2)
        except ValueError as error:
            raise RuntimeError(
                f"Unknown B2 status 0x{reply.b2:02X}: {reply.raw.hex(' ')}"
            ) from error

    @staticmethod
    def _require_status(reply: RawRunzeReply, *allowed: RunzeStatus) -> RunzeStatus:
        status = RunzeDevice._status(reply)

        if status not in allowed:
            expected = ", ".join(f"{item.name} (0x{item:02X})" for item in allowed)
            raise RuntimeError(
                f"Unexpected device status {status.name}; expected {expected}.\n"
                f"Reply: {reply.raw.hex(' ')}"
            )

        return status

    def _require_reply_address(self, reply: RawRunzeReply) -> None:
        if reply.address != self.address:
            raise RuntimeError(
                f"Unexpected reply address 0x{reply.address:02X}; "
                f"expected 0x{self.address:02X}.\n"
                f"Reply: {reply.raw.hex(' ')}"
            )

    @staticmethod
    def _decode_baud_rate(reply: RawRunzeReply) -> int:
        RunzeDevice._require_status(reply, RunzeStatus.OK)
        code = reply.value_u16

        try:
            return BAUD_RATE_BY_CODE[code]
        except KeyError as error:
            raise RuntimeError(
                f"Unknown baud rate code 0x{code:04X}.\nReply: {reply.raw.hex(' ')}"
            ) from error

    def _wait_until_idle(
        self,
        status_query: Callable[[], RunzeStatus],
        *,
        operation: str,
        timeout_s: float,
        poll_interval_s: float,
    ) -> None:
        """Poll a device-specific status command until the operation finishes."""
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")

        deadline = time.monotonic() + timeout_s

        while True:
            status = status_query()

            if status is RunzeStatus.OK:
                return
            if status not in (RunzeStatus.MOTOR_BUSY, RunzeStatus.TASK_EXECUTING):
                raise RuntimeError(
                    f"{self.device_name} {operation} did not complete normally: "
                    f"{status.name} (0x{status:02X})"
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"{self.device_name} {operation} did not become idle "
                    f"within {timeout_s} s"
                )

            time.sleep(min(poll_interval_s, remaining))

    def _send_common(
        self, function: int, b3: int = 0x00, b4: int = 0x00
    ) -> RawRunzeReply:
        command = build_common_command(self.address, function, b3, b4)
        reply = self.bus.send_command(command)
        self._require_reply_address(reply)
        return reply

    def _send_factory(
        self, function: int, password: bytes, parameters: bytes
    ) -> RawRunzeReply:
        command = build_factory_command(self.address, function, password, parameters)
        return self.bus.send_command(command)
