"""Tests for config loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_approval_proxy.config import ProxyConfig, load_upstream_config


@pytest.fixture
def tmp_config(tmp_path):
    """Write a config JSON and return the path."""

    def _write(data: dict | list) -> Path:
        p = tmp_path / "mcp.json"
        p.write_text(json.dumps(data))
        return p

    return _write


# ─────────────────────────────────────────────────────────────────────────────
# Config format parsing
# ─────────────────────────────────────────────────────────────────────────────


class TestConfigFormats:
    def test_claude_desktop_format(self, tmp_config):
        p = tmp_config(
            {
                "mcpServers": {
                    "filesystem": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                    },
                    "sqlite": {
                        "command": "uvx",
                        "args": ["mcp-server-sqlite", "--db-path", "/tmp/test.db"],
                    },
                }
            }
        )
        servers, proxy_cfg = load_upstream_config(p)
        assert len(servers) == 2
        assert servers[0].name == "filesystem"
        assert servers[0].command == "npx"
        assert servers[0].args == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        assert servers[1].name == "sqlite"
        assert isinstance(proxy_cfg, ProxyConfig)

    def test_single_server_format(self, tmp_config):
        p = tmp_config({"command": "npx", "args": ["-y", "some-mcp-server"]})
        servers, _ = load_upstream_config(p)
        assert len(servers) == 1
        assert servers[0].name == "upstream"
        assert servers[0].command == "npx"

    def test_array_format(self, tmp_config):
        p = tmp_config(
            [
                {"name": "a", "command": "cmd_a", "args": []},
                {"name": "b", "command": "cmd_b", "args": ["--flag"]},
            ]
        )
        servers, _ = load_upstream_config(p)
        assert len(servers) == 2
        assert servers[0].name == "a"
        assert servers[1].name == "b"
        assert servers[1].args == ["--flag"]

    def test_url_upstream_http(self, tmp_config):
        p = tmp_config({"url": "http://localhost:8080/mcp"})
        servers, _ = load_upstream_config(p)
        assert servers[0].transport_type == "http"
        assert servers[0].url == "http://localhost:8080/mcp"

    def test_url_upstream_sse(self, tmp_config):
        p = tmp_config({"url": "http://localhost:8080/sse"})
        servers, _ = load_upstream_config(p)
        assert servers[0].transport_type == "sse"

    def test_missing_command_and_url_raises(self, tmp_config):
        p = tmp_config({"not": "valid"})
        with pytest.raises(ValueError, match="Unrecognised config format"):
            load_upstream_config(p)

    def test_env_vars_included(self, tmp_config):
        p = tmp_config(
            {
                "mcpServers": {
                    "myserver": {
                        "command": "npx",
                        "args": [],
                        "env": {"MY_KEY": "my_value", "DEBUG": "1"},
                    }
                }
            }
        )
        servers, _ = load_upstream_config(p)
        assert servers[0].env == {"MY_KEY": "my_value", "DEBUG": "1"}

    def test_empty_env_defaults_to_dict(self, tmp_config):
        p = tmp_config({"command": "myserver", "args": []})
        servers, _ = load_upstream_config(p)
        assert servers[0].env == {}

    def test_headers_parsed(self, tmp_config):
        p = tmp_config(
            {
                "url": "http://localhost:8080/mcp",
                "headers": {"Authorization": "Bearer token123"},
            }
        )
        servers, _ = load_upstream_config(p)
        assert servers[0].headers == {"Authorization": "Bearer token123"}


# ─────────────────────────────────────────────────────────────────────────────
# Approval rules
# ─────────────────────────────────────────────────────────────────────────────


class TestApprovalRules:
    def test_basic_rules_parsed(self, tmp_config):
        p = tmp_config(
            {
                "mcpServers": {
                    "fs": {
                        "command": "npx",
                        "args": [],
                        "approvalRules": {
                            "mode": "annotated",
                            "alwaysAllow": ["read_file", "list_dir"],
                            "alwaysDeny": ["delete_file"],
                        },
                    }
                }
            }
        )
        servers, _ = load_upstream_config(p)
        s = servers[0]
        assert s.mode == "annotated"
        assert "read_file" in s.always_allow
        assert "list_dir" in s.always_allow
        assert "delete_file" in s.always_deny

    def test_pattern_rules_parsed(self, tmp_config):
        p = tmp_config(
            {
                "command": "x",
                "args": [],
                "approvalRules": {
                    "allowPatterns": ["get_*", "list_*"],
                    "denyPatterns": ["*delete*", "*destroy*"],
                },
            }
        )
        servers, _ = load_upstream_config(p)
        s = servers[0]
        assert "get_*" in s.allow_patterns
        assert "list_*" in s.allow_patterns
        assert "*delete*" in s.deny_patterns
        assert "*destroy*" in s.deny_patterns

    def test_custom_annotations_parsed(self, tmp_config):
        p = tmp_config(
            {
                "command": "x",
                "args": [],
                "approvalRules": {
                    "customAnnotations": {
                        "ambiguous_tool": {"destructiveHint": True},
                        "safe_reader": {"readOnlyHint": True},
                    }
                },
            }
        )
        servers, _ = load_upstream_config(p)
        s = servers[0]
        assert s.custom_annotations["ambiguous_tool"]["destructiveHint"] is True
        assert s.custom_annotations["safe_reader"]["readOnlyHint"] is True

    def test_timeout_parsed(self, tmp_config):
        p = tmp_config(
            {
                "command": "x",
                "args": [],
                "approvalRules": {"timeout": 45, "timeoutAction": "approve"},
            }
        )
        servers, _ = load_upstream_config(p)
        s = servers[0]
        assert s.timeout == 45.0
        assert s.timeout_action == "approve"

    def test_names_lowercased(self, tmp_config):
        p = tmp_config(
            {
                "command": "x",
                "args": [],
                "approvalRules": {"alwaysAllow": ["ReadFile", "ListDir"]},
            }
        )
        servers, _ = load_upstream_config(p)
        assert "readfile" in servers[0].always_allow
        assert "listdir" in servers[0].always_allow

    def test_custom_annotations_keys_lowercased(self, tmp_config):
        p = tmp_config(
            {
                "command": "x",
                "args": [],
                "approvalRules": {"customAnnotations": {"MyTool": {"destructiveHint": True}}},
            }
        )
        servers, _ = load_upstream_config(p)
        assert "mytool" in servers[0].custom_annotations


# ─────────────────────────────────────────────────────────────────────────────
# approvalProxy section
# ─────────────────────────────────────────────────────────────────────────────


class TestProxyConfig:
    def test_proxy_config_defaults(self, tmp_config):
        p = tmp_config({"command": "x", "args": []})
        _, proxy_cfg = load_upstream_config(p)
        assert proxy_cfg.dry_run is False
        assert proxy_cfg.audit_log is None
        assert proxy_cfg.default_timeout == 120.0
        assert proxy_cfg.default_timeout_action == "deny"
        assert proxy_cfg.approval_ttl_seconds == 0.0
        assert proxy_cfg.explain_decisions is False
        assert proxy_cfg.high_risk_requires_double_confirmation is False
        assert proxy_cfg.approval_retry_attempts == 2
        assert proxy_cfg.approval_retry_initial_backoff_seconds == 0.5
        assert proxy_cfg.approval_retry_max_backoff_seconds == 5.0
        assert proxy_cfg.approval_retry_backoff_multiplier == 2.0
        assert proxy_cfg.approval_on_timeout == "deny"
        assert proxy_cfg.approval_on_transport_error == "fallback"
        assert proxy_cfg.approval_allow_insecure_http is False
        assert proxy_cfg.approval_dedupe_key_fields == ["server", "tool", "args"]

    def test_proxy_config_from_file(self, tmp_config):
        p = tmp_config(
            {
                "mcpServers": {"s": {"command": "x", "args": []}},
                "approvalProxy": {
                    "dryRun": True,
                    "auditLog": "/tmp/audit.jsonl",
                    "defaultTimeout": 60,
                    "defaultTimeoutAction": "approve",
                    "approvalTtlSeconds": 30,
                    "explainDecisions": True,
                    "highRiskRequiresDoubleConfirmation": True,
                    "approvalRetryAttempts": 3,
                    "approvalRetryInitialBackoffSeconds": 1.0,
                    "approvalRetryMaxBackoffSeconds": 8.0,
                    "approvalRetryBackoffMultiplier": 3.0,
                    "approvalRetryableStatusCodes": [429, 503],
                    "approvalOnTimeout": "fallback",
                    "approvalOnTransportError": "deny",
                    "approvalAllowInsecureHttp": True,
                    "approvalAllowedHosts": ["bridge.local"],
                    "approvalAuthToken": "token-1",
                    "approvalDedupeKeyFields": ["tool", "args"],
                    "approvalDedupeArgKeys": ["path"],
                },
            }
        )
        _, proxy_cfg = load_upstream_config(p)
        assert proxy_cfg.dry_run is True
        assert proxy_cfg.audit_log == "/tmp/audit.jsonl"
        assert proxy_cfg.default_timeout == 60.0
        assert proxy_cfg.default_timeout_action == "approve"
        assert proxy_cfg.approval_ttl_seconds == 30.0
        assert proxy_cfg.explain_decisions is True
        assert proxy_cfg.high_risk_requires_double_confirmation is True
        assert proxy_cfg.approval_retry_attempts == 3
        assert proxy_cfg.approval_retry_initial_backoff_seconds == 1.0
        assert proxy_cfg.approval_retry_max_backoff_seconds == 8.0
        assert proxy_cfg.approval_retry_backoff_multiplier == 3.0
        assert proxy_cfg.approval_retryable_status_codes == [429, 503]
        assert proxy_cfg.approval_on_timeout == "fallback"
        assert proxy_cfg.approval_on_transport_error == "deny"
        assert proxy_cfg.approval_allow_insecure_http is True
        assert proxy_cfg.approval_allowed_hosts == ["bridge.local"]
        assert proxy_cfg.approval_auth_token == "token-1"
        assert proxy_cfg.approval_dedupe_key_fields == ["tool", "args"]
        assert proxy_cfg.approval_dedupe_arg_keys == ["path"]

    def test_invalid_mode_raises(self, tmp_config):
        p = tmp_config(
            {
                "command": "x",
                "args": [],
                "approvalRules": {"mode": "invalid-mode"},
            }
        )
        with pytest.raises(ValueError, match="invalid mode"):
            load_upstream_config(p)

    def test_negative_approval_ttl_raises(self, tmp_config):
        p = tmp_config(
            {
                "command": "x",
                "args": [],
                "approvalProxy": {"approvalTtlSeconds": -1},
            }
        )
        with pytest.raises(ValueError, match="approvalTtlSeconds must be >= 0"):
            load_upstream_config(p)

    def test_invalid_failure_action_raises(self, tmp_config):
        p = tmp_config(
            {
                "command": "x",
                "args": [],
                "approvalProxy": {"approvalOnTimeout": "explode"},
            }
        )
        with pytest.raises(ValueError, match="approvalOnTimeout must be one of"):
            load_upstream_config(p)

    def test_invalid_dedupe_key_field_raises(self, tmp_config):
        p = tmp_config(
            {
                "command": "x",
                "args": [],
                "approvalProxy": {"approvalDedupeKeyFields": ["tool", "unknown"]},
            }
        )
        with pytest.raises(ValueError, match="approvalDedupeKeyFields contains invalid values"):
            load_upstream_config(p)
