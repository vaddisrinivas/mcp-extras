"""Tests for the JSONL audit logger."""

from __future__ import annotations

import json

from mcp_extras.audit import AuditLogger, _sanitise


class TestAuditLogger:
    def test_writes_jsonl_to_file(self, tmp_path):
        log = tmp_path / "audit.jsonl"
        a = AuditLogger(path=log)
        a.log(server="s", tool="t", args={}, decision="approved", risk="medium")
        lines = log.read_text().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["server"] == "s"
        assert record["tool"] == "t"
        assert record["decision"] == "approved"
        assert record["risk"] == "medium"

    def test_multiple_appends(self, tmp_path):
        log = tmp_path / "audit.jsonl"
        a = AuditLogger(path=log)
        a.log(server="s", tool="t1", args={}, decision="approved")
        a.log(server="s", tool="t2", args={}, decision="denied")
        lines = log.read_text().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["tool"] == "t1"
        assert json.loads(lines[1])["tool"] == "t2"

    def test_creates_parent_directories(self, tmp_path):
        log = tmp_path / "deep" / "nested" / "audit.jsonl"
        a = AuditLogger(path=log)
        a.log(server="s", tool="t", args={}, decision="passed")
        assert log.exists()

    def test_dry_run_flag_recorded(self, tmp_path):
        log = tmp_path / "audit.jsonl"
        a = AuditLogger(path=log, dry_run=True)
        a.log(server="s", tool="t", args={}, decision="dry_run")
        record = json.loads(log.read_text())
        assert record["dry_run"] is True

    def test_timestamp_format(self, tmp_path):
        log = tmp_path / "audit.jsonl"
        a = AuditLogger(path=log)
        a.log(server="s", tool="t", args={}, decision="passed")
        record = json.loads(log.read_text())
        ts = record["ts"]
        # Should end with 'Z' and contain 'T'
        assert ts.endswith("Z")
        assert "T" in ts

    def test_duration_ms_recorded(self, tmp_path):
        log = tmp_path / "audit.jsonl"
        a = AuditLogger(path=log)
        a.log(server="s", tool="t", args={}, decision="passed", duration_ms=123.456)
        record = json.loads(log.read_text())
        assert record["duration_ms"] == 123.5  # rounded to 1dp

    def test_args_included(self, tmp_path):
        log = tmp_path / "audit.jsonl"
        a = AuditLogger(path=log)
        a.log(server="s", tool="t", args={"path": "/tmp/x"}, decision="approved")
        record = json.loads(log.read_text())
        assert record["args"]["path"] == "/tmp/x"

    def test_no_path_does_not_raise(self):
        # AuditLogger with no path should not fail (writes to stderr)
        a = AuditLogger(path=None)
        a.log(server="s", tool="t", args={}, decision="passed")  # should not raise

    def test_not_enabled_when_no_path(self):
        a = AuditLogger(path=None)
        assert a.enabled is False

    def test_enabled_when_path_given(self, tmp_path):
        a = AuditLogger(path=tmp_path / "audit.jsonl")
        assert a.enabled is True


class TestSanitise:
    def test_short_strings_unchanged(self):
        result = _sanitise({"key": "short value"})
        assert result["key"] == "short value"

    def test_long_strings_truncated(self):
        result = _sanitise({"data": "x" * 1000})
        assert len(result["data"]) < 510
        assert result["data"].endswith("…")

    def test_non_string_values_unchanged(self):
        result = _sanitise({"num": 42, "flag": True, "items": [1, 2, 3]})
        assert result["num"] == 42
        assert result["flag"] is True
        assert result["items"] == [1, 2, 3]

    def test_exactly_500_chars_not_truncated(self):
        result = _sanitise({"data": "x" * 500})
        assert result["data"] == "x" * 500

    def test_501_chars_truncated(self):
        result = _sanitise({"data": "x" * 501})
        assert result["data"].endswith("…")
        assert len(result["data"]) == 501  # 500 + ellipsis
