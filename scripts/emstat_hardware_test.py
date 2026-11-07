"""Run EmStat4S integration tests against a PalmSens dummy cell.

Run this module from the project root in the project environment:

    python -m scripts.emstat_hardware_test

Close PSTrace and any other program that may be using the instrument first.
These tests perform real chronoamperometry measurements at 0.5 V.
"""

from __future__ import annotations

import json
import logging
import math
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

import pypalmsens as ps
from pypalmsens.data import Measurement

from palmsens.emstat4s import (
    ChronoamperometrySettings,
    EmStat4S,
    EmStatDataBatch,
    MeasurementAbortedError,
    PowerFrequencyHz,
)

logging.basicConfig(level=logging.INFO)

INTERVAL_TIME_S = 0.1
POTENTIAL_V = 0.5
SHORT_RUN_TIME_S = 0.5


def _settings(*, run_time_s: float) -> ChronoamperometrySettings:
    return ChronoamperometrySettings(
        interval_time_s=INTERVAL_TIME_S, potential_v=POTENTIAL_V, run_time_s=run_time_s
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _expect_value_error(name: str, action: Callable[[], object]) -> None:
    try:
        action()
    except ValueError:
        return

    raise AssertionError(f"{name} did not raise ValueError")


def _curve_values(measurement: Measurement) -> tuple[list[float], list[float]]:
    _require(
        measurement.n_curves == 1, f"Expected one curve; got {measurement.n_curves}"
    )

    curve = measurement.curves[0]
    times_s = [float(value) for value in curve.x_array.to_list()]
    currents_ua = [float(value) for value in curve.y_array.to_list()]

    _require(curve.x_array.quantity == "Time", "Curve X quantity is not Time")
    _require(curve.x_array.unit == "s", "Curve X unit is not seconds")
    _require(curve.y_array.quantity == "Current", "Curve Y quantity is not Current")
    _require(curve.y_array.unit == "µA", "Curve Y unit is not microamperes")
    _require(len(times_s) == len(currents_ua), "Curve X/Y lengths differ")

    return times_s, currents_ua


def _assert_complete_measurement(
    measurement: Measurement, settings: ChronoamperometrySettings
) -> None:
    times_s, currents_ua = _curve_values(measurement)
    expected_points = round(settings.run_time_s / settings.interval_time_s) + 1

    _require(
        len(times_s) == expected_points,
        f"Expected {expected_points} points; got {len(times_s)}",
    )
    _require(bool(times_s), "Measurement contains no points")
    _require(
        math.isclose(times_s[0], 0.0, rel_tol=0.0, abs_tol=1e-9),
        f"First time is {times_s[0]!r}, not 0 s",
    )
    _require(
        math.isclose(times_s[-1], settings.run_time_s, rel_tol=0.0, abs_tol=1e-9),
        f"Last time is {times_s[-1]!r}, not {settings.run_time_s} s",
    )
    _require(
        all(left <= right for left, right in pairwise(times_s)),
        "Measurement times are not monotonic",
    )
    _require(
        all(math.isfinite(value) for value in currents_ua),
        "Measurement contains a non-finite current",
    )


def _flatten_batches(batches: list[EmStatDataBatch]) -> tuple[list[float], list[float]]:
    times_s = [time_s for batch in batches for time_s in batch.x_values]
    currents_ua = [current_ua for batch in batches for current_ua in batch.currents_ua]
    return times_s, currents_ua


def _assert_callback_matches(
    measurement: Measurement, batches: list[EmStatDataBatch]
) -> None:
    _require(bool(batches), "The callback did not receive any batches")

    callback_times_s, callback_currents_ua = _flatten_batches(batches)
    curve_times_s, curve_currents_ua = _curve_values(measurement)

    _require(
        len(callback_times_s) == len(curve_times_s),
        "Callback and curve contain different numbers of time values",
    )
    _require(
        len(callback_currents_ua) == len(curve_currents_ua),
        "Callback and curve contain different numbers of current values",
    )

    for index, (callback_value, curve_value) in enumerate(
        zip(callback_times_s, curve_times_s)
    ):
        _require(
            math.isclose(callback_value, curve_value, rel_tol=1e-12, abs_tol=1e-12),
            f"Time mismatch at index {index}: "
            f"callback={callback_value!r}, curve={curve_value!r}",
        )

    for index, (callback_value, curve_value) in enumerate(
        zip(callback_currents_ua, curve_currents_ua)
    ):
        _require(
            math.isclose(callback_value, curve_value, rel_tol=1e-12, abs_tol=1e-12),
            f"Current mismatch at index {index}: "
            f"callback={callback_value!r}, curve={curve_value!r}",
        )


def _assert_jsonl_file(path: Path) -> None:
    _require(path.is_file(), f"Stream file was not created: {path}")
    _require(path.stat().st_size > 0, f"Stream file is empty: {path}")

    record_count = 0
    with path.open(encoding="utf-8") as stream_file:
        for line_number, line in enumerate(stream_file, start=1):
            if not line.strip():
                continue

            try:
                json.loads(line)
            except json.JSONDecodeError as error:
                raise AssertionError(
                    f"Invalid JSONL record at {path}:{line_number}"
                ) from error

            record_count += 1

    _require(record_count > 0, f"Stream file has no JSONL records: {path}")


def _drain_queue(data_queue: queue.Queue[EmStatDataBatch]) -> list[EmStatDataBatch]:
    batches: list[EmStatDataBatch] = []

    while True:
        try:
            batches.append(data_queue.get_nowait())
        except queue.Empty:
            return batches


def test_validation() -> None:
    print("\n[1/6] Validation")

    invalid_settings: tuple[tuple[str, Callable[[], object]], ...] = (
        ("zero interval", lambda: ChronoamperometrySettings(0.0, POTENTIAL_V, 1.0)),
        (
            "negative interval",
            lambda: ChronoamperometrySettings(-0.1, POTENTIAL_V, 1.0),
        ),
        (
            "non-finite interval",
            lambda: ChronoamperometrySettings(math.nan, POTENTIAL_V, 1.0),
        ),
        ("non-finite potential", lambda: ChronoamperometrySettings(0.1, math.inf, 1.0)),
        ("zero duration", lambda: ChronoamperometrySettings(0.1, POTENTIAL_V, 0.0)),
        (
            "negative duration",
            lambda: ChronoamperometrySettings(0.1, POTENTIAL_V, -1.0),
        ),
        (
            "non-finite duration",
            lambda: ChronoamperometrySettings(0.1, POTENTIAL_V, math.inf),
        ),
    )

    for name, action in invalid_settings:
        _expect_value_error(name, action)

    invalid_frequency = cast(PowerFrequencyHz, 55)
    _expect_value_error(
        "invalid power frequency",
        lambda: EmStat4S(power_frequency_hz=invalid_frequency),
    )

    print("PASS: invalid settings were rejected without using the instrument")


def test_argument_combinations() -> None:
    print("\n[2/6] Optional callback and stream combinations")

    settings = _settings(run_time_s=SHORT_RUN_TIME_S)
    emstat = EmStat4S(power_frequency_hz=50)
    cases = (
        ("neither", False, False),
        ("callback_only", True, False),
        ("stream_only", False, True),
        ("callback_and_stream", True, True),
    )

    with TemporaryDirectory(prefix="emstat-test-") as temporary_directory:
        output_directory = Path(temporary_directory)

        for name, use_callback, use_stream in cases:
            batches: list[EmStatDataBatch] = []
            stream_path = output_directory / f"{name}.jsonl" if use_stream else None

            measurement = emstat.measure_chronoamperometry(
                settings=settings,
                on_data=batches.append if use_callback else None,
                stream_path=stream_path,
            )

            _assert_complete_measurement(measurement, settings)

            if use_callback:
                _assert_callback_matches(measurement, batches)

            if stream_path is not None:
                _assert_jsonl_file(stream_path)

            if name == "callback_and_stream":
                session_path = output_directory / "emstat_test.pssession"
                ps.save_session_file(session_path, [measurement])
                _require(session_path.is_file(), "Session file was not created")
                _require(session_path.stat().st_size > 0, "Session file is empty")

            print(f"PASS: {name}")


def test_sequential_measurements() -> None:
    print("\n[3/6] Sequential reuse")

    settings = _settings(run_time_s=SHORT_RUN_TIME_S)
    emstat = EmStat4S(power_frequency_hz=50)

    _require(
        emstat.abort_measurement() is False,
        "Abort unexpectedly reported an active measurement before the test",
    )

    for measurement_number in range(1, 4):
        measurement = emstat.measure_chronoamperometry(settings=settings)
        _assert_complete_measurement(measurement, settings)
        _require(
            emstat.abort_measurement() is False,
            "Manager remained active after a completed measurement",
        )
        print(f"PASS: sequential measurement {measurement_number}")


def test_realtime_delivery() -> None:
    print("\n[4/6] Real-time queue delivery")

    settings = _settings(run_time_s=2.0)
    emstat = EmStat4S(power_frequency_hz=50)
    data_queue: queue.Queue[EmStatDataBatch] = queue.Queue()
    batches: list[EmStatDataBatch] = []
    received_before_completion = False

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            emstat.measure_chronoamperometry, settings=settings, on_data=data_queue.put
        )
        deadline = time.monotonic() + 10.0

        while not future.done():
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                emstat.abort_measurement()
                raise AssertionError("Real-time measurement did not finish in time")

            try:
                batch = data_queue.get(timeout=min(0.25, remaining_s))
            except queue.Empty:
                continue

            batches.append(batch)
            if not future.done():
                received_before_completion = True

        measurement = future.result()

    batches.extend(_drain_queue(data_queue))

    _require(
        received_before_completion,
        "No callback data arrived before the measurement completed",
    )
    _assert_complete_measurement(measurement, settings)
    _assert_callback_matches(measurement, batches)

    print("PASS: callback data was consumed while measurement was active")


