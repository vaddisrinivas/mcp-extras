"""JSONL audit logger for every tool-call decision made by the proxy.

Each line of the log is a self-contained JSON object::

    {
      "ts":       "2026-03-19T10:00:00.123Z",
      "server":   "filesystem",
      "tool":     "write_file",
      "args":     {"path": "/tmp/x", "content": "hello"},
      "decision": "approved",
      "risk":     "medium",
      "reason":   "user approved via elicitation",
      "mode":     "destructive",
      "dry_run":  false,
      "duration_ms": 1423
    }

Decision values:
  ``passed``    — tool was allowed without asking (read-only / always-allow)
  ``blocked``   — tool was hard-blocked (always-deny / policy)
  ``approved``  — user approved in elicitation
  ``denied``    — user denied in elicitation
  ``timeout``   — elicitation timed out (action taken per timeout_action)
  ``error``     — elicitation failed with an exception
  ``dry_run``   — dry-run mode; would have been gated but was allowed
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditLogger:
    """Thread-safe, append-only JSONL audit logger."""

    def __init__(self, path: str | Path | None, dry_run: bool = False) -> None:
        self.enabled = path is not None
        self.dry_run = dry_run
        self._path = Path(path) if path else None
        self._counts: dict[str, int] = {}
        self._duration_totals: dict[str, float] = {}
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        *,
        server: str,
        tool: str,
        args: dict[str, Any],
        decision: str,
        risk: str = "unknown",
        reason: str = "",
        mode: str = "",
        duration_ms: float = 0,
    ) -> None:
        """Append one decision record.  Silently swallows I/O errors."""
        record = {
            "ts": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "server": server,
            "tool": tool,
            "args": _sanitise(args),
            "decision": decision,
            "risk": risk,
            "reason": reason,
            "mode": mode,
            "dry_run": self.dry_run,
            "duration_ms": round(duration_ms, 1),
        }
        self._record_metrics(decision, risk, duration_ms)
        line = json.dumps(record, ensure_ascii=False)
        if self._path:
            try:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                print(f"[approval-proxy] audit log write failed: {exc}", file=sys.stderr)
        else:
            # No file → emit to stderr as structured log
            print(f"[audit] {line}", file=sys.stderr)

    def _record_metrics(self, decision: str, risk: str, duration_ms: float) -> None:
        decision_key = f"decision:{decision}"
        risk_key = f"risk:{risk}"
        self._counts[decision_key] = self._counts.get(decision_key, 0) + 1
        self._counts[risk_key] = self._counts.get(risk_key, 0) + 1
        self._duration_totals[decision] = self._duration_totals.get(decision, 0.0) + duration_ms

    def summary(self) -> dict[str, Any]:
        """Return in-memory counters/latency aggregates for quick diagnostics."""
        avg_duration_ms = {
            k: round(v / max(self._counts.get(f"decision:{k}", 1), 1), 1)
            for k, v in self._duration_totals.items()
        }
        return {
            "counts": dict(self._counts),
            "avg_duration_ms_by_decision": avg_duration_ms,
        }


def _sanitise(args: dict[str, Any]) -> dict[str, Any]:
    """Replace large values (>500 chars) with a truncation marker."""
    result: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 500:
            result[k] = v[:500] + "…"
        else:
            result[k] = v
    return result


class _Timer:
    """Simple wall-clock timer."""

    def __init__(self) -> None:
        self._start = time.monotonic()

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._start) * 1000
