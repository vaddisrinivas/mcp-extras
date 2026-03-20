"""Approval transport interfaces and concrete WhatsApp implementations."""

from __future__ import annotations

import asyncio
import random
import sys
from abc import ABC, abstractmethod
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, field_validator

from .errors import ApprovalPolicyError, ApprovalTimeoutError, ApprovalTransportError

_DEFAULT_RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})


class TransportPolicy(BaseModel):
    """Configuration for transport retries, failures, and security checks."""

    model_config = ConfigDict(extra="ignore")

    retry_attempts: int = 2
    retry_initial_backoff_seconds: float = 0.5
    retry_max_backoff_seconds: float = 5.0
    retry_backoff_multiplier: float = 2.0
    retryable_status_codes: frozenset[int] = _DEFAULT_RETRYABLE_STATUS_CODES
    on_timeout: str = "deny"  # deny|fallback
    on_transport_error: str = "fallback"  # deny|fallback
    allow_insecure_http: bool = False
    allowed_hosts: frozenset[str] = frozenset()
    auth_token: str | None = None

    @field_validator("retry_attempts")
    @classmethod
    def validate_retry_attempts(cls, v: int) -> int:
        if v < 1:
            raise ValueError("retry_attempts must be >= 1")
        return v

    @field_validator("retry_initial_backoff_seconds")
    @classmethod
    def validate_retry_initial_backoff_seconds(cls, v: float) -> float:
        if v < 0:
            raise ValueError("retry_initial_backoff_seconds must be >= 0")
        return v

    @field_validator("retry_max_backoff_seconds")
    @classmethod
    def validate_retry_max_backoff_seconds(cls, v: float) -> float:
        if v < 0:
            raise ValueError("retry_max_backoff_seconds must be >= 0")
        return v

    @field_validator("retry_backoff_multiplier")
    @classmethod
    def validate_retry_backoff_multiplier(cls, v: float) -> float:
        if v < 1:
            raise ValueError("retry_backoff_multiplier must be >= 1")
        return v

    @field_validator("on_timeout")
    @classmethod
    def validate_on_timeout(cls, v: str) -> str:
        if v not in {"deny", "fallback"}:
            raise ValueError("on_timeout must be one of: deny, fallback")
        return v

    @field_validator("on_transport_error")
    @classmethod
    def validate_on_transport_error(cls, v: str) -> str:
        if v not in {"deny", "fallback"}:
            raise ValueError("on_transport_error must be one of: deny, fallback")
        return v


class ApprovalTransport(ABC):
    """Transport contract used by approval engines."""

    @abstractmethod
    async def request(
        self,
        *,
        question: str,
        timeout: float,
        tool_name: str,
    ) -> bool | None:
        """Request approval and return approved/denied/indeterminate."""


class ChainedTransport(ApprovalTransport):
    """Try transports in order; continue only on indeterminate results."""

    def __init__(self, transports: list[ApprovalTransport], default: bool | None = None):
        if not transports:
            raise ApprovalPolicyError("ChainedTransport requires at least one transport")
        self.transports = transports
        self.default = default

    async def request(
        self,
        *,
        question: str,
        timeout: float,
        tool_name: str,
    ) -> bool | None:
        for transport in self.transports:
            result = await transport.request(
                question=question, timeout=timeout, tool_name=tool_name
            )
            if result is not None:
                return result
        return self.default


class _HttpTransportBase(ApprovalTransport):
    def __init__(self, bridge_url: str, policy: TransportPolicy | None = None) -> None:
        self.bridge_url = bridge_url.rstrip("/")
        self.policy = policy or TransportPolicy()
        self._validate_bridge_url()

    def _validate_bridge_url(self) -> None:
        parsed = urlparse(self.bridge_url)
        if parsed.scheme not in {"http", "https"}:
            raise ApprovalPolicyError("bridge_url must use http:// or https://")
        host = (parsed.hostname or "").lower()
        if not host:
            raise ApprovalPolicyError("bridge_url must include a host")

        if self.policy.allowed_hosts and host not in self.policy.allowed_hosts:
            allowed = ", ".join(sorted(self.policy.allowed_hosts))
            raise ApprovalPolicyError(
                f"bridge_url host {host!r} is not in allowed_hosts: {allowed}"
            )

        if (
            parsed.scheme == "http"
            and not self.policy.allow_insecure_http
            and host not in _LOCAL_HOSTS
        ):
            raise ApprovalPolicyError(
                "insecure http bridge_url denied for non-local host; set allow_insecure_http=true to override"
            )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.policy.auth_token:
            headers["Authorization"] = f"Bearer {self.policy.auth_token}"
        return headers

    @staticmethod
    def _is_retryable_exception(exc: Exception, retryable_status_codes: frozenset[int]) -> bool:
        if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in retryable_status_codes
        return False

    async def _call_with_retry(self, op) -> None:
        """Retry with exponential backoff for status codes."""
        delay = self.policy.retry_initial_backoff_seconds
        last_exc: Exception | None = None
        for attempt in range(1, self.policy.retry_attempts + 1):
            try:
                return await op()
            except Exception as exc:
                last_exc = exc
                is_retryable = self._is_retryable_exception(exc, self.policy.retryable_status_codes)
                if attempt >= self.policy.retry_attempts or not is_retryable:
                    break
                await asyncio.sleep(
                    min(delay, self.policy.retry_max_backoff_seconds) + random.uniform(0, 0.05)
                )
                delay = min(
                    max(delay, 0.0) * self.policy.retry_backoff_multiplier,
                    self.policy.retry_max_backoff_seconds,
                )
        if last_exc is None:
            raise ApprovalTransportError("request failed with unknown error")
        raise last_exc

    def _resolve_failure(self, tool_name: str, exc: Exception) -> bool | None:
        if isinstance(exc, ApprovalTimeoutError):
            print(f"[approval-proxy] timeout for `{tool_name}`: {exc}", file=sys.stderr)
            return None if self.policy.on_timeout == "fallback" else False
        print(f"[approval-proxy] transport error for `{tool_name}`: {exc}", file=sys.stderr)
        return None if self.policy.on_transport_error == "fallback" else False


