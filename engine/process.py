"""Authenticated local engine transport, durable command IDs, exclusive ownership."""

import hashlib
import json
import logging
import os
import pickle
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from multiprocessing.connection import Client, Listener
from pathlib import Path
from uuid import uuid4

from engine.service import ExperimentEngine


@dataclass(frozen=True)
class CallableAttribute:
    name: str


class OwnerLock:
    def __init__(self, path):
        self.file = open(path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0)
                self.file.write(b"0")
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            self.file.close()
            raise RuntimeError("An engine already owns this workspace")

    def close(self):
        self.file.close()


class CommandJournal:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.cache = {}
        with self._connection() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS command_journal (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, updated REAL NOT NULL)"
            )
            c.execute(
                "UPDATE command_journal SET status='interrupted' WHERE status='running'"
            )

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def begin(self, identifier, payload):
        fingerprint = hashlib.sha256(pickle.dumps(payload)).hexdigest()
        with self.lock, self._connection() as c:
            old = c.execute(
                "SELECT fingerprint,status FROM command_journal WHERE id=?",
                (identifier,),
            ).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise RuntimeError("Command ID was reused for a different request")
                if identifier in self.cache:
                    return self.cache[identifier]
                raise RuntimeError(
                    f"This command was already {old[1]}; inspect current state instead of replaying it"
                )
            c.execute(
                "INSERT INTO command_journal VALUES (?,?,?,?)",
                (identifier, fingerprint, "running", time.time()),
            )
        return None

    def finish(self, identifier, response):
        with self.lock, self._connection() as c:
            c.execute(
                "UPDATE command_journal SET status=?,updated=? WHERE id=?",
                (
                    "completed" if response[0] == "result" else "failed",
                    time.time(),
                    identifier,
                ),
            )
            self.cache[identifier] = response
            if len(self.cache) > 1000:
                self.cache.pop(next(iter(self.cache)))


