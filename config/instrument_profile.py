"""Conservative planning limits; physical characterization is still required."""

import json
import math
from pathlib import Path

DEFAULT_MINIMUM_STROKE_ML = 0.05


def minimum_stroke_ml(root: Path) -> float:
    path = Path(root) / "instrument-profile.json"
    if not path.exists():
        return DEFAULT_MINIMUM_STROKE_ML
    value = float(json.loads(path.read_text())["minimum_stroke_ml"])
    if not math.isfinite(value) or value <= 0:
        raise ValueError("minimum_stroke_ml must be finite and positive")
    return value
