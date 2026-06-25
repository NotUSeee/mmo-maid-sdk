"""
Testing utilities — test plugin handlers locally without Docker or the full platform.

Usage::

    from yourbot_sdk.testing import MockContext, make_event

    def test_ping_handler():
        ctx = MockContext()
        event = make_event("message_create", content="!ping", channel_id="123")
        my_handler(ctx, event)
        assert len(ctx.messages_sent) == 1
        assert ctx.messages_sent[0]["content"] == "Pong!"

    def test_kv_counter():
        ctx = MockContext()
        ctx.kv.increment("counter")
        ctx.kv.increment("counter")
        assert ctx.kv.get("counter") == 2
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ._exceptions import CapabilityError


class MockClock:
    """Clock abstraction for deterministic time-dependent tests (0.5.4+).

    By default tracks wall clock — existing tests that construct
    ``MockContext()`` without passing a clock see no behavioral change.

    For deterministic tests of TTLs, cooldowns, or scheduled paths,
    pass ``start=<float>`` to freeze time, then call ``advance(seconds)``
    to step forward::

        clock = MockClock(start=1000.0)
        ctx = MockContext(clock=clock)
        ctx.kv.set("k", "v", ttl_seconds=30)
        clock.advance(31)
        assert ctx.kv.get("k") is None  # expired

    The wall-clock default mode raises ``RuntimeError`` on ``advance()``
    so the failure mode is loud rather than silently doing nothing.
    """

    def __init__(self, start: Optional[float] = None) -> None:
        # start=None → wall-clock passthrough (backwards-compatible default)
        # start=<float> → frozen at that epoch; advance() steps it forward
        self._frozen_now: Optional[float] = start

    def now(self) -> float:
        if self._frozen_now is None:
            return time.time()
        return self._frozen_now

    def advance(self, seconds: float) -> None:
        if self._frozen_now is None:
            raise RuntimeError(
                "MockClock() defaults to wall-clock mode and cannot be "
                "advanced. Construct as MockClock(start=<epoch>) to enable "
                "advance()."
            )
        self._frozen_now += float(seconds)


class _MockSecrets:
    """In-memory plugin secrets store for testing.

    Mirrors the ctx.secrets API: get returns None for unset keys, set/delete
    operate per-server (the mock just uses a flat dict keyed by key name —
    real per-server scoping is exercised in integration tests).
    """

    def __init__(self) -> None:
        import threading
        self._lock = threading.Lock()
        self._store: Dict[str, str] = {}

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            return self._store.get(str(key))

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._store[str(key)] = str(value)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(str(key), None)


class _MockKv:
    """In-memory KV store for testing. Thread-safe."""

    def __init__(self, clock: Optional[MockClock] = None) -> None:
        import threading
        self._lock = threading.Lock()
        self._store: Dict[str, Any] = {}
        self._ttls: Dict[str, float] = {}  # key -> expiry timestamp
        self._clock = clock or MockClock()
        # Records every mutating call so tests (and `yourbot dev`) can report
        # what the handler wrote. Each entry: {"op", "key", ...}.
        self.writes: List[Dict[str, Any]] = []

    def _is_expired(self, key: str) -> bool:
        if key in self._ttls and self._ttls[key] < self._clock.now():
            del self._store[key]
            del self._ttls[key]
            return True
        return False

    def get(self, key: str) -> Any:
        with self._lock:
            if self._is_expired(key):
                return None
            return self._store.get(key)

    def set(self, key: str, value: Any, *, ttl_seconds: int = 0) -> None:
        with self._lock:
            self._store[key] = value
            if ttl_seconds > 0:
                self._ttls[key] = self._clock.now() + ttl_seconds
            elif key in self._ttls:
                del self._ttls[key]
            self.writes.append({"op": "set", "key": key, "value": value, "ttl_seconds": ttl_seconds})

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)
            self._ttls.pop(key, None)
            self.writes.append({"op": "delete", "key": key})

    def list(self, prefix: str = "", limit: int = 100) -> List[str]:
        with self._lock:
            keys = sorted(k for k in self._store if k.startswith(prefix) and not self._is_expired(k))
            return keys[:limit]

    def get_many(self, keys: List[str]) -> Dict[str, Any]:
        with self._lock:
            return {k: self._store[k] for k in keys if k in self._store and not self._is_expired(k)}

    def set_many(self, entries: Dict[str, Any]) -> None:
        with self._lock:
            self._store.update(entries)
            for k, v in entries.items():
                self.writes.append({"op": "set", "key": k, "value": v, "ttl_seconds": 0})

    def exists(self, key: str) -> bool:
        with self._lock:
            return key in self._store and not self._is_expired(key)

    def count(self, prefix: str = "") -> int:
        with self._lock:
            return len([k for k in self._store if k.startswith(prefix) and not self._is_expired(k)])

    def increment(self, key: str, amount: int = 1, *, path: str = "") -> Any:
        """Atomic increment. Mirrors the real ctx.kv.increment signature.

        With ``path`` empty, treats the value as a plain integer. With a
        ``path``, treats the value as a JSON object and increments that field.
        """
        with self._lock:
            if path:
                obj = self._store.get(key)
                if not isinstance(obj, dict):
                    obj = {}
                current = obj.get(path, 0)
                if not isinstance(current, (int, float)):
                    current = 0
                new_val = int(current) + amount
                obj[path] = new_val
                self._store[key] = obj
            else:
                current = self._store.get(key, 0)
                if not isinstance(current, (int, float)):
                    current = 0
                new_val = int(current) + amount
                self._store[key] = new_val
            self.writes.append({"op": "increment", "key": key, "path": path, "amount": amount})
            return new_val

    def decrement(self, key: str, amount: int = 1) -> int:
        return self.increment(key, amount=-amount)

    def list_values(self, prefix: str = "", limit: int = 100) -> Dict[str, Any]:
        with self._lock:
            result = {}
            for k in sorted(self._store):
                if k.startswith(prefix) and not self._is_expired(k):
                    result[k] = self._store[k]
                    if len(result) >= limit:
                        break
        return result


class _MockDiscord:
    """Records all Discord API calls for assertion."""

    def __init__(self) -> None:
        self.messages_sent: List[Dict[str, Any]] = []
        self.messages_edited: List[Dict[str, Any]] = []
        self.messages_deleted: List[Dict[str, Any]] = []
        self.messages_pinned: List[Dict[str, Any]] = []
        self.messages_unpinned: List[Dict[str, Any]] = []
        self.reactions_added: List[Dict[str, Any]] = []
        self.roles_added: List[Dict[str, Any]] = []
        self.roles_removed: List[Dict[str, Any]] = []
        self.members_banned: List[Dict[str, Any]] = []
        self.members_unbanned: List[Dict[str, Any]] = []
        self.members_kicked: List[Dict[str, Any]] = []
        self.members_timed_out: List[Dict[str, Any]] = []
        self.nicknames_set: List[Dict[str, Any]] = []
        self.channels_created: List[Dict[str, Any]] = []
        self.channels_edited: List[Dict[str, Any]] = []
        self.channels_deleted: List[Dict[str, Any]] = []
        self.threads_created: List[Dict[str, Any]] = []
        self.threads_edited: List[Dict[str, Any]] = []
        self.permissions_set: List[Dict[str, Any]] = []
        self.permissions_deleted: List[Dict[str, Any]] = []
        self.webhooks_created: List[Dict[str, Any]] = []
        self.webhooks_executed: List[Dict[str, Any]] = []
        self.webhooks_deleted: List[Dict[str, Any]] = []
        self._message_store: List[Dict[str, Any]] = []

    def set_messages(self, messages: List[Dict[str, Any]]) -> None:
        """Test helper: seed channel history that get_messages/iter_messages read.

        Each message needs at least an ``id``; before/after/limit are honoured so
        pagination (and ctx.discord.iter_messages) can be exercised offline.
        """
        self._message_store = sorted(messages, key=lambda m: int(m["id"]))

    def send_message(self, *, channel_id: str, content: str = "", embeds=None, components=None, files=None) -> Dict[str, Any]:
        msg = {"channel_id": channel_id, "content": content, "embeds": embeds, "components": components, "files": files, "message_id": str(len(self.messages_sent) + 1)}
        self.messages_sent.append(msg)
        return {"ok": True, "message_id": msg["message_id"], "channel_id": channel_id}

    def edit_message(self, *, channel_id: str, message_id: str, content=None, embeds=None, components=None) -> Dict[str, Any]:
        self.messages_edited.append({
            "channel_id": channel_id, "message_id": message_id,
            "content": content, "embeds": embeds, "components": components,
        })
        return {"ok": True}

    def delete_message(self, *, channel_id: str, message_id: str) -> None:
        self.messages_deleted.append({"channel_id": channel_id, "message_id": message_id})

    def bulk_delete_messages(self, *, channel_id: str, message_ids: List[str]) -> None:
        self.messages_deleted.extend([{"channel_id": channel_id, "message_id": m} for m in message_ids])

    def add_reaction(self, *, channel_id: str, message_id: str, emoji: str) -> None:
        self.reactions_added.append({"channel_id": channel_id, "message_id": message_id, "emoji": emoji})

    def get_member(self, *, user_id: str) -> Dict[str, Any]:
        return {"user_id": user_id, "username": f"user_{user_id}", "display_name": f"User {user_id}", "roles": [], "bot": False}

    def get_channel(self, *, channel_id: str) -> Dict[str, Any]:
        return {"id": channel_id, "name": f"channel-{channel_id}", "type": 0}

    def list_roles(self) -> List[Dict[str, Any]]:
        return [{"id": "1", "name": "@everyone", "color": 0, "position": 0}]

    def list_members(self, *, role_id=None, limit=100, after=None) -> List[Dict[str, Any]]:
        return []

    def search_members(self, query: str, *, limit=25) -> List[Dict[str, Any]]:
        return []

    def get_messages(self, *, channel_id: str, limit=50, before=None, after=None) -> List[Dict[str, Any]]:
        limit = min(max(1, int(limit)), 50)
        store = sorted(self._message_store, key=lambda m: int(m["id"]))  # ascending
        if after is not None:
            # Discord `after` returns the OLDEST `limit` messages newer than the id.
            cand = [m for m in store if int(m["id"]) > int(after)]
            page = cand[:limit]
        elif before is not None:
            # `before` returns the NEWEST `limit` messages older than the id.
            cand = [m for m in store if int(m["id"]) < int(before)]
            page = cand[-limit:]
        else:
            page = store[-limit:]  # newest `limit` overall
        # Discord returns messages newest-first.
        return sorted(page, key=lambda m: int(m["id"]), reverse=True)

    def iter_messages(self, *, channel_id: str, batch_size: int = 50, before=None, after=None):
        """Mirror of ctx.discord.iter_messages — pages over set_messages() data."""
        batch_size = min(max(1, int(batch_size)), 50)
        forward = after is not None
        cursor = str(after) if forward else (str(before) if before is not None else None)
        while True:
            kwargs: Dict[str, Any] = {"channel_id": channel_id, "limit": batch_size}
            if cursor is not None:
                kwargs["after" if forward else "before"] = cursor
            batch = self.get_messages(**kwargs)
            if not batch:
                return
            for msg in batch:
                yield msg
            ids = [int(m["id"]) for m in batch if str(m.get("id", "")).isdigit()]
            if not ids:
                return
            next_cursor = str(max(ids)) if forward else str(min(ids))
            if next_cursor == cursor or len(batch) < batch_size:
                return
            cursor = next_cursor

    def add_role(self, *, user_id: str, role_id: str, reason: str = "") -> None:
        self.roles_added.append({"user_id": user_id, "role_id": role_id, "reason": reason})

    def remove_role(self, *, user_id: str, role_id: str, reason: str = "") -> None:
        self.roles_removed.append({"user_id": user_id, "role_id": role_id, "reason": reason})

    def ban_member(self, *, user_id: str, reason: str = "", delete_message_seconds: int = 0) -> None:
        self.members_banned.append({"user_id": user_id, "reason": reason})

    def unban_member(self, *, user_id: str) -> None:
        self.members_unbanned.append({"user_id": user_id})

    def kick_member(self, *, user_id: str, reason: str = "") -> None:
        self.members_kicked.append({"user_id": user_id, "reason": reason})

    def timeout_member(self, *, user_id: str, duration_seconds: int, reason: str = "") -> None:
        self.members_timed_out.append({"user_id": user_id, "duration_seconds": duration_seconds, "reason": reason})

    def create_channel(self, *, name: str, channel_type: int = 0, **kwargs) -> Dict[str, Any]:
        ch = {"id": str(len(self.channels_created) + 100), "name": name, "type": channel_type, **kwargs}
        self.channels_created.append(ch)
        return ch

    def edit_channel(self, *, channel_id: str, **kwargs) -> None:
        self.channels_edited.append({"channel_id": channel_id, **kwargs})

    def delete_channel(self, *, channel_id: str) -> None:
        self.channels_deleted.append({"channel_id": channel_id})

    # Bulk operations
    def add_role_bulk(self, *, user_ids: List[str], role_id: str, reason: str = "") -> Dict[str, Any]:
        for uid in user_ids:
            self.roles_added.append({"user_id": uid, "role_id": role_id, "reason": reason})
        return {"success": len(user_ids), "failed": 0, "errors": []}

    def remove_role_bulk(self, *, user_ids: List[str], role_id: str, reason: str = "") -> Dict[str, Any]:
        for uid in user_ids:
            self.roles_removed.append({"user_id": uid, "role_id": role_id, "reason": reason})
        return {"success": len(user_ids), "failed": 0, "errors": []}

    def timeout_bulk(self, *, user_ids: List[str], duration_seconds: int, reason: str = "") -> Dict[str, Any]:
        for uid in user_ids:
            self.members_timed_out.append({"user_id": uid, "duration_seconds": duration_seconds, "reason": reason})
        return {"success": len(user_ids), "failed": 0, "errors": []}

    def kick_bulk(self, *, user_ids: List[str], reason: str = "") -> Dict[str, Any]:
        for uid in user_ids:
            self.members_kicked.append({"user_id": uid, "reason": reason})
        return {"success": len(user_ids), "failed": 0, "errors": []}

    # Permission overwrites
    def set_channel_permissions(self, *, channel_id: str, target_id: str, allow: str = "0", deny: str = "0", target_type: int = 0) -> None:
        self.permissions_set.append({"channel_id": channel_id, "target_id": target_id, "allow": allow, "deny": deny, "target_type": target_type})

    def delete_channel_permission(self, *, channel_id: str, target_id: str) -> None:
        self.permissions_deleted.append({"channel_id": channel_id, "target_id": target_id})

    # Threads
    def create_thread(self, *, channel_id: str, name: str, thread_type: int = 11, auto_archive_duration: int = 1440) -> Dict[str, Any]:
        t = {"id": str(len(self.threads_created) + 200), "name": name, "type": thread_type, "archived": False}
        self.threads_created.append(t)
        return t

    def edit_thread(self, *, thread_id: str, **kwargs) -> None:
        self.threads_edited.append({"thread_id": thread_id, **kwargs})

    # Pins
    def pin_message(self, *, channel_id: str, message_id: str) -> None:
        self.messages_pinned.append({"channel_id": channel_id, "message_id": message_id})

    def unpin_message(self, *, channel_id: str, message_id: str) -> None:
        self.messages_unpinned.append({"channel_id": channel_id, "message_id": message_id})

    # Guild info
    def get_guild(self) -> Dict[str, Any]:
        return {"id": "999", "name": "Test Server", "icon": None, "member_count": 100, "premium_tier": 0, "features": [], "owner_id": "1"}

    def list_channels(self) -> List[Dict[str, Any]]:
        return [{"id": "1", "name": "general", "type": 0, "parent_id": None, "position": 0}]

    # Nickname
    def set_nickname(self, *, user_id: str, nickname: Optional[str] = None) -> None:
        self.nicknames_set.append({"user_id": user_id, "nickname": nickname})

    # Webhooks
    def create_webhook(self, *, channel_id: str, name: str) -> Dict[str, Any]:
        wh = {"id": str(len(self.webhooks_created) + 300), "token": "mock_token", "channel_id": channel_id, "name": name}
        self.webhooks_created.append(wh)
        return wh

    def execute_webhook(self, *, webhook_id: str, webhook_token: str, content: str = "", embeds=None, username=None, avatar_url=None) -> Dict[str, Any]:
        self.webhooks_executed.append({"webhook_id": webhook_id, "content": content, "embeds": embeds, "username": username})
        return {"message_id": str(len(self.webhooks_executed))}

    def delete_webhook(self, *, webhook_id: str) -> None:
        self.webhooks_deleted.append({"webhook_id": webhook_id})


class _MockHttp:
    """Records HTTP calls and returns configurable mock responses."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self._mock_responses: Dict[str, Dict[str, Any]] = {}  # url pattern -> response

    def mock_response(self, url_contains: str, *, status: int = 200, body: str = "", headers: Optional[Dict] = None) -> None:
        """Configure a mock response for requests matching url_contains."""
        self._mock_responses[url_contains] = {"status": status, "body_bytes": body, "headers": headers or {}, "truncated": False}

    def request(self, method: str, url: str, headers=None, body=None, params=None,
                secret_auth=None, auth=None) -> Dict[str, Any]:
        self.requests.append({"method": method, "url": url, "headers": headers, "body": body,
                              "params": params, "secret_auth": secret_auth, "auth": auth})
        for pattern, resp in self._mock_responses.items():
            if pattern in url:
                return resp
        return {"status": 200, "body_bytes": "", "headers": {}, "truncated": False}

    def get(self, url: str, headers=None, params=None, secret_auth=None, auth=None) -> Dict[str, Any]:
        return self.request("GET", url, headers=headers, params=params, secret_auth=secret_auth, auth=auth)

    def post(self, url: str, body: str = "", headers=None, params=None, secret_auth=None, auth=None) -> Dict[str, Any]:
        return self.request("POST", url, headers=headers, body=body, params=params, secret_auth=secret_auth, auth=auth)


