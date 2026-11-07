import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from palmsens.data_types import Measurement

from palmsens.emstat_session_repository import EmStatSessionRepository


class EmStatSessionRepositoryTests(unittest.TestCase):
    def test_nested_run_session_uses_relative_path_and_is_listed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = EmStatSessionRepository(directory)

            def save_session(path: str, _measurements: object) -> None:
                Path(path).write_bytes(b"session")

            with patch(
                "palmsens.emstat_session_repository.ps.save_session_file",
                side_effect=save_session,
            ):
                saved = repository.save(
                    measurement=cast(Measurement, object()),
                    measurement_type="chronoamperometry",
                    measurement_name="cycle",
                    relative_directory="000001_experiment",
                    sequence_number=1,
                )

            self.assertTrue(saved.file_name.startswith("000001_experiment/"))
            self.assertEqual(repository.list_sessions()[0].file_name, saved.file_name)

    def test_duplicate_sequence_in_one_run_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = EmStatSessionRepository(directory)

            def save_session(path: str, _measurements: object) -> None:
                Path(path).write_bytes(b"session")

            with patch(
                "palmsens.emstat_session_repository.ps.save_session_file",
                side_effect=save_session,
            ):
                repository.save(
                    measurement=cast(Measurement, object()),
                    measurement_type="chronoamperometry",
                    measurement_name=None,
                    relative_directory="000001_experiment",
                    sequence_number=1,
                )
                with self.assertRaises(FileExistsError):
                    repository.save(
                        measurement=cast(Measurement, object()),
                        measurement_type="chronoamperometry",
                        measurement_name=None,
                        relative_directory="000001_experiment",
                        sequence_number=1,
                    )


if __name__ == "__main__":
    unittest.main()
