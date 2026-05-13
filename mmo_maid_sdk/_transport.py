"""
Low-level JSON-RPC transport over stdin/stdout.

Fixes applied:
  F33: read_loop no longer runs handlers inline. A dedicated dispatcher thread
       pulls from _dispatch_queue, keeping the reader thread free to unblock
       pending call() waiters immediately.
  F34: call() checks evt.wait() return value, raises TimeoutError on timeout,
       and always pops _pending to prevent unbounded growth.
  M4:  RateLimitError.retry_after is now parsed from host error messages.
"""
from __future__ import annotations

import json
import os
import queue as _queue
import sys
import threading
import traceback as _traceback
from typing import Any, Callable, Dict, Optional


class Transport:
    """
    Thread-safe JSON-RPC transport over stdin/stdout.

    Two threads:
      1. read_loop  — reads stdin, routes responses to pending call() waiters
                      or puts notifications into _dispatch_queue.  Never runs
                      user code.
      2. dispatcher — pulls (handler, params) pairs from _dispatch_queue and
                      executes them.  This is where plugin event handlers run.
    """

    def __init__(self) -> None:
        self._write_lock = threading.Lock()
        self._pending: Dict[int, threading.Event] = {}
        self._results: Dict[int, Any] = {}
        self._errors: Dict[int, str] = {}
        self._next_id = 1
        self._next_id_lock = threading.Lock()
        # Registered notification handlers: method -> callable
        self._handlers: Dict[str, Callable] = {}

        # F33: separate dispatcher threads so read_loop never blocks on user code.
        # Multiple dispatch threads allow concurrent event processing within one
        # worker — each event's RPC calls (kv.get, kv.put, discord.*) run in
        # parallel, multiplying per-worker throughput.
        self._dispatch_queue: _queue.Queue = _queue.Queue(maxsize=2048)
        _dispatch_threads = max(1, int(os.environ.get("MMO_SDK_DISPATCH_THREADS", "4") or 4))
        for i in range(_dispatch_threads):
            t = threading.Thread(target=self._dispatch_loop, daemon=True, name=f"dispatch-{i}")
            t.start()
        # Callback invoked when dispatch queue drains (for flushing ack buffers)
        self._on_queue_idle: Optional[Callable] = None

    # ── Dispatcher thread ──────────────────────────────────────────────────

    def _dispatch_loop(self) -> None:
        """Pull notification handlers off the queue and execute them."""
        while True:
            handler, params = self._dispatch_queue.get()
            try:
                handler(params)
            except Exception:
                try:
                    self.notify("plugin.log", {
                        "level": "error",
                        "message": _traceback.format_exc()[:4000],
                    })
                except Exception:
                    pass
            # When queue drains, flush any buffered acks
            if self._dispatch_queue.empty() and self._on_queue_idle:
                try:
                    self._on_queue_idle()
                except Exception:
                    pass

    # ── Auto-ACK helper ───────────────────────────────────────────────────

    def _auto_ack_event(self, method: str, params: dict) -> None:
        """ACK an event that won't be processed (no handler or queue full).

        Without this, unprocessed events sit in the runner's pending list,
        get reclaimed up to MAX_ATTEMPTS times, then dead-letter — wasting
        resources and polluting the DLQ with events the plugin never intended
        to handle.
        """
        event_id = params.get("event_id")
        if event_id:
            try:
                self.notify("event.ack", {"event_id": event_id})
            except Exception:
                pass

    # ── Writing ────────────────────────────────────────────────────────────

    def _write(self, obj: Dict[str, Any]) -> None:
        line = json.dumps(obj, separators=(",", ":"))
        with self._write_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def _next_req_id(self) -> int:
        with self._next_id_lock:
            rid = self._next_id
            self._next_id += 1
        return rid

    # ── RPC calls (plugin → host) ──────────────────────────────────────────

    def call(self, method: str, params: Dict[str, Any]) -> Any:
        """
        Send a request to the host and block until the response arrives.

        F34: checks evt.wait() return value and raises TimeoutError on timeout
             instead of silently returning None.  Always cleans up _pending.

        Raises TimeoutError on 30s timeout, RuntimeError on RPC error,
        CapabilityError on capability denial.
        """
        from ._exceptions import CapabilityError

        rid = self._next_req_id()
        evt = threading.Event()
        self._pending[rid] = evt

        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})

        fired = evt.wait(timeout=30)

        # F34: always pop _pending on all completion paths
        self._pending.pop(rid, None)

        if not fired:
            # F34: clean up any stale result/error entries, then raise
            self._results.pop(rid, None)
            self._errors.pop(rid, None)
            from ._exceptions import RpcTimeoutError
            raise RpcTimeoutError(f"RPC timeout: {method} (30s)")

        if rid in self._errors:
            err = self._errors.pop(rid, "unknown error")
            err_lower = err.lower()
            if "capability" in err_lower:
                raise CapabilityError(err)
            if "quota exceeded" in err_lower or "rate limit" in err_lower:
                from ._exceptions import RateLimitError
                # M4: Parse retry_after from "remaining=X.X/min" in error message
                _retry = 5  # default: wait 5 seconds
                import re as _re
                _m = _re.search(r"remaining=([\d.]+)/min", err)
                if _m:
                    remaining = float(_m.group(1))
                    _retry = 60 if remaining <= 0 else max(1, int(60 / max(remaining, 1)))
                raise RateLimitError(err, retry_after=_retry)
            if "discord api error" in err_lower:
                from ._exceptions import DiscordApiError
                code = 0
                # Try to parse status code from "Discord API error 403: ..."
                parts = err.split()
                for p in parts:
                    if p.isdigit():
                        code = int(p)
                        break
                raise DiscordApiError(err, status_code=code)
            if ("bot lacks" in err_lower) or ("missing permission" in err_lower) or ("lacks permission" in err_lower):
                from ._exceptions import SdkPermissionError
                raise SdkPermissionError(err)
            if "kv quota" in err_lower or "plugin_kv quota" in err_lower:
                from ._exceptions import KvQuotaError
                raise KvQuotaError(err)
            if "required" in err_lower and ("must be" in err_lower or "invalid" in err_lower):
                from ._exceptions import ValidationError
                raise ValidationError(err)
            raise RuntimeError(f"RPC error ({method}): {err}")

        return self._results.pop(rid, None)

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        """Send a notification to the host (no response expected)."""
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    # ── Reading ────────────────────────────────────────────────────────────

    def register_handler(self, method: str, fn: Callable) -> None:
        self._handlers[method] = fn

    def read_loop(self) -> None:
        """
        Run forever reading stdin.  Dispatches:
          - Responses (has "id" + "result"/"error") → unblock pending call()
          - Notifications (has "method", no "id") → put on dispatch queue

        F33: Notifications are never executed inline here.  The dispatcher
             thread handles them so this loop stays unblocked.

        Exits when stdin closes (host shut down the plugin).
        """
        for line in sys.stdin:
            s = line.strip()
            if not s:
                continue
            try:
                msg = json.loads(s)
            except json.JSONDecodeError:
                continue

            if not isinstance(msg, dict):
                continue

            msg_id = msg.get("id")
            method = msg.get("method")

            # ── Response to one of our calls ──────────────────────────────
            if msg_id is not None and msg_id in self._pending:
                if "error" in msg:
                    err = msg["error"]
                    self._errors[msg_id] = (
                        err.get("message") if isinstance(err, dict) else str(err)
                    )
                else:
                    self._results[msg_id] = msg.get("result")
                self._pending[msg_id].set()
                continue

            # ── Request from host (dashboard data calls, etc.) ──────────────
            # Has both "method" and "id" — host expects a response.
            if method and msg_id is not None and msg_id not in self._pending:
                handler = self._handlers.get(method)
                if handler:
                    def _respond(req_id, req_handler, req_params):
                        try:
                            result = req_handler(req_params)
                            self._write({"jsonrpc": "2.0", "id": req_id, "result": result})
                        except Exception as e:
                            self._write({"jsonrpc": "2.0", "id": req_id, "error": {"message": str(e)}})
                    # Dispatch on the queue so it doesn't block the read loop
                    try:
                        self._dispatch_queue.put_nowait(
                            (lambda p, rid=msg_id, rh=handler: _respond(rid, rh, p), msg.get("params") or {})
                        )
                    except _queue.Full:
                        self._write({"jsonrpc": "2.0", "id": msg_id, "error": {"message": "dispatch queue full"}})
                else:
                    self._write({"jsonrpc": "2.0", "id": msg_id, "error": {"message": f"unknown method: {method}"}})
                continue

            # ── Notification from host (event, boot, etc.) ─────────────────
            # F33: put on dispatch queue — never run inline
            if method and msg_id is None:
                handler = self._handlers.get(method)
                if handler:
                    try:
                        self._dispatch_queue.put_nowait((handler, msg.get("params") or {}))
                    except _queue.Full:
                        # Backpressure: drop notification if dispatcher is saturated.
                        # Auto-ACK the dropped event so it doesn't sit in pending
                        # forever and eventually dead-letter.
                        self._auto_ack_event(method, msg.get("params") or {})
                        try:
                            self.notify("plugin.log", {
                                "level": "warning",
                                "message": f"Dispatch queue full — dropped {method}",
                            })
                        except Exception:
                            pass
                elif method.startswith("event."):
                    # No handler registered for this event type — auto-ACK so
                    # the event doesn't permanently sit in the runner's pending
                    # list, get reclaimed 5 times, and dead-letter.
                    self._auto_ack_event(method, msg.get("params") or {})