class _MockWs:
    """Records ctx.ws.* calls for assertion in tests.

    No real socket — ``ensure`` returns a connecting stub. To simulate inbound
    frames in a test, dispatch an event to your ``@on_ws_message`` handler
    directly (the harness does not run the broker).
    """

    def __init__(self) -> None:
        self.ensured: List[Dict[str, Any]] = []
        self.sent: List[Dict[str, Any]] = []
        self.closed: List[str] = []
        self.allowed_hosts: List[str] = []
        self.revoked_hosts: List[str] = []

    def ensure(self, name: str, url: str, *, secret_auth=None, auth=None, subscribe=None, binary: bool = False) -> Dict[str, Any]:
        self.ensured.append({"name": name, "url": url, "secret_auth": secret_auth,
                             "auth": auth, "subscribe": list(subscribe or []), "binary": binary})
        return {"conn_id": f"mock-{name}", "name": name, "state": "connecting"}

    def send(self, name: str, data: Any) -> Dict[str, Any]:
        self.sent.append({"name": name, "data": data})
        return {"ok": True}

    def close(self, name: str) -> Dict[str, Any]:
        self.closed.append(name)
        return {"ok": True}

    def allow_host(self, host: str) -> Dict[str, Any]:
        self.allowed_hosts.append(host)
        return {"ok": True, "host": host}

    def revoke_host(self, host: str) -> Dict[str, Any]:
        self.revoked_hosts.append(host)
        return {"ok": True, "host": host}


