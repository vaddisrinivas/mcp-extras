"""CLI approval channel — prints to stderr, reads y/n from stdin.

Works in stdio MCP mode because MCP traffic goes via stdin/stdout and we use stderr
for the prompt display. The human types directly into the terminal.

For non-interactive / piped use, set AUTO_APPROVE=1 env var (useful for testing).
"""

from __future__ import annotations

import asyncio
import os
import sys

from .base import ApprovalChannel, ApprovalRequest, ApprovalResult

# ANSI colours (disabled if not a tty)
_TTY = sys.stderr.isatty()
YELLOW = "\033[33m" if _TTY else ""
GREEN = "\033[32m" if _TTY else ""
RED = "\033[31m" if _TTY else ""
RESET = "\033[0m" if _TTY else ""
BOLD = "\033[1m" if _TTY else ""


class CliChannel(ApprovalChannel):
    """Interactive CLI approval — blocks until user types y or n."""

    def __init__(self, auto_approve: bool = False, timeout: float = 120.0):
        super().__init__()
        self.auto_approve = auto_approve or os.environ.get("AUTO_APPROVE", "").lower() in (
            "1",
            "true",
            "yes",
        )
        self.timeout = timeout
        self._lock = asyncio.Lock()  # serialise concurrent requests

    async def request(self, req: ApprovalRequest) -> ApprovalResult:
        if self.auto_approve:
            return ApprovalResult(approved=True, reason="AUTO_APPROVE")

        async with self._lock:
            return await asyncio.wait_for(self._ask(req), timeout=self.timeout)

    async def _ask(self, req: ApprovalRequest) -> ApprovalResult:
        summary = self._format_request(req)
        print(f"\n{BOLD}{YELLOW}{summary}{RESET}", file=sys.stderr)
        print(f"{BOLD}Approve? [y/N] {RESET}", end="", file=sys.stderr, flush=True)

        loop = asyncio.get_event_loop()
        answer = await loop.run_in_executor(None, lambda: sys.stdin.readline().strip().lower())

        if answer in ("y", "yes"):
            print(f"{GREEN}✅ Approved{RESET}\n", file=sys.stderr)
            return ApprovalResult(approved=True)
        else:
            print(f"{RED}❌ Denied{RESET}\n", file=sys.stderr)
            return ApprovalResult(approved=False, reason="User denied")
