"""Deterministic instrument simulation, using the production fluidics code."""

import math
import os
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

from controllers.runze_controller import RunzeController
from engine.measurement import Array, Curve, PortableMeasurement, simulated_bytes
from fluidics.runze_fluidics import RunzeFluidics
from palmsens.emstat4s import EmStat4S, EmStatDataBatch, MeasurementAbortedError
from runze.bus import RunzeBus
from runze.device import RunzeStatus
from runze.protocol import RawRunzeReply
from runze.sv07m import SV07M, SV07MStatus
from runze.sy01b import SY01B


def simulation_delay(variable, default):
    value = float(os.environ.get(variable, str(default)))
    if not math.isfinite(value) or value < 0 or value > 5:
        raise ValueError(f"{variable} must be between 0 and 5 seconds")
    return value


class SimulatedSyringe(SY01B):
    def __init__(self):
        self.syringe_volume_ml = 5.0
        self.position = 0
        self.valve_state = 1
        self.inlet_valve_state = 1
        self.outlet_valve_state = 3
        self.rpm = 10
        self.trace = []
        self.fail_next = False
        self._stopped = threading.Event()

    def query_syringe_position(self):
        return self.position

    def query_syringe_position_or_none(self):
        return self.position

    def query_motor_status(self):
        return RunzeStatus.OK

    def query_valve_status(self):
        return RunzeStatus.OK

    def query_valve_state(self):
        return self.valve_state

    def set_dynamic_speed_rpm(self, rpm):
        self._validate_dynamic_speed_rpm(rpm)
        self.rpm = rpm
        return RawRunzeReply(b"", 0, 0, 0, 0)

    def move_valve_to_state(self, state, wait=True):
        self.valve_state = state
        return RawRunzeReply(b"", 0, 0, 0, 0)

    def move_plunger_to_position(self, position, wait=True):
        self._validate_syringe_position(position)
        if self.fail_next:
            self.fail_next = False
            self.position = (self.position + position) // 2
            raise RuntimeError("Simulated serial failure after partial movement")
        self.trace.append((self.position, position, self.valve_state, self.rpm))
        self.position = position
        time.sleep(simulation_delay("AUTONOMOUS_FLOW_SIMULATION_STEP_DELAY_S", 0.002))
        return RawRunzeReply(b"", 0, 0, 0, 0)

    def synchronize_syringe_position(self, *, wait=True):
        return RawRunzeReply(b"", 0, 0, 0, 0)

    def reset_syringe(self, wait=True):
        self.position = 0
        return RawRunzeReply(b"", 0, 0, 0, 0)

    def emergency_stop(self):
        self._stopped.set()
        return RawRunzeReply(b"", 0, 0, 0, 0)


class SimulatedSelector(SV07M):
    def __init__(self):
        self.port = 1

    def read_status(self):
        return SV07MStatus(RunzeStatus.OK, self.port)

    def ensure_port(self, port):
        self.port = port

    def move_to_port(self, port, wait=True):
        self.port = port
        return RawRunzeReply(b"", 0, 0, 0, 0)

    def reset_to_home(self, wait=True):
        self.port = 1
        return RawRunzeReply(b"", 0, 0, 0, 0)

    def emergency_stop(self):
        return RawRunzeReply(b"", 0, 0, 0, 0)


class SimulatedRunzeController(RunzeController):
    def connect(self, *, port="simulation", trace=False):
        if self.connected:
            raise RuntimeError("Already connected")
        self._bus = cast(RunzeBus, SimpleNamespace(connected=True, close=lambda: None))
        self._syringe = SimulatedSyringe()
        self._selector = SimulatedSelector()
        self._syringe_state = self._selector_state = "ready"
        assert self._plumbing is not None
        self._fluidics = RunzeFluidics(
            syringe=self._syringe,
            selector=self._selector,
            plumbing=self._plumbing,
            run_step=self._run_sequence_step,
        )
        return self._read_status(self._syringe, self._selector)


class SimulatedEmStat(EmStat4S):
    def __init__(self):
        self._connected = False
        self._active = False
        self._stop = threading.Event()

    @property
    def connected(self):
        return self._connected

    @property
    def abort_available(self):
        return self._active

    def connect(self):
        self._connected = True
        return True

    def disconnect(self):
        if self._active:
            raise RuntimeError("Measurement is active")
        self._connected = False
        return True

    def abort_measurement(self):
        self._stop.set()
        return self._active

    def estimate_chronoamperometry_duration_s(self, **kw):
        s = kw["settings"]
        return s.run_time_s + s.equilibration_time_s

    def estimate_cyclic_voltammetry_duration_s(self, **kw):
        s = kw["settings"]
        return (
            s.equilibration_time_s
            + s.scan_count
            * (
                abs(s.vertex1_potential_v - s.begin_potential_v)
                + abs(s.vertex2_potential_v - s.vertex1_potential_v)
                + abs(s.vertex2_potential_v - s.begin_potential_v)
            )
            / s.scan_rate_v_per_s
        )

    def estimate_linear_sweep_voltammetry_duration_s(self, **kw):
        s = kw["settings"]
        return (
            s.equilibration_time_s
            + s.sweep_count
            * abs(s.end_potential_v - s.begin_potential_v)
            / s.scan_rate_v_per_s
        )

    def _simulate_measure(
        self, *, settings, on_data=None, on_measurement_starting=None, **kw
    ):
        self._active = True
        self._stop.clear()
        curves = []
        try:
            if on_measurement_starting:
                on_measurement_starting()
            count = getattr(settings, "scan_count", getattr(settings, "sweep_count", 1))
            for scan in range(count):
                xs = []
                ys = []
                for index in range(41):
                    if self._stop.wait(
                        simulation_delay(
                            "AUTONOMOUS_FLOW_SIMULATION_POINT_DELAY_S", 0.005
                        )
                    ):
                        raise MeasurementAbortedError(
                            None, measurement_type="chronoamperometry"
                        )
                    if hasattr(settings, "run_time_s"):
                        x = index * settings.run_time_s / 40
                    else:
                        x = (
                            settings.begin_potential_v
                            + (
                                getattr(
                                    settings,
                                    "end_potential_v",
                                    getattr(settings, "vertex1_potential_v", 1),
                                )
                                - settings.begin_potential_v
                            )
                            * index
                            / 40
                        )
                    y = 10 * math.exp(-(((index - 25) / 8) ** 2)) + (scan + 1) * 0.2
                    xs.append(x)
                    ys.append(y)
                    if on_data:
                        on_data(EmStatDataBatch(scan + 1, (x,), (y,)))
                curves.append(
                    Curve(f"Scan {scan + 1}", Array(tuple(xs)), Array(tuple(ys)))
                )
            m = PortableMeasurement(
                tuple(curves), "Simulated measurement", datetime.now(UTC)
            )
            return PortableMeasurement(
                m.curves, m.title, m.timestamp, simulated_bytes(m)
            )
        finally:
            self._active = False

    measure_chronoamperometry = _simulate_measure
    measure_cyclic_voltammetry = _simulate_measure
    measure_linear_sweep_voltammetry = _simulate_measure