class _MockInteraction:
    """Records interaction responses for assertion."""

    def __init__(self) -> None:
        self.responses: List[Dict[str, Any]] = []
        self.defers: List[Dict[str, Any]] = []
        self.followups: List[Dict[str, Any]] = []
        self.modals_sent: List[Dict[str, Any]] = []

    def respond(self, content: str = "", embeds=None, components=None, ephemeral: bool = False, allowed_mentions=None, update_message: bool = False) -> None:
        self.responses.append({
            "content": content, "embeds": embeds, "components": components,
            "ephemeral": ephemeral, "allowed_mentions": allowed_mentions,
            "update_message": update_message,
        })

    def defer(self, ephemeral: bool = False) -> None:
        self.defers.append({"ephemeral": ephemeral})

    def followup(self, content: str = "", embeds=None, components=None, ephemeral: bool = False, allowed_mentions=None) -> Dict[str, Any]:
        msg_id = str(len(self.followups) + 1)
        self.followups.append({
            "content": content, "embeds": embeds, "components": components,
            "ephemeral": ephemeral, "allowed_mentions": allowed_mentions,
            "message_id": msg_id,
        })
        return {"message_id": msg_id, "channel_id": ""}

    def send_modal(self, title: str, custom_id: str, fields=None) -> None:
        self.modals_sent.append({"title": title, "custom_id": custom_id, "fields": fields})


