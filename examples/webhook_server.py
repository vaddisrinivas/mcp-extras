"""Webhook server that holds requests until you manually approve/deny via curl.

Run:
    uv run python examples/webhook_server.py

It receives approval requests, prints a curl command, and waits for you to run it.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# Shared state: the webhook POST blocks until you hit /respond
_lock = threading.Lock()
_event = threading.Event()
_decision: bool = False


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global _decision
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        message = body.get("message", "(no message)")
        tool = "(unknown)"
        for line in message.split("\n"):
            if "Tool:" in line or "tool:" in line:
                tool = line.strip()
                break

        print(f"\n{'=' * 60}")
        print(f"APPROVAL REQUEST RECEIVED")
        print(f"{tool}")
        print(f"{'=' * 60}")
        print(f"\nRun one of these:\n")
        print(f"  curl -s http://127.0.0.1:9999/approve")
        print(f"  curl -s http://127.0.0.1:9999/deny")
        print(f"\nWaiting...\n")

        _event.clear()
        _event.wait(timeout=120)

        with _lock:
            approve = _decision

        response = {
            "action": "accept" if approve else "decline",
            "content": {"approved": approve, "reason": "manual decision"},
        }

        print(f">>> {'APPROVED' if approve else 'DENIED'}\n")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

    def do_GET(self):
        global _decision
        path = self.path.rstrip("/")

        if path == "/approve":
            with _lock:
                _decision = True
            _event.set()
            self._respond("approved")
        elif path == "/deny":
            with _lock:
                _decision = False
            _event.set()
            self._respond("denied")
        else:
            self._respond("unknown path — use /approve or /deny", 404)

    def _respond(self, msg, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(f"{msg}\n".encode())

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 9999), Handler)
    server.daemon_threads = True
    print("Webhook server on http://127.0.0.1:9999")
    print("Waiting for approval requests...\n")
    server.serve_forever()
