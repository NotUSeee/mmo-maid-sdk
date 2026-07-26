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

import contextvars
import json
import os
import queue as _queue
import sys
import threading
import traceback as _traceback
from typing import Any, Callable, Dict, Optional, Tuple

# C5 (cross-tenant context drift): the runner issues a unique event_id in every
# event notification it sends to the plugin. The handler dispatch sets this
# ContextVar to the (event_id, server_id) of the event currently being handled,
# and Transport.call() echoes the event_id back on every outbound RPC. The host
# then resolves the RPC's tenant by that trusted event_id instead of "most
# recently set context", which leaks across servers in pool mode.
#
# Why a ContextVar (not threading.local): event handlers run synchronously
# inside the dispatch thread that pulls from _dispatch_queue, and call() runs in
# the same call stack — so the value set at dispatch time is visible to call().
# ContextVars are isolated per-thread by default, so concurrent dispatch threads
# each carry their own event_id; and if handlers ever become coroutines the value
# still propagates across await. Background tasks (schedule/cron/on_ready) run on
# their own threads with this UNSET, so call() injects nothing and the host falls
# back to its existing behavior — making this change strictly additive.
_current_event: "contextvars.ContextVar[Optional[Tuple[str, str]]]" = contextvars.ContextVar(
    "yourbot_current_event", default=None
)


def set_current_event(event_id: Optional[str], server_id: Optional[str] = None) -> "contextvars.Token":
    """Bind the current event correlator for the duration of one handler.

    Returns a token; pass it to ``reset_current_event`` in a ``finally`` so the
    correlator never leaks into the next event dispatched on the same thread.
    """
    return _current_event.set(
        (str(event_id), str(server_id or "")) if event_id else None
    )