class _MockMetrics:
    """In-memory metrics recorder matching _MetricsApi interface."""

    def __init__(self) -> None:
        self.recorded: List[Dict[str, Any]] = []

    def record(self, metric: str, value: float = 1.0, tags: Optional[Dict[str, str]] = None) -> None:
        self.recorded.append({"metric": metric, "value": value, "tags": tags or {}})

    def query(self, metric: str, *, period: str = "7d", group_by: Optional[str] = None, aggregate: str = "sum") -> Dict[str, Any]:
        return {"labels": [], "series": [], "total": 0}

    def total(self, metric: str, *, period: str = "30d") -> float:
        return 0.0


class _MockSql:
    """In-memory SQL recorder matching _SqlApi interface."""

    def __init__(self) -> None:
        self.executed: List[Dict[str, Any]] = []

    def execute(self, sql: str, params: Optional[list] = None) -> int:
        self.executed.append({"sql": sql, "params": params})
        return 0

    def query(self, sql: str, params: Optional[list] = None, limit: int = 1000) -> List[Dict[str, Any]]:
        self.executed.append({"sql": sql, "params": params, "limit": limit})
        return []

    def query_one(self, sql: str, params: Optional[list] = None) -> Optional[Dict[str, Any]]:
        self.executed.append({"sql": sql, "params": params})
        return None

    def scalar(self, sql: str, params: Optional[list] = None) -> Any:
        self.executed.append({"sql": sql, "params": params})
        return None


