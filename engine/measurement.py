"""Portable raw curve data crossing process boundaries."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Array:
    values: tuple[float, ...]

    def to_list(self):
        return list(self.values)


@dataclass(frozen=True)
class Curve:
    title: str
    x_array: Array
    y_array: Array


@dataclass(frozen=True)
class PortableMeasurement:
    curves: tuple[Curve, ...]
    title: str = "Measurement"
    timestamp: datetime | None = None
    session_bytes: bytes = b""


def portable(measurement, session_bytes=b""):
    return PortableMeasurement(
        tuple(
            Curve(
                str(c.title),
                Array(tuple(float(x) for x in c.x_array.to_list())),
                Array(tuple(float(y) for y in c.y_array.to_list())),
            )
            for c in measurement.curves
        ),
        str(measurement.title),
        measurement.timestamp
        if isinstance(getattr(measurement, "timestamp", None), datetime)
        else datetime.now(UTC),
        session_bytes,
    )


def simulated_bytes(measurement):
    return (
        b"AUTONOMOUS_FLOW_SIMULATION\n"
        + json.dumps(
            [
                {"title": c.title, "x": c.x_array.values, "y": c.y_array.values}
                for c in measurement.curves
            ]
        ).encode()
    )


def from_simulated_bytes(data):
    rows = json.loads(data.split(b"\n", 1)[1])
    return PortableMeasurement(
        tuple(
            Curve(r["title"], Array(tuple(r["x"])), Array(tuple(r["y"]))) for r in rows
        ),
        "Simulated measurement",
        datetime.now(UTC),
        data,
    )
