"""Locally-hosted MCP server for pyclaudir.

We run the FastMCP streamable-HTTP ASGI app under uvicorn on a random port on
``127.0.0.1``. The Claude Code subprocess is launched with ``--mcp-config``
pointing at a temp file describing this server, so the subprocess never
discovers tools by any other path.

Tool discovery is fully automatic: at startup we walk every module in
``pyclaudir/tools/``, collect every ``BaseTool`` subclass, instantiate it with
the shared :class:`ToolContext`, and register a flat-parameter wrapper with
FastMCP.
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import pkgutil
import tempfile
import time
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP

from . import tools as tools_pkg
from .tools.base import BaseTool, ToolContext, ToolResult

log = logging.getLogger(__name__)

#: The MCP "server name" Claude sees. Tool names become ``mcp__<server>__<name>``.
MCP_SERVER_NAME = "pyclaudir"


def discover_tool_classes() -> list[type[BaseTool]]:
    """Walk ``pyclaudir.tools`` and return every concrete BaseTool subclass."""
    found: list[type[BaseTool]] = []
    seen: set[str] = set()
    for mod_info in pkgutil.iter_modules(tools_pkg.__path__):
        if mod_info.name in {"base", "__init__"}:
            continue
        module = importlib.import_module(f"{tools_pkg.__name__}.{mod_info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if not issubclass(obj, BaseTool) or obj is BaseTool:
                continue
            if inspect.isabstract(obj):
                continue
            if obj.__name__ in seen:
                continue
            seen.add(obj.__name__)
            found.append(obj)
    return found


def _make_wrapper(tool: BaseTool, db_logger):
    """Build a flat-parameter callable FastMCP can introspect.

    Pydantic field info is dropped because FastMCP reads ``inspect.signature``,
    not Pydantic, but the input schema (types, required, defaults) is
    preserved. The wrapper validates with the model, runs the tool, beats the
    heartbeat, and audit-logs the call.
    """
    args_model = tool.args_model
    fields = args_model.model_fields

    params = []
    for fname, finfo in fields.items():
        default = inspect.Parameter.empty if finfo.is_required() else finfo.default
        params.append(
            inspect.Parameter(
                fname,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=finfo.annotation,
            )
        )
    sig = inspect.Signature(parameters=params, return_annotation=str)

    async def wrapper(**kwargs: Any) -> str:
        start = time.perf_counter()
        err: str | None = None
        result: ToolResult | None = None
        try:
            args = args_model(**kwargs)
            result = await tool.run(args)
        except Exception as exc:  # surfaced to Claude as a tool error string
            err = f"{type(exc).__name__}: {exc}"
            log.exception("tool %s failed", tool.name)
            result = ToolResult(content=err, is_error=True)
        finally:
            tool.ctx.heartbeat.beat()
            duration_ms = int((time.perf_counter() - start) * 1000)
            if db_logger is not None:
                try:
                    await db_logger(
                        tool_name=tool.name,
                        args_json=json.dumps(kwargs, default=str),
                        result_json=None if err else json.dumps(
                            {"content": result.content, "data": result.data} if result else {},
                            default=str,
                        ),
                        error=err,
                        duration_ms=duration_ms,
                    )
                except Exception:  # pragma: no cover - audit must never crash a tool
                    log.exception("audit log failed for tool %s", tool.name)
        if result and result.is_error:
            # Raising here makes FastMCP report it as a tool error, which
            # Claude can see and react to.
            raise RuntimeError(result.content)
        return result.content if result else ""

    wrapper.__name__ = tool.name
    wrapper.__doc__ = tool.description
    wrapper.__signature__ = sig  # type: ignore[attr-defined]
    wrapper.__annotations__ = {p.name: p.annotation for p in params} | {"return": str}
    return wrapper


def build_fastmcp(ctx: ToolContext, *, db_logger=None) -> tuple[FastMCP, list[BaseTool]]:
    """Construct a FastMCP server with every discovered tool registered."""
    mcp = FastMCP(name=MCP_SERVER_NAME)
    instances: list[BaseTool] = []
    for cls in discover_tool_classes():
        instance = cls(ctx)
        instances.append(instance)
        wrapper = _make_wrapper(instance, db_logger)
        mcp.add_tool(wrapper, name=instance.name, description=instance.description)
        log.info("registered MCP tool %s", instance.name)
    return mcp, instances


class McpServer:
    """Run a FastMCP HTTP server on a random localhost port via uvicorn."""

    def __init__(self, ctx: ToolContext, *, db_logger=None) -> None:
        self._ctx = ctx
        self._db_logger = db_logger
        self.mcp, self.tools = build_fastmcp(ctx, db_logger=db_logger)
        self._server: uvicorn.Server | None = None
        self._task = None
        self.port: int | None = None

    @property
    def url(self) -> str:
        if self.port is None:
            raise RuntimeError("MCP server has not started yet")
        return f"http://127.0.0.1:{self.port}/mcp"

    def write_mcp_config(
        self,
        path: Path | None = None,
        *,
        extra_servers: dict | None = None,
    ) -> Path:
        """Write the JSON file pyclaudir hands to ``claude --mcp-config``.

        ``extra_servers`` is merged into ``mcpServers`` alongside our local
        pyclaudir server. Use it to add external MCP servers (e.g.
        Atlassian) without touching this module's internals.
        """
        servers = {
            MCP_SERVER_NAME: {
                "type": "http",
                "url": self.url,
            }
        }
        if extra_servers:
            servers.update(extra_servers)
        cfg = {"mcpServers": servers}
        if path is None:
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", prefix="pyclaudir-mcp-", delete=False
            )
            path = Path(tmp.name)
            tmp.close()
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return path

    async def start(self) -> None:
        import asyncio

        app = self.mcp.streamable_http_app()
        config = uvicorn.Config(
            app=app,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        # Start the server as a background task; wait until uvicorn assigns
        # a port (it does so in startup before the serve loop begins).
        self._task = asyncio.create_task(self._server.serve(), name="pyclaudir-mcp")
        for _ in range(200):  # ~2s
            await asyncio.sleep(0.01)
            if self._server.started and self._server.servers:
                socks = self._server.servers[0].sockets
                if socks:
                    self.port = socks[0].getsockname()[1]
                    log.info("MCP server listening on %s", self.url)
                    return
        raise RuntimeError("MCP server failed to start within 2s")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await self._task
            except Exception:  # pragma: no cover
                log.exception("MCP server task crashed during shutdown")
        self._server = None
        self._task = None
