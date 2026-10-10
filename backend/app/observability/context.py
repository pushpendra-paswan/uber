from contextvars import ContextVar

# One mutable dict per request (or per background task), created by the middleware. Everything else changes the dict and
# never calls set(): a value written in a threadpool dependency or an SQLAlchemy greenlet runs in a copy of the context,
# and only a change to the SAME dict is still visible when the access line is written.
request_context: ContextVar[dict] = ContextVar("request_context")
