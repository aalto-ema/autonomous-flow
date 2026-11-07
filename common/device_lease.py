"""Process-held OS locks prevent engines in different workspaces sharing devices."""

import hashlib
import os
import tempfile
from pathlib import Path


class DeviceLease:
    def __init__(self, identity):
        directory = Path(tempfile.gettempdir()) / "autonomous-flow-device-locks"
        directory.mkdir(mode=0o700, exist_ok=True)
        self.file = open(
            directory / (hashlib.sha256(identity.encode()).hexdigest() + ".lock"), "a+b"
        )
        try:
            if os.name == "nt":
                import msvcrt

                self.file.write(b"0")
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            raise RuntimeError(
                "Another engine owns this physical device; disconnect it there first"
            ) from error

    def close(self):
        self.file.close()
