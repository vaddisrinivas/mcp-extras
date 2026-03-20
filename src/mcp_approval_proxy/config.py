"""Load and parse upstream MCP server config (Claude Desktop / claude.json format).

Supported config formats
------------------------

1. Claude Desktop / claude.json::

    {
      "mcpServers": {
        "filesystem": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
          "env": {"MY_VAR": "value"},
          "approvalRules": {
            "mode": "destructive",
            "alwaysAllow":   ["read_file", "list_dir"],
            "alwaysDeny":    ["delete_file"],
            "allowPatterns": ["get_*", "list_*", "read_*"],
            "denyPatterns":  ["*delete*", "*destroy*", "*wipe*"],
            "customAnnotations": {
              "some_risky_tool": {"destructiveHint": true}
            },
            "timeout":       60,
            "timeoutAction": "deny"
          }
        }
      },
      "approvalProxy": {
        "dryRun":              false,
        "auditLog":            "/tmp/mcp-approvals.jsonl",
        "defaultTimeout":      120,
        "defaultTimeoutAction": "deny"
      }
    }

2. Single server (direct)::

    { "command": "npx", "args": [...] }

3. Array of servers::

    [{ "name": "a", "command": "cmd_a" }, ...]

4. HTTP / SSE upstream::

    { "url": "http://localhost:8080/sse" }
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_settings import BaseSettings

_VALID_MODES = {"all", "destructive", "annotated", "none"}
_VALID_TIMEOUT_ACTIONS = {"approve", "deny"}
_VALID_FAILURE_ACTIONS = {"deny", "fallback"}
_DEFAULT_RETRYABLE_STATUS_CODES = [408, 409, 425, 429, 500, 502, 503, 504]
_VALID_DEDUPE_FIELDS = {"server", "tool", "args", "risk"}


class ServerConfig(BaseModel):
    """Configuration for a single upstream MCP server."""

    model_config = ConfigDict(extra="ignore")

    name: str

    # ── Transport ──────────────────────────────────────────────────────────────
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    url: str = ""  # http/sse endpoint
    headers: dict[str, str] = {}  # extra HTTP headers
    transport_type: str = "stdio"  # "stdio"|"http"|"sse"

    # ── Per-server approval rules (override global) ────────────────────────────
    mode: str | None = None  # "all"|"destructive"|"annotated"|"none"
    always_allow: list[str] = []  # exact names → skip approval
    always_deny: list[str] = []  # exact names → hard block
    allow_patterns: list[str] = []  # fnmatch globs → skip approval
    deny_patterns: list[str] = []  # fnmatch globs → hard block
    custom_annotations: dict[str, dict] = {}
    timeout: float | None = None  # elicitation timeout (seconds)
    timeout_action: str | None = None  # "approve"|"deny" on timeout
    approval_ttl_seconds: float | None = None
    explain_decisions: bool | None = None
    high_risk_requires_double_confirmation: bool | None = None
    approval_retry_attempts: int | None = None
    approval_retry_initial_backoff_seconds: float | None = None
    approval_retry_max_backoff_seconds: float | None = None
    approval_retry_backoff_multiplier: float | None = None
    approval_retryable_status_codes: list[int] | None = None
    approval_on_timeout: str | None = None
    approval_on_transport_error: str | None = None
    approval_allow_insecure_http: bool | None = None
    approval_allowed_hosts: list[str] = []
    approval_auth_token: str | None = None
    approval_dedupe_key_fields: list[str] = []
    approval_dedupe_arg_keys: list[str] = []

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_MODES:
            raise ValueError(f"invalid mode {v!r}; expected one of {_VALID_MODES}")
        return v

    @field_validator("timeout_action")
    @classmethod
    def validate_timeout_action(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_TIMEOUT_ACTIONS:
            raise ValueError(
                f"invalid timeoutAction {v!r}; expected one of {_VALID_TIMEOUT_ACTIONS}"
            )
        return v

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("timeout must be >= 0")
        return v

    @field_validator("approval_ttl_seconds")
    @classmethod
    def validate_approval_ttl_seconds(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("approvalTtlSeconds must be >= 0")
        return v

    @field_validator("approval_retry_attempts")
    @classmethod
    def validate_approval_retry_attempts(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("approvalRetryAttempts must be >= 1")
        return v

    @field_validator("approval_retry_initial_backoff_seconds")
    @classmethod
    def validate_approval_retry_initial_backoff_seconds(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("approvalRetryInitialBackoffSeconds must be >= 0")
        return v

    @field_validator("approval_retry_max_backoff_seconds")
    @classmethod
    def validate_approval_retry_max_backoff_seconds(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("approvalRetryMaxBackoffSeconds must be >= 0")
        return v

    @field_validator("approval_retry_backoff_multiplier")
    @classmethod
    def validate_approval_retry_backoff_multiplier(cls, v: float | None) -> float | None:
        if v is not None and v < 1:
            raise ValueError("approvalRetryBackoffMultiplier must be >= 1")
        return v

    @field_validator("approval_on_timeout")
    @classmethod
    def validate_approval_on_timeout(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_FAILURE_ACTIONS:
            raise ValueError(f"approvalOnTimeout must be one of {_VALID_FAILURE_ACTIONS}")
        return v

    @field_validator("approval_on_transport_error")
    @classmethod
    def validate_approval_on_transport_error(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_FAILURE_ACTIONS:
            raise ValueError(f"approvalOnTransportError must be one of {_VALID_FAILURE_ACTIONS}")
        return v

    @field_validator("approval_dedupe_key_fields")
    @classmethod
    def validate_approval_dedupe_key_fields(cls, v: list[str]) -> list[str]:
        invalid = [val for val in v if val not in _VALID_DEDUPE_FIELDS]
        if invalid:
            raise ValueError(f"approvalDedupeKeyFields contains invalid values: {invalid}")
        return v


class ProxyConfig(BaseSettings):
    """Global proxy settings from the top-level ``approvalProxy`` section."""

    model_config = ConfigDict(extra="ignore", env_prefix="APPROVAL_")

    dry_run: bool = False
    audit_log: str | None = None  # path to JSONL audit log
    default_timeout: float = 120.0
    default_timeout_action: str = "deny"  # "approve"|"deny"
    approval_ttl_seconds: float = 0.0
    explain_decisions: bool = False
    high_risk_requires_double_confirmation: bool = False
    approval_retry_attempts: int = 2
    approval_retry_initial_backoff_seconds: float = 0.5
    approval_retry_max_backoff_seconds: float = 5.0
    approval_retry_backoff_multiplier: float = 2.0
    approval_retryable_status_codes: list[int] = list(_DEFAULT_RETRYABLE_STATUS_CODES)
    approval_on_timeout: str = "deny"
    approval_on_transport_error: str = "fallback"
    approval_allow_insecure_http: bool = False
    approval_allowed_hosts: list[str] = []
    approval_auth_token: str | None = None
    approval_dedupe_key_fields: list[str] = ["server", "tool", "args"]
    approval_dedupe_arg_keys: list[str] = []

    @field_validator("default_timeout_action")
    @classmethod
    def validate_default_timeout_action(cls, v: str) -> str:
        if v not in _VALID_TIMEOUT_ACTIONS:
            raise ValueError(f"defaultTimeoutAction must be one of {_VALID_TIMEOUT_ACTIONS}")
        return v

    @field_validator("default_timeout")
    @classmethod
    def validate_default_timeout(cls, v: float) -> float:
        if v < 0:
            raise ValueError("defaultTimeout must be >= 0")
        return v

    @field_validator("approval_ttl_seconds")
    @classmethod
    def validate_approval_ttl_seconds(cls, v: float) -> float:
        if v < 0:
            raise ValueError("approvalTtlSeconds must be >= 0")
        return v

    @field_validator("approval_retry_attempts")
    @classmethod
    def validate_approval_retry_attempts(cls, v: int) -> int:
        if v < 1:
            raise ValueError("approvalRetryAttempts must be >= 1")
        return v

    @field_validator("approval_retry_initial_backoff_seconds")
    @classmethod
    def validate_approval_retry_initial_backoff_seconds(cls, v: float) -> float:
        if v < 0:
            raise ValueError("approvalRetryInitialBackoffSeconds must be >= 0")
        return v

    @field_validator("approval_retry_max_backoff_seconds")
    @classmethod
    def validate_approval_retry_max_backoff_seconds(cls, v: float) -> float:
        if v < 0:
            raise ValueError("approvalRetryMaxBackoffSeconds must be >= 0")
        return v

    @field_validator("approval_retry_backoff_multiplier")
    @classmethod
    def validate_approval_retry_backoff_multiplier(cls, v: float) -> float:
        if v < 1:
            raise ValueError("approvalRetryBackoffMultiplier must be >= 1")
        return v

    @field_validator("approval_on_timeout")
    @classmethod
    def validate_approval_on_timeout(cls, v: str) -> str:
        if v not in _VALID_FAILURE_ACTIONS:
            raise ValueError(f"approvalOnTimeout must be one of {_VALID_FAILURE_ACTIONS}")
        return v

    @field_validator("approval_on_transport_error")
    @classmethod
    def validate_approval_on_transport_error(cls, v: str) -> str:
        if v not in _VALID_FAILURE_ACTIONS:
            raise ValueError(f"approvalOnTransportError must be one of {_VALID_FAILURE_ACTIONS}")
        return v

    @field_validator("approval_dedupe_key_fields")
    @classmethod
    def validate_approval_dedupe_key_fields(cls, v: list[str]) -> list[str]:
        invalid = [val for val in v if val not in _VALID_DEDUPE_FIELDS]
        if invalid:
            raise ValueError(f"approvalDedupeKeyFields contains invalid values: {invalid}")
        return v


def load_upstream_config(path: str | Path) -> tuple[list[ServerConfig], ProxyConfig]:
    """
    Parse an upstream config file.

    Returns ``(servers, proxy_config)`` where *proxy_config* is populated from
    the optional top-level ``approvalProxy`` section.
    """
    data = json.loads(Path(path).read_text())
    servers: list[ServerConfig] = []

    if isinstance(data, list):
        for item in data:
            servers.append(_parse_server_entry(item.get("name", f"server-{len(servers)}"), item))
    elif "mcpServers" in data:
        for name, entry in data["mcpServers"].items():
            servers.append(_parse_server_entry(name, entry))
    elif "command" in data or "url" in data:
        servers.append(_parse_server_entry("upstream", data))
    else:
        raise ValueError(f"Unrecognised config format in {path}")

    proxy_section = data.get("approvalProxy", {}) if isinstance(data, dict) else {}
    return servers, _parse_proxy_config(proxy_section)


# ── Internal helpers ──────────────────────────────────────────────────────────


def _parse_proxy_config(section: dict) -> ProxyConfig:
    """Parse approvalProxy section into ProxyConfig."""
    normalized = {
        "dry_run": section.get("dryRun", False),
        "audit_log": section.get("auditLog"),
        "default_timeout": float(section.get("defaultTimeout", 120.0)),
        "default_timeout_action": str(section.get("defaultTimeoutAction", "deny")),
        "approval_ttl_seconds": float(section.get("approvalTtlSeconds", 0.0)),
        "explain_decisions": bool(section.get("explainDecisions", False)),
        "high_risk_requires_double_confirmation": bool(
            section.get("highRiskRequiresDoubleConfirmation", False)
        ),
        "approval_retry_attempts": int(section.get("approvalRetryAttempts", 2)),
        "approval_retry_initial_backoff_seconds": float(
            section.get("approvalRetryInitialBackoffSeconds", 0.5)
        ),
        "approval_retry_max_backoff_seconds": float(
            section.get("approvalRetryMaxBackoffSeconds", 5.0)
        ),
        "approval_retry_backoff_multiplier": float(
            section.get("approvalRetryBackoffMultiplier", 2.0)
        ),
        "approval_retryable_status_codes": [
            int(code)
            for code in section.get(
                "approvalRetryableStatusCodes",
                _DEFAULT_RETRYABLE_STATUS_CODES,
            )
        ],
        "approval_on_timeout": str(section.get("approvalOnTimeout", "deny")),
        "approval_on_transport_error": str(section.get("approvalOnTransportError", "fallback")),
        "approval_allow_insecure_http": bool(section.get("approvalAllowInsecureHttp", False)),
        "approval_allowed_hosts": [str(v).lower() for v in section.get("approvalAllowedHosts", [])],
        "approval_auth_token": (
            str(section["approvalAuthToken"]) if "approvalAuthToken" in section else None
        ),
        "approval_dedupe_key_fields": [
            str(v).lower()
            for v in section.get("approvalDedupeKeyFields", ["server", "tool", "args"])
        ],
        "approval_dedupe_arg_keys": [str(v) for v in section.get("approvalDedupeArgKeys", [])],
    }
    return ProxyConfig(**normalized)


def _parse_server_entry(name: str, entry: dict) -> ServerConfig:
    """Parse a server entry dict into ServerConfig."""
    rules: dict = entry.get("approvalRules", {})

    url = entry.get("url", "")
    transport_type = "stdio"
    if url:
        transport_type = "sse" if url.rstrip("/").endswith("/sse") else "http"

    command = os.path.expandvars(entry.get("command", ""))
    args = [os.path.expandvars(str(a)) for a in entry.get("args", [])]
    raw_env = {k: os.path.expandvars(str(v)) for k, v in entry.get("env", {}).items()}
    headers = {str(k): str(v) for k, v in entry.get("headers", {}).items()}

    normalized = {
        "name": name,
        "command": command,
        "args": args,
        "env": raw_env,
        "url": url,
        "headers": headers,
        "transport_type": transport_type,
        "mode": rules.get("mode"),
        "always_allow": [t.lower() for t in rules.get("alwaysAllow", [])],
        "always_deny": [t.lower() for t in rules.get("alwaysDeny", [])],
        "allow_patterns": [p.lower() for p in rules.get("allowPatterns", [])],
        "deny_patterns": [p.lower() for p in rules.get("denyPatterns", [])],
        "custom_annotations": {
            k.lower(): dict(v) for k, v in rules.get("customAnnotations", {}).items()
        },
        "timeout": float(rules["timeout"]) if "timeout" in rules else None,
        "timeout_action": rules.get("timeoutAction"),
        "approval_ttl_seconds": (
            float(rules["approvalTtlSeconds"]) if "approvalTtlSeconds" in rules else None
        ),
        "explain_decisions": (
            bool(rules["explainDecisions"]) if "explainDecisions" in rules else None
        ),
        "high_risk_requires_double_confirmation": (
            bool(rules["highRiskRequiresDoubleConfirmation"])
            if "highRiskRequiresDoubleConfirmation" in rules
            else None
        ),
        "approval_retry_attempts": (
            int(rules["approvalRetryAttempts"]) if "approvalRetryAttempts" in rules else None
        ),
        "approval_retry_initial_backoff_seconds": (
            float(rules["approvalRetryInitialBackoffSeconds"])
            if "approvalRetryInitialBackoffSeconds" in rules
            else None
        ),
        "approval_retry_max_backoff_seconds": (
            float(rules["approvalRetryMaxBackoffSeconds"])
            if "approvalRetryMaxBackoffSeconds" in rules
            else None
        ),
        "approval_retry_backoff_multiplier": (
            float(rules["approvalRetryBackoffMultiplier"])
            if "approvalRetryBackoffMultiplier" in rules
            else None
        ),
        "approval_retryable_status_codes": (
            [int(code) for code in rules["approvalRetryableStatusCodes"]]
            if "approvalRetryableStatusCodes" in rules
            else None
        ),
        "approval_on_timeout": (
            str(rules["approvalOnTimeout"]) if "approvalOnTimeout" in rules else None
        ),
        "approval_on_transport_error": (
            str(rules["approvalOnTransportError"]) if "approvalOnTransportError" in rules else None
        ),
        "approval_allow_insecure_http": (
            bool(rules["approvalAllowInsecureHttp"])
            if "approvalAllowInsecureHttp" in rules
            else None
        ),
        "approval_allowed_hosts": [str(v).lower() for v in rules.get("approvalAllowedHosts", [])],
        "approval_auth_token": (
            str(rules["approvalAuthToken"]) if "approvalAuthToken" in rules else None
        ),
        "approval_dedupe_key_fields": [
            str(v).lower() for v in rules.get("approvalDedupeKeyFields", [])
        ],
        "approval_dedupe_arg_keys": [str(v) for v in rules.get("approvalDedupeArgKeys", [])],
    }
    return ServerConfig(**normalized)