class _MockEphemeral:
    """In-memory ephemeral state matching _EphemeralApi interface."""

    def __init__(self, clock: Optional[MockClock] = None) -> None:
        self._counter_hits: Dict[str, List[float]] = {}  # key -> hit timestamps
        self._cooldowns: Dict[str, float] = {}  # key -> expiry timestamp
        self._seen: Dict[str, float] = {}  # key -> expiry timestamp
        self._flags: Dict[str, float] = {}  # key -> expiry timestamp
        self._clock = clock or MockClock()

    def counter(self, key: str, window_seconds: int = 60) -> int:
        """Sliding-window counter, matching the real ctx.ephemeral.counter.

        Returns the number of hits within the trailing ``window_seconds``. Hits
        older than the window are dropped, so with a MockClock you can advance
        past the window and watch the count reset — essential for accurate
        rate-limit tests (the old mock was monotonic and never reset).
        """
        now = self._clock.now()
        hits = [t for t in self._counter_hits.get(key, []) if t > now - window_seconds]
        hits.append(now)
        self._counter_hits[key] = hits
        return len(hits)

    def cooldown_set(self, key: str, ttl_seconds: int = 60) -> None:
        self._cooldowns[key] = self._clock.now() + ttl_seconds

    def cooldown_check(self, key: str) -> Dict[str, Any]:
        expiry = self._cooldowns.get(key, 0)
        remaining = max(0, expiry - self._clock.now())
        return {"active": remaining > 0, "remaining_seconds": remaining}

    def dedup(self, key: str, ttl_seconds: int = 3600) -> bool:
        now = self._clock.now()
        if key in self._seen and self._seen[key] > now:
            return False  # already seen
        self._seen[key] = now + ttl_seconds
        return True  # first time

    def flag_set(self, key: str, ttl_seconds: int = 3600) -> None:
        self._flags[key] = self._clock.now() + ttl_seconds

    def flag_check(self, key: str) -> bool:
        return self._flags.get(key, 0) > self._clock.now()