def reset_current_event(token: "contextvars.Token") -> None:
    """Restore the previous event correlator (paired with set_current_event)."""
    try:
        _current_event.reset(token)
    except Exception:
        # A reset can only fail if the token came from a different Context; in
        # that case force-clear so we never leave a stale correlator behind.
        _current_event.set(None)


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
        # WP6: one lock guards the RPC-correlation state so read_loop and the
        # call()-side timeout cleanup never race a check-then-act on _pending
        # (which previously KeyError'd and permanently killed the reader thread).
        self._state_lock = threading.Lock()
        self._closed = False
        self._pending: Dict[int, threading.Event] = {}
        self._results: Dict[int, Any] = {}
        self._errors: Dict[int, str] = {}
        # Structured error fields (code/retry_after) shipped by newer hosts
        # alongside the message. Keyed like _errors; absent for message-only
        # errors from older hosts, so everything downstream treats it as
        # optional.
        self._error_meta: Dict[int, Dict[str, Any]] = {}
        self._next_id = 1
        self._next_id_lock = threading.Lock()
        # Registered notification handlers: method -> callable
        self._handlers: Dict[str, Callable] = {}

        # F33: separate dispatcher threads so read_loop never blocks on user code.
        # Multiple dispatch threads allow concurrent event processing within one
        # worker — each event's RPC calls (kv.get, kv.put, discord.*) run in
        # parallel, multiplying per-worker throughput.
        self._dispatch_queue: _queue.Queue = _queue.Queue(maxsize=2048)
        _dispatch_threads = max(1, int(
            os.environ.get("YOURBOT_SDK_DISPATCH_THREADS")
            or os.environ.get("MMO_SDK_DISPATCH_THREADS")  # legacy fallback
            or 4
        ))
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

        Raises RpcTimeoutError on 30s timeout and a typed SdkError subclass on
        RPC error (CapabilityError on capability denial, ...; RpcError — which
        also subclasses RuntimeError — when the error maps to nothing more
        specific).
        """
        from ._exceptions import CapabilityError, RpcError, RpcTimeoutError

        rid = self._next_req_id()
        evt = threading.Event()
        with self._state_lock:
            if self._closed:
                raise RpcTimeoutError(f"RPC aborted: transport closed ({method})")
            self._pending[rid] = evt

        # C5: stamp the current event's runner-issued event_id (and, as a
        # best-effort hint only, its server_id) so the host can resolve THIS
        # call's tenant by the trusted event_id rather than "most recent".
        # Only when a handler is active — background tasks leave _current_event
        # unset, so we emit exactly the legacy frame (no _event_id) and the host
        # keeps its current resolution. We copy params so we never mutate the
        # caller's dict, and never overwrite a key the caller already set.
        cur = _current_event.get()
        if cur is not None:
            _eid, _srv = cur
            if _eid and (not isinstance(params, dict) or "_event_id" not in params):
                params = dict(params) if isinstance(params, dict) else {}
                params["_event_id"] = _eid
                if _srv and "_pool_discord_srv_id" not in params:
                    params["_pool_discord_srv_id"] = _srv

        try:
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except Exception:
            # A failed write (e.g. json.dumps TypeError on a non-serializable
            # param) means no response will ever arrive — drop the pending
            # entry instead of leaking it.
            with self._state_lock:
                self._pending.pop(rid, None)
            raise

        fired = evt.wait(timeout=30)

        # WP6: pop correlation state atomically under the lock. On the success
        # path read_loop already popped _pending and published _results/_errors;
        # on the timeout/closed path we pop here. The deref is never split from
        # the membership test, so read_loop can never KeyError on a vanished key.
        with self._state_lock:
            self._pending.pop(rid, None)
            closed = self._closed
            had_error = rid in self._errors
            err = self._errors.pop(rid, None)
            meta = self._error_meta.pop(rid, None) or {}
            result = self._results.pop(rid, None)

        if not fired:
            if closed:
                raise RpcTimeoutError(f"RPC aborted: transport closed ({method})")
            raise RpcTimeoutError(f"RPC timeout: {method} (30s)")

        if had_error:
            err = err or "unknown error"
            err_lower = err.lower()
            import re as _re
            # Structured fields from newer hosts (validated in _dispatch_message);
            # both stay None/absent on message-only errors from older hosts.
            _struct_code = meta.get("code")
            _retry_field = meta.get("retry_after")
            # "(retry in <N>s)" appears in hour-scale limiter messages (e.g. the
            # DDL window) where the legacy remaining=/min parse has nothing to
            # work with — it is both a retry_after source and a rate-limit signal.
            _retry_m = _re.search(r"retry in ([\d.]+)s", err_lower)

            def _retry_after_secs() -> float:
                # Accuracy order: structured retry_after field, then the
                # "(retry in <N>s)" message parse, then the legacy M4
                # "remaining=X.X/min" parse, then the historical 5s default.
                if _retry_field is not None:
                    return _retry_field
                if _retry_m:
                    try:
                        return float(_retry_m.group(1))
                    except ValueError:
                        pass
                _m = _re.search(r"remaining=([\d.]+)/min", err)
                if _m:
                    remaining = float(_m.group(1))
                    return 60.0 if remaining <= 0 else float(max(1, int(60 / max(remaining, 1))))
                return 5.0

            def _perm_name() -> str:
                # Populates SdkPermissionError.permission. The structured field
                # from newer hosts wins; otherwise best-effort parse of the two
                # host message shapes ("bot lacks X permission" and
                # "missing permission: X"). Empty string when unknown.
                _p = meta.get("permission")
                if isinstance(_p, str) and _p:
                    return _p
                _pm = _re.search(r"lacks ([A-Za-z_ ]+?) permission", err)
                if _pm:
                    return _pm.group(1).strip()
                _pm = _re.search(r"missing permission[:\s]+([A-Za-z_]+)", err, _re.IGNORECASE)
                return _pm.group(1) if _pm else ""

            # ── Structured code wins over message heuristics ──────────────
            # Unknown codes deliberately fall through to the substring path so
            # a newer host can never regress an older mapping.
            if _struct_code is not None:
                if _struct_code in ("RATE_LIMITED", "QUOTA_EXCEEDED"):
                    from ._exceptions import RateLimitError
                    raise RateLimitError(err, retry_after=_retry_after_secs(), code=_struct_code)
                if _struct_code == "CAPABILITY_DENIED":
                    hint = "" if "manifest" in err_lower else (
                        " (add the capability to capabilities_required in your manifest.json)"
                    )
                    raise CapabilityError(err + hint)
                if _struct_code == "KV_QUOTA_EXCEEDED":
                    from ._exceptions import KvQuotaError
                    raise KvQuotaError(err)
                if _struct_code == "DISCORD_API_ERROR":
                    from ._exceptions import DiscordApiError
                    _m = _re.search(r"error\s+(\d{3})", err_lower)
                    raise DiscordApiError(err, status_code=int(_m.group(1)) if _m else 0)
                if _struct_code == "BOT_MISSING_PERMISSION":
                    from ._exceptions import SdkPermissionError
                    raise SdkPermissionError(err, permission=_perm_name())
                if _struct_code == "VALIDATION_ERROR":
                    from ._exceptions import ValidationError
                    raise ValidationError(err)

            if "capability" in err_lower:
                # Append an actionable hint pointing at the manifest, since this
                # is the most common first-run failure.
                hint = "" if "manifest" in err_lower else (
                    " (add the capability to capabilities_required in your manifest.json)"
                )
                raise CapabilityError(err + hint)
            # KV-quota must be checked before the generic quota branch: the host
            # message "plugin_kv quota exceeded" also contains "quota exceeded",
            # so the generic branch would otherwise mask KvQuotaError.
            if "kv quota" in err_lower or "plugin_kv quota" in err_lower:
                from ._exceptions import KvQuotaError
                raise KvQuotaError(err)
            if "quota exceeded" in err_lower or "rate limit" in err_lower or _retry_m:
                from ._exceptions import RateLimitError
                _code = "QUOTA_EXCEEDED" if "quota exceeded" in err_lower else "RATE_LIMITED"
                raise RateLimitError(err, retry_after=_retry_after_secs(), code=_code)
            if "discord api error" in err_lower:
                from ._exceptions import DiscordApiError
                # WP6: parse "Discord API error 403: ..." — the prior token
                # split failed because "403:" is not .isdigit().
                _m = _re.search(r"error\s+(\d{3})", err_lower)
                code = int(_m.group(1)) if _m else 0
                raise DiscordApiError(err, status_code=code)
            if ("bot lacks" in err_lower) or ("missing permission" in err_lower) or ("lacks permission" in err_lower):
                from ._exceptions import SdkPermissionError
                raise SdkPermissionError(err, permission=_perm_name())
            if "required" in err_lower and ("must be" in err_lower or "invalid" in err_lower):
                from ._exceptions import ValidationError
                raise ValidationError(err)
            # RpcError subclasses both SdkError (so `except SdkError` is a true
            # catch-all, as documented) and RuntimeError (what this raised
            # historically).
            raise RpcError(f"RPC error ({method}): {err}")

        return result

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

        WP6: every per-message body is wrapped so a single malformed/racy
        message can never terminate the reader thread, and on EOF/host-death all
        parked call() waiters are failed fast instead of each hanging 30s.

        Exits when stdin closes (host shut down the plugin).
        """
        try:
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
                try:
                    self._dispatch_message(msg)
                except Exception:
                    # A single message must never kill the reader thread.
                    try:
                        self.notify("plugin.log", {
                            "level": "error",
                            "message": "transport dispatch error: " + _traceback.format_exc()[:2000],
                        })
                    except Exception:
                        pass
        finally:
            self._fail_all_pending("transport closed (host disconnected)")

    def _fail_all_pending(self, reason: str) -> None:
        """WP6: mark the transport closed and wake every parked call() waiter so
        they fail fast instead of blocking the full 30s timeout."""
        with self._state_lock:
            self._closed = True
            pending = list(self._pending.items())
            for rid, _evt in pending:
                self._errors[rid] = reason
            self._pending.clear()
        for _rid, evt in pending:
            evt.set()

    def _dispatch_message(self, msg: Dict[str, Any]) -> None:
        msg_id = msg.get("id")
        method = msg.get("method")

        # ── Response to one of our calls ──────────────────────────────
        if msg_id is not None:
            with self._state_lock:
                evt = self._pending.pop(msg_id, None)
                if evt is not None:
                    if "error" in msg:
                        err = msg["error"]
                        if isinstance(err, dict):
                            _msg = err.get("message")
                            self._errors[msg_id] = (
                                _msg if _msg is None or isinstance(_msg, str) else str(_msg)
                            )
                            # Newer hosts ship optional structured fields
                            # alongside message. Validate types here so call()
                            # can trust the meta dict; old message-only hosts
                            # simply never populate it.
                            _meta: Dict[str, Any] = {}
                            _c = err.get("code")
                            if isinstance(_c, str) and _c:
                                _meta["code"] = _c
                            _ra = err.get("retry_after")
                            if isinstance(_ra, (int, float)) and not isinstance(_ra, bool) and _ra >= 0:
                                _meta["retry_after"] = float(_ra)
                            _p = err.get("permission")
                            if isinstance(_p, str) and _p:
                                _meta["permission"] = _p
                            if _meta:
                                self._error_meta[msg_id] = _meta
                        else:
                            # Legacy/defensive: non-dict error payloads degrade
                            # to their string form, exactly as before.
                            self._errors[msg_id] = str(err)
                    else:
                        self._results[msg_id] = msg.get("result")
            if evt is not None:
                # WP6: publish result/error under the lock (above), set the Event
                # outside it, and only when the id was still pending. The pop is
                # atomic, so a concurrent timed-out call() can never make this a
                # KeyError-on-a-vanished-key.
                evt.set()
                return

        # ── Request from host (dashboard data calls, etc.) ──────────────
        # Has both "method" and "id" — host expects a response.
        if method and msg_id is not None:
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
            return

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
                # No handler registered for this event type — auto-ACK so the
                # event doesn't permanently sit in the runner's pending list,
                # get reclaimed 5 times, and dead-letter.
                self._auto_ack_event(method, msg.get("params") or {})
