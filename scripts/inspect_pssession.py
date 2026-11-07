"""Inspect measurements and curves stored in a PalmSens session file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pypalmsens as ps


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Show the measurements, curve IDs, titles, point counts, and axes "
            "stored in a PalmSens .pssession file."
        )
    )
    parser.add_argument(
        "session_path",
        nargs="?",
        type=Path,
        help="Path to the .pssession file, relative to the project root or absolute.",
    )
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Inspect the most recently modified .pssession in measurements/.",
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path("measurements"),
        help="Directory searched by --latest (default: measurements).",
    )

    arguments = parser.parse_args()

    if arguments.latest == (arguments.session_path is not None):
        parser.error("provide either SESSION_PATH or --latest, but not both")

    return arguments


def _find_latest_session(directory: Path) -> Path:
    session_files = tuple(directory.glob("*.pssession"))

    if not session_files:
        raise FileNotFoundError(f"No .pssession files found in {directory}")

    return max(session_files, key=lambda path: path.stat().st_mtime_ns)


def _inspect_session(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Session file does not exist: {path}")

    measurements = ps.load_session_file(str(path))

    print(f"File: {path.resolve()}")
    print(f"Measurements: {len(measurements)}")

    for measurement_index, measurement in enumerate(measurements, start=1):
        curves = measurement.curves
        curve_metadata = [
            json.loads(curve.metadata_json().decode("utf-8")) for curve in curves
        ]
        curve_ids = [metadata.get("id") for metadata in curve_metadata]

        print()
        print(
            f"Measurement {measurement_index}: "
            f"title={measurement.title!r}, "
            f"timestamp={measurement.timestamp}, "
            f"curves={measurement.n_curves}, "
            f"unique_curve_ids={len(set(curve_ids))}"
        )

        for curve_index, (curve, metadata) in enumerate(
            zip(curves, curve_metadata, strict=True), start=1
        ):
            x_values = curve.x_array.to_list()
            first_x = x_values[0] if x_values else None
            last_x = x_values[-1] if x_values else None

            print(
                f"  Curve {curve_index}: "
                f"id={metadata.get('id', 'unknown')}, "
                f"title={curve.title!r}, "
                f"points={curve.n_points}, "
                f"x={curve.x_label} ({curve.x_unit}), "
                f"y={curve.y_label} ({curve.y_unit}), "
                f"first_x={first_x}, "
                f"last_x={last_x}"
            )


def main() -> int:
    arguments = _parse_arguments()

    try:
        path = (
            _find_latest_session(arguments.directory)
            if arguments.latest
            else arguments.session_path
        )
        assert path is not None
        _inspect_session(path)
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
