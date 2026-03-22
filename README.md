# mcp-approval-proxy

[![CI](https://github.com/vaddisrinivas/mcp-approval-proxy/actions/workflows/test.yml/badge.svg)](https://github.com/vaddisrinivas/mcp-approval-proxy/actions/workflows/test.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/mcp-approval-proxy.svg)](https://pypi.org/project/mcp-approval-proxy)

A FastMCP middleware library that intercepts MCP tool calls and gates write/destructive
operations behind human approval. Supports **MCP-native elicitation** (inline dialog in
Claude Code / Claude Desktop), **HTTP webhooks** (custom approval dashboards),
**WhatsApp polls via Baileys/nanoclaw**, **WhatsApp text via WAHA**, and a chained
fallback model so desktop and mobile clients are both covered.

## The Problem: MCP Tools Without Guardrails

The [Model Context Protocol](https://modelcontextprotocol.io/) gives AI agents access to powerful tools — file systems, databases, APIs, shell commands. But MCP has no built-in approval mechanism. Every tool call executes immediately, with no human oversight.

This creates a **trust gap**:

- An agent can `delete_file`, `drop_table`, or `deploy_to_prod` with zero confirmation
- There's no audit trail of what was approved vs what the agent decided on its own
- You can't distinguish "the human asked for this" from "the agent hallucinated this was needed"

**mcp-approval-proxy** closes this gap. Drop it in front of any MCP server — no code changes required — and destructive operations require explicit human approval before execution.

```
Without proxy:  Claude → delete_file() → done (hope that was right)
With proxy:     Claude → delete_file() → "Approve?" → human clicks yes → done
```

## Architecture

```
MCP Client (Claude Code / Claude Desktop / mobile)
      │  stdio / SSE / streamable-http
      v
ApprovalMiddleware  ──── this library
      │  on_call_tool() intercepts every tool call
      │  ├── hard-blocked (always_deny / deny_patterns) ──> error
      │  ├── pass-through (read-only / always_allow / mode=none) ──> upstream
      │  └── needs approval
      │            │
      │     ApprovalEngine.request_approval()
      │            ├── ElicitationEngine  ── MCP elicitation/create dialog
      │            ├── WebhookEngine      ── HTTP webhook (custom approval systems)
      │            ├── WhatsAppEngine     ── nanoclaw /approvals HTTP API
      │            ├── WAHAEngine         ── WAHA REST API (text message polling)
      │            └── ChainedEngine      ── try engines in sequence
      │            │
      │     approved ──> forward to upstream MCP server
      │     denied   ──> error CallToolResult
      v
Upstream MCP Server (subprocess / HTTP / in-process FastMCP)
```

## Install

```bash
pip install mcp-approval-proxy
# or with uv
uv add mcp-approval-proxy
```

Requires Python 3.11+, FastMCP >= 3.0.

Dependencies: `fastmcp`, `httpx`, `pydantic`, `pydantic-settings`, `click`, `rich`.

## Quick start — standalone proxy

Wrap any MCP server listed in your Claude Desktop config and gate its write tools:

```bash
# Mode: destructive (default) — gate tools with destructiveHint or write-like names
approval-proxy --upstream ~/.config/claude/claude_desktop_config.json --server filesystem

# Gate every tool
approval-proxy --upstream ./mcp.json --mode all

# Always allow reads, hard-deny delete patterns, explain denials
approval-proxy --upstream ./mcp.json \
  --allow "read_*" --allow "list_*" \
  --deny delete_file \
  --explain

# Comma-separated patterns also work
approval-proxy --upstream ./mcp.json \
  --allow "read_*,list_*,get_*" \
  --deny "delete_*,destroy_*"

# Cache repeated approvals for 30 s; two-step confirm for high-risk actions
approval-proxy --upstream ./mcp.json \
  --approve-ttl 30 \
  --high-risk-double-confirm

# Dry-run: log what would be gated, never block
approval-proxy --upstream ./mcp.json --dry-run

# Write an audit log
approval-proxy --upstream ./mcp.json --audit-log /tmp/approvals.jsonl

# Expose via SSE instead of stdio
approval-proxy --upstream ./mcp.json --transport sse --host 0.0.0.0 --port 8765

# 30-second elicitation timeout, auto-deny on timeout
approval-proxy --upstream ./mcp.json --timeout 30 --timeout-action deny
```

Add to Claude Code / `claude.json`:

```json
{
  "mcpServers": {
    "filesystem-guarded": {
      "command": "approval-proxy",
      "args": ["--upstream", "/path/to/filesystem-config.json", "--mode", "destructive"]
    }
  }
}
```

### CLI flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--upstream`, `-u` | `FILE` | *required* | Path to upstream MCP server config JSON |
| `--server`, `-s` | `NAME` | first server | Which server from the config to proxy |
| `--mode`, `-m` | `choice` | `destructive` | `destructive` / `all` / `annotated` / `none` |
| `--allow` | `PATTERN` | | Tool names or fnmatch globs that bypass approval (repeatable, comma-separated) |
| `--deny` | `PATTERN` | | Tool names or fnmatch globs that are permanently blocked (repeatable, comma-separated) |
| `--timeout` | `SECONDS` | from config | Seconds to wait for elicitation response |
| `--timeout-action` | `ACTION` | from config | `approve` or `deny` on timeout |
| `--approve-ttl` | `SECONDS` | `0` | Cache identical approved tool calls for N seconds |
| `--explain` | flag | `false` | Return policy/risk details in deny/block responses |
| `--high-risk-double-confirm` | flag | `false` | Require two approvals for high-risk actions |
| `--dry-run` | flag | `false` | Log decisions but never actually block |
| `--audit-log` | `FILE` | | Append every decision as a JSON line to FILE |
| `--transport` | `choice` | `stdio` | `stdio` / `sse` / `streamable-http` |
| `--host` | `HOST` | `127.0.0.1` | Bind host (sse/streamable-http only) |
| `--port` | `PORT` | `8765` | Bind port (sse/streamable-http only) |
| `--version` | flag | | Print version and exit |

## Quick start — library (in-process FastMCP)

```python
from fastmcp import FastMCP
from mcp_approval_proxy import ApprovalMiddleware

mcp = FastMCP("my-server")

@mcp.tool()
def delete_record(id: str) -> str:
    ...

mw = ApprovalMiddleware(mode="destructive", server_name="my-server")
mcp.add_middleware(mw)
mcp.run()
```

## Approval modes

| Mode | Behaviour |
|------|-----------|
| `destructive` (default) | Gate tools with `destructiveHint=true` or write-like name tokens |
| `all` | Gate every tool call regardless of annotations |
| `annotated` | Only gate tools explicitly annotated `destructiveHint=true` |
| `none` | Pass-through — no gating (useful for dry-run testing) |

Write-like name tokens recognised by `destructive` mode: `write`, `create`, `update`,
`delete`, `remove`, `move`, `rename`, `execute`, `run`, `push`, `deploy`, `install`, and
~40 more. Snake_case, camelCase, kebab-case, and PascalCase are all split correctly.

## Engines

### ElicitationEngine (default)

Sends an `elicitation/create` request to the connected MCP client. The user sees a
formatted inline dialog with tool name, risk level, arguments, and annotations.

```python
from mcp_approval_proxy.engines import ElicitationEngine

engine = ElicitationEngine(
    timeout=120,              # seconds to wait (default 120)
    timeout_action="deny",    # "approve" | "deny" on timeout (default "deny")
    fallthrough_on_timeout=False,  # True -> pass None to ChainedEngine instead
)
```

Returns `None` (fall-through) when the client does not support elicitation —
a `ChainedEngine` will then try the next engine.

### WebhookEngine — HTTP webhook approval

Send approval requests to an HTTP webhook using the MCP **elicitation/create** format.
Use this to integrate with custom approval systems, dashboards, or external services:

```python
from mcp_approval_proxy.engines import WebhookEngine

engine = WebhookEngine(
    url="https://approval-service.example.com/elicit",
    timeout=120.0,
    headers={"Authorization": "Bearer token123"},
)
```

The webhook receives a POST request with the MCP `ElicitRequestFormParams` schema:
```json
{
  "mode": "form",
  "message": "Approval required — HIGH RISK\n...",
  "requestedSchema": {
    "type": "object",
    "properties": {
      "approved": {"type": "boolean"},
      "reason": {"type": "string"}
    },
    "required": ["approved"]
  }
}
```

Expected response (`ElicitResult` format):
```json
{
  "action": "accept|decline|cancel",
  "content": {"approved": true, "reason": "..."}
}
```

### WhatsAppEngine — nanoclaw Baileys approvals

Sends a WhatsApp poll via the **nanoclaw approvals API** (no QR re-scanning — uses
the existing authenticated Baileys session):

```python
from mcp_approval_proxy.engines import WhatsAppEngine
from mcp_approval_proxy.transports import TransportPolicy

engine = WhatsAppEngine(
    bridge_url="http://nanoclaw:3002",   # nanoclaw approvals HTTP endpoint
    api_mode="approvals",                # "approvals" | "whatsapp_poll" | "auto"
    poll_interval=1.0,                   # status poll interval in seconds
    timeout=120.0,                       # total seconds to wait for vote
    transport_policy=TransportPolicy(
        allow_insecure_http=True,        # required for non-localhost Docker services
    ),
)
```

**nanoclaw API contract:**
```
POST /approvals  { "message": "...", "timeoutMs": 120000 }  ->  { "id": "abc123" }
GET  /approvals/{id}  ->  { "status": "pending" | "approved" | "denied" }
```

### WAHAEngine — WAHA text-message polling

Sends a text message via WAHA (WhatsApp HTTP API) and polls for a keyword reply.
Use this if you run WAHA (NOWEB engine) rather than nanoclaw/Baileys:

```python
from mcp_approval_proxy.engines import WAHAEngine

engine = WAHAEngine(
    waha_url="http://waha:3000",
    chat_id="18128035718@c.us",     # target chat JID
    session="default",               # WAHA session name
    api_key="",                      # WAHA API key (if auth enabled)
    timeout=300.0,
    poll_interval=2.0,
)
```

Recognised approval words (case-insensitive): `yes`, `ok`, `approve`, `y`
Recognised denial words (case-insensitive): `no`, `deny`, `denied`, `cancel`, `n`

> **Note:** WAHA NOWEB cannot decrypt poll vote messages, so `WAHAEngine` uses
> plain text messages rather than native WhatsApp polls.

### ChainedEngine — fallback across engines

The canonical production setup: try MCP elicitation first (fast, native); fall back to
WhatsApp if the client doesn't support it (mobile, CLI, non-Claude clients) or times out.

```python
from mcp_approval_proxy.engines import ChainedEngine, ElicitationEngine, WhatsAppEngine
from mcp_approval_proxy.transports import TransportPolicy

engine = ChainedEngine([
    ElicitationEngine(timeout=30, fallthrough_on_timeout=True),
    WhatsAppEngine(
        bridge_url="http://nanoclaw:3002",
        api_mode="approvals",
        timeout=120,
        transport_policy=TransportPolicy(allow_insecure_http=True),
    ),
])
```

`ChainedEngine` tries engines in order. An engine returns:
- `True` — approved (stops here)
- `False` — denied (stops here)
- `None` — indeterminate (try next engine)

If all engines return `None`, `ChainedEngine.default` is used (default: `False` = deny).

## Inline tool decoration

For in-process FastMCP servers you can annotate tools directly:

```python
from mcp_approval_proxy.decorators import approval_required

@mcp.tool()
@approval_required(force=True, risk="high", reason="Permanently removes data")
def delete_record(id: str) -> str: ...

@mcp.tool()
@approval_required(always_allow=True)
def list_records() -> list[str]: ...

@mcp.tool()
@approval_required(annotations={"destructiveHint": True})
def overwrite_config(data: dict) -> None: ...
```

Call `await middleware.register_from_server(mcp)` after decorating to apply all metadata.

## ApprovalMiddleware — full parameter reference

```python
ApprovalMiddleware(
    # -- Gating policy --
    mode="destructive",             # destructive | all | annotated | none
    always_allow=["read_file"],     # exact tool names: always pass-through
    always_deny=["rm_rf"],          # exact tool names: always block
    allow_patterns=["read_*"],      # fnmatch globs: pass-through
    deny_patterns=["*delete*"],     # fnmatch globs: block
    custom_annotations={            # override tool annotations by name
        "my_tool": {"destructiveHint": True},
    },

    # -- Approval engine --
    engine=ChainedEngine([...]),    # default: ElicitationEngine(timeout, timeout_action)
    timeout=120.0,                  # used only when engine= not supplied
    timeout_action="deny",          # used only when engine= not supplied

    # -- Idempotency / deduplication --
    approval_ttl_seconds=30.0,      # cache approvals for N seconds (0 = disabled)
    approval_dedupe_key_fields=["server","tool","args"],  # what to hash
    approval_dedupe_arg_keys=[],    # restrict arg hashing to these keys

    # -- Retry --
    approval_retry_attempts=1,
    approval_retry_initial_backoff_seconds=0.0,
    approval_retry_backoff_multiplier=2.0,
    approval_retry_max_backoff_seconds=5.0,

    # -- UX / observability --
    dry_run=False,                  # log decisions but never block
    explain_decisions=False,        # include reason in deny messages
    high_risk_requires_double_confirmation=False,
    server_name="upstream",         # label used in logs and approval messages
    audit=AuditLogger(...),         # custom audit logger
)
```

## Environment variable overrides

`ProxyConfig` uses [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
with the `APPROVAL_` prefix, so every global config field can be overridden via environment
variables:

| Env var | Config field | Default |
|---------|-------------|---------|
| `APPROVAL_DRY_RUN` | `dry_run` | `false` |
| `APPROVAL_DEFAULT_TIMEOUT` | `default_timeout` | `120.0` |
| `APPROVAL_DEFAULT_TIMEOUT_ACTION` | `default_timeout_action` | `deny` |
| `APPROVAL_APPROVAL_TTL_SECONDS` | `approval_ttl_seconds` | `0.0` |
| `APPROVAL_EXPLAIN_DECISIONS` | `explain_decisions` | `false` |
| `APPROVAL_HIGH_RISK_REQUIRES_DOUBLE_CONFIRMATION` | `high_risk_requires_double_confirmation` | `false` |
| `APPROVAL_AUDIT_LOG` | `audit_log` | |

Config file values take precedence over env vars when loaded explicitly via `load_upstream_config()`.

## TransportPolicy — HTTP hardening

`WhatsAppEngine` and `NanoclawApprovalsTransport` accept a `TransportPolicy` to control
HTTP transport behaviour:

```python
from mcp_approval_proxy.transports import TransportPolicy

policy = TransportPolicy(
    retry_attempts=2,
    retry_initial_backoff_seconds=0.5,
    retry_max_backoff_seconds=5.0,
    retry_backoff_multiplier=2.0,
    retryable_status_codes=frozenset({429, 500, 502, 503, 504}),
    on_timeout="deny",              # "deny" | "fallback"
    on_transport_error="fallback",  # "deny" | "fallback"
    allow_insecure_http=False,      # must be True for non-localhost internal services
    allowed_hosts=frozenset(),      # restrict to specific hosts (empty = allow all)
    auth_token=None,                # Bearer token for the bridge endpoint
)
```

`allow_insecure_http=True` is required when `bridge_url` uses `http://` and the host is
not `localhost`, `127.0.0.1`, `::1`, or `host.docker.internal`.  Set this when your
approval service is an internal Docker container (e.g. `http://nanoclaw:3002`).

## Upstream config file format

The CLI supports Claude Desktop / `claude.json` format:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "approvalRules": {
        "mode": "destructive",
        "alwaysAllow": ["read_file", "list_dir"],
        "alwaysDeny": ["delete_file"],
        "allowPatterns": ["get_*", "list_*", "read_*"],
        "denyPatterns": ["*delete*", "*destroy*", "*wipe*"],
        "customAnnotations": {
          "some_risky_tool": {"destructiveHint": true}
        },
        "timeout": 60,
        "timeoutAction": "deny",
        "approvalTtlSeconds": 30,
        "explainDecisions": true,
        "highRiskRequiresDoubleConfirmation": false,
        "approvalRetryAttempts": 2,
        "approvalRetryInitialBackoffSeconds": 0.5,
        "approvalRetryMaxBackoffSeconds": 5.0,
        "approvalRetryBackoffMultiplier": 2.0,
        "approvalOnTimeout": "deny",
        "approvalOnTransportError": "fallback",
        "approvalAllowInsecureHttp": false,
        "approvalAllowedHosts": [],
        "approvalDedupeKeyFields": ["server", "tool", "args"]
      }
    }
  },
  "approvalProxy": {
    "dryRun": false,
    "auditLog": "/tmp/mcp-approvals.jsonl",
    "defaultTimeout": 120,
    "defaultTimeoutAction": "deny",
    "approvalTtlSeconds": 0,
    "explainDecisions": false,
    "highRiskRequiresDoubleConfirmation": false
  }
}
```

Also supports single-server format (`{"command": "...", "args": [...]}`) and array format
(`[{"name": "a", "command": "cmd_a"}, ...]`).

## Docker deployment (claw-over-9000 pattern)

When running the agent stack in Docker, host-bridge uses this library mounted as a
volume and connects to nanoclaw's Baileys approvals API over a shared Docker network:

```yaml
# docker-compose.yml (host-bridge service)
environment:
  NANOCLAW_APPROVALS_URL: "http://nanoclaw:3002"
  APPROVAL_DEFAULT_TIMEOUT: "120"
  APPROVAL_DRY_RUN: "false"
networks:
  - nanoclaw-net

networks:
  nanoclaw-net:
    external: true
```

```python
# host-bridge/main.py
import os
from fastmcp import FastMCP
from mcp_approval_proxy import ApprovalMiddleware
from mcp_approval_proxy.engines import ChainedEngine, ElicitationEngine, WhatsAppEngine
from mcp_approval_proxy.transports import TransportPolicy

engine = ChainedEngine([
    ElicitationEngine(timeout=30, fallthrough_on_timeout=True),
    WhatsAppEngine(
        bridge_url=os.environ["NANOCLAW_APPROVALS_URL"],
        api_mode="approvals",
        poll_interval=1.0,
        timeout=120,
        transport_policy=TransportPolicy(allow_insecure_http=True),
    ),
])

mcp = FastMCP("my-server")
mcp.add_middleware(ApprovalMiddleware(
    mode="all",
    server_name="my-server",
    engine=engine,
    explain_decisions=True,
))
```

## Custom transport / engine

Implement `ApprovalTransport` to add Slack, PagerDuty, email, etc.:

```python
from mcp_approval_proxy.transports import ApprovalTransport

class SlackApprovalTransport(ApprovalTransport):
    async def request(self, *, question: str, timeout: float, tool_name: str) -> bool | None:
        # post to Slack, poll for reaction, return True/False/None
        ...
```

Or subclass `ApprovalEngine` directly for higher-level control over message formatting
and the full `ApprovalContext`:

```python
from mcp_approval_proxy.engines import ApprovalEngine, ApprovalContext

class PagerDutyEngine(ApprovalEngine):
    async def request_approval(self, ctx: ApprovalContext) -> bool | None:
        # ctx.tool_name, ctx.args, ctx.risk, ctx.reason, ctx.annotations ...
        ...
```

## Audit log

Every gated call is logged to stderr (or a file) as newline-delimited JSON:

```json
{"ts": "2025-01-15T10:23:45.123Z", "server": "my-server", "tool": "shell",
 "decision": "approved", "risk": "high", "reason": "approved via ChainedEngine",
 "mode": "all", "duration_ms": 60683.1, "args": {"cmd": "ls -la"}}
```

```python
from mcp_approval_proxy.audit import AuditLogger

# Write to file
audit = AuditLogger("/var/log/approvals.jsonl")

# Or suppress (dry_run=True also suppresses blocks)
audit = AuditLogger(None, dry_run=True)
```

## Risk classification

| Risk | Condition |
|------|-----------|
| `high` | `destructiveHint=true` OR name contains: `delete`, `destroy`, `remove`, `drop`, `wipe`, `kill`, `format`, ... |
| `medium` | Name contains a write-like token (see full list in `middleware.py`) |
| `low` | All other tools when `mode="all"` |
| `unknown` | Pass-through tools in non-`all` modes |

## Risks and Limitations

- **Not a security boundary**: This is an approval layer, not a sandbox. A determined attacker with access to the MCP transport can bypass it.
- **Elicitation support varies**: Not all MCP clients support `elicitation/create`. The ChainedEngine fallback pattern handles this, but test with your client.
- **WhatsApp engines require external services**: WhatsAppEngine needs nanoclaw; WAHAEngine needs WAHA. These add operational complexity.
- **Approval fatigue**: In `mode=all`, users may rubber-stamp approvals. Use `destructive` mode and `allow_patterns` to reduce noise.
- **Single point of failure**: If the proxy crashes, all tool calls are blocked. Use process managers (systemd, Docker restart policies) in production.

## Related Projects

- **[olivetin-mcp](https://github.com/vaddisrinivas/olivetin-mcp)** — Hardened MCP server for OliveTin with built-in approval (uses this library's approval patterns)
- **[Model Context Protocol](https://modelcontextprotocol.io/)** — The open standard for AI tool integration. [Spec](https://spec.modelcontextprotocol.io/) | [GitHub](https://github.com/modelcontextprotocol)
- **[FastMCP](https://github.com/jlowin/fastmcp)** — The Python MCP framework this library builds on
- **[OliveTin](https://github.com/OliveTin/OliveTin)** — Safe, parameterized shell action execution with web UI
- **[nanoclaw](https://github.com/vaddisrinivas/nanoclaw)** — WhatsApp bridge (Baileys) with approvals API
- **[OpenClaw](https://github.com/AgentrDev/openclaw)** — Another approach to AI agent orchestration

## Development

```bash
git clone https://github.com/vaddisrinivas/mcp-approval-proxy
cd mcp-approval-proxy
uv sync
uv run pytest         # 207 tests
uv run ruff check .
uv run ruff format .
```

## License

MIT
