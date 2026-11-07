from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from palmsens import sdk as ps
from palmsens.data_types import Measurement

from palmsens.emstat4s import EmStatMeasurementType

logger = logging.getLogger(__name__)

DEFAULT_MEASUREMENTS_DIRECTORY = Path(__file__).resolve().parents[1] / "measurements"

_SESSION_FILE_NUMBER_WIDTH = 6
_SESSION_FILE_PATTERN = re.compile(
    r"^(?P<number>\d+)_"
    r"(?P<measurement_type>"
    r"chronoamperometry|cyclic_voltammetry|linear_sweep_voltammetry"
    r")"
    r"(?:_(?P<timestamp>\d{8}-\d{6}))?"
    r"(?:_(?P<measurement_name>.+))?\.pssession$"
)
_INVALID_FILE_NAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_MEASUREMENT_TYPE_LABELS: dict[EmStatMeasurementType, str] = {
    "chronoamperometry": "Chronoamperometry",
    "cyclic_voltammetry": "Cyclic voltammetry",
    "linear_sweep_voltammetry": "Linear sweep voltammetry",
}


@dataclass(frozen=True)
class SavedEmStatSession:
    file_name: str
    sequence_number: int
    measurement_type: EmStatMeasurementType
    measurement_name: str | None
    saved_at: datetime

    @property
    def display_label(self) -> str:
        measurement_label = _MEASUREMENT_TYPE_LABELS[self.measurement_type]
        timestamp_label = self.saved_at.strftime("%Y-%m-%d %H:%M:%S")

        if self.measurement_name:
            return f"{self.measurement_name} — {measurement_label} — {timestamp_label}"

        return f"{measurement_label} — {timestamp_label}"


