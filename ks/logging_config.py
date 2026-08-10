"""Startup/lifecycle logging and structured per-tool-call JSONL logging.

configure_logging() must run as the first executable statement in
ks/server.py -- before the FastMCP()/AuthSettings() construction -- so that
an import-time failure (the historical AuthSettings crash, see
ks-authsettings-startup-crash-incident-v1.0.md) is itself logged rather than
producing a silent, traceless crash-loop.
"""

import functools
import inspect
import json
import logging
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

from mcp.server.auth.middleware.auth_context import get_access_token

from . import config

ROOT_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
ISO_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"

_tools_logger = logging.getLogger("ks.tools")
_tools_logger.propagate = False


def configure_logging(level: int = logging.INFO) -> None:
    """Root handler to stderr with ISO-8601 timestamps. Idempotent."""
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(fmt=ROOT_FMT, datefmt=ISO_DATEFMT))
    root.addHandler(handler)
    root.setLevel(level)

    _configure_tools_handler()
    _patch_uvicorn_logging_config()


def _configure_tools_handler() -> None:
    if _tools_logger.handlers:
        return
    config.TOOLS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        config.TOOLS_LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    _tools_logger.addHandler(handler)
    _tools_logger.setLevel(logging.INFO)


def _patch_uvicorn_logging_config() -> None:
    """Prepend a timestamp to uvicorn's default/access formatters in place.

    uvicorn.Config.__init__'s log_config parameter defaults to a direct
    reference to uvicorn.config.LOGGING_CONFIG, applied via dictConfig at
    Config-construction time -- deep inside mcp.run(), well after this
    module has already run. Mutating the dict object in place (rather than
    replacing it) is what makes the change visible to that later call.
    Relies on that shared-mutable-default behavior holding across uvicorn
    versions -- re-verify empirically after any uvicorn upgrade.
    """
    import uvicorn.config as uv_config

    prefix = "%(asctime)s "
    for fmt_name in ("default", "access"):
        formatter = uv_config.LOGGING_CONFIG["formatters"][fmt_name]
        if not formatter["fmt"].startswith(prefix):
            formatter["fmt"] = prefix + formatter["fmt"]
        formatter["datefmt"] = ISO_DATEFMT


def get_client_id() -> str:
    """Originating OAuth client for write attribution (Decision 29)."""
    token = get_access_token()
    if token is not None and getattr(token, "client_id", None):
        return token.client_id
    return "unauthenticated"


def _classify_outcome(result: object) -> str:
    if isinstance(result, dict):
        if "ok" in result:
            return "ok" if result["ok"] else "rejected"
        if "error" in result:
            return "rejected"
    return "ok"


def _build_detail(tool_name: str, bound_args: dict, result: object) -> dict:
    if tool_name == "vault_search":
        count = None
        if isinstance(result, dict) and isinstance(result.get("results"), list):
            count = len(result["results"])
        return {"query": bound_args.get("query"), "result_count": count}
    if tool_name in ("vault_write", "vault_patch", "vault_archive", "vault_browse"):
        return {"path": bound_args.get("path")}
    if tool_name == "vault_reindex":
        return {"force": bound_args.get("force", False)}
    return {}


def logged_tool(fn):
    """Structured JSONL logging for an MCP tool call.

    Apply beneath @mcp.tool() (i.e. wrap the raw function first). FastMCP's
    schema generation still sees the true signature/docstring through
    functools.wraps' __wrapped__ (inspect.signature follows it by default),
    while the callable it registers and invokes is this wrapper.
    """
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.monotonic()
        client_id = get_client_id()
        try:
            bound_args = sig.bind_partial(*args, **kwargs).arguments
        except TypeError:
            bound_args = kwargs

        outcome = "error"
        result = None
        error = None
        try:
            result = fn(*args, **kwargs)
            outcome = _classify_outcome(result)
            return result
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            record = {
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "client_id": client_id,
                "tool": fn.__name__,
                "outcome": outcome,
                "duration_ms": round((time.monotonic() - start) * 1000, 1),
                "detail": _build_detail(fn.__name__, bound_args, result),
                "error": error,
            }
            _tools_logger.info(json.dumps(record, default=str))

    return wrapper
