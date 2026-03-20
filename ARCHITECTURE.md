# Architecture

Internal design notes for contributors and integrators.

## Module map

```
src/mcp_approval_proxy/
├── __init__.py          Public API surface — re-exports all user-facing symbols
├── middleware.py        ApprovalMiddleware (FastMCP Middleware subclass)
├── engines.py           ApprovalEngine ABC + built-in engines
├── transports.py        ApprovalTransport ABC + HTTP transport implementations
├── audit.py             AuditLogger + _Timer helper
├── config.py            ProxyConfig / ServerConfig (CLI upstream config parsing)
├── decorators.py        @approval_required tool decorator
├── errors.py            Custom exception hierarchy
└── proxy.py             build_proxy() factory (create_proxy + middleware wiring)
```

## Request lifecycle

```
on_call_tool(context, call_next)
       │
       ├─ Resolve annotations (merge custom_annotations overrides)
       ├─ _risk_level() → "high" | "medium" | "low" | "unknown"
       ├─ _needs_approval() → None (hard block) | False (pass-through) | True (needs approval)
       │
       ├─ decision is None ──→ AuditLogger("blocked") → _deny()
       │
       ├─ decision is False ──→ AuditLogger("passed") → call_next()
       │
       └─ decision is True
              │
              ├─ Check _approval_cache (TTL hit) ──→ call_next()
              │
              ├─ Acquire per-key asyncio.Lock (deduplicates concurrent same-call requests)
              │      └─ Re-check cache (double-check pattern)
              │
              ├─ Build ApprovalContext
              │
              ├─ Retry loop (approval_retry_attempts)
              │      └─ engine.request_approval(ctx) → True | False | None
              │
              ├─ None after retries → treat as False (deny)
              │
              ├─ high_risk_requires_double_confirmation: second engine call
              │
              ├─ True  → _cache_approval(key) → AuditLogger("approved") → call_next()
              └─ False → AuditLogger("denied") → _deny()
```

## Engines

All engines implement `ApprovalEngine.request_approval(ctx) -> bool | None`.

### Return contract

| Return | Meaning | ChainedEngine |
|--------|---------|---------------|
| `True` | Approved | Stops; forwards call |
| `False` | Denied | Stops; returns error |
| `None` | Indeterminate | Tries next engine |

`None` is used when: client doesn't support elicitation, connection failed, session not
found.  It means "I can't decide — let something else try."

### ElicitationEngine

Uses `fastmcp_ctx.elicit(message, response_type=bool)` which maps to `elicitation/create`
in the MCP protocol.  Falls back to `None` when:
- `fastmcp_context` is `None` (no session — e.g. direct tool call in tests)
- `await _client_supports_elicitation(ctx)` returns `False`
- `elicit()` raises an exception
- `asyncio.wait_for` times out AND `fallthrough_on_timeout=True`

### WhatsAppEngine

Thin orchestration layer — formats the approval message, delegates to `self.transport`
(an `ApprovalTransport` instance).  Does not contain any HTTP code itself.

Created via `build_whatsapp_transport(api_mode=...)`:
- `"whatsapp_poll"` → `WhatsAppPollTransport` (legacy `/whatsapp_poll` endpoint)
- `"approvals"` → `NanoclawApprovalsTransport`
- `"auto"` → `ChainedTransport([WhatsAppPollTransport, NanoclawApprovalsTransport])`

### WAHAEngine

Full self-contained engine for WAHA REST API — no `ApprovalTransport` delegation.
Sends a text message, polls `GET /api/{session}/chats/{chatId}/messages` for keyword
replies.  Uses `asyncio.get_running_loop().time()` for deadline calculation.

The per-engine `asyncio.Lock` serialises concurrent approvals — only one pending
WhatsApp message at a time (avoids user confusion from overlapping requests).

## Transports

`ApprovalTransport.request(*, question, timeout, tool_name) -> bool | None`

### _HttpTransportBase

