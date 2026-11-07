"""Own the PalmSens/.NET SDK in an isolated process with bounded IPC waits."""

import multiprocessing as mp
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from engine.measurement import portable


def _worker(normal, control):
    from multiprocessing.connection import wait

    from palmsens import sdk
    from palmsens.emstat4s import EmStat4S, MeasurementAbortedError

    instrument = EmStat4S()
    send_lock = threading.Lock()
    busy = False

    def send(value):
        with send_lock:
            normal.send(value)

    def with_session(measurement):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "measurement.pssession"
            sdk.save_session_file(str(path), [measurement])
            return portable(measurement, path.read_bytes())

    def perform(name, kwargs):
        nonlocal busy
        try:
            if name == "load_session":
                result = portable(sdk.load_session_file(kwargs["path"])[0])
            else:
                if name.startswith("measure_"):
                    kwargs["on_data"] = lambda batch: send(("data", batch))
                    kwargs["on_measurement_starting"] = lambda: send(("started", None))
                result = getattr(instrument, name)(**kwargs)
                if name.startswith("measure_"):
                    result = with_session(result)
            response = ("result", result)
        except MeasurementAbortedError as error:
            try:
                partial = (
                    with_session(error.partial_measurement)
                    if error.partial_measurement is not None
                    else None
                )
                response = ("aborted", (error.measurement_type, partial))
            except Exception as save_error:
                response = (
                    "error",
                    f"Measurement aborted and its partial session could not be saved: {save_error}",
                )
        except BaseException as error:
            response = ("error", f"{type(error).__name__}: {error}")
        finally:
            busy = False
        send(response)

    while True:
        try:
            ready = wait([normal, control])
            if control in ready:
                name = control.recv()
                if name == "shutdown":
                    break
                if name == "abort":

                    def abort():
                        try:
                            instrument.abort_measurement()
                        except Exception:
                            pass

                    threading.Thread(target=abort, daemon=True).start()
                    control.send(True)
            if normal in ready:
                name, kwargs = normal.recv()
                if busy:
                    send(("error", "PalmSens worker is busy"))
                    continue
                busy = True
                threading.Thread(
                    target=perform, args=(name, kwargs), daemon=True
                ).start()
        except (EOFError, OSError):
            break


class PalmSensWorker:
    def __init__(self):
        self._connected = False
        self._active = False
        self.faulted = False
        self._lock = threading.Lock()
        self._control_lock = threading.Lock()
        self._start()

    def _start(self):
        context = mp.get_context("spawn")
        self._normal, normal = context.Pipe()
        self._control, control = context.Pipe()
        self._process = context.Process(
            target=_worker, args=(normal, control), name="palmsens-sdk", daemon=True
        )
        self._process.start()
        normal.close()
        control.close()

    @property
    def connected(self):
        return self._connected and not self.faulted

    @property
    def abort_available(self):
        return self._active

    def _call(
        self, name, *, timeout=30, on_data=None, on_measurement_starting=None, **kwargs
    ):
        if self.faulted:
            raise RuntimeError(
                "PalmSens communication failed; verify physical state and recover the instrument"
            )
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("PalmSens is already in use")
        try:
            self._normal.send((name, kwargs))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not self._normal.poll(min(0.1, max(0, deadline - time.monotonic()))):
                    if not self._process.is_alive():
                        raise RuntimeError("PalmSens SDK worker exited")
                    continue
                kind, value = self._normal.recv()
                if kind == "data" and on_data:
                    on_data(value)
                elif kind == "started" and on_measurement_starting:
                    on_measurement_starting()
                elif kind == "result":
                    return value
                elif kind == "aborted":
                    from palmsens.emstat4s import MeasurementAbortedError

                    raise MeasurementAbortedError(value[1], measurement_type=value[0])
                elif kind == "error":
                    raise RuntimeError(value)
            self.faulted = True
            self.abort_measurement()
            raise TimeoutError(
                "PalmSens SDK timed out. Hardware state is unknown; recovery is required."
            )
        except (EOFError, OSError):
            self.faulted = True
            raise RuntimeError("PalmSens worker connection was lost")
        finally:
            self._lock.release()

    def connect(self):
        result = self._call("connect")
        self._connected = True
        return result

    def disconnect(self):
        result = self._call("disconnect")
        self._connected = False
        return result

    def abort_measurement(self):
        with self._control_lock:
            if not self._process.is_alive():
                return False
            self._control.send("abort")
            if self._control.poll(1):
                return self._control.recv()
            self.faulted = True
            return False

    def _measure(self, name, **kwargs):
        self._active = True
        settings = kwargs["settings"]
        if hasattr(settings, "run_time_s"):
            duration = settings.run_time_s + settings.equilibration_time_s
        elif hasattr(settings, "scan_count"):
            travel = (
                abs(settings.vertex1_potential_v - settings.begin_potential_v)
                + abs(settings.vertex2_potential_v - settings.vertex1_potential_v)
                + abs(settings.vertex2_potential_v - settings.begin_potential_v)
            )
            duration = (
                settings.equilibration_time_s
                + settings.scan_count * travel / settings.scan_rate_v_per_s
            )
        else:
            duration = settings.sweep_count * (
                settings.equilibration_time_s
                + abs(settings.end_potential_v - settings.begin_potential_v)
                / settings.scan_rate_v_per_s
            )
        try:
            return self._call(name, timeout=max(30, 1.25 * duration + 30), **kwargs)
        finally:
            self._active = False

    def measure_chronoamperometry(self, **kw):
        return self._measure("measure_chronoamperometry", **kw)

    def measure_cyclic_voltammetry(self, **kw):
        return self._measure("measure_cyclic_voltammetry", **kw)

    def measure_linear_sweep_voltammetry(self, **kw):
        return self._measure("measure_linear_sweep_voltammetry", **kw)

    def estimate_chronoamperometry_duration_s(self, **kw):
        return self._call("estimate_chronoamperometry_duration_s", **kw)

    def estimate_cyclic_voltammetry_duration_s(self, **kw):
        return self._call("estimate_cyclic_voltammetry_duration_s", **kw)

    def estimate_linear_sweep_voltammetry_duration_s(self, **kw):
        return self._call("estimate_linear_sweep_voltammetry_duration_s", **kw)

    def load_session(self, path):
        return self._call("load_session", path=str(path))

    def recover(self):
        self.close(force=True)
        self._connected = False
        self._active = False
        self.faulted = False
        self._start()

    def close(self, *, force=False):
        if self._active and not force:
            raise RuntimeError("PalmSens is measuring")
        if self._process.is_alive():
            try:
                self._control.send("shutdown")
            except (OSError, EOFError):
                pass
            self._process.join(1)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(1)
        self._normal.close()
        self._control.close()
