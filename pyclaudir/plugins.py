"""Plugin config loader for pyclaudir.

Single repo-root file ``plugins.json`` declares:

* ``tool_groups`` — toggles for the Claude Code built-in tool groups
  (``bash``, ``code``, ``subagents``). The env vars
  ``PYCLAUDIR_ENABLE_BASH/CODE/SUBAGENTS`` still work as overrides; if
  the env var is set to ``"1"`` the group is on, otherwise the JSON
  value wins.
* ``mcps`` — one entry per external MCP server. Each entry names the
  command to spawn, ``args``/``env`` (with ``${VAR}`` interpolation
  from the process env), the ``allowed_tools`` list to add to
  ``--allowedTools``, and an ``enabled`` flag.
* ``skills_disabled`` — names of directories under ``skills/`` to
  hide from the agent.

Behavior on a fresh clone (no ``plugins.json``) is locked-down:
empty plugins, no external MCPs, no skill toggles. Default file
shipped at repo root mirrors today's credential-gated integrations.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger(__name__)

#: Top-level keys allowed in ``plugins.json``. Any other key crashes boot.
_TOP_LEVEL_KEYS = frozenset({
    "tool_groups",
    "mcps",
    "skills_disabled",
    "builtin_tools_disabled",
})

#: Per-MCP keys allowed in each entry of ``mcps``. Any other key crashes boot.
#: ``type`` is optional and defaults to ``"stdio"`` for backward compatibility.
#: ``command``/``args``/``env`` are valid only on stdio entries; ``url`` and
#: ``headers`` only on ``http``/``sse``.
_MCP_KEYS = frozenset({
    "name", "type", "allowed_tools", "enabled",
    # stdio
    "command", "args", "env",
    # http / sse
    "url", "headers",
})

#: Always-required per-MCP keys (regardless of transport).
_MCP_REQUIRED = frozenset({"name", "allowed_tools", "enabled"})

#: Supported MCP transports. Mirrors what Claude Code's ``--mcp-config``
#: accepts. ``stdio`` spawns a subprocess; ``http`` / ``sse`` reach a
#: remote server (auth via static ``headers``). OAuth flows aren't
#: managed by pyclaudir — supply an already-issued token via
#: ``${VAR}`` interpolation.
_MCP_TRANSPORTS = frozenset({"stdio", "http", "sse"})

#: Per-transport key constraints.
_STDIO_ONLY = frozenset({"command", "args", "env"})
_REMOTE_ONLY = frozenset({"url", "headers"})

#: Tool-group names recognised in ``tool_groups``.
_TOOL_GROUP_KEYS = frozenset({"bash", "code", "subagents"})

#: ``${VAR}`` reference. Bare ``$VAR`` is intentionally not supported —
#: the brace form is unambiguous and what the schema documents.
_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


class PluginsConfigError(RuntimeError):
    """Malformed ``plugins.json``. Boot must fail loudly."""


@dataclass(frozen=True)
class McpPluginSpec:
    """One external MCP server, post-interpolation, ready to wire up.

    Three transports are supported, mirroring the canonical Anthropic
    MCP server config shape that Claude Code's ``--mcp-config`` reads:

    * **stdio** — subprocess; ``command`` is required, ``args``/``env``
      optional.
    * **http** — remote streamable-HTTP server; ``url`` is required,
      static ``headers`` optional (typical auth: ``"Authorization":
      "Bearer ${TOKEN}"``).
    * **sse** — remote SSE server; same field shape as ``http``.

    The unused fields stay at their defaults (``None`` / empty
    mapping) per transport.
    """

    name: str
    type: str
    allowed_tools: tuple[str, ...]
    # stdio
    command: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    # http / sse
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Plugins:
    """Parsed ``plugins.json``."""

    tool_groups: Mapping[str, bool] = field(default_factory=dict)
    mcps: tuple[McpPluginSpec, ...] = ()
    skills_disabled: frozenset[str] = frozenset()
    #: Names of built-in pyclaudir tools (e.g. ``create_poll``,
    #: ``render_html``) to exclude from MCP registration. The names
    #: are validated against the discovered tool classes at
    #: ``McpServer`` construction time — a typo crashes boot with a
    #: list of available names.
    builtin_tools_disabled: frozenset[str] = frozenset()


def _interp_str(value: str, env: Mapping[str, str]) -> str | None:
    """Substitute every ``${VAR}`` in ``value`` from ``env``.

    Returns the substituted string, or ``None`` if any referenced var is
    missing or empty. Callers treat ``None`` as "skip this plugin" —
    matches today's "credentials missing → MCP not spawned" semantics.
    """
    missing: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        var = match.group(1)
        v = env.get(var, "")
        if v == "":
            missing.append(var)
            return ""
        return v

    out = _VAR_RE.sub(_sub, value)
    if missing:
        return None
    return out


def _interp_str_dict(
    raw: Mapping[str, Any], env: Mapping[str, str], *, owner: str, field_name: str,
) -> tuple[dict[str, str] | None, str | None]:
    """Interpolate every value in a ``{str: str}`` mapping.

    Returns ``(out, None)`` on success, or ``(None, "field[key]")``
    when any value's ``${VAR}`` is unresolved.
    """
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise PluginsConfigError(
                f"mcps[{owner!r}].{field_name} entries must be string→string"
            )
        sub = _interp_str(v, env)
        if sub is None:
            return None, f"{field_name}[{k}]"
        out[k] = sub
    return out, None


def _interp_mcp(raw: dict[str, Any], env: Mapping[str, str]) -> tuple[McpPluginSpec | None, str | None]:
    """Interpolate one MCP entry. Returns ``(spec, None)`` on success,
    or ``(None, reason)`` if an unresolved ``${VAR}`` was found.
    """
    name = raw["name"]
    transport = raw.get("type", "stdio")

    if transport == "stdio":
        args_raw = raw.get("args", []) or []
        args_out: list[str] = []
        for i, a in enumerate(args_raw):
            if not isinstance(a, str):
                raise PluginsConfigError(
                    f"mcps[{name!r}].args[{i}] must be a string"
                )
            sub = _interp_str(a, env)
            if sub is None:
                return None, f"args[{i}]"
            args_out.append(sub)

        env_out, missing = _interp_str_dict(
            raw.get("env", {}) or {}, env, owner=name, field_name="env",
        )
        if env_out is None:
            return None, missing

        return (
            McpPluginSpec(
                name=name,
                type=transport,
                allowed_tools=tuple(raw["allowed_tools"]),
                command=raw["command"],
                args=tuple(args_out),
                env=env_out,
            ),
            None,
        )

    # http / sse — remote server
    url = raw["url"]
    if not isinstance(url, str):
        raise PluginsConfigError(f"mcps[{name!r}].url must be a string")
    url_sub = _interp_str(url, env)
    if url_sub is None:
        return None, "url"

    headers_out, missing = _interp_str_dict(
        raw.get("headers", {}) or {}, env, owner=name, field_name="headers",
    )
    if headers_out is None:
        return None, missing

    return (
        McpPluginSpec(
            name=name,
            type=transport,
            allowed_tools=tuple(raw["allowed_tools"]),
            url=url_sub,
            headers=headers_out,
        ),
        None,
    )


def _validate_top_level(data: Any, path: Path) -> None:
    if not isinstance(data, dict):
        raise PluginsConfigError(f"{path}: top level must be a JSON object")
    unknown = set(data.keys()) - _TOP_LEVEL_KEYS
    if unknown:
        raise PluginsConfigError(
            f"{path}: unknown top-level key(s): {sorted(unknown)}; "
            f"allowed: {sorted(_TOP_LEVEL_KEYS)}"
        )


def _validate_tool_groups(raw: Any, path: Path) -> dict[str, bool]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PluginsConfigError(f"{path}: tool_groups must be an object")
    unknown = set(raw.keys()) - _TOOL_GROUP_KEYS
    if unknown:
        raise PluginsConfigError(
            f"{path}: tool_groups has unknown key(s): {sorted(unknown)}; "
            f"allowed: {sorted(_TOOL_GROUP_KEYS)}"
        )
    out: dict[str, bool] = {}
    for k, v in raw.items():
        if not isinstance(v, bool):
            raise PluginsConfigError(
                f"{path}: tool_groups.{k} must be true/false, got {v!r}"
            )
        out[k] = v
    return out


def _validate_mcps(raw: Any, path: Path) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PluginsConfigError(f"{path}: mcps must be an array")
    seen_names: set[str] = set()
    out: list[dict[str, Any]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise PluginsConfigError(f"{path}: mcps[{i}] must be an object")
        unknown = set(entry.keys()) - _MCP_KEYS
        if unknown:
            raise PluginsConfigError(
                f"{path}: mcps[{i}] has unknown key(s): {sorted(unknown)}; "
                f"allowed: {sorted(_MCP_KEYS)}"
            )
        missing = _MCP_REQUIRED - set(entry.keys())
        if missing:
            raise PluginsConfigError(
                f"{path}: mcps[{i}] missing required key(s): {sorted(missing)}"
            )
        name = entry["name"]
        if not isinstance(name, str) or not name:
            raise PluginsConfigError(
                f"{path}: mcps[{i}].name must be a non-empty string"
            )
        if name in seen_names:
            raise PluginsConfigError(
                f"{path}: mcps has duplicate name {name!r}; names become the "
                f"mcp__<name>__ namespace and must be unique"
            )
        seen_names.add(name)
        transport = entry.get("type", "stdio")
        if transport not in _MCP_TRANSPORTS:
            raise PluginsConfigError(
                f"{path}: mcps[{name!r}].type must be one of "
                f"{sorted(_MCP_TRANSPORTS)}; got {transport!r}"
            )
        present = set(entry.keys())
        if transport == "stdio":
            forbidden = present & _REMOTE_ONLY
            if forbidden:
                raise PluginsConfigError(
                    f"{path}: mcps[{name!r}] is type=stdio but has "
                    f"remote-only key(s) {sorted(forbidden)}; use "
                    f"command/args/env, not url/headers"
                )
            if "command" not in entry:
                raise PluginsConfigError(
                    f"{path}: mcps[{name!r}] (type=stdio) requires 'command'"
                )
            if not isinstance(entry["command"], str) or not entry["command"]:
                raise PluginsConfigError(
                    f"{path}: mcps[{name!r}].command must be a non-empty string"
                )
        else:  # http or sse
            forbidden = present & _STDIO_ONLY
            if forbidden:
                raise PluginsConfigError(
                    f"{path}: mcps[{name!r}] is type={transport} but has "
                    f"stdio-only key(s) {sorted(forbidden)}; use url/headers, "
                    f"not command/args/env"
                )
            if "url" not in entry:
                raise PluginsConfigError(
                    f"{path}: mcps[{name!r}] (type={transport}) requires 'url'"
                )
            if not isinstance(entry["url"], str) or not entry["url"]:
                raise PluginsConfigError(
                    f"{path}: mcps[{name!r}].url must be a non-empty string"
                )
        if not isinstance(entry["enabled"], bool):
            raise PluginsConfigError(
                f"{path}: mcps[{name!r}].enabled must be true/false"
            )
        allowed = entry["allowed_tools"]
        if not isinstance(allowed, list) or not allowed:
            raise PluginsConfigError(
                f"{path}: mcps[{name!r}].allowed_tools must be a non-empty list"
            )
        for j, t in enumerate(allowed):
            if not isinstance(t, str) or not t:
                raise PluginsConfigError(
                    f"{path}: mcps[{name!r}].allowed_tools[{j}] must be a non-empty string"
                )
        out.append(entry)
    return out


def _validate_skills_disabled(raw: Any, path: Path) -> frozenset[str]:
    return _validate_string_list(raw, path, key="skills_disabled")


def _validate_builtin_tools_disabled(raw: Any, path: Path) -> frozenset[str]:
    return _validate_string_list(raw, path, key="builtin_tools_disabled")


def _validate_string_list(raw: Any, path: Path, *, key: str) -> frozenset[str]:
    if raw is None:
        return frozenset()
    if not isinstance(raw, list):
        raise PluginsConfigError(f"{path}: {key} must be an array")
    for i, name in enumerate(raw):
        if not isinstance(name, str) or not name:
            raise PluginsConfigError(
                f"{path}: {key}[{i}] must be a non-empty string"
            )
    return frozenset(raw)


def load_plugins(path: Path, *, env: Mapping[str, str] | None = None) -> Plugins:
    """Load and validate ``plugins.json``.

    A missing file is fine and produces an empty :class:`Plugins`. A
    file with malformed JSON or a schema violation raises
    :class:`PluginsConfigError`. An ``${VAR}`` that resolves empty in
    an enabled MCP is logged at INFO and that MCP is skipped.
    """
    if env is None:
        env = os.environ

    if not path.exists():
        example = path.with_suffix(path.suffix + ".example")
        if example.exists():
            log.warning(
                "no plugins.json at %s — using locked-down defaults (no "
                "external MCPs, no tool groups). Copy the shipped example "
                "to enable integrations: cp %s %s",
                path, example, path,
            )
        else:
            log.info("no plugins.json at %s — using locked-down defaults", path)
        return Plugins()

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PluginsConfigError(f"could not read {path}: {exc}") from exc

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise PluginsConfigError(f"{path}: invalid JSON: {exc}") from exc

    _validate_top_level(data, path)
    tool_groups = _validate_tool_groups(data.get("tool_groups"), path)
    mcps_raw = _validate_mcps(data.get("mcps"), path)
    skills_disabled = _validate_skills_disabled(data.get("skills_disabled"), path)
    builtin_tools_disabled = _validate_builtin_tools_disabled(
        data.get("builtin_tools_disabled"), path,
    )

    mcps_out: list[McpPluginSpec] = []
    for entry in mcps_raw:
        name = entry["name"]
        if not entry["enabled"]:
            log.info("mcp %s skipped (disabled in plugins.json)", name)
            continue
        spec, missing = _interp_mcp(entry, env)
        if spec is None:
            log.info("mcp %s skipped (unresolved ${VAR} in %s)", name, missing)
            continue
        mcps_out.append(spec)

    return Plugins(
        tool_groups=tool_groups,
        mcps=tuple(mcps_out),
        skills_disabled=skills_disabled,
        builtin_tools_disabled=builtin_tools_disabled,
    )