# Default capability set for MockContext() when no list is passed — grants
# everything so existing tests don't need to enumerate capabilities.
_DEFAULT_MOCK_CAPS = [
    "storage:kv", "storage:sql", "storage:secrets",
    "discord:send_message", "discord:edit_message",
    "discord:delete_message", "discord:add_reaction", "discord:read",
    "discord:manage_channels", "discord:moderate_members",
    "discord:ban_members", "discord:kick_members", "discord:manage_roles",
    "discord:manage_webhooks", "interaction:respond", "proxy:http",
]

# Method → required capability per Context sub-API. Mirrors §18 of PLUGIN_DEV.md
# and the real host RPC capability gates, so a handler that works under the mock
# also has the capabilities it needs in production.
_KV_CAPS = {m: "storage:kv" for m in (
    "get", "set", "delete", "list", "get_many", "set_many",
    "exists", "count", "increment", "decrement", "list_values",
)}
_SECRETS_CAPS = {m: "storage:secrets" for m in ("get", "set", "delete")}
_SQL_CAPS = {m: "storage:sql" for m in ("execute", "query", "query_one", "scalar")}
_HTTP_CAPS = {m: "proxy:http" for m in ("get", "post", "request")}
_WS_CAPS = {m: "proxy:websocket" for m in ("ensure", "send", "close", "allow_host", "revoke_host")}
_INTERACTION_CAPS = {m: "interaction:respond" for m in ("respond", "defer", "followup", "send_modal")}
_DISCORD_CAPS = {
    "send_message": "discord:send_message",
    "pin_message": "discord:send_message",
    "unpin_message": "discord:send_message",
    "edit_message": "discord:edit_message",
    "delete_message": "discord:delete_message",
    "bulk_delete_messages": "discord:delete_message",
    "add_reaction": "discord:add_reaction",
    "get_member": "discord:read",
    "get_channel": "discord:read",
    "list_roles": "discord:read",
    "list_members": "discord:read",
    "search_members": "discord:read",
    "get_messages": "discord:read",
    "iter_messages": "discord:read",
    "get_guild": "discord:read",
    "list_channels": "discord:read",
    "create_channel": "discord:manage_channels",
    "edit_channel": "discord:manage_channels",
    "delete_channel": "discord:manage_channels",
    "set_channel_permissions": "discord:manage_channels",
    "delete_channel_permission": "discord:manage_channels",
    "create_thread": "discord:manage_channels",
    "edit_thread": "discord:manage_channels",
    "create_webhook": "discord:manage_webhooks",
    "execute_webhook": "discord:manage_webhooks",
    "delete_webhook": "discord:manage_webhooks",
    "ban_member": "discord:ban_members",
    "unban_member": "discord:ban_members",
    "kick_member": "discord:kick_members",
    "kick_bulk": "discord:kick_members",
    "add_role": "discord:manage_roles",
    "remove_role": "discord:manage_roles",
    "add_role_bulk": "discord:manage_roles",
    "remove_role_bulk": "discord:manage_roles",
    "timeout_member": "discord:moderate_members",
    "timeout_bulk": "discord:moderate_members",
    "set_nickname": "discord:moderate_members",
}