Shared base for HTTP transports:
- `_validate_bridge_url()` — scheme check, host allowlist, insecure HTTP guard
- `_call_with_retry(op)` — exponential backoff with jitter for retryable errors
- `_resolve_failure(tool_name, exc)` — maps `ApprovalTimeoutError` / transport errors to
  `None` (fallback) or `False` (deny) according to `TransportPolicy`

### NanoclawApprovalsTransport

Two-phase protocol:
1. `POST /approvals {message, timeoutMs}` → `{id}`
2. Poll `GET /approvals/{id}` every `poll_interval` seconds until `status != "pending"`

The poll deadline is `timeout + 15 s` — the 15-second grace period allows nanoclaw to
expire the poll server-side and return a final `denied` status rather than the client
timing out first and raising `ApprovalTimeoutError`.

## Approval deduplication

`_approval_key(tool_name, tool_args, risk)` hashes a JSON payload of the configured
dedupe fields (`server`, `tool`, `args`, `risk`) using SHA-256.

The double-check lock pattern:
```
check cache (no lock)
    ↓ miss
acquire per-key Lock
    check cache again (under lock)
        ↓ miss
    call engine
    store in cache
release lock
    ↓
cleanup: pop lock from dict if unlocked (no waiters)
```

This ensures that two simultaneous calls for the same tool+args both wait for a single
approval dialog, and the second caller gets the cached result without prompting the user
twice.

## Memory management

**`_approval_locks`**: Dictionary of `{approval_key: asyncio.Lock}`.  The lock is
removed from the dict in the `finally` block after the `async with lock:` exits,
guarded by `lock.locked() == False` (i.e. no other coroutine is waiting on it).

**`_approval_cache`**: Dictionary of `{approval_key: expiry_monotonic}`.  Expired entries
are lazily removed in `_is_approval_cached()`.  Opportunistic bulk cleanup runs in
`_cache_approval()` when the cache exceeds 500 entries, evicting all expired keys.

## Risk classification

`_risk_level(tool_name, annotations, mode)`:

1. `destructiveHint=True` OR name tokens ∩ `_HIGH_RISK_WORDS` → `"high"`
2. Name tokens ∩ `_WRITE_WORDS` → `"medium"`
3. `mode == "all"` → `"low"`
4. Otherwise → `"unknown"`

Name splitting: `_SPLIT_RE` handles `snake_case`, `kebab-case`, `camelCase`,
`PascalCase`, space-separated.

## Audit log schema

```jsonc
{
  "ts": "ISO-8601 UTC",
  "server": "upstream name",
  "tool": "tool_name",
  "decision": "approved|denied|blocked|passed|dry_run|error",
  "risk": "high|medium|low|unknown",
  "reason": "human-readable string",
  "mode": "destructive|all|annotated|none",
  "duration_ms": 60683.1,       // milliseconds from first intercept to decision
  "args": { ... }               // sanitised (secrets redacted by key heuristic)
}
```

`AuditLogger` sanitises args by replacing values of keys matching `_SECRET_KEY_RE`
(`password`, `token`, `secret`, `api_key`, `apikey`, `credential`, `auth`, `private_key`)
with `"***"`.

## Error hierarchy

```
ApprovalProxyError          Base
├── ApprovalPolicyError     Misconfiguration (bad mode, invalid host, etc.)
├── ApprovalTransportError  HTTP transport failure (non-retryable or budget exhausted)
└── ApprovalTimeoutError    Polling deadline exceeded
```

## Extending

### New approval channel

1. Subclass `ApprovalTransport` and implement `request()`.
2. Pass it to `WhatsAppEngine(transport=my_transport)`, or
3. Subclass `ApprovalEngine` directly for full control over message building and context.

### New risk tier

Add tokens to `_HIGH_RISK_WORDS` / `_WRITE_WORDS` in `middleware.py`, or override
per-tool via `custom_annotations={"my_tool": {"destructiveHint": True}}`.

### Custom dedupe key

```python
ApprovalMiddleware(
    approval_dedupe_key_fields=["tool"],  # dedupe by tool name only (ignore args)
    # or restrict which arg keys contribute to the hash:
    approval_dedupe_arg_keys=["path", "operation"],
)
```
