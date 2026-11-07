"""Load the vendor runtime only when a real SDK operation is requested."""

import importlib
import os
from pathlib import Path


def load():
    if not os.environ.get("DOTNET_ROOT") and Path("/usr/local/share/dotnet").is_dir():
        os.environ["DOTNET_ROOT"] = "/usr/local/share/dotnet"
    try:
        return importlib.import_module("pypalmsens")
    except (ImportError, RuntimeError) as error:
        raise RuntimeError(
            "PalmSens could not start. Install the .NET 9 runtime for your machine "
            "and check DOTNET_ROOT. You can use --simulate without the SDK."
        ) from error


def save_session_file(path, measurements):
    return load().save_session_file(path, measurements)


def load_session_file(path):
    return load().load_session_file(path)


def __getattr__(name):
    return getattr(load(), name)
