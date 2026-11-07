# runze/bus.py

import logging
import threading
import time
from typing import Self

import serial

from .protocol import RawRunzeReply, parse_common_reply

trace_logger = logging.getLogger("runze.trace")

TraceExchange = tuple[bytes, bytes]


class RunzeCommunicationError(RuntimeError):
    """A Runze serial communication or reply error."""


class RunzeBus:
    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        *,
        timeout: float = 1.0,
        trace: bool = False,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        self._port = port
        self._trace = trace
        self._closed = False
        self._lock = threading.Lock()

        self._last_trace_exchange: TraceExchange | None = None
        self._trace_repeat_count = 0

        try:
            self._serial = serial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=timeout,
                write_timeout=timeout,
            )
        except serial.SerialException as error:
            raise RunzeCommunicationError(
                f"Could not open Runze serial connection on {port}"
            ) from error

        time.sleep(0.2)

    @property
    def connected(self) -> bool:
        return not self._closed and self._serial.is_open

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("RunzeBus is closed")

    def send_command(self, command: bytes) -> RawRunzeReply:
        if not isinstance(command, bytes):
            raise TypeError("command must be of type bytes")

        try:
            with self._lock:
                self._ensure_open()

                self._serial.write(command)
                self._serial.flush()
                reply = self._serial.read(8)

                self._record_trace_exchange(command=command, reply=reply)

        except serial.SerialException as error:
            if self._trace:
                self._flush_trace_repeats()

                trace_logger.info(
                    "%s | TX %s | SERIAL ERROR: %s", self._port, command.hex(" "), error
                )

                self._last_trace_exchange = None

            raise RunzeCommunicationError(
                "Runze serial communication failed"
            ) from error

        if not reply:
            raise RunzeCommunicationError("No reply received from Runze device")

        if len(reply) != 8:
            raise RunzeCommunicationError(
                f"Expected 8-byte Runze reply, received {len(reply)} bytes.\n"
                f"Reply: {reply.hex(' ')}"
            )

        try:
            return parse_common_reply(reply)
        except ValueError as error:
            raise RunzeCommunicationError(
                f"Invalid Runze reply: {reply.hex(' ')}"
            ) from error

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._flush_trace_repeats()
                self._serial.close()
                self._closed = True

    def __enter__(self) -> Self:
        with self._lock:
            self._ensure_open()
            return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    # Trace helpers ---

    def _record_trace_exchange(self, *, command: bytes, reply: bytes) -> None:
        if not self._trace:
            return

        exchange = (command, reply)

        if exchange == self._last_trace_exchange:
            self._trace_repeat_count += 1
            return

        self._flush_trace_repeats()

        trace_logger.info(
            "%s | TX %s | RX %s",
            self._port,
            command.hex(" "),
            self._format_trace_bytes(reply),
        )

        self._last_trace_exchange = exchange

    def _flush_trace_repeats(self) -> None:
        if not self._trace:
            return

        if self._trace_repeat_count == 0:
            return

        trace_logger.info(
            "%s | previous exchange repeated %d additional times",
            self._port,
            self._trace_repeat_count,
        )

        self._trace_repeat_count = 0

    @staticmethod
    def _format_trace_bytes(data: bytes) -> str:
        if not data:
            return "<no reply>"

        return data.hex(" ")