def test_concurrent_rejection() -> None:
    print("\n[5/6] Concurrent measurement rejection")

    active_settings = _settings(run_time_s=2.0)
    short_settings = _settings(run_time_s=SHORT_RUN_TIME_S)
    emstat = EmStat4S(power_frequency_hz=50)
    first_batch_received = threading.Event()
    batches: list[EmStatDataBatch] = []

    def on_data(batch: EmStatDataBatch) -> None:
        batches.append(batch)
        first_batch_received.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            emstat.measure_chronoamperometry, settings=active_settings, on_data=on_data
        )

        if not first_batch_received.wait(timeout=10.0):
            emstat.abort_measurement()
            raise AssertionError("The first measurement did not produce data in time")

        started_at = time.monotonic()
        try:
            emstat.measure_chronoamperometry(settings=short_settings)
        except RuntimeError as error:
            _require(
                str(error) == "An Emstat4 measurement is already in progress",
                f"Unexpected concurrent-measurement error: {error}",
            )
        else:
            raise AssertionError("A concurrent measurement was not rejected")

        rejection_time_s = time.monotonic() - started_at
        _require(
            rejection_time_s < 1.0,
            f"Concurrent rejection took {rejection_time_s:.3f} seconds",
        )

        measurement = future.result(timeout=10.0)

    _assert_complete_measurement(measurement, active_settings)
    _assert_callback_matches(measurement, batches)

    recovery_measurement = emstat.measure_chronoamperometry(settings=short_settings)
    _assert_complete_measurement(recovery_measurement, short_settings)

    print("PASS: overlap was rejected and later measurement succeeded")


