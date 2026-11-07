import logging.config
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIRECTORY = PROJECT_ROOT / "logs"
APPLICATION_LOG_FILE = LOG_DIRECTORY / "autonomous_flow.log"
RUNZE_TRACE_LOG_FILE = LOG_DIRECTORY / "runze_trace.log"


def _build_logging_config() -> dict[str, Any]:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": (
                    "%(asctime)s | %(levelname)-8s | "
                    "PID=%(process)d | %(threadName)s | "
                    "%(name)s | %(message)s"
                ),
                "datefmt": "%Y-%m-%d %H:%M:%S",
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": "INFO",
                "formatter": "standard",
                "stream": "ext://sys.stderr",
            },
            "application_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "DEBUG",
                "formatter": "standard",
                "filename": str(APPLICATION_LOG_FILE),
                "maxBytes": 5 * 1024 * 1024,
                "backupCount": 5,
                "encoding": "utf-8",
                "delay": True,
            },
            "runze_trace_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "INFO",
                "formatter": "standard",
                "filename": str(RUNZE_TRACE_LOG_FILE),
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 5,
                "encoding": "utf-8",
                "delay": True,
            },
        },
        "loggers": {
            "runze.trace": {
                "level": "INFO",
                "handlers": ["runze_trace_file"],
                "propagate": False,
            },
            "pypalmsens": {"level": "INFO"},
            "asyncio": {"level": "WARNING"},
            "nicegui": {"level": "WARNING"},
            "uvicorn": {"level": "WARNING"},
            "uvicorn.access": {"level": "WARNING"},
            "watchfiles": {"level": "WARNING"},
            "pymodbus": {"level": "WARNING"},
        },
        "root": {"level": "DEBUG", "handlers": ["console", "application_file"]},
    }


def configure_logging() -> Path:
    """Configure application-wide logging and return the log path."""
    LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)

    logging.config.dictConfig(_build_logging_config())

    return APPLICATION_LOG_FILE
