from engine.measurement import from_simulated_bytes
from palmsens.emstat_session_repository import EmStatSessionRepository


class PortableSessionRepository(EmStatSessionRepository):
    def __init__(self, directory, instrument):
        super().__init__(directory)
        self.instrument = instrument

    def load(self, file_name):
        from dataclasses import replace

        path = self._resolve_session_file(file_name)
        entry = self._entry_from_path(path)
        if entry is None:
            raise ValueError("Unrecognized session filename")
        entry = replace(entry, file_name=file_name)
        data = path.read_bytes()
        if data.startswith(b"AUTONOMOUS_FLOW_SIMULATION\n"):
            return entry, from_simulated_bytes(data)
        return entry, self.instrument.load_session(path)
