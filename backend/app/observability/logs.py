import json
import logging
import sys
import traceback
from datetime import datetime, timezone

from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.observability.context import request_context

# The only `extra` keys that reach a log line. Any other key is dropped silently, so a field that carries personal data
# cannot get out by accident. Adding a field means adding it here and to tests/test_logging.py.
FIELD_WHITELIST = (
    "request_id", "user_id", "role", "component", "ride_id", "driver_id", "offer_id", "method", "route", "status",
    "duration_ms", "db_queries", "db_ms", "from_status", "to_status", "ws_close_code", "sqlstate", "constraint", "exc_type",
    "stack", "service", "outcome", "count",
)
# Read from the request context at format time, so every line of a request carries them.
CONTEXT_FIELDS = ("request_id", "user_id", "role", "component")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        try:
            line = {
                "ts": datetime.fromtimestamp(record.created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(record.msecs):03d}Z",
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            context = request_context.get(None) or {}
            for key in CONTEXT_FIELDS:
                if context.get(key) is not None:
                    line[key] = context[key]
            for key in FIELD_WHITELIST:
                if key in record.__dict__:
                    line[key] = record.__dict__[key]

            # Never the exception's message: SQLAlchemy messages carry SQL parameters and httpx messages carry URLs.
            if record.exc_info and record.exc_info[1] is not None:
                error = record.exc_info[1]
                line["exc_type"] = type(error).__name__
                line["stack"] = "".join(traceback.format_tb(record.exc_info[2]))
                if isinstance(error, DBAPIError):
                    line["sqlstate"] = getattr(error.orig, "sqlstate", None)
                    line["constraint"] = getattr(error.orig.__cause__, "constraint_name", None)
            return json.dumps(line, default=str)
        except Exception:
            # A formatter that raises would lose the line (and the logging module would print a traceback to stderr).
            return json.dumps({"level": "ERROR", "logger": "app.observability.logs", "msg": "log_format_error"})


def setup_logging() -> None:
    """Called once at startup. One JSON line per record on stdout, for the app's loggers and for uvicorn's."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    # uvicorn prints the client address and the full path with the query string when a WebSocket is accepted. Our own
    # ws_connected line replaces it.
    handler.addFilter(lambda record: not (record.name == "uvicorn.error" and '"WebSocket %s"' in str(record.msg)))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())

    # uvicorn gives its own loggers their own handlers: remove them so everything goes through the one above.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    # The access log prints full paths with query strings. We write our own access line.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers = []
    access_logger.propagate = False
    # httpx logs every request URL at INFO, and the URLs hold coordinates and search text. websockets logs every frame at DEBUG,
    # and the first frame of a connection holds the token.
    for name in ("httpx", "httpcore", "websockets"):
        logging.getLogger(name).setLevel(logging.WARNING)