class _CapGuard:
    """Wraps a mock sub-API so gated method calls raise ``CapabilityError`` when
    the MockContext was created without that capability — mirroring the real host.

    Non-gated methods (``ctx.metrics``, ``ctx.ephemeral``, ``ctx.log``) are never
    wrapped, and non-method attributes (e.g. ``discord.messages_sent``) pass
    straight through so assertions keep working.
    """

    def __init__(self, target: Any, caps: Dict[str, str], has_cap) -> None:
        self._target = target
        self._caps = caps
        self._has_cap = has_cap

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._target, name)
        cap = self._caps.get(name)
        if cap is None or not callable(attr):
            return attr
        has_cap = self._has_cap

        def _wrapped(*args, **kwargs):
            if not has_cap(cap):
                raise CapabilityError(
                    f"capability {cap} required for {name}() — grant it with "
                    f"MockContext(capabilities=[{cap!r}, ...]) or add it to your manifest"
                )
            return attr(*args, **kwargs)

        return _wrapped


class MockContext:
    """Drop-in replacement for Context that records all calls in-memory.

    Usage::

        ctx = MockContext(server_id="123", plugin_id="my_plugin")
        my_handler(ctx, event)
        assert ctx.messages_sent[0]["content"] == "Hello!"

    By default the mock enforces capabilities: calling a gated method (e.g.
    ``ctx.discord.send_message``) without the matching capability raises
    ``CapabilityError``, just like production. Pass ``strict_capabilities=False``
    to disable this (legacy "everything allowed" behaviour).
    """

    def __init__(
        self,
        server_id: str = "999999999",
        plugin_id: str = "test_plugin",
        version: str = "1.0.0",
        capabilities: Optional[List[str]] = None,
        clock: Optional[MockClock] = None,
        strict_capabilities: bool = True,
    ) -> None:
        self.server_id = server_id
        self.plugin_id = plugin_id
        self.version = version
        self.strict_capabilities = strict_capabilities
        # None → grant all capabilities (back-compat default). An explicit list
        # (including the empty list) is honoured verbatim, so capabilities=[]
        # means "no capabilities" — useful for testing capability gating.
        self.capabilities = set(_DEFAULT_MOCK_CAPS if capabilities is None else capabilities)
        # Synthetic clock for deterministic TTL/cooldown tests; defaults to
        # wall-clock passthrough so existing tests are unaffected.
        self.clock = clock or MockClock()
        self.kv = _MockKv(clock=self.clock)
        self.discord = _MockDiscord()
        self.http = _MockHttp()
        self.ws = _MockWs()
        self.interaction = _MockInteraction()
        self.metrics = _MockMetrics()
        self.sql = _MockSql()
        self.ephemeral = _MockEphemeral(clock=self.clock)
        self.secrets = _MockSecrets()
        self.log_entries: List[Dict[str, Any]] = []
        self._current_interaction_id: Optional[str] = None

        # Enforce capabilities by default so a handler that passes the mock has
        # the capabilities it needs in production. ctx.metrics / ctx.ephemeral /
        # ctx.log are ungated and stay unwrapped.
        if strict_capabilities:
            self.kv = _CapGuard(self.kv, _KV_CAPS, self.has_capability)
            self.discord = _CapGuard(self.discord, _DISCORD_CAPS, self.has_capability)
            self.http = _CapGuard(self.http, _HTTP_CAPS, self.has_capability)
            self.ws = _CapGuard(self.ws, _WS_CAPS, self.has_capability)
            self.interaction = _CapGuard(self.interaction, _INTERACTION_CAPS, self.has_capability)
            self.sql = _CapGuard(self.sql, _SQL_CAPS, self.has_capability)
            self.secrets = _CapGuard(self.secrets, _SECRETS_CAPS, self.has_capability)

    @property
    def request_id(self) -> str:
        """Mirror of Context.request_id (added in SDK 0.5.3)."""
        return self._current_interaction_id or ""

    def log(self, message: str, level: str = "info", tags: Optional[List[str]] = None, **extra) -> None:
        self.log_entries.append({"message": message, "level": level, "tags": tags or [], **extra})

    def has_capability(self, cap: str) -> bool:
        return cap in self.capabilities

    # Convenience accessors
    @property
    def messages_sent(self) -> List[Dict[str, Any]]:
        return self.discord.messages_sent

    @property
    def messages_edited(self) -> List[Dict[str, Any]]:
        return self.discord.messages_edited

    @property
    def messages_deleted(self) -> List[Dict[str, Any]]:
        return self.discord.messages_deleted

    @property
    def roles_added(self) -> List[Dict[str, Any]]:
        return self.discord.roles_added

    @property
    def roles_removed(self) -> List[Dict[str, Any]]:
        return self.discord.roles_removed

    @property
    def members_banned(self) -> List[Dict[str, Any]]:
        return self.discord.members_banned

    @property
    def members_kicked(self) -> List[Dict[str, Any]]:
        return self.discord.members_kicked

    @property
    def modals_sent(self) -> List[Dict[str, Any]]:
        return self.interaction.modals_sent

    @property
    def kv_writes(self) -> List[Dict[str, Any]]:
        """Every mutating ctx.kv call (set/delete/increment/...) the handler made."""
        return self.kv.writes

    @property
    def log_lines(self) -> List[str]:
        """The message text of every ctx.log() call, in order."""
        return [str(e.get("message", "")) for e in self.log_entries]


