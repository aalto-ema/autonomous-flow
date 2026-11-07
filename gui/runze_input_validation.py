from common.validation import (
    require_finite_number,
    require_non_negative_finite_number,
    require_positive_finite_number,
)
from runze.sy01b import SY01B_MAX_DYNAMIC_SPEED_RPM, SY01B_MIN_DYNAMIC_SPEED_RPM

CONCENTRATION_FACTORS_TO_MOL_L = {"M": 1.0, "mM": 1e-3, "µM": 1e-6}


def read_non_negative_number(value: float | None, *, name: str) -> float:
    if value is None:
        raise ValueError(f"{name} is required")

    try:
        require_non_negative_finite_number(value, name=name)
    except TypeError as error:
        raise ValueError(str(error)) from error

    return float(value)


def read_positive_number(value: float | None, *, name: str) -> float:
    if value is None:
        raise ValueError(f"{name} is required")

    try:
        require_positive_finite_number(value, name=name)
    except TypeError as error:
        raise ValueError(str(error)) from error

    return float(value)


def read_positive_integer(value: float | None, *, name: str) -> int:
    numeric_value = read_positive_number(value, name=name)

    if not numeric_value.is_integer():
        raise ValueError(f"{name} must be a positive integer")

    return int(numeric_value)


def read_integer_in_range(
    value: float | None, *, name: str, minimum: int, maximum: int
) -> int:
    if value is None:
        raise ValueError(f"{name} is required")

    try:
        require_finite_number(value, name=name)
    except TypeError as error:
        raise ValueError(str(error)) from error

    numeric_value = float(value)

    if not numeric_value.is_integer():
        raise ValueError(f"{name} must be an integer")

    integer_value = int(numeric_value)

    if not minimum <= integer_value <= maximum:
        raise ValueError(f"{name} must be in range {minimum}-{maximum}")

    return integer_value


def read_rpm(value: float | None, *, name: str) -> int:
    return read_integer_in_range(
        value,
        name=name,
        minimum=SY01B_MIN_DYNAMIC_SPEED_RPM,
        maximum=SY01B_MAX_DYNAMIC_SPEED_RPM,
    )


def read_concentration_mol_l(value: float | None, *, unit: str, name: str) -> float:
    numeric_value = read_positive_number(value, name=name)
    try:
        factor = CONCENTRATION_FACTORS_TO_MOL_L[unit]
    except KeyError as error:
        raise ValueError(f"Unsupported concentration unit: {unit!r}") from error
    return numeric_value * factor
