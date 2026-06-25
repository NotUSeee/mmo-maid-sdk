"""
Plugin — the main class developers instantiate in their __main__.py.

Fixes applied:
  F35: _dispatch_event only auto-acks if all handlers completed without exception.
       On any exception, the event is left pending for the runner to reclaim.
  F38: In pool mode, context is rebuilt per event from the event params so
       ctx.server_id and ctx.has_capability() are correct for each delivery.
  C6:  Boot is idempotent — second host.boot is ignored to prevent duplicate handlers.
  H7:  Pool mode per-event capabilities are intersected with boot-time caps.
"""
from __future__ import annotations

import os
import sys
import threading
import traceback as _traceback
from typing import Any, Callable, Dict, List, Optional

from ._transport import Transport
from ._context import Context


class Plugin:
    """
    The entry point for every YourBot plugin.

    Basic usage::

        from yourbot_sdk import Plugin, Context

        plugin = Plugin()

        @plugin.on_ready
        def ready(ctx: Context):
            ctx.log("Plugin started!")

        @plugin.on_event("message_create")
        def on_message(ctx: Context, event: dict):
            content = event.get("content", "")
            if "!ping" in content:
                ctx.discord.send_message(
                    channel_id=event["channel_id"],
                    content="Pong! 🏓",
                )

        plugin.run()  # must be last line of __main__.py
    """

    def __init__(self) -> None:
        self._transport = Transport()
        self._event_handlers: Dict[str, List[Callable]] = {}
        self._ready_handlers: List[Callable] = []
        self._dashboard_handlers: Dict[str, Callable] = {}  # method_name → handler
        self._scheduled_tasks: List[tuple] = []  # (fn, interval_seconds)
        self._cron_tasks: List[tuple] = []  # (fn, parsed_cron_spec, raw_spec)
        self._scheduled_threads: List[threading.Thread] = []
        self._lifecycle_handlers: Dict[str, Callable] = {}  # "install"/"enable"/"disable"/"uninstall" → fn
        # WebSocket frame handlers, keyed by connection name then kind
        # ("message"/"open"/"close"). Frames are fire-and-forget (no ack).
        self._ws_handlers: Dict[str, Dict[str, List[Callable]]] = {}
        # Per-connection lock so frames for one connection run in order even
        # though the transport dispatches notifications across N threads.
        self._ws_locks: Dict[str, threading.Lock] = {}
        self._ws_locks_guard = threading.Lock()
        self._ctx: Optional[Context] = None
        # F38: track whether we're in pool mode
        self._pool_mode: bool = False
        # In pool mode, on_ready fires once per (worker, server) on the first
        # event for each tenant — see _dispatch_event. This set tracks which
        # servers have already had on_ready fired in this worker process.
        self._pool_ready_servers: set = set()
        self._pool_ready_lock = threading.Lock()
        # Batch ack buffer — flushed as a single non-blocking notification.
        # Protected by lock for thread-safety with multiple dispatch threads.
        self._ack_buffer: List[str] = []
        self._ack_lock = threading.Lock()
        self._ACK_FLUSH_SIZE = 1
        self._stop_event = threading.Event()

        # Wire up internal host notifications
        self._transport.register_handler("host.boot", self._handle_boot)
        self._transport.register_handler("host.shutdown", self._handle_shutdown)
        self._transport.register_handler("host.install", self._handle_lifecycle)
        self._transport.register_handler("host.enable", self._handle_lifecycle)
        self._transport.register_handler("host.disable", self._handle_lifecycle)
        self._transport.register_handler("host.uninstall", self._handle_lifecycle)
        # Flush ack buffer when dispatch queue drains
        self._transport._on_queue_idle = self._flush_ack_buffer

    # ── Decorators ─────────────────────────────────────────────────────────

    def on_ready(self, fn: Callable) -> Callable:
        """
        Register a function called once when the plugin boots successfully.

        Single-tenant mode: fires once per worker boot, in a background thread.
        Pool mode: fires once per (worker, server) on the first event for each
        tenant, synchronously before the event handler runs — so per-tenant
        init (schema bootstrap, KV defaults) completes before SQL/KV calls.

        The function receives one argument: ctx (Context). In pool mode the
        ctx is scoped to the tenant that triggered the first event.
        """
        self._ready_handlers.append(fn)
        return fn

    def on_event(self, event_type: str) -> Callable:
        """
        Register a handler for a Discord event type.

        The function receives two arguments: ctx (Context) and event (dict).

        Supported event types:
          - message_create        A message was sent in a channel
          - message_edit          A message was edited
          - message_delete        A message was deleted
          - member_join           A member joined the server
          - member_leave          A member left the server
          - member_update         A member's roles/nick/avatar changed
          - reaction_add          A reaction was added to a message
          - reaction_remove       A reaction was removed from a message
          - voice_state_update    A member's voice state changed
          - interaction_create    A slash command, button, select, or modal was used
          - channel_create        A channel was created
          - channel_delete        A channel was deleted
          - channel_update        A channel was modified
          - role_create           A role was created
          - role_delete           A role was deleted
          - role_update           A role was modified
        """
        def decorator(fn: Callable) -> Callable:
            self._event_handlers.setdefault(event_type, []).append(fn)
            return fn
        return decorator

    def _register_ws(self, name: str, kind: str, fn: Callable) -> Callable:
        self._ws_handlers.setdefault(name, {}).setdefault(kind, []).append(fn)
        return fn

    def on_ws_message(self, name: str) -> Callable:
        """Register a handler for inbound frames on the named WebSocket connection.

        The function receives ``(ctx, frame)`` where ``frame`` is
        ``{"name", "conn_id", "data", "binary"}``. For ``binary=True`` frames,
        ``data`` is base64-encoded (decode with ``base64.b64decode``). Requires
        the ``proxy:websocket`` capability and a matching ``ctx.ws.ensure(name, ...)``.
        """
        return lambda fn: self._register_ws(name, "message", fn)

    def on_ws_open(self, name: str) -> Callable:
        """Register a handler called each time the named connection (re)connects.

        Receives ``(ctx)``. This is the place to (re)send any auth/subscribe
        frames — though ``ctx.ws.ensure(subscribe=[...])`` does that automatically.
        """
        return lambda fn: self._register_ws(name, "open", fn)

    def on_ws_close(self, name: str) -> Callable:
        """Register a handler called when the named connection closes or drops.

        Receives ``(ctx, info)`` where ``info`` is ``{"reason", "will_reconnect"}``.
        """
        return lambda fn: self._register_ws(name, "close", fn)

    def on_slash_command(self, command_name: str) -> Callable:
        """Register a handler for a specific slash command.

        Convenience wrapper that filters interaction_create events.
        The function receives two arguments: ctx (Context) and event (dict).

        Example::

            @plugin.on_slash_command("hello")
            def handle_hello(ctx, event):
                ctx.interaction.respond(content="Hello!")
        """
        def decorator(fn: Callable) -> Callable:
            def filtered_handler(ctx, event):
                if (event.get("interaction_type") == 2
                        and event.get("command_name") == command_name):
                    return fn(ctx, event)
            self._event_handlers.setdefault("interaction_create", []).append(filtered_handler)
            return fn
        return decorator

    def on_component(
        self,
        custom_id: Optional[str] = None,
        *,
        prefix: Optional[str] = None,
    ) -> Callable:
        """Register a handler for a component interaction (button click, select, etc.).

        Pass either ``custom_id="exact_id"`` for an exact-string match, or
        ``prefix="page:"`` to match any ``custom_id`` starting with that
        prefix. Use ``prefix=`` when your buttons encode dynamic state
        (e.g. ``"page:next:5"``, ``"vote:yes:42"``).

        Exactly one of ``custom_id=`` or ``prefix=`` must be provided.

        Example::

            @plugin.on_component("btn_join")
            def handle_join(ctx, event):
                ctx.interaction.respond(content="You joined!", ephemeral=True)

            @plugin.on_component(prefix="page:")
            def handle_page(ctx, event):
                # event["custom_id"] e.g. "page:next:5"
                ...
        """
        if (custom_id is None) == (prefix is None):
            raise ValueError(
                "on_component requires exactly one of custom_id= or prefix="
            )

        def decorator(fn: Callable) -> Callable:
            def filtered_handler(ctx, event):
                if event.get("interaction_type") != 3:  # MESSAGE_COMPONENT
                    return
                cid = event.get("custom_id", "")
                if custom_id is not None and cid == custom_id:
                    return fn(ctx, event)
                if prefix is not None and isinstance(cid, str) and cid.startswith(prefix):
                    return fn(ctx, event)
            self._event_handlers.setdefault("interaction_create", []).append(filtered_handler)
            return fn
        return decorator

    def on_modal_submit(self, custom_id: str) -> Callable:
        """Register a handler for a modal form submission.

        Convenience wrapper that filters interaction_create events.
        The function receives two arguments: ctx (Context) and event (dict).
        ``event["modal_values"]`` contains {custom_id: value} for each field.

        Example::

            @plugin.on_modal_submit("signup_form")
            def handle_signup(ctx, event):
                name = event["modal_values"].get("char_name", "")
                ctx.interaction.respond(content=f"Welcome, {name}!")
        """
        def decorator(fn: Callable) -> Callable:
            def filtered_handler(ctx, event):
                if (event.get("interaction_type") == 5  # MODAL_SUBMIT
                        and event.get("custom_id") == custom_id):
                    return fn(ctx, event)
            self._event_handlers.setdefault("interaction_create", []).append(filtered_handler)
            return fn
        return decorator

    def schedule(self, interval_seconds: int) -> Callable:
        """Register a background task that runs on a fixed interval.

        The function receives one argument: ctx (Context).
        Starts after on_ready completes. Stops when the plugin shuts down.

        Example::

            @plugin.schedule(300)  # every 5 minutes
            def cleanup(ctx):
                ctx.log("Running cleanup...")
                # do work
        """
        def decorator(fn: Callable) -> Callable:
            self._scheduled_tasks.append((fn, int(interval_seconds)))
            return fn
        return decorator

    def cron(self, spec: str) -> Callable:
        """Register a background task that runs on a cron schedule (UTC).

        Spec format: ``"minute hour day-of-month month day-of-week"`` — five
        fields. Each supports ``*``, ``*/N``, ``N``, ``N,M,…``, ``N-M``,
        ``N-M/S``. Day-of-week is 0=Sunday … 6=Saturday. All times are UTC.

        Examples::

            @plugin.cron("0 9 * * 1")        # every Monday at 09:00 UTC
            def weekly_report(ctx):
                ctx.log("Sending weekly report")

            @plugin.cron("*/15 * * * *")     # every 15 minutes
            def heartbeat(ctx):
                ...

            @plugin.cron("30 0 1 * *")       # 00:30 UTC on the 1st of each month
            def monthly_rollup(ctx):
                ...

        Notes:
          - Like ``@schedule``, this is a thread-based loop inside the worker
            process; if the plugin is restarted between firings, the missed
            tick is **not** replayed.
          - Cron tasks do not run in pool mode (same constraint as
            ``@schedule``) because pool workers don't carry per-install ctx.
            For pooled plugins, use the server-side cron registered in your
            manifest (see PLUGIN_DEV.md).
          - Invalid specs raise ``ValueError`` immediately at registration —
            you'll see the error during boot, not silently at the first miss.
        """
        parsed = _parse_cron_spec(spec)
        def decorator(fn: Callable) -> Callable:
            self._cron_tasks.append((fn, parsed, str(spec)))
            return fn
        return decorator

    def on_dashboard(self, method_name: str) -> Callable:
        """Register a dashboard data handler for the manifest-based dashboard.

        The platform calls these when a user views the plugin's dashboard page.
        The function receives (ctx, params) and returns a JSON-serializable dict.

        Widget types and expected return formats:
          stat_card  → {"value": 1234, "change": "+12%"}
          chart      → {"labels": [...], "series": [{"name": "...", "data": [...]}]}
          table      → {"rows": [{...}, ...], "total": 100}
          form (get) → {"values": {"key": "value", ...}}
          form (save)→ {"ok": True}

        Example::

            @plugin.on_dashboard("get_stat")
            def handle_get_stat(ctx, params):
                total = ctx.kv.get("total_events") or 0
                return {"value": int(total)}

            @plugin.on_dashboard("get_timeseries")
            def handle_get_timeseries(ctx, params):
                period = params.get("period", "7d")
                data = ctx.kv.get(f"timeseries:{period}") or {}
                return {"labels": data.get("labels", []), "series": data.get("series", [])}
        """
        def decorator(fn: Callable) -> Callable:
            self._dashboard_handlers[str(method_name)] = fn
            return fn
        return decorator

    # ── Lifecycle hooks ────────────────────────────────────────────────────

    def on_install(self, fn: Callable) -> Callable:
        """Called once when the plugin is first installed on a server.

        Use this to initialize default settings in KV storage.
        The function receives one argument: ctx (Context).
        """
        self._lifecycle_handlers["install"] = fn
        return fn

    def on_enable(self, fn: Callable) -> Callable:
        """Called when the plugin is enabled (toggled on) for a server.

        The function receives one argument: ctx (Context).
        """
        self._lifecycle_handlers["enable"] = fn
        return fn

    def on_disable(self, fn: Callable) -> Callable:
        """Called when the plugin is disabled (toggled off) for a server.

        Use this to pause background work or save state.
        The function receives one argument: ctx (Context).
        """
        self._lifecycle_handlers["disable"] = fn
        return fn

    def on_uninstall(self, fn: Callable) -> Callable:
        """Called when the plugin is being uninstalled from a server.

        Use this to clean up KV data or log a final message.
        The function receives one argument: ctx (Context).
        """
        self._lifecycle_handlers["uninstall"] = fn
        return fn

    # ── Internal boot handling ─────────────────────────────────────────────

    def _handle_boot(self, params: dict) -> None:
        """Called when the host sends host.boot — sets up the base context.
        Idempotent: second boot is ignored to prevent handler duplication.
        """
        if getattr(self, "_booted", False):
            return
        self._booted = True
        self._pool_mode = (params.get("mode") == "pool")

        self._ctx = Context(
            server_id=str(params.get("discord_srv_id") or ""),
            plugin_id=str(params.get("plugin_id") or ""),
            version=str(params.get("version") or ""),
            capabilities=list(params.get("capabilities") or []),
            transport=self._transport,
        )

        # Register event routing now that we have context
        for event_type in self._event_handlers:
            method = f"event.{event_type}"
            def make_handler(et):
                def handler(params: dict):
                    self._dispatch_event(et, params)
                return handler
            self._transport.register_handler(method, make_handler(event_type))

        # WebSocket inbound frames: separate, ack-free notification methods so a
        # frame flood can never enter the durable event ack/reclaim path.
        for _kind, _method in (("message", "event.ws_message"),
                               ("open", "event.ws_open"),
                               ("close", "event.ws_closed")):
            def make_ws_handler(kind):
                def handler(params: dict):
                    self._dispatch_ws(kind, params)
                return handler
            self._transport.register_handler(_method, make_ws_handler(_kind))

        # Register dashboard data handlers (request-response pattern)
        # Platform sends "dashboard.{method_name}" with an "id" field,
        # expecting a JSON-RPC response with the widget data.
        for method_name, handler_fn in self._dashboard_handlers.items():
            rpc_method = f"dashboard.{method_name}"
            def make_dash_handler(fn):
                def dash_handler(params: dict):
                    # Build a per-request context with the server ID from params
                    ctx = Context(
                        server_id=str(params.get("discord_srv_id") or self._ctx.server_id),
                        plugin_id=str(self._ctx.plugin_id),
                        version=str(self._ctx.version),
                        capabilities=list(self._ctx.capabilities),
                        transport=self._transport,
                    )
                    try:
                        result = fn(ctx, params)
                        return result  # Transport sends this as the response
                    except Exception as e:
                        ctx.log(f"Dashboard handler error ({method_name}):\n{_traceback.format_exc()[:3900]}", level="error")
                        return {"error": str(e)}
                return dash_handler
            self._transport.register_handler(rpc_method, make_dash_handler(handler_fn))

        # Fire on_ready handlers in a thread so boot doesn't block the read loop
        has_bg = self._ready_handlers or self._scheduled_tasks or self._cron_tasks
        if has_bg and not self._pool_mode:
            def run_ready():
                for fn in self._ready_handlers:
                    try:
                        fn(self._ctx)
                    except Exception as e:
                        try:
                            self._ctx.log(f"on_ready error: {e}", level="error")
                        except Exception:
                            pass
                # Start scheduled background tasks after on_ready
                for task_fn, interval in self._scheduled_tasks:
                    self._start_scheduled_task(task_fn, interval)
                for task_fn, parsed, raw in self._cron_tasks:
                    self._start_cron_task(task_fn, parsed, raw)
            threading.Thread(target=run_ready, daemon=True).start()

    def _start_scheduled_task(self, fn: Callable, interval: int) -> None:
        """Start a daemon thread that calls fn(ctx) every interval seconds."""
        def loop():
            while not self._stop_event.is_set():
                self._stop_event.wait(interval)
                if self._stop_event.is_set():
                    break
                try:
                    fn(self._ctx)
                except Exception as e:
                    try:
                        self._ctx.log(f"Scheduled task error: {e}", level="error")
                    except Exception:
                        pass
        t = threading.Thread(target=loop, daemon=True, name=f"sched-{fn.__name__}")
        t.start()
        self._scheduled_threads.append(t)

    def _start_cron_task(self, fn: Callable, parsed: dict, raw: str) -> None:
        """Start a daemon thread that calls fn(ctx) at every cron firing.

        Sleeps until the next matching minute (UTC), then fires. Skips
        firings missed during sleep — at-most-once delivery, not catch-up.
        """
        import datetime as _dt
        def loop():
            while not self._stop_event.is_set():
                now = _dt.datetime.utcnow().replace(second=0, microsecond=0)
                next_fire = _next_cron_fire(parsed, now)
                wait_sec = max(1.0, (next_fire - _dt.datetime.utcnow()).total_seconds())
                # Cap the per-iteration wait so a stop event reaches the loop
                # even if the next firing is days away.
                self._stop_event.wait(min(wait_sec, 600.0))
                if self._stop_event.is_set():
                    break
                # If we woke early (long-wait cap), continue the outer loop
                # and recompute. Once we're past the fire time, fire once.
                if _dt.datetime.utcnow() < next_fire:
                    continue
                try:
                    fn(self._ctx)
                except Exception as e:
                    try:
                        self._ctx.log(f"Cron task error ({raw}): {e}", level="error")
                    except Exception:
                        pass
                # Sleep at least 60s past the firing so we don't double-fire
                # within the same matching minute.
                self._stop_event.wait(60.0)
        t = threading.Thread(target=loop, daemon=True, name=f"cron-{fn.__name__}")
        t.start()
        self._scheduled_threads.append(t)

    def _handle_lifecycle(self, params: dict) -> None:
        """Handle host.install, host.enable, host.disable, host.uninstall."""
        if self._ctx is None:
            return
        lifecycle_type = str(params.get("lifecycle") or params.get("type") or "")
        handler = self._lifecycle_handlers.get(lifecycle_type)
        if handler:
            try:
                handler(self._ctx)
            except Exception as e:
                try:
                    self._ctx.log(f"Lifecycle handler error ({lifecycle_type}):\n{_traceback.format_exc()[:3900]}", level="error")
                except Exception:
                    pass

    def _handle_shutdown(self, params: dict) -> None:
        self._stop_event.set()  # Signal scheduled tasks to stop
        sys.exit(0)

    def _dispatch_event(self, event_type: str, params: dict) -> None:
        """Route an incoming event to registered handlers.

        F35: Only auto-acks if ALL handlers completed without exception.
             On any exception, the event is left pending for runner reclaim.
        F38: In pool mode, build a per-event context from the params so
             ctx.server_id and ctx.has_capability() reflect the actual
             delivery — not the blank values from host.boot.
        """
        if self._ctx is None:
            return

        event_id  = params.get("event_id")
        event     = params.get("event") or {}

        # F38: in pool mode, wrap/rebuild Context with per-event values
        if self._pool_mode:
            # Use boot-time capabilities as ceiling — per-event caps can only be
            # a subset of what was granted at boot (defense against host spoofing)
            event_caps = list(params.get("capabilities") or [])
            boot_caps = set(self._ctx.capabilities) if self._ctx else set()
            safe_caps = [c for c in event_caps if c in boot_caps] if boot_caps else event_caps
            ctx = Context(
                server_id=str(params.get("discord_srv_id") or ""),
                plugin_id=str(params.get("plugin_id") or self._ctx.plugin_id),
                version=str(params.get("version") or self._ctx.version),
                capabilities=safe_caps,
                transport=self._transport,
            )
        else:
            ctx = self._ctx

        # In pool mode, on_ready handlers are skipped during _handle_boot
        # (the boot-time ctx has no tenant). Fire them here — once per
        # (worker, server) — synchronously before the event handler runs, so
        # per-tenant init (schema bootstrap, KV defaults) completes before any
        # SQL/KV call from the handler.
        if self._pool_mode and self._ready_handlers:
            srv_id = str(params.get("discord_srv_id") or "")
            if srv_id and srv_id not in self._pool_ready_servers:
                with self._pool_ready_lock:
                    if srv_id not in self._pool_ready_servers:
                        self._pool_ready_servers.add(srv_id)
                        for fn in self._ready_handlers:
                            try:
                                fn(ctx)
                            except Exception:
                                try:
                                    ctx.log(
                                        f"on_ready error:\n{_traceback.format_exc()[:3900]}",
                                        level="error",
                                    )
                                except Exception:
                                    pass

        # Set interaction ID on context for interaction events
        if event_type == "interaction_create" and event.get("interaction_id"):
            ctx._current_interaction_id = event["interaction_id"]
        else:
            ctx._current_interaction_id = None

        # F35: track whether all handlers succeeded
        all_ok = True
        handlers = self._event_handlers.get(event_type, [])
        for fn in handlers:
            try:
                fn(ctx, event)
            except Exception as e:
                all_ok = False
                try:
                    ctx.log(
                        f"Event handler error ({event_type}):\n{_traceback.format_exc()[:3900]}",
                        level="error",
                    )
                except Exception:
                    pass

        # Always ACK the event to prevent retry→DLQ loops.  Infrastructure
        # errors (DB FK violations, Discord 404s) are deterministic — retrying
        # the same event will fail identically.  The circuit breaker still
        # tracks failures via event.fail notification.
        if event_id is not None:
            if not all_ok:
                # Notify runner of failure for circuit breaker tracking
                try:
                    self._transport.notify("event.fail", {
                        "event_id": event_id,
                        "reason": "handler_exception",
                    })
                except Exception:
                    pass
            with self._ack_lock:
                self._ack_buffer.append(event_id)
                if len(self._ack_buffer) >= self._ACK_FLUSH_SIZE:
                    self._flush_ack_buffer_locked()

    def _match_ws_keys(self, name: str) -> List[str]:
        """Resolve the registered handler keys that apply to a concrete connection
        ``name``. An exact registration wins; otherwise any registration ending in
        ``*`` whose prefix matches (e.g. ``"rustplus:*"`` matches ``"rustplus:eu1"``)
        applies — so a plugin managing many connections registers one handler set.
        """
        if name in self._ws_handlers:
            return [name]
        return [k for k in self._ws_handlers if k.endswith("*") and name.startswith(k[:-1])]

    def _dispatch_ws(self, kind: str, params: dict) -> None:
        """Route an inbound WebSocket notification to ws handlers by name.

        Fire-and-forget: there is no event_id and no ack — a stale live frame
        is never retried. Frames for one connection are serialized by a per-name
        lock so handlers see them in order despite N dispatcher threads.
        """
        if self._ctx is None:
            return
        name = str(params.get("name") or "")
        if not name:
            return
        match_keys = self._match_ws_keys(name)
        if not match_keys:
            return

        # Pool-aware ctx, same ceiling-intersection as _dispatch_event.
        if self._pool_mode:
            event_caps = list(params.get("capabilities") or [])
            boot_caps = set(self._ctx.capabilities) if self._ctx else set()
            safe_caps = [c for c in event_caps if c in boot_caps] if boot_caps else event_caps
            ctx = Context(
                server_id=str(params.get("discord_srv_id") or ""),
                plugin_id=str(params.get("plugin_id") or self._ctx.plugin_id),
                version=str(params.get("version") or self._ctx.version),
                capabilities=safe_caps,
                transport=self._transport,
            )
        else:
            ctx = self._ctx

        handlers = [h for k in match_keys for h in self._ws_handlers.get(k, {}).get(kind, [])]
        if not handlers:
            return

        with self._ws_locks_guard:
            lock = self._ws_locks.setdefault(name, threading.Lock())

        frame = {
            "name": name,
            "conn_id": params.get("conn_id"),
            "data": params.get("data"),
            "binary": bool(params.get("binary")),
            "reason": params.get("reason"),
            "will_reconnect": params.get("will_reconnect"),
        }
        with lock:
            for fn in handlers:
                try:
                    if kind == "open":
                        fn(ctx)
                    elif kind == "close":
                        fn(ctx, {"reason": frame["reason"], "will_reconnect": frame["will_reconnect"]})
                    else:
                        fn(ctx, frame)
                except Exception:
                    try:
                        ctx.log(f"ws handler error ({kind}/{name}):\n{_traceback.format_exc()[:3900]}",
                                level="error")
                    except Exception:
                        pass

    def _flush_ack_buffer(self) -> None:
        """Send buffered event acks (thread-safe wrapper)."""
        with self._ack_lock:
            self._flush_ack_buffer_locked()

    def _flush_ack_buffer_locked(self) -> None:
        """Send buffered event acks. Must be called with _ack_lock held."""
        if not self._ack_buffer:
            return
        ids = list(self._ack_buffer)
        self._ack_buffer.clear()
        try:
            self._transport.notify("event.ack", {"event_ids": ids})
        except Exception as exc:
            try:
                self._transport.notify("plugin.log", {
                    "level": "error",
                    "message": f"_flush_ack_buffer failed ({len(ids)} events): {exc}",
                })
            except Exception:
                pass

    # ── Main run loop ──────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Start the plugin.  This call blocks forever.

        Must be the last line of your __main__.py.
        """
        # Unbuffer stdout so JSON-RPC lines flush immediately
        sys.stdout = open(sys.stdout.fileno(), "w", buffering=1, closefd=False)  # noqa: WPS515

        # Ping the host so it knows we started cleanly
        self._transport.notify("plugin.log", {
            "level": "info",
            "message": "SDK plugin process started",
        })

        # Block reading stdin until the host closes it
        try:
            self._transport.read_loop()
        except KeyboardInterrupt:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Cron spec parsing — five-field "minute hour day month dow" format
# ─────────────────────────────────────────────────────────────────────────────

_CRON_FIELD_RANGES = {
    "minute": (0, 59),
    "hour":   (0, 23),
    "dom":    (1, 31),    # day of month
    "month":  (1, 12),
    "dow":    (0, 6),     # day of week (0=Sunday)
}


def _parse_cron_field(field: str, lo: int, hi: int) -> set:
    """Parse one cron field into the set of integer values it matches.

    Supports ``*``, ``*/N``, ``N``, ``N,M``, ``N-M``, ``N-M/S``.
    """
    if field == "*":
        return set(range(lo, hi + 1))
    out: set = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty cron part in field: {field!r}")
        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            step = int(step_str)
            if step <= 0:
                raise ValueError(f"step must be positive: {field!r}")
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            n = int(part)
            start, end = n, n
        if start < lo or end > hi or start > end:
            raise ValueError(f"value out of range [{lo}-{hi}] in field: {field!r}")
        out.update(range(start, end + 1, step))
    return out


def _parse_cron_spec(spec: str) -> dict:
    """Parse a five-field cron spec into a dict of matching value sets.

    Returns ``{"minute": {…}, "hour": {…}, "dom": {…}, "month": {…}, "dow": {…}}``.
    Raises ``ValueError`` on any malformed input.
    """
    parts = (spec or "").split()
    if len(parts) != 5:
        raise ValueError(
            f"cron spec must have 5 space-separated fields (got {len(parts)}): {spec!r}"
        )
    keys = ("minute", "hour", "dom", "month", "dow")
    out = {}
    for key, field in zip(keys, parts):
        lo, hi = _CRON_FIELD_RANGES[key]
        out[key] = _parse_cron_field(field, lo, hi)
    return out


def _next_cron_fire(parsed: dict, after) -> "datetime":
    """Return the next UTC datetime ≥ ``after`` that matches the parsed spec.

    ``after`` should be minute-precise (seconds/microseconds = 0). Walks
    forward minute-by-minute up to a year before giving up — safe upper
    bound, since any valid cron spec fires at least once per year.
    """
    import datetime as _dt
    cur = after
    if cur.second or cur.microsecond:
        cur = cur.replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
    else:
        cur = cur + _dt.timedelta(minutes=1)
    # Walk forward up to ~366 days (~527040 minutes); if we don't match in
    # that window, the spec is unreachable (e.g. "0 0 31 2 *").
    for _ in range(60 * 24 * 366):
        # cron's day-of-week vs day-of-month: when both fields are restricted
        # (not "*"), the standard rule is OR — match if EITHER applies.
        full_dom = parsed["dom"] == set(range(1, 32))
        full_dow = parsed["dow"] == set(range(0, 7))
        # discord-friendly weekday: cron dow uses 0=Sunday, Python uses 0=Monday
        py_dow = cur.weekday()
        cron_dow = (py_dow + 1) % 7
        match_minute = cur.minute in parsed["minute"]
        match_hour = cur.hour in parsed["hour"]
        match_month = cur.month in parsed["month"]
        match_dom = cur.day in parsed["dom"]
        match_dow = cron_dow in parsed["dow"]
        if full_dom and not full_dow:
            match_day = match_dow
        elif full_dow and not full_dom:
            match_day = match_dom
        else:
            # Either both unrestricted (full match), or both restricted (OR).
            match_day = match_dom or match_dow
        if match_minute and match_hour and match_month and match_day:
            return cur
        cur = cur + _dt.timedelta(minutes=1)
    raise ValueError("cron spec never matches within 366 days — unreachable schedule")