class WhatsAppPollTransport(_HttpTransportBase):
    """Legacy /whatsapp_poll transport."""

    _APPROVE = "✅ Approve"
    _DENY = "❌ Deny"

    async def request(
        self,
        *,
        question: str,
        timeout: float,
        tool_name: str,
    ) -> bool | None:
        payload = {"question": question, "options": [self._APPROVE, self._DENY]}

        async def _send_once():
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{self.bridge_url}/whatsapp_poll",
                    json=payload,
                    headers=self._headers(),
                )
                resp.raise_for_status()
                return resp.json()

        try:
            data = await self._call_with_retry(_send_once)
        except Exception as exc:
            return self._resolve_failure(tool_name, exc)

        choice = data.get("choice", "")
        approved = choice == self._APPROVE
        print(
            f"[approval-proxy] WhatsApp {'approved' if approved else 'denied'} `{tool_name}` (choice={choice!r})",
            file=sys.stderr,
        )
        return approved


class NanoclawApprovalsTransport(_HttpTransportBase):
    """Nanoclaw /approvals + /approvals/{id} transport."""

    def __init__(
        self,
        bridge_url: str,
        poll_interval: float = 1.0,
        policy: TransportPolicy | None = None,
    ) -> None:
        super().__init__(bridge_url=bridge_url, policy=policy)
        self.poll_interval = poll_interval

    async def request(
        self,
        *,
        question: str,
        timeout: float,
        tool_name: str,
    ) -> bool | None:
        timeout_ms = int(max(timeout, 1) * 1000)
        create_payload = {"message": question, "timeoutMs": timeout_ms}

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                approval_id = await self._create_approval(client, create_payload)
                return await self._poll_status(client, approval_id, timeout)
        except Exception as exc:
            return self._resolve_failure(tool_name, exc)

    async def _create_approval(self, client: httpx.AsyncClient, payload: dict) -> str:
        async def _create_once():
            resp = await client.post(
                f"{self.bridge_url}/approvals",
                json=payload,
                headers=self._headers(),
            )
            resp.raise_for_status()
            approval_id = resp.json().get("id", "")
            if not approval_id:
                raise ApprovalTransportError("approvals API did not return an id")
            return approval_id

        return await self._call_with_retry(_create_once)

    async def _poll_status(
        self, client: httpx.AsyncClient, approval_id: str, timeout: float
    ) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout + 15
        while loop.time() < deadline:
            await asyncio.sleep(self.poll_interval)
            status_resp = await client.get(f"{self.bridge_url}/approvals/{approval_id}")
            if status_resp.status_code == 404:
                return False
            status_resp.raise_for_status()
            status = str(status_resp.json().get("status", "pending")).lower()
            if status == "pending":
                continue
            return status == "approved"
        raise ApprovalTimeoutError("approval polling timed out")


def build_whatsapp_transport(
    *,
    bridge_url: str,
    api_mode: str = "auto",
    poll_interval: float = 1.0,
    policy: TransportPolicy | None = None,
) -> ApprovalTransport:
    """Factory for WhatsApp-related transport wiring."""
    if api_mode not in {"auto", "whatsapp_poll", "approvals"}:
        raise ApprovalPolicyError("api_mode must be one of: auto, whatsapp_poll, approvals")

    if api_mode == "whatsapp_poll":
        return WhatsAppPollTransport(bridge_url=bridge_url, policy=policy)
    if api_mode == "approvals":
        return NanoclawApprovalsTransport(
            bridge_url=bridge_url,
            poll_interval=poll_interval,
            policy=policy,
        )
    return ChainedTransport(
        [
            WhatsAppPollTransport(bridge_url=bridge_url, policy=policy),
            NanoclawApprovalsTransport(
                bridge_url=bridge_url,
                poll_interval=poll_interval,
                policy=policy,
            ),
        ],
        default=None,
    )