def test_abort_and_recovery() -> None:
    print("\n[6/6] Abort and recovery")

    active_settings = _settings(run_time_s=10.0)
    recovery_settings = _settings(run_time_s=SHORT_RUN_TIME_S)
    emstat = EmStat4S(power_frequency_hz=50)
    first_batch_received = threading.Event()

    def on_data(_: EmStatDataBatch) -> None:
        first_batch_received.set()

    with TemporaryDirectory(prefix="emstat-abort-test-") as temporary_directory:
        stream_path = Path(temporary_directory) / "aborted_measurement.jsonl"

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                emstat.measure_chronoamperometry,
                settings=active_settings,
                on_data=on_data,
                stream_path=stream_path,
            )

            if not first_batch_received.wait(timeout=10.0):
                emstat.abort_measurement()
                raise AssertionError(
                    "The abort-test measurement did not produce data in time"
                )

            _require(
                emstat.abort_measurement() is True,
                "Abort did not find the active measurement",
            )

            try:
                future.result(timeout=5.0)
            except FutureTimeoutError as error:
                raise AssertionError(
                    "Measurement did not terminate within 5 seconds of abort"
                ) from error
            except MeasurementAbortedError as error:
                partial_measurement = error.partial_measurement

                if partial_measurement is None:
                    print("Abort returned no partial measurement")
                else:
                    times_s, _ = _curve_values(partial_measurement)
                    print(
                        "Abort returned a partial measurement containing "
                        f"{len(times_s)} complete point(s)"
                    )
            except Exception as error:
                raise AssertionError("Abort raised an unexpected exception") from error
            else:
                raise AssertionError(
                    "Aborted measurement was returned as successfully completed"
                )

        _require(
            emstat.abort_measurement() is False,
            "Manager remained active after abortion completed",
        )
        _assert_jsonl_file(stream_path)

    recovery_measurement = emstat.measure_chronoamperometry(settings=recovery_settings)
    _assert_complete_measurement(recovery_measurement, recovery_settings)

    print("PASS: abort terminated promptly and later measurement succeeded")


def main() -> None:
    print("EmStat4S hardware tests")
    print("Use a PalmSens dummy cell and close other instrument software first.")

    test_validation()
    test_argument_combinations()
    test_sequential_measurements()
    test_realtime_delivery()
    test_concurrent_rejection()
    test_abort_and_recovery()

    print("\nAll automated EmStat4S hardware tests passed.")
    print(
        "Remaining manual check: while idle, disconnect the instrument, verify "
        "that measurement reports 'No EmStat4 instrument was found', reconnect, "
        "and verify that a short measurement succeeds."
    )


if __name__ == "__main__":
    main()
