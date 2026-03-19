"""Load and parse upstream MCP server config (Claude Desktop / claude.json format)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class ServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # Per-server approval rules (override global)
    mode: str | None = None          # "all" | "destructive" | "annotated" | "none"
    always_allow: list[str] = field(default_factory=list)   # tool names always allowed without approval
    always_deny: list[str] = field(default_factory=list)    # tool names always blocked


def load_upstream_config(path: str | Path) -> list[ServerConfig]:
    """
    Load MCP server config from a JSON file.

    Supports multiple formats:
    1. Claude Desktop format:
       { "mcpServers": { "name": { "command": ..., "args": [...], "env": {...} } } }

    2. Single server (direct):
       { "command": "npx", "args": [...] }

    3. Array of servers:
       [{ "name": "fs", "command": "npx", "args": [...] }, ...]

    Each server can optionally include an "approvalRules" block:
       {
         "command": "...",
         "approvalRules": {
           "mode": "destructive",
           "alwaysAllow": ["read_file", "list_dir"],
           "alwaysDeny": ["delete_file"]
         }
       }
    """
    data = json.loads(Path(path).read_text())

    servers: list[ServerConfig] = []

    if isinstance(data, list):
        for item in data:
            servers.append(_parse_server_entry(item.get("name", f"server-{len(servers)}"), item))
    elif "mcpServers" in data:
        for name, entry in data["mcpServers"].items():
            servers.append(_parse_server_entry(name, entry))
    elif "command" in data:
        servers.append(_parse_server_entry("upstream", data))
    else:
        raise ValueError(f"Unrecognised config format in {path}")

    return servers


def _parse_server_entry(name: str, entry: dict) -> ServerConfig:
    rules = entry.get("approvalRules", {})

    # Expand env vars in command / args
    command = os.path.expandvars(entry["command"])
    args = [os.path.expandvars(str(a)) for a in entry.get("args", [])]

    # Merge provided env with current env defaults
    raw_env = {k: os.path.expandvars(str(v)) for k, v in entry.get("env", {}).items()}

    return ServerConfig(
        name=name,
        command=command,
        args=args,
        env=raw_env,
        mode=rules.get("mode"),
        always_allow=[t.lower() for t in rules.get("alwaysAllow", [])],
        always_deny=[t.lower() for t in rules.get("alwaysDeny", [])],
    )
