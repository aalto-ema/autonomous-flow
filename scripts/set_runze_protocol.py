# scripts/set_runze_protocol.py

import time
from enum import Enum

import serial

from runze.protocol import (
    PROTOCOL_QUERY,
    SWITCH_TO_ASCII,
    SWITCH_TO_RUNZE,
    parse_protocol_query_reply,
    parse_protocol_switch_reply,
)

# Run only with one device connected to RS485 bus!


class ProtocolAction(Enum):
    QUERY = 1
    SET_RUNZE = 2
    SET_ASCII = 3


PORT = "COM5"
BAUDRATE = 9600
ACTION = ProtocolAction.QUERY  # Change this to QUERY, SET_RUNZE or SET_ASCII as needed


def send_raw(command: bytes, reply_size: int = 10) -> bytes:
    with serial.Serial(
        port=PORT,
        baudrate=BAUDRATE,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=1.0,
        write_timeout=1.0,
    ) as ser:
        time.sleep(0.2)
        ser.write(command)
        ser.flush()
        return ser.read(reply_size)


def main():
    target_mode: str | None = None

    if ACTION is ProtocolAction.QUERY:
        command = PROTOCOL_QUERY
    elif ACTION is ProtocolAction.SET_RUNZE:
        command = SWITCH_TO_RUNZE
        target_mode = "runze"
    elif ACTION is ProtocolAction.SET_ASCII:
        command = SWITCH_TO_ASCII
        target_mode = "ascii"
    else:
        raise RuntimeError(f"Invalid action: {ACTION}")

    reply = send_raw(command)

    if ACTION is ProtocolAction.QUERY:
        mode = parse_protocol_query_reply(reply)
        print(f"Current protocol: {mode}")
    else:
        parse_protocol_switch_reply(reply)
        assert target_mode is not None
        print(f"Protocol switched successfully to {target_mode} mode")


if __name__ == "__main__":
    main()
