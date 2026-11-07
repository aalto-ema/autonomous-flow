import math


def require_finite_number(value: object, *, name: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be an int or float")

    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")

    return value


def require_positive_finite_number(value: object, *, name: str) -> None:
    numeric_value = require_finite_number(value, name=name)

    if numeric_value <= 0:
        raise ValueError(f"{name} must be positive")


def require_non_negative_finite_number(value: object, *, name: str) -> None:
    numeric_value = require_finite_number(value, name=name)

    if numeric_value < 0:
        raise ValueError(f"{name} must be non-negative")


def require_integer(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")

    return value


def require_positive_integer(value: object, *, name: str) -> None:
    integer_value = require_integer(value, name=name)

    if integer_value <= 0:
        raise ValueError(f"{name} must be positive")


def require_integer_in_range(
    value: object, *, name: str, minimum: int, maximum: int
) -> None:
    integer_value = require_integer(value, name=name)

    if not minimum <= integer_value <= maximum:
        raise ValueError(
            f"{name} must be in range {minimum}-{maximum}; got {integer_value}"
        )
