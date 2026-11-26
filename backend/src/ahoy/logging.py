"""Log redaction: no credential reaches a log line.

`RedactingFilter` rewrites a record in place: the message is merged with its args and
redacted, and any exception / stack text is formatted and redacted up front (a Formatter
reuses `record.exc_text`, so the raw traceback is never rendered again).

`install_logging()` attaches the filter to the root and uvicorn loggers and to their
handlers. A logger's own filters do not see records propagated from child loggers (e.g.
`ahoy.github` -> root), and uvicorn replaces its handlers when it configures logging, so
the same filter is also applied to every record at creation by wrapping `Logger.makeRecord`
(after `extra=` is copied on, which a record factory would miss). Redaction is idempotent,
so a record passing several of these is harmless.

Limits: an `extra=` container (dict/list/tuple/set) is replaced by its redacted `str()` only
when redaction changed it; otherwise the object stays. A Logger subclass that overrides
`makeRecord` without calling super() is covered only by the handler filters. A record whose
redaction itself fails (e.g. a broken `__str__`/`__repr__`) is replaced by a fixed
placeholder: logging never raises into the caller, never drops the record, never leaks.
"""

from __future__ import annotations

import logging
import re
import traceback
from typing import Any

from ahoy.config import Settings
from ahoy.github.http import redact

LOGGER_NAMES = ("", "uvicorn", "uvicorn.error", "uvicorn.access", "ahoy")
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
}

_EXTRA_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"github_pat_[A-Za-z0-9_]+"), "github_pat_[REDACTED]"),
    # Login / pre-auth cookie values: Cookie and Set-Cookie strings, dict reprs and JSON.
    (
        re.compile(r"((?:__Host-)?ahoy_(?:sid|preauth))(['\"]?\s*[:=]\s*['\"]?)[^;,\s\"']+"),
        r"\1\2[REDACTED]",
    ),
    # Authorization as a dict repr / JSON key, with or without a scheme.
    (
        re.compile(r"(?i)(authorization['\"]\s*:\s*['\"])[^'\"\r\n]+"),
        r"\1[REDACTED]",
    ),
)


def redact_log(text: str) -> str:
    """GitHub token shapes, Authorization/Bearer/Basic values, JWTs, aHoy cookie values."""
    text = redact(text)
    for pattern, repl in _EXTRA_REDACTIONS:
        text = pattern.sub(repl, text)
    return text


UNLOGGABLE = "<unloggable record redacted>"


def _message(record: logging.LogRecord) -> str:
    try:
        return record.getMessage()
    except Exception:  # a bad format string must not break logging, nor leak args raw
        return f"{record.msg!s} {record.args!r}"


def _scrub(record: logging.LogRecord) -> None:
    """Fallback when redaction fails: keep nothing that could hold a secret."""
    record.msg = UNLOGGABLE
    record.args = None
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    for name in list(vars(record)):
        if name not in _STANDARD_ATTRS:
            setattr(record, name, "[REDACTED]")


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            self._redact(record)
        except Exception:
            _scrub(record)
        return True

    @staticmethod
    def _redact(record: logging.LogRecord) -> None:
        record.msg = redact_log(_message(record))
        record.args = None
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
            record.exc_info = None  # only the redacted text survives
        if record.exc_text:
            record.exc_text = redact_log(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_log(record.stack_info)
        # `extra=` fields and anything else set on the record.
        for name, value in list(vars(record).items()):
            if name in _STANDARD_ATTRS:
                continue
            if isinstance(value, str):
                setattr(record, name, redact_log(value))
            elif isinstance(value, dict | list | tuple | set | frozenset):
                text = str(value)
                clean = redact_log(text)
                if clean != text:
                    setattr(record, name, clean)


_FILTER = RedactingFilter()
# Reload-safe: if a wrapper is already installed, wrap what it wraps, never the wrapper.
_current = logging.Logger.makeRecord
_original_make_record = getattr(_current, "_ahoy_original", _current)


def _make_record(self: logging.Logger, *args: Any, **kwargs: Any) -> logging.LogRecord:
    # After `extra=` has been copied onto the record, unlike a record factory.
    record = _original_make_record(self, *args, **kwargs)
    _FILTER.filter(record)  # never raises: falls back to _scrub
    return record


_make_record._ahoy_wrapper = True  # type: ignore[attr-defined]
_make_record._ahoy_original = _original_make_record  # type: ignore[attr-defined]


def _install_make_record() -> None:
    if not getattr(logging.Logger.makeRecord, "_ahoy_wrapper", False):
        setattr(logging.Logger, "makeRecord", _make_record)  # noqa: B010


def install_logging(settings: Settings | None = None) -> RedactingFilter:
    """Attach the redacting filter everywhere records can be emitted. Idempotent."""
    _install_make_record()
    if settings is not None and settings.ahoy_env == "prod":
        # Handler.handleError would print the raw exception chain (args included) to stderr.
        logging.raiseExceptions = False
    for name in LOGGER_NAMES:
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)
        for handler in logger.handlers:
            if _FILTER not in handler.filters:
                handler.addFilter(_FILTER)
    last = logging.lastResort
    if last is not None and _FILTER not in last.filters:
        last.addFilter(_FILTER)
    logging.getLogger("ahoy.request").setLevel(logging.INFO)
    return _FILTER
