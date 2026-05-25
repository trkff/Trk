"""
Structured logging system with daily file rotation and SQLite persistence.
Two destinations: console (stdout) and daily rotated file.

Custom levels (beyond standard DEBUG/INFO/WARNING/ERROR/CRITICAL):
  CANDLE  = 15  — high-frequency candle read events (shown only in debug mode)
  SIGNALS = 22  — signal detection events (always shown)
  BACKTEST = 24 — backtest progress events (always shown)
"""

import logging
import threading
import sys
from pathlib import Path
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler

from bot import db

# ── Custom log levels ──────────────────────────────────────────────────────
CANDLE   = 15   # below INFO — noisy candle events, visible only in debug mode
SIGNALS  = 22   # above INFO — signal detection, always visible
BACKTEST = 24   # above INFO — backtest progress, always visible

logging.addLevelName(CANDLE,   "CANDLE")
logging.addLevelName(SIGNALS,  "SIGNALS")
logging.addLevelName(BACKTEST, "BACKTEST")

# Thread-local flag: set to True inside backtest simulation threads so that
# log.signals() calls from strategies are silently dropped (backtest signals
# are not live-bot signals and should not appear in the SIGNALS filter).
_backtest_local = threading.local()


def in_backtest() -> bool:
    return getattr(_backtest_local, "active", False)


def set_backtest_mode(active: bool) -> None:
    _backtest_local.active = active


def _log_candle(self, message, *args, **kwargs):
    if self.isEnabledFor(CANDLE):
        self._log(CANDLE, message, args, **kwargs)


def _log_signals(self, message, *args, **kwargs):
    if in_backtest():
        return  # suppress strategy signals during backtest simulation
    if self.isEnabledFor(SIGNALS):
        self._log(SIGNALS, message, args, **kwargs)


def _log_backtest(self, message, *args, **kwargs):
    if self.isEnabledFor(BACKTEST):
        self._log(BACKTEST, message, args, **kwargs)


logging.Logger.candle   = _log_candle
logging.Logger.signals  = _log_signals
logging.Logger.backtest = _log_backtest

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)


class DbLogHandler(logging.Handler):
    """Writes log records into the SQLite logs table."""

    def emit(self, record):
        try:
            ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
            db.insert_log(ts, record.levelname, record.name, self.format(record))
        except Exception:
            pass  # never crash on logging


def setup_logger(name: str = "bot", debug: bool = False) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    level = logging.DEBUG if debug else logging.INFO
    logger.setLevel(level)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)-14s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # Daily rotated file handler
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = LOGS_DIR / f"bot_{today}.log"
    file_handler = TimedRotatingFileHandler(
        str(log_file), when="midnight", interval=1, backupCount=30, utc=True
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    file_handler.suffix = "%Y-%m-%d"
    logger.addHandler(file_handler)

    # SQLite handler
    db_handler = DbLogHandler()
    db_handler.setLevel(level)
    db_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(db_handler)

    return logger


def set_debug(enabled: bool):
    """Toggle DEBUG level on all bot loggers at runtime."""
    level = logging.DEBUG if enabled else logging.INFO
    for name in list(logging.Logger.manager.loggerDict):
        if name.startswith("bot"):
            lg = logging.getLogger(name)
            lg.setLevel(level)
            for h in lg.handlers:
                h.setLevel(level)


def get_logger(module: str) -> logging.Logger:
    return logging.getLogger(f"bot.{module}")
