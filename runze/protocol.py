# runze/protocol.py

from dataclasses import dataclass

from common.validation import require_integer_in_range

STX = 0xCC
ETX = 0xDD

PROTOCOL_QUERY = bytes.fromhex("91 EB 07 00 00 00 00 00 00 D5 28 FF F8")
SWITCH_TO_RUNZE = bytes.fromhex("91 EB 03 00 00 02 08 00 00 0C 0A 69 69")
SWITCH_TO_ASCII = bytes.fromhex("91 EB 03 00 00 0A 08 00 00 6D 19 D8 C9")


@dataclass(frozen=True)
class RawRunzeReply:
    raw: bytes
    address: int
    b2: int
    b3: int
    b4: int

    @property
    def value_u16(self) -> int:
        return self.b3 | (self.b4 << 8)


def _require_byte(name: str, value: int) -> None:
    require_integer_in_range(value, name=name, minimum=0x00, maximum=0xFF)


def checksum(payload: bytes) -> bytes:
    value = sum(payload) & 0xFFFF
    return value.to_bytes(2, "little")


def build_common_command(
    address: int, function: int, b3: int = 0, b4: int = 0
) -> bytes:
    _require_byte("address", address)
    _require_byte("function", function)
    _require_byte("b3", b3)
    _require_byte("b4", b4)

    payload = bytes([STX, address, function, b3, b4, ETX])
    return payload + checksum(payload)


def build_factory_command(
    address: int, function: int, password: bytes, parameters: bytes
) -> bytes:
    _require_byte("address", address)
    _require_byte("function", function)
    if len(password) != 4:
        raise ValueError("Password must be 4 bytes long")
    if len(parameters) != 4:
        raise ValueError("Parameters must be 4 bytes long")

    payload = bytes([STX, address, function, *password, *parameters, ETX])
    return payload + checksum(payload)


def parse_common_reply(frame: bytes) -> RawRunzeReply:
    if len(frame) != 8:
        raise ValueError("Expected 8-byte reply frame")
    if frame[0] != STX or frame[5] != ETX:
        raise ValueError("Invalid reply frame start/end bytes")
    if checksum(frame[:6]) != frame[6:]:
        raise ValueError("Invalid reply frame checksum")

    return RawRunzeReply(
        raw=frame, address=frame[1], b2=frame[2], b3=frame[3], b4=frame[4]
    )


def _validate_protocol_mode_reply(reply: bytes) -> None:
    if len(reply) != 10:
        raise ValueError("Expected 10-byte protocol reply")
    if reply[:2] != b"\x91\xeb":
        raise ValueError("Unexpected protocol reply prefix")


def parse_protocol_query_reply(reply: bytes) -> str:
    _validate_protocol_mode_reply(reply)

    match reply[2]:
        case 0x02:
            return "runze"
        case 0x0A:
            return "ascii"
        case code:
            raise RuntimeError(f"Unknown protocol code: 0x{code:02X}")


def parse_protocol_switch_reply(reply: bytes) -> None:
    _validate_protocol_mode_reply(reply)

    if reply[2] != 0x00:
        raise RuntimeError(f"Protocol switch failed: {reply.hex(' ')}")


def split_u16_le(value: int, *, name: str = "value") -> tuple[int, int]:
    require_integer_in_range(value, name=name, minimum=0x0000, maximum=0xFFFF)
    return value & 0xFF, value >> 8