def _serve(root, simulate, ready=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(process)d | %(threadName)s | %(name)s | %(levelname)s | %(message)s",
    )
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    endpoint = root / "engine-endpoint.json"
    owner = None
    engine = None
    listener = None
    try:
        owner = OwnerLock(root / "engine.lock")
        engine = ExperimentEngine(root, simulate=simulate)
        journal = CommandJournal(root / "commands.sqlite3")
        auth = os.urandom(32)
        listener = Listener(("127.0.0.1", 0), authkey=auth)
        info = {
            "address": list(listener.address),
            "auth": auth.hex(),
            "pid": os.getpid(),
            "simulation": simulate,
        }
        fd = os.open(endpoint, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(info, f)
        if ready is not None:
            ready.send(("ready", info))
            ready.close()
        shutdown = threading.Event()
        lease = threading.Lock()

        def handle(connection):
            if not lease.acquire(blocking=False):
                connection.send(
                    ("error", "Another interface already controls this engine")
                )
                connection.close()
                return
            send_lock = threading.Lock()
            jobs = []

            def send(identifier, response):
                try:
                    with send_lock:
                        connection.send((identifier, response))
                except (OSError, EOFError):
                    engine.request_pause()

            connection.send(("ready", None))

            def execute(identifier, kind, target, name, args, kwargs):
                mutation = False
                journal_started = False
                try:
                    if (
                        target not in engine.targets
                        or name.startswith("_")
                        or name
                        in {"invoke", "targets", "instrument", "root", "publish"}
                    ):
                        raise ValueError("Unsupported engine request")
                    attribute = getattr(engine.targets[target], name)
                    if kind == "get":
                        response = (
                            "result",
                            CallableAttribute(name)
                            if callable(attribute)
                            else attribute,
                        )
                    else:
                        mutation = not name.startswith(
                            (
                                "list_",
                                "load_",
                                "current_",
                                "available_",
                                "objective_",
                                "syringe_",
                                "source_stock",
                                "source_setup",
                            )
                        ) and name not in {
                            "snapshot",
                            "events",
                            "data_since",
                            "source_line_state_editable",
                            "source_fraction_for_concentration",
                        }
                        if mutation:
                            old = journal.begin(
                                identifier, (target, name, args, kwargs)
                            )
                            if old is not None:
                                send(identifier, old)
                                return
                            journal_started = True
                        result = engine.invoke(target, name, args, kwargs)
                        response = ("result", result)
                        if target == "engine" and name == "close":
                            shutdown.set()
                except BaseException as error:
                    response = ("error", f"{type(error).__name__}: {error}")
                if journal_started:
                    journal.finish(identifier, response)
                send(identifier, response)

            try:
                while not shutdown.is_set():
                    if not connection.poll(0.2):
                        continue
                    message = connection.recv()
                    if message == "detach":
                        break
                    job = threading.Thread(target=execute, args=message, daemon=True)
                    jobs = [j for j in jobs if j.is_alive()]
                    jobs.append(job)
                    job.start()
            except (EOFError, OSError):
                pass
            finally:
                engine.request_pause()
                connection.close()
                lease.release()

        # accept() is woken by the client after a successful shutdown request.
        while not shutdown.is_set():
            connection = listener.accept()
            if shutdown.is_set():
                connection.close()
                break
            threading.Thread(target=handle, args=(connection,), daemon=True).start()
    except BaseException as error:
        try:
            if ready is not None:
                ready.send(("error", str(error)))
            else:
                print(str(error), file=sys.stderr, flush=True)
        except (OSError, EOFError):
            pass
    finally:
        if listener:
            listener.close()
        if engine and not engine.busy:
            try:
                engine.close()
            except Exception:
                pass
        if owner:
            endpoint.unlink(missing_ok=True)
            owner.close()


class RemoteController:
    def __init__(self, client, target):
        self._client = client
        self._target = target

    def __getattr__(self, name):
        aliases = {
            "repository": "repository",
            "runze_controller": "runze",
            "emstat_controller": "emstat",
            "engine": "engine",
        }
        if name in aliases:
            return getattr(self._client, aliases[name])
        result = self._client.request("get", self._target, name).result(timeout=5)
        if isinstance(result, CallableAttribute):
            return lambda *args, **kwargs: self._client.request(
                "call", self._target, name, args, kwargs
            ).result(timeout=None)
        return result


class EngineProcess:
    """A detached subprocess does not make UI exit wait for the engine."""

    def __init__(self, process):
        self.process = process

    def is_alive(self):
        return self.process.poll() is None

    def join(self, seconds=None):
        try:
            self.process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            pass

    def terminate(self):
        self.process.terminate()


class EngineClient:
    def __init__(self, root, *, simulate=False, start=True):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._process = None
        endpoint = self.root / "engine-endpoint.json"
        info = None
        connection = None
        if endpoint.exists():
            try:
                candidate = json.loads(endpoint.read_text())
                connection = Client(
                    tuple(candidate["address"]),
                    authkey=bytes.fromhex(candidate["auth"]),
                )
                info = candidate
            except (ConnectionError, OSError, EOFError):
                pass
        if info is None:
            if not start:
                raise RuntimeError("No engine is running")
            command = [
                sys.executable,
                "-m",
                "engine.process",
                "--serve",
                str(self.root),
            ]
            if simulate:
                command.append("--simulate")
            with open(self.root / "engine.log", "ab", buffering=0) as log:
                self._process = EngineProcess(
                    subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        start_new_session=os.name != "nt",
                    )
                )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if not self._process.is_alive():
                    raise RuntimeError(
                        "Engine startup failed. See " + str(self.root / "engine.log")
                    )
                if endpoint.exists():
                    try:
                        candidate = json.loads(endpoint.read_text())
                        connection = Client(
                            tuple(candidate["address"]),
                            authkey=bytes.fromhex(candidate["auth"]),
                        )
                        info = candidate
                        break
                    except (ConnectionError, OSError, EOFError, json.JSONDecodeError):
                        pass
                time.sleep(0.02)
            if info is None:
                raise TimeoutError(
                    "Engine startup timed out. See " + str(self.root / "engine.log")
                )
        if connection is None:
            raise RuntimeError("No engine connection")
        if info["simulation"] != simulate:
            connection.close()
            raise RuntimeError("The existing engine uses a different hardware mode")
        kind, value = connection.recv()
        if kind == "error":
            connection.close()
            raise RuntimeError(value)
        self.info = info
        self.connection = connection
        self._pending = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._closed = False
        for name in ("engine", "runze", "emstat", "experiment", "repository"):
            setattr(self, name, RemoteController(self, name))
        threading.Thread(
            target=self._receive, name="engine-events", daemon=True
        ).start()

    def _receive(self):
        try:
            while not self._closed:
                identifier, (kind, value) = self.connection.recv()
                with self._lock:
                    future = self._pending.pop(identifier, None)
                if future:
                    if kind == "result":
                        future.set_result(value)
                    else:
                        future.set_exception(RuntimeError(value))
        except (OSError, EOFError, AttributeError, ValueError, TypeError):
            with self._lock:
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(
                            RuntimeError(
                                "Engine connection lost; reconnect and inspect recovery state"
                            )
                        )
                self._pending.clear()

    def request(self, kind, target, name, args=(), kwargs=None, *, identifier=None):
        identifier = identifier or uuid4().hex
        future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("Interface is disconnected")
            if identifier in self._pending:
                raise RuntimeError("This command ID is already pending")
            self._pending[identifier] = future
        with self._send_lock:
            self.connection.send((identifier, kind, target, name, args, kwargs or {}))
        return future

    def detach(self):
        if self._closed:
            return
        with self._send_lock:
            try:
                self.connection.send("detach")
            except (OSError, EOFError):
                pass
        self._closed = True
        self.connection.close()

    def close(self):
        self.request("call", "engine", "close").result(timeout=5)
        # Wake the listener so the owner releases its lock and exits.
        try:
            wake = Client(
                tuple(self.info["address"]), authkey=bytes.fromhex(self.info["auth"])
            )
            wake.close()
        except (OSError, EOFError):
            pass
        self.detach()
        if self._process:
            self._process.join(5)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", required=True)
    parser.add_argument("--simulate", action="store_true")
    options = parser.parse_args()
    from engine.process import _serve as run_server

    run_server(options.serve, options.simulate)