class EmStatSessionRepository:
    """Save, list, and load application-owned PalmSens session files."""

    def __init__(
        self, measurements_directory: str | Path = DEFAULT_MEASUREMENTS_DIRECTORY
    ) -> None:
        self._measurements_directory = Path(measurements_directory).resolve()
        self._file_lock = threading.Lock()

    @property
    def measurements_directory(self) -> Path:
        return self._measurements_directory

    def save(
        self,
        *,
        measurement: Measurement,
        measurement_type: EmStatMeasurementType,
        measurement_name: str | None,
        relative_directory: str | Path | None = None,
        sequence_number: int | None = None,
    ) -> SavedEmStatSession:
        safe_measurement_name = self._sanitize_measurement_name(measurement_name)
        saved_at = datetime.now().astimezone()

        with self._file_lock:
            self._measurements_directory.mkdir(parents=True, exist_ok=True)
            target_directory = self._resolve_target_directory(relative_directory)
            target_directory.mkdir(parents=True, exist_ok=True)
            session_file, sequence_number = self._reserve_session_file(
                target_directory=target_directory,
                measurement_type=measurement_type,
                measurement_name=safe_measurement_name,
                saved_at=saved_at,
                sequence_number=sequence_number,
            )
            temporary_file = session_file.with_name(
                f".{session_file.stem}.{uuid4().hex}.tmp.pssession"
            )

            try:
                if getattr(measurement, "session_bytes", b""):
                    temporary_file.write_bytes(measurement.session_bytes)
                else:
                    ps.save_session_file(str(temporary_file), [measurement])
                os.replace(temporary_file, session_file)
            except Exception:
                temporary_file.unlink(missing_ok=True)
                session_file.unlink(missing_ok=True)
                raise

        return SavedEmStatSession(
            file_name=session_file.relative_to(self._measurements_directory).as_posix(),
            sequence_number=sequence_number,
            measurement_type=measurement_type,
            measurement_name=safe_measurement_name,
            saved_at=saved_at,
        )

    def list_sessions(self) -> tuple[SavedEmStatSession, ...]:
        with self._file_lock:
            if not self._measurements_directory.is_dir():
                return ()

            sessions = tuple(
                SavedEmStatSession(
                    file_name=path.relative_to(self._measurements_directory).as_posix(),
                    sequence_number=session.sequence_number,
                    measurement_type=session.measurement_type,
                    measurement_name=session.measurement_name,
                    saved_at=session.saved_at,
                )
                for path in self._measurements_directory.rglob("*.pssession")
                if (session := self._entry_from_path(path)) is not None
            )

        return tuple(
            sorted(
                sessions,
                key=lambda session: (session.saved_at, session.sequence_number),
                reverse=True,
            )
        )

    def load(self, file_name: str) -> tuple[SavedEmStatSession, Measurement]:
        session_file = self._resolve_session_file(file_name)
        entry = self._entry_from_path(session_file)

        if entry is None:
            raise ValueError(f"Unsupported EmStat session filename: {file_name}")

        entry = SavedEmStatSession(
            file_name=session_file.relative_to(self._measurements_directory).as_posix(),
            sequence_number=entry.sequence_number,
            measurement_type=entry.measurement_type,
            measurement_name=entry.measurement_name,
            saved_at=entry.saved_at,
        )

        measurements = ps.load_session_file(str(session_file))

        if len(measurements) != 1:
            raise RuntimeError(
                f"Expected one measurement in {file_name}; found {len(measurements)}"
            )

        return entry, measurements[0]

    def _reserve_session_file(
        self,
        *,
        target_directory: Path,
        measurement_type: EmStatMeasurementType,
        measurement_name: str | None,
        saved_at: datetime,
        sequence_number: int | None,
    ) -> tuple[Path, int]:
        if sequence_number is not None:
            if isinstance(sequence_number, bool) or sequence_number <= 0:
                raise ValueError("sequence_number must be a positive integer")
            if any(
                entry.sequence_number == sequence_number
                for path in target_directory.glob("*.pssession")
                if (entry := self._entry_from_path(path)) is not None
            ):
                raise FileExistsError(
                    f"Session sequence {sequence_number} already exists in "
                    f"{target_directory}"
                )
            next_number = sequence_number
        else:
            used_numbers = [
                entry.sequence_number
                for path in target_directory.glob("*.pssession")
                if (entry := self._entry_from_path(path)) is not None
            ]
            next_number = max(used_numbers, default=0) + 1

        while True:
            name_parts = [
                f"{next_number:0{_SESSION_FILE_NUMBER_WIDTH}d}",
                measurement_type,
                saved_at.strftime("%Y%m%d-%H%M%S"),
            ]

            if measurement_name:
                name_parts.append(measurement_name)

            session_file = target_directory / ("_".join(name_parts) + ".pssession")

            try:
                session_file.touch(exist_ok=False)
            except FileExistsError:
                if sequence_number is not None:
                    raise FileExistsError(
                        f"Session sequence {sequence_number} already exists in "
                        f"{target_directory}"
                    )
                next_number += 1
                continue

            return session_file, next_number

    def _resolve_session_file(self, file_name: str) -> Path:
        if not isinstance(file_name, str) or not file_name:
            raise ValueError("Saved measurement filename is required")

        relative_path = Path(file_name)
        if (
            relative_path.is_absolute()
            or "\\" in file_name
            or ".." in relative_path.parts
        ):
            raise ValueError("Saved measurement path must be relative and contained")

        directory = self._measurements_directory.resolve()
        session_file = (directory / relative_path).resolve()

        if directory not in session_file.parents:
            raise ValueError("Saved measurement must be inside measurements directory")

        if not session_file.is_file():
            raise FileNotFoundError(f"Saved measurement does not exist: {file_name}")

        return session_file

    def _resolve_target_directory(self, relative_directory: str | Path | None) -> Path:
        directory = self._measurements_directory.resolve()
        if relative_directory is None:
            return directory
        relative_path = Path(relative_directory)
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or "\\" in str(relative_directory)
        ):
            raise ValueError("Session directory must be relative and contained")
        target = (directory / relative_path).resolve()
        if target != directory and directory not in target.parents:
            raise ValueError("Session directory must be inside measurements directory")
        return target

    @staticmethod
    def _sanitize_measurement_name(measurement_name: str | None) -> str | None:
        if measurement_name is None:
            return None

        if not isinstance(measurement_name, str):
            raise TypeError("measurement_name must be a string or None")

        safe_name = _INVALID_FILE_NAME_CHARACTERS.sub("-", measurement_name.strip())
        safe_name = " ".join(safe_name.split()).strip(" .")
        safe_name = safe_name[:80].rstrip(" .")

        return safe_name or None

    @staticmethod
    def _entry_from_path(path: Path) -> SavedEmStatSession | None:
        if path.stat().st_size == 0:
            logger.warning("Ignoring empty EmStat session file: %s", path)
            return None

        match = _SESSION_FILE_PATTERN.fullmatch(path.name)

        if match is None:
            logger.warning("Ignoring unrecognized EmStat session file: %s", path)
            return None

        measurement_type_text = match.group("measurement_type")
        measurement_type: EmStatMeasurementType
        if measurement_type_text == "chronoamperometry":
            measurement_type = "chronoamperometry"
        elif measurement_type_text == "cyclic_voltammetry":
            measurement_type = "cyclic_voltammetry"
        else:
            measurement_type = "linear_sweep_voltammetry"

        timestamp_text = match.group("timestamp")
        saved_at = (
            datetime.strptime(timestamp_text, "%Y%m%d-%H%M%S").astimezone()
            if timestamp_text
            else datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).astimezone()
        )

        return SavedEmStatSession(
            file_name=path.name,
            sequence_number=int(match.group("number")),
            measurement_type=measurement_type,
            measurement_name=match.group("measurement_name"),
            saved_at=saved_at,
        )