def make_event(event_type: str, **kwargs) -> dict:
    """Create a mock event dict with sensible defaults.

    Examples::

        event = make_event("message_create", content="!ping", channel_id="123")
        event = make_event("member_join", user_id="456")
        event = make_event("interaction_create", command_name="ban", interaction_type=2)
    """
    defaults = {
        "message_create": {
            "message_id": "100", "channel_id": "1", "guild_id": "999",
            "author_id": "200", "author_username": "testuser", "author_bot": False,
            "content": "", "timestamp": "2026-01-01T00:00:00Z",
        },
        "message_edit": {
            "message_id": "100", "channel_id": "1", "guild_id": "999",
            "content": "",
        },
        "message_delete": {
            "message_id": "100", "channel_id": "1", "guild_id": "999",
        },
        "member_join": {
            "user_id": "200", "username": "newuser", "guild_id": "999",
        },
        "member_leave": {
            "user_id": "200", "username": "leftuser", "guild_id": "999",
        },
        "member_update": {
            "user_id": "200", "guild_id": "999", "roles": [],
        },
        "reaction_add": {
            "message_id": "100", "channel_id": "1", "user_id": "200",
            "emoji": "👍", "guild_id": "999",
        },
        "reaction_remove": {
            "message_id": "100", "channel_id": "1", "user_id": "200",
            "emoji": "👍", "guild_id": "999",
        },
        "voice_state_update": {
            "user_id": "200", "channel_id": "1", "guild_id": "999",
            "self_mute": False, "self_deaf": False,
        },
        "interaction_create": {
            "interaction_id": "300", "interaction_type": 2, "guild_id": "999",
            "channel_id": "1", "user_id": "200", "command_name": "",
            "custom_id": "", "modal_values": {},
        },
        "channel_create": {"id": "1", "name": "new-channel", "type": 0, "guild_id": "999"},
        "channel_delete": {"id": "1", "name": "deleted-channel", "guild_id": "999"},
        "channel_update": {"id": "1", "name": "updated-channel", "guild_id": "999"},
        "role_create": {"id": "1", "name": "new-role", "guild_id": "999"},
        "role_delete": {"id": "1", "name": "deleted-role", "guild_id": "999"},
        "role_update": {"id": "1", "name": "updated-role", "guild_id": "999"},
    }

    base = dict(defaults.get(event_type, {}))
    base.update(kwargs)
    return base
