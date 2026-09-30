# logging_config.py - structured JSON logging with rotation
import logging
import logging.config
import os

LOG_DIR = os.getenv("LOG_DIR", "/opt/sales-analyzer/logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
            "fmt": "%(asctime)s %(levelname)s %(name)s %(message)s",
            "datefmt": "%Y-%m-%dT%H:%M:%S%z",
        },
        "console": {
            "format": "%(asctime)s %(levelname)-8s %(name)s %(message)s",
            "datefmt": "%H:%M:%S",
        },
    },
    "handlers": {
        "file": {
            "class": "logging.handlers.TimedRotatingFileHandler",
            "filename": os.path.join(os.getenv("LOG_DIR", "/opt/sales-analyzer/logs"), "feed.log"),
            "when": "midnight",
            "interval": 1,
            "backupCount": 30,
            "encoding": "utf-8",
            "formatter": "json",
            "level": "INFO",
        },
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "console",
            "level": "INFO",
        },
        "error_file": {
            "class": "logging.handlers.TimedRotatingFileHandler",
            "filename": os.path.join(os.getenv("LOG_DIR", "/opt/sales-analyzer/logs"), "errors.log"),
            "when": "midnight",
            "interval": 1,
            "backupCount": 90,
            "encoding": "utf-8",
            "formatter": "json",
            "level": "ERROR",
        },
    },
    "root": {
        "level": "INFO",
        "handlers": ["console", "file", "error_file"],
    },
    "loggers": {
        "feed_jenkins_builds": {"level": "INFO", "propagate": True},
        "ml.severity_llm": {"level": "INFO", "propagate": True},
        "watch_builds": {"level": "INFO", "propagate": True},
    },
}

def setup_logging():
    logging.config.dictConfig(LOGGING_CONFIG)