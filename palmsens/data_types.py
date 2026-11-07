"""Structural measurement types; importing these never starts the PalmSens SDK."""

from typing import Any, Protocol


class Measurement(Protocol):
    @property
    def curves(self) -> Any: ...


class CallbackData(Protocol):
    id: int
    start: int
    x_array: Any
    y_array: Any
