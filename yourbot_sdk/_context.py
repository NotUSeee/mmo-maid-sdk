"""
Context — the object your event handlers receive.

Every handler gets a Context that exposes:
  - ctx.log(message)                print to plugin audit log
  - ctx.kv.get/set/delete           per-server key-value storage
  - ctx.kv.list/get_many/set_many   batch KV operations
  - ctx.kv.increment/decrement      atomic counters
  - ctx.kv.list_values              get key-value pairs by prefix
  - ctx.discord.send_message        send Discord messages (with embeds)
  - ctx.discord.edit_message        edit a message by ID
  - ctx.discord.delete_message      delete a message by ID
  - ctx.discord.add_reaction        add a reaction emoji to a message
  - ctx.discord.get_member          look up a server member
  - ctx.discord.get_channel         look up a channel
  - ctx.discord.list_roles          list all roles in the server
  - ctx.discord.list_members        paginated member listing
  - ctx.discord.search_members      search members by name
  - ctx.discord.get_messages        fetch channel message history
  - ctx.discord.create_channel       create a channel (voice/text/category)
  - ctx.discord.edit_channel         edit a channel's properties
  - ctx.discord.delete_channel       delete a channel
  - ctx.discord.timeout_member       timeout a member
  - ctx.discord.ban_member           ban a member
  - ctx.discord.kick_member          kick a member
  - ctx.discord.add_role             add a role to a member
  - ctx.discord.remove_role          remove a role from a member
  - ctx.discord.*_bulk               bulk operations (add_role_bulk, etc.)
  - ctx.http.get/post/request        make HTTP requests (approved domains only)
  - ctx.interaction.respond           respond to a slash command or button
  - ctx.interaction.defer             acknowledge with "thinking..."
  - ctx.interaction.followup          send follow-up messages
  - ctx.interaction.send_modal        show a modal dialog
  - ctx.metrics.record                record a data point
  - ctx.metrics.query                 query aggregated metrics
  - ctx.metrics.total                 get a single aggregate total
  - ctx.sql.execute                   run DDL/DML statements
  - ctx.sql.query                     run SELECT queries
  - ctx.sql.query_one                 get single row
  - ctx.sql.scalar                    get single value
  - ctx.server_id                     the Discord server ID this install is for
  - ctx.plugin_id                     your plugin's ID
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ._transport import Transport
    from .responses import Member, Role, Channel, Guild, Message


class _SecretsApi:
    """ctx.secrets — encrypted per-plugin secrets (requires storage:secrets capability).

    Plugins use this to read sensitive values the dev configured in the dev
    portal (API keys, signing secrets, etc.) without committing them to git.
    Values are encrypted at rest with AES-GCM via the platform's master key.

    Resolution order:
      1. ``ctx.secrets.get("FOO")`` first checks for a per-server override at
         the current server (set by the plugin itself via ``ctx.secrets.set``
         or by an admin in a future per-server UI).
      2. Falls back to the dev-level default set by the plugin author in the
         dev portal Settings → Plugin secrets page.
      3. Returns None if neither is set.

    Per-server values are scoped to ``ctx.server_id``. They never bleed
    across servers, never leak into KV, never appear in plugin stdout or
    logs.
    """

    def __init__(self, transport: "Transport") -> None:
        self._t = transport

    def get(self, key: str) -> Optional[str]:
        """Read a secret. Returns None if not set.

        Args:
            key: 1-64 chars, letters/digits/underscore/hyphen/dot only.

        Returns:
            Plaintext secret, or None if not set at either scope.
        """
        result = self._t.call("secrets.get", {"key": str(key)})
        if isinstance(result, dict) and "value" in result:
            v = result["value"]
            return str(v) if v is not None else None
        return None

    def set(self, key: str, value: str) -> None:
        """Store a per-server secret.

        Per-server values override the dev-level default for the current
        ``ctx.server_id`` only. To clear a value, call ``ctx.secrets.delete``.

        Args:
            key: 1-64 chars (alphanumeric + _.-).
            value: 1-4096 chars (UTF-8).
        """
        self._t.call("secrets.set", {"key": str(key), "value": str(value)})

    def delete(self, key: str) -> None:
        """Remove a per-server secret. No-op if not set.

        Note: this only deletes the per-server override. The dev-level
        default (set in the dashboard) is unaffected.
        """
        self._t.call("secrets.del", {"key": str(key)})


class _KvApi:
    """ctx.kv — key-value storage (requires storage:kv capability)."""

    def __init__(self, transport: "Transport") -> None:
        self._t = transport

    def get(self, key: str) -> Any:
        """Get a value by key.  Returns the stored value, or None if not set."""
        result = self._t.call("kv.get", {"key": str(key)})
        if isinstance(result, dict) and "value" in result:
            v = result["value"]
            if isinstance(v, dict) and "value_json" in v:
                return v["value_json"]
            return v
        return None

    def set(self, key: str, value: Any, *, ttl_seconds: int = 0) -> None:
        """Store a value.  Value must be JSON-serialisable.

        Args:
            key: Storage key.
            value: JSON-serialisable value.
            ttl_seconds: Auto-expire after this many seconds (0 = no expiry).
        """
        params = {"key": str(key), "value": value}
        if ttl_seconds > 0:
            params["ttl_seconds"] = int(ttl_seconds)
        self._t.call("kv.put", params)

    def delete(self, key: str) -> None:
        """Delete a key (no-op if it doesn't exist)."""
        self._t.call("kv.del", {"key": str(key)})

    def increment(self, key: str, *, path: str = "", amount: int = 1) -> Any:
        """Atomic increment in a single RPC round-trip.

        Much faster than get() + modify + set() for counters.

        Args:
            key: Storage key.
            path: Dot-separated path into a JSON object (e.g. "total").
                  Empty string = treat value as a plain integer.
            amount: How much to add (default 1).

        Returns:
            The new value after increment.
        """
        result = self._t.call("kv.increment", {
            "key": str(key), "path": str(path), "amount": int(amount),
        })
        if isinstance(result, dict):
            return result.get("value")
        return result

    def list(self, prefix: str = "", limit: int = 100) -> List[str]:
        """List stored key names, optionally filtered by prefix.  Up to 1000 results."""
        result = self._t.call("kv.list", {"prefix": prefix, "limit": limit})
        if isinstance(result, dict):
            return result.get("keys") or []
        return []

    def get_many(self, keys: List[str]) -> Dict[str, Any]:
        """Batch get up to 50 keys.  Returns {key: value}; missing keys omitted."""
        result = self._t.call("kv.mget", {"keys": keys})
        if isinstance(result, dict):
            return result.get("values") or {}
        return {}

    def exists(self, key: str) -> bool:
        """Check if a key exists without loading its value."""
        result = self._t.call("kv.get", {"key": str(key)})
        if isinstance(result, dict) and "value" in result:
            return result["value"] is not None
        return False

    def count(self, prefix: str = "") -> int:
        """Count keys matching a prefix. Useful for pagination."""
        result = self._t.call("kv.list", {"prefix": prefix, "limit": 0, "count_only": True})
        if isinstance(result, dict):
            return result.get("count", 0)
        return 0

    def set_many(self, entries: Dict[str, Any]) -> None:
        """Batch set up to 25 key-value pairs.  All values must be JSON-serialisable."""
        self._t.call("kv.mput", {"entries": entries})

    def decrement(self, key: str, amount: int = 1) -> int:
        """Atomically decrement a numeric value. Creates with `-amount` if key doesn't exist.

        Returns the new value after decrementing.  Requires capability: storage:kv
        """
        result = self._t.call("kv.decr", {"key": str(key), "amount": int(amount)})
        if isinstance(result, dict):
            return result.get("value", 0)
        return 0

    def list_values(self, prefix: str = "", limit: int = 100) -> Dict[str, Any]:
        """List stored key-value pairs matching a prefix. Returns {key: value} dict.

        Up to 100 results.  Avoids the N+1 problem of list() + get() calls.
        Requires capability: storage:kv
        """
        result = self._t.call("kv.list_values", {"prefix": prefix, "limit": limit})
        if isinstance(result, dict):
            return result.get("values") or {}
        return {}


class _DiscordApi:
    """ctx.discord — Discord actions (require specific capabilities)."""

    def __init__(self, transport: "Transport") -> None:
        self._t = transport

    def send_message(
        self,
        *,
        channel_id: str,
        content: str = "",
        embeds: Optional[List[Dict[str, Any]]] = None,
        components: Optional[list] = None,
        files: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """Send a message.  Returns dict with 'message_id' and 'channel_id'.

        Requires capability: discord:send_message

        At least one of content, embeds, components, or files must be provided.
        Components are ActionRow objects or raw dicts matching Discord's format.

        Args:
            files: List of file dicts, each with:
                - ``filename``: Display name (e.g. ``"chart.png"``)
                - ``data_b64``: Base64-encoded file content
                Max 3 files, 8 MB total.

        Example::

            import base64
            with open("chart.png", "rb") as f:
                data = base64.b64encode(f.read()).decode()
            ctx.discord.send_message(
                channel_id="123",
                content="Here's the chart:",
                files=[{"filename": "chart.png", "data_b64": data}],
            )
        """
        params: Dict[str, Any] = {"channel_id": str(channel_id)}
        if content:
            params["content"] = str(content)
        if embeds:
            params["embeds"] = embeds
        if components:
            params["components"] = [c.to_dict() if hasattr(c, "to_dict") else c for c in components]
        if files:
            params["files"] = files[:3]
        result = self._t.call("discord.send_message", params)
        return result if isinstance(result, dict) else {}

    def edit_message(
        self,
        *,
        channel_id: str,
        message_id: str,
        content: Optional[str] = None,
        embeds: Optional[List[Dict[str, Any]]] = None,
        components: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Edit an existing message.  Only bot-owned messages can be edited.

        Requires capability: discord:edit_message

        Pass ``None`` to leave a field unchanged. Pass an empty list to
        clear that field:
          - ``content=""`` — clear text
          - ``embeds=[]`` — clear all embeds
          - ``components=[]`` — clear all buttons / select menus
        """
        params: Dict[str, Any] = {
            "channel_id": str(channel_id),
            "message_id": str(message_id),
        }
        if content is not None:
            params["content"] = str(content)
        if embeds is not None:
            params["embeds"] = embeds
        if components is not None:
            params["components"] = [
                c.to_dict() if hasattr(c, "to_dict") else c for c in components
            ]
        result = self._t.call("discord.edit_message", params)
        return result if isinstance(result, dict) else {}

    def delete_message(self, *, channel_id: str, message_id: str) -> None:
        """Delete a message.  Requires capability: discord:delete_message"""
        self._t.call("discord.delete_message", {
            "channel_id": str(channel_id),
            "message_id": str(message_id),
        })

    def bulk_delete_messages(self, *, channel_id: str, message_ids: List[str]) -> None:
        """Bulk delete 2-100 messages (must be < 14 days old). Requires capability: discord:delete_message"""
        self._t.call("discord.bulk_delete_messages", {
            "channel_id": str(channel_id),
            "message_ids": [str(m) for m in message_ids[:100]],
        })

    def add_reaction(self, *, channel_id: str, message_id: str, emoji: str) -> None:
        """Add a reaction.  emoji is a unicode char or 'name:id' for custom.

        Requires capability: discord:add_reaction
        """
        self._t.call("discord.add_reaction", {
            "channel_id": str(channel_id),
            "message_id": str(message_id),
            "emoji": str(emoji),
        })

    def get_member(self, *, user_id: str) -> "Member":
        """Look up a server member.  Requires capability: discord:read

        Returns: user_id, username, display_name, nick, avatar, roles, joined_at, bot.
        """
        result = self._t.call("discord.get_member", {"user_id": str(user_id)})
        if isinstance(result, dict):
            return result.get("member") or {}
        return {}

    def get_channel(self, *, channel_id: str) -> "Channel":
        """Look up a channel.  Requires capability: discord:read

        Returns: id, name, type, topic, parent_id, position, nsfw.
        """
        result = self._t.call("discord.get_channel", {"channel_id": str(channel_id)})
        if isinstance(result, dict):
            return result.get("channel") or {}
        return {}

    def list_roles(self) -> "List[Role]":
        """List all server roles.  Requires capability: discord:read

        Returns list of: id, name, color, position, managed, mentionable.
        """
        result = self._t.call("discord.list_roles", {})
        if isinstance(result, dict):
            return result.get("roles") or []
        return []

    def list_members(
        self,
        *,
        role_id: Optional[str] = None,
        limit: int = 100,
        after: Optional[str] = None,
    ) -> "List[Member]":
        """Paginated member listing. Requires capability: discord:read

        Args:
            role_id: Filter to members with this role (optional).
            limit: Max members to return (1-100, default 100).
            after: User ID to paginate after (cursor-based pagination).

        Returns list of: user_id, username, display_name, nick, avatar, roles, joined_at, bot.
        """
        params: Dict[str, Any] = {"limit": min(max(1, int(limit)), 100)}
        if role_id is not None:
            params["role_id"] = str(role_id)
        if after is not None:
            params["after"] = str(after)
        result = self._t.call("discord.list_members", params)
        if isinstance(result, dict):
            return result.get("members") or []
        return []

    def search_members(self, query: str, *, limit: int = 25) -> "List[Member]":
        """Search members by username or nickname. Requires capability: discord:read

        Args:
            query: Search string (matches username and nickname).
            limit: Max results (1-25, default 25).

        Returns list of: user_id, username, display_name, nick, roles, joined_at, bot.
        """
        result = self._t.call("discord.search_members", {
            "query": str(query),
            "limit": min(max(1, int(limit)), 25),
        })
        if isinstance(result, dict):
            return result.get("members") or []
        return []

    def get_messages(
        self,
        *,
        channel_id: str,
        limit: int = 50,
        before: Optional[str] = None,
        after: Optional[str] = None,
    ) -> "List[Message]":
        """Fetch message history from a channel. Requires capability: discord:read

        Args:
            channel_id: The channel to fetch messages from.
            limit: Max messages (1-50, default 50).
            before: Fetch messages before this message ID.
            after: Fetch messages after this message ID.

        Returns list of: id, channel_id, author_id, author_username, author_bot,
                         content, timestamp, edited_timestamp, attachments, embeds, pinned.
        """
        params: Dict[str, Any] = {
            "channel_id": str(channel_id),
            "limit": min(max(1, int(limit)), 50),
        }
        if before is not None:
            params["before"] = str(before)
        if after is not None:
            params["after"] = str(after)
        result = self._t.call("discord.get_messages", params)
        if isinstance(result, dict):
            return result.get("messages") or []
        return []

    def iter_messages(
        self,
        *,
        channel_id: str,
        batch_size: int = 50,
        before: Optional[str] = None,
        after: Optional[str] = None,
    ) -> "Iterator[Message]":
        """Walk a channel's full message history, paging automatically.

        ``get_messages`` returns at most 50 messages per call; this generator
        keeps fetching pages and yields one message at a time so you can audit
        or export an entire channel without managing cursors yourself.
        Requires capability: discord:read.

        Direction:
          * default / ``before`` — walk newest → oldest (optionally starting
            before a given message id).
          * ``after`` — walk oldest → newest, starting after a given message id.

        Args:
            channel_id: The channel to walk.
            batch_size: Messages per underlying fetch (1-50, default 50).
            before: Start before this message id (newest→oldest walk).
            after: Start after this message id (oldest→newest walk).

        Yields:
            One message dict at a time (see ``get_messages`` for the shape).
        """
        batch_size = min(max(1, int(batch_size)), 50)
        forward = after is not None
        cursor: Optional[str] = str(after) if forward else (str(before) if before is not None else None)
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
            # Snowflake ids are monotonic, so compute the next cursor explicitly
            # rather than trusting intra-batch ordering.
            next_cursor = str(max(ids)) if forward else str(min(ids))
            if next_cursor == cursor or len(batch) < batch_size:
                return
            cursor = next_cursor

    # ── Channel management (Phase 2) ──────────────────────────────────

    def create_channel(
        self,
        *,
        name: str,
        channel_type: int = 0,
        category_id: Optional[str] = None,
        topic: Optional[str] = None,
        user_limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create a channel. Requires capability: discord:manage_channels

        Args:
            name: Channel name (max 100 chars).
            channel_type: 0=text, 2=voice, 4=category, 13=stage, 15=forum.
            category_id: Parent category ID (optional).
            topic: Channel topic (text channels only, max 1024 chars).
            user_limit: Max users (voice channels only, 0-99).

        Returns: dict with id, name, type of the created channel.
        """
        params: Dict[str, Any] = {
            "name": str(name)[:100],
            "channel_type": int(channel_type),
        }
        if category_id is not None:
            params["category_id"] = str(category_id)
        if topic is not None:
            params["topic"] = str(topic)[:1024]
        if user_limit is not None:
            params["user_limit"] = max(0, min(99, int(user_limit)))
        result = self._t.call("discord.create_channel", params)
        return result if isinstance(result, dict) else {}

    def delete_channel(self, *, channel_id: str) -> None:
        """Delete a channel. Requires capability: discord:manage_channels"""
        self._t.call("discord.delete_channel", {"channel_id": str(channel_id)})

    def edit_channel(
        self,
        *,
        channel_id: str,
        name: Optional[str] = None,
        topic: Optional[str] = None,
        user_limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Edit a channel's properties. Requires capability: discord:manage_channels

        Pass None to leave a property unchanged.
        """
        params: Dict[str, Any] = {"channel_id": str(channel_id)}
        if name is not None:
            params["name"] = str(name)[:100]
        if topic is not None:
            params["topic"] = str(topic)[:1024]
        if user_limit is not None:
            params["user_limit"] = max(0, min(99, int(user_limit)))
        result = self._t.call("discord.edit_channel", params)
        return result if isinstance(result, dict) else {}

    # ── Member management (Phase 2) ───────────────────────────────────

    def timeout_member(
        self, *, user_id: str, duration_seconds: int, reason: str = "",
    ) -> None:
        """Timeout a member. Requires capability: discord:moderate_members

        Args:
            user_id: The member's Discord user ID.
            duration_seconds: How long to timeout (max 2419200 = 28 days).
            reason: Audit log reason (max 512 chars).
        """
        self._t.call("discord.timeout_member", {
            "user_id": str(user_id),
            "duration_seconds": max(0, min(2419200, int(duration_seconds))),
            "reason": str(reason)[:512],
        })

    def ban_member(
        self, *, user_id: str, reason: str = "", delete_message_seconds: int = 0,
    ) -> None:
        """Ban a member. Requires capability: discord:ban_members

        Args:
            user_id: The member's Discord user ID.
            reason: Audit log reason (max 512 chars).
            delete_message_seconds: How far back to delete messages (0-604800 = 7 days).
        """
        self._t.call("discord.ban_member", {
            "user_id": str(user_id),
            "reason": str(reason)[:512],
            "delete_message_seconds": max(0, min(604800, int(delete_message_seconds))),
        })

    def unban_member(self, *, user_id: str) -> None:
        """Unban a user. Requires capability: discord:ban_members"""
        self._t.call("discord.unban_member", {"user_id": str(user_id)})

    def kick_member(self, *, user_id: str, reason: str = "") -> None:
        """Kick a member. Requires capability: discord:kick_members"""
        self._t.call("discord.kick_member", {
            "user_id": str(user_id),
            "reason": str(reason)[:512],
        })

    def add_role(self, *, user_id: str, role_id: str, reason: str = "") -> None:
        """Add a role to a member. Requires capability: discord:manage_roles"""
        self._t.call("discord.add_role", {
            "user_id": str(user_id),
            "role_id": str(role_id),
            "reason": str(reason)[:512],
        })

    def remove_role(self, *, user_id: str, role_id: str, reason: str = "") -> None:
        """Remove a role from a member. Requires capability: discord:manage_roles"""
        self._t.call("discord.remove_role", {
            "user_id": str(user_id),
            "role_id": str(role_id),
            "reason": str(reason)[:512],
        })

    # ── Bulk operations ──────────────────────────────────────────────────

    def add_role_bulk(self, *, user_ids: List[str], role_id: str, reason: str = "") -> Dict[str, Any]:
        """Add a role to multiple members. Max 25 users per call.

        Requires capability: discord:manage_roles
        Returns: {"success": int, "failed": int, "errors": [...]}
        """
        result = self._t.call("discord.add_role_bulk", {
            "user_ids": [str(u) for u in user_ids[:25]],
            "role_id": str(role_id),
            "reason": str(reason)[:512],
        })
        return result if isinstance(result, dict) else {"success": 0, "failed": 0, "errors": []}

    def remove_role_bulk(self, *, user_ids: List[str], role_id: str, reason: str = "") -> Dict[str, Any]:
        """Remove a role from multiple members. Max 25 users per call.

        Requires capability: discord:manage_roles
        Returns: {"success": int, "failed": int, "errors": [...]}
        """
        result = self._t.call("discord.remove_role_bulk", {
            "user_ids": [str(u) for u in user_ids[:25]],
            "role_id": str(role_id),
            "reason": str(reason)[:512],
        })
        return result if isinstance(result, dict) else {"success": 0, "failed": 0, "errors": []}

    def timeout_bulk(self, *, user_ids: List[str], duration_seconds: int, reason: str = "") -> Dict[str, Any]:
        """Timeout multiple members. Max 25 users per call.

        Requires capability: discord:moderate_members
        Returns: {"success": int, "failed": int, "errors": [...]}
        """
        result = self._t.call("discord.timeout_bulk", {
            "user_ids": [str(u) for u in user_ids[:25]],
            "duration_seconds": int(duration_seconds),
            "reason": str(reason)[:512],
        })
        return result if isinstance(result, dict) else {"success": 0, "failed": 0, "errors": []}

    def kick_bulk(self, *, user_ids: List[str], reason: str = "") -> Dict[str, Any]:
        """Kick multiple members. Max 25 users per call.

        Requires capability: discord:kick_members
        Returns: {"success": int, "failed": int, "errors": [...]}
        """
        result = self._t.call("discord.kick_bulk", {
            "user_ids": [str(u) for u in user_ids[:25]],
            "reason": str(reason)[:512],
        })
        return result if isinstance(result, dict) else {"success": 0, "failed": 0, "errors": []}

    # ── Permission overwrites ───────────────────────────────────────────

    def set_channel_permissions(
        self, *, channel_id: str, target_id: str,
        allow: str = "0", deny: str = "0", target_type: int = 0,
    ) -> None:
        """Set permission overwrites on a channel. Requires capability: discord:manage_channels

        Args:
            channel_id: The channel to modify.
            target_id: Role ID (target_type=0) or user ID (target_type=1).
            allow: Permission bitfield to allow (as string).
            deny: Permission bitfield to deny (as string).
            target_type: 0=role, 1=member.

        Note: Dangerous permissions (ADMINISTRATOR, MANAGE_GUILD, MANAGE_ROLES,
        MANAGE_WEBHOOKS, KICK/BAN_MEMBERS) are automatically stripped.
        """
        self._t.call("discord.set_channel_permissions", {
            "channel_id": str(channel_id),
            "target_id": str(target_id),
            "allow": str(allow),
            "deny": str(deny),
            "target_type": int(target_type),
        })

    def delete_channel_permission(self, *, channel_id: str, target_id: str) -> None:
        """Remove a permission overwrite from a channel. Requires capability: discord:manage_channels"""
        self._t.call("discord.delete_channel_permission", {
            "channel_id": str(channel_id),
            "target_id": str(target_id),
        })

    # ── Threads ─────────────────────────────────────────────────────────

    def create_thread(
        self, *, channel_id: str, name: str,
        thread_type: int = 11, auto_archive_duration: int = 1440,
    ) -> Dict[str, Any]:
        """Create a thread in a channel. Requires capability: discord:manage_channels

        Args:
            channel_id: Parent channel.
            name: Thread name (max 100 chars).
            thread_type: 11=public, 12=private.
            auto_archive_duration: Minutes until auto-archive (60, 1440, 4320, 10080).

        Returns: dict with id, name, type, archived.
        """
        result = self._t.call("discord.create_thread", {
            "channel_id": str(channel_id),
            "name": str(name)[:100],
            "thread_type": int(thread_type),
            "auto_archive_duration": int(auto_archive_duration),
        })
        if isinstance(result, dict):
            return result.get("thread") or {}
        return {}

    def edit_thread(
        self, *, thread_id: str,
        archived: Optional[bool] = None, locked: Optional[bool] = None,
        name: Optional[str] = None, auto_archive_duration: Optional[int] = None,
    ) -> None:
        """Edit a thread. Requires capability: discord:manage_channels

        Pass None to leave a property unchanged.
        """
        params: Dict[str, Any] = {"thread_id": str(thread_id)}
        if archived is not None:
            params["archived"] = bool(archived)
        if locked is not None:
            params["locked"] = bool(locked)
        if name is not None:
            params["name"] = str(name)[:100]
        if auto_archive_duration is not None:
            params["auto_archive_duration"] = int(auto_archive_duration)
        self._t.call("discord.edit_thread", params)

    # ── Pins ────────────────────────────────────────────────────────────

    def pin_message(self, *, channel_id: str, message_id: str) -> None:
        """Pin a message. Requires capability: discord:send_message"""
        self._t.call("discord.pin_message", {
            "channel_id": str(channel_id),
            "message_id": str(message_id),
        })

    def unpin_message(self, *, channel_id: str, message_id: str) -> None:
        """Unpin a message. Requires capability: discord:send_message"""
        self._t.call("discord.unpin_message", {
            "channel_id": str(channel_id),
            "message_id": str(message_id),
        })

    # ── Guild info ──────────────────────────────────────────────────────

    def get_guild(self) -> "Guild":
        """Get server info. Requires capability: discord:read

        Returns: id, name, icon, member_count, premium_tier, features, owner_id, description.
        """
        result = self._t.call("discord.get_guild", {})
        if isinstance(result, dict):
            return result.get("guild") or {}
        return {}

    def list_channels(self) -> "List[Channel]":
        """List all channels in the server. Requires capability: discord:read

        Returns list of: id, name, type, parent_id, position.
        Channel types: 0=text, 2=voice, 4=category, 5=announcement, 13=stage, 15=forum.
        """
        result = self._t.call("discord.list_channels", {})
        if isinstance(result, dict):
            return result.get("channels") or []
        return []

    # ── Nickname ────────────────────────────────────────────────────────

    def set_nickname(self, *, user_id: str, nickname: Optional[str] = None) -> None:
        """Set a member's nickname. Pass None to reset. Requires capability: discord:moderate_members"""
        params: Dict[str, Any] = {"user_id": str(user_id)}
        if nickname is not None:
            params["nickname"] = str(nickname)[:32]
        else:
            params["nickname"] = None
        self._t.call("discord.set_nickname", params)

    # ── Webhooks ────────────────────────────────────────────────────────

    def create_webhook(self, *, channel_id: str, name: str) -> Dict[str, Any]:
        """Create a webhook. Requires capability: discord:manage_webhooks

        Returns: id, token, channel_id, name. Store the token — you need it for execute_webhook.
        """
        result = self._t.call("discord.create_webhook", {
            "channel_id": str(channel_id),
            "name": str(name)[:80],
        })
        if isinstance(result, dict):
            return result.get("webhook") or {}
        return {}

    def execute_webhook(
        self, *, webhook_id: str, webhook_token: str,
        content: str = "", embeds: Optional[List[Dict[str, Any]]] = None,
        username: Optional[str] = None, avatar_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a message via webhook. Requires capability: discord:manage_webhooks

        Args:
            webhook_id: Webhook ID from create_webhook().
            webhook_token: Webhook token from create_webhook().
            content: Message text.
            embeds: List of embed dicts.
            username: Override the webhook's display name.
            avatar_url: Override the webhook's avatar.

        Returns: dict with message_id.
        """
        params: Dict[str, Any] = {
            "webhook_id": str(webhook_id),
            "webhook_token": str(webhook_token),
        }
        if content:
            params["content"] = str(content)
        if embeds:
            params["embeds"] = embeds
        if username:
            params["username"] = str(username)
        if avatar_url:
            params["avatar_url"] = str(avatar_url)
        result = self._t.call("discord.execute_webhook", params)
        return result if isinstance(result, dict) else {}

    def delete_webhook(self, *, webhook_id: str) -> None:
        """Delete a webhook. Requires capability: discord:manage_webhooks"""
        self._t.call("discord.delete_webhook", {"webhook_id": str(webhook_id)})


class _HttpApi:
    """ctx.http — outbound HTTP requests (requires proxy:http capability)."""

    def __init__(self, transport: "Transport") -> None:
        self._t = transport

    def request(
        self, method: str, url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Make an HTTP request.  Only approved domains are reachable.

        ``params`` is a dict of query-string parameters; values may be
        strings or lists of strings (lists are encoded as repeated keys,
        e.g. ``{"k": ["a", "b"]}`` becomes ``k=a&k=b``). The encoded query
        string is appended to ``url`` with the correct separator (``?``
        or ``&``).

        Returns: status (int), headers (dict), body_bytes (str), truncated (bool).
        """
        if params:
            import urllib.parse as _up
            query_string = _up.urlencode(params, doseq=True)
            if query_string:
                url = f"{url}{'&' if '?' in url else '?'}{query_string}"
        result = self._t.call("proxy.request", {
            "method": method.upper(), "url": url,
            "headers": headers or {}, "body": body,
        })
        return result or {}

    def get(
        self, url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self.request("GET", url, headers=headers, params=params)

    def post(
        self, url: str,
        *,
        body: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self.request("POST", url, headers=headers, body=body, params=params)


class _InteractionApi:
    """ctx.interaction — respond to slash commands, buttons, selects, and modals.

    Requires capability: interaction:respond

    These methods only work inside an interaction_create event handler.
    The interaction_id and token are automatically extracted from the
    current event context.
    """

    def __init__(self, transport: "Transport", ctx: "Context" = None) -> None:
        self._t = transport
        self._ctx = ctx

    def _iid(self) -> str:
        """Get the current interaction ID from context."""
        if self._ctx and hasattr(self._ctx, "_current_interaction_id"):
            return self._ctx._current_interaction_id or ""
        return ""

    def respond(
        self,
        *,
        content: str = "",
        embeds: Optional[List[Dict[str, Any]]] = None,
        components: Optional[list] = None,
        ephemeral: bool = False,
        allowed_mentions: Optional[Dict[str, Any]] = None,
        update_message: bool = False,
    ) -> None:
        """Send an immediate response to the interaction.

        Must be called within 3 seconds of receiving the interaction.
        Can only be called once per interaction — use defer() + followup()
        for slow operations.

        Args:
            content: Message text (max 2000 chars).
            embeds: List of embed dicts (max 10).
            components: List of ActionRow objects or dicts.
            ephemeral: If True, only the interacting user sees the response.
                Ignored when ``update_message=True`` (the message being
                updated keeps its visibility).
            allowed_mentions: Discord allowed_mentions object controlling
                which mentions actually ping. Common shapes:
                  ``{"parse": []}``                — suppress all pings
                  ``{"parse": ["users"]}``         — only user mentions ping
                  ``{"users": ["123", "456"]}``    — only these user IDs ping
            update_message: If True, EDIT the message the component is
                attached to (Discord UPDATE_MESSAGE) instead of sending a
                new message. Only valid inside a component (button /
                select menu) interaction handler — the platform rejects it
                for slash commands and modal submits. The fields you pass
                REPLACE the message's current content/embeds/components.
                Use this to update game boards, dashboards, paginated
                lists, etc. in place.
        """
        payload = {"_interaction_id": self._iid(),
            "content": str(content)[:2000],
            "embeds": (embeds or [])[:10],
            "components": [c.to_dict() if hasattr(c, "to_dict") else c for c in (components or [])],
            "ephemeral": bool(ephemeral),
        }
        if update_message:
            payload["update_message"] = True
        if allowed_mentions is not None:
            payload["allowed_mentions"] = allowed_mentions
        self._t.call("interaction.respond", payload)

    def defer(self, *, ephemeral: bool = False) -> None:
        """Acknowledge the interaction and show a "thinking..." indicator.

        You have 15 minutes after deferring to send a followup().
        Use this when your handler needs more than 3 seconds to process.
        """
        self._t.call("interaction.defer", {"_interaction_id": self._iid(), "ephemeral": bool(ephemeral)})

    def followup(
        self,
        *,
        content: str = "",
        embeds: Optional[List[Dict[str, Any]]] = None,
        components: Optional[list] = None,
        ephemeral: bool = False,
        allowed_mentions: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Send a follow-up message after defer().

        Can be called multiple times. Each creates a new message.

        Returns a dict with the created message's identifiers — at
        minimum ``{"message_id": str, "channel_id": str}`` — so you can
        edit or delete it later. Returns an empty dict if the platform
        could not capture the response (e.g. transient parse failure);
        the message was still sent in that case.

        See ``respond()`` for ``allowed_mentions`` semantics.
        """
        payload = {"_interaction_id": self._iid(),
            "content": str(content)[:2000],
            "embeds": (embeds or [])[:10],
            "components": [c.to_dict() if hasattr(c, "to_dict") else c for c in (components or [])],
            "ephemeral": bool(ephemeral),
        }
        if allowed_mentions is not None:
            payload["allowed_mentions"] = allowed_mentions
        result = self._t.call("interaction.followup", payload)
        return result if isinstance(result, dict) else {}

    def send_modal(
        self,
        *,
        title: str,
        custom_id: str,
        fields: Optional[list] = None,
    ) -> None:
        """Show a modal dialog to the user.

        Only works on slash command and component interactions (not on
        another modal submit). The user's response arrives as a new
        interaction_create event with interaction_type=5 (MODAL_SUBMIT).

        Args:
            title: Modal title (max 45 chars).
            custom_id: ID to match in your on_modal_submit handler (max 100 chars).
            fields: List of TextInput objects or ActionRow(TextInput(...)) objects.
        """
        rows = []
        for f in (fields or []):
            if hasattr(f, "to_dict"):
                d = f.to_dict()
                # Wrap bare TextInput in an ActionRow
                if d.get("type") == 4:  # TEXT_INPUT
                    rows.append({"type": 1, "components": [d]})
                else:
                    rows.append(d)
            elif isinstance(f, dict):
                rows.append(f)
        self._t.call("interaction.send_modal", {"_interaction_id": self._iid(),
            "title": str(title)[:45],
            "custom_id": str(custom_id)[:100],
            "components": rows[:5],
        })


class Context:
    """The context object passed to every event handler.

    Attributes:
        server_id    Discord server (guild) ID as a string
        plugin_id    Your plugin's ID string
        version      Installed version string
        kv           Key-value storage API
        discord      Discord actions API
        http         HTTP proxy API
        interaction  Interaction response API (slash commands, buttons, modals)
        metrics      Time-series metrics API (available to all plugins)
        sql          Sandboxed SQL API (requires storage:sql capability)
        ephemeral    Fast rate counters, cooldowns, dedup (no capability required)
    """

    def __init__(
        self, *, server_id: str, plugin_id: str, version: str,
        capabilities: list, transport: "Transport",
    ) -> None:
        self.server_id = server_id
        self.plugin_id = plugin_id
        self.version = version
        self.capabilities = set(capabilities or [])
        self._transport = transport

        self.kv          = _KvApi(transport)
        self.discord     = _DiscordApi(transport)
        self.http        = _HttpApi(transport)
        self.interaction = _InteractionApi(transport, ctx=self)
        self.metrics     = _MetricsApi(transport)
        self.sql         = _SqlApi(transport)
        self.ephemeral   = _EphemeralApi(transport)
        self.secrets     = _SecretsApi(transport)
        self._current_interaction_id: Optional[str] = None

    @property
    def request_id(self) -> str:
        """Stable correlation ID for the current event.

        Returns the Discord interaction ID inside an ``interaction_create``
        handler, or an empty string outside an event. Pass this through
        ``ctx.log(..., request_id=ctx.request_id)`` to correlate log lines
        across handler entry, RPC calls, and downstream work.
        """
        return self._current_interaction_id or ""

    def log(self, message: str, *, level: str = "info", tags: Optional[List[str]] = None, **extra) -> None:
        """Write to the plugin audit log.  Visible to server admins in the dashboard.

        Args:
            message: Log message (max 4000 chars).
            level: "info", "warning", or "error".
            tags: Optional list of tags for filtering (e.g., ["moderation", "ban"]).
            **extra: Additional key-value context (e.g., user_id="123", reason="spam").
        """
        params: Dict[str, Any] = {
            "level": str(level),
            "message": str(message)[:4000],
        }
        if tags:
            params["tags"] = [str(t) for t in tags[:10]]
        if extra:
            params["extra"] = {str(k): str(v)[:500] for k, v in list(extra.items())[:20]}
        self._transport.notify("plugin.log", params)

    def has_capability(self, cap: str) -> bool:
        """Check if this install has a specific capability approved."""
        return cap in self.capabilities


class _MetricsApi:
    """ctx.metrics — time-series metrics storage (available to all plugins).

    Record numeric data points with tags, then query aggregated results.
    Platform handles storage, rollups, and retention (90 days).
    """

    def __init__(self, transport: "Transport") -> None:
        self._t = transport

    def record(self, metric: str, value: float = 1.0, tags: Optional[Dict[str, str]] = None) -> None:
        """Record a data point.

        Args:
            metric: Metric name — must start with a letter, may contain
                [a-zA-Z0-9_.], max 128 chars.
            value: Numeric value (default 1.0).
            tags: Optional key-value tags for grouping (max 10 tags).

        Example::
            ctx.metrics.record("messages", 1, tags={"channel_id": "123"})
            ctx.metrics.record("voice_minutes", 5.2, tags={"user_id": "456"})
        """
        params: Dict[str, Any] = {"metric": str(metric), "value": float(value)}
        if tags:
            params["tags"] = {str(k): str(v) for k, v in tags.items()}
        self._t.call("metrics.record", params)

    def query(
        self, metric: str, *, period: str = "7d",
        group_by: Optional[str] = None, aggregate: str = "sum",
    ) -> Dict[str, Any]:
        """Query aggregated metrics.

        Args:
            metric: Metric name to query.
            period: Time window — "1h", "24h", "7d", "30d", "90d".
            group_by: Optional tag key to group by (e.g., "channel_id").
            aggregate: Aggregation function — "sum", "count", "avg", "min", "max".

        Returns:
            {"labels": ["2026-03-17", ...], "series": [{"name": "...", "data": [...]}], "total": float}
        """
        params: Dict[str, Any] = {"metric": str(metric), "period": str(period), "aggregate": str(aggregate)}
        if group_by:
            params["group_by"] = str(group_by)
        result = self._t.call("metrics.query", params)
        return result if isinstance(result, dict) else {"labels": [], "series": [], "total": 0}

    def total(self, metric: str, *, period: str = "30d") -> float:
        """Get a single aggregate total for a metric.

        Args:
            metric: Metric name.
            period: Time window — "1h", "24h", "7d", "30d", "90d".

        Returns:
            The total (sum) as a float.
        """
        result = self._t.call("metrics.total", {"metric": str(metric), "period": str(period)})
        if isinstance(result, dict):
            return float(result.get("total", 0))
        return 0.0


class _SqlApi:
    """ctx.sql — sandboxed SQL (requires storage:sql capability, staff-reviewed).

    Each plugin gets an isolated Postgres schema. You can create tables,
    insert data, and run queries — but cannot access platform tables.
    """

    def __init__(self, transport: "Transport") -> None:
        self._t = transport

    def execute(self, sql: str, params: Optional[list] = None) -> int:
        """Execute DDL/DML (CREATE TABLE, INSERT, UPDATE, DELETE).

        Args:
            sql: SQL statement with %s placeholders for params.
            params: List of parameter values (optional).

        Returns:
            Number of rows affected.

        Example::
            ctx.sql.execute(
                "INSERT INTO user_stats (user_id, messages) VALUES (%s, 1) "
                "ON CONFLICT (user_id) DO UPDATE SET messages = user_stats.messages + 1",
                ["123456"]
            )
        """
        result = self._t.call("sql.execute", {"sql": str(sql), "params": list(params or [])})
        if isinstance(result, dict):
            return int(result.get("rowcount", 0))
        return 0

    def query(self, sql: str, params: Optional[list] = None, *, limit: int = 1000) -> List[Dict[str, Any]]:
        """Run a SELECT query. Returns list of dicts, max 1000 rows.

        Args:
            sql: SELECT statement with %s placeholders.
            params: List of parameter values (optional).
            limit: Max rows to return (1-1000, default 1000).

        Example::
            rows = ctx.sql.query(
                "SELECT user_id, messages FROM user_stats ORDER BY messages DESC LIMIT 10"
            )
            for row in rows:
                print(row["user_id"], row["messages"])
        """
        result = self._t.call("sql.query", {
            "sql": str(sql), "params": list(params or []), "limit": min(int(limit), 1000),
        })
        if isinstance(result, dict):
            return result.get("rows") or []
        return []

    def query_one(self, sql: str, params: Optional[list] = None) -> Optional[Dict[str, Any]]:
        """Run a SELECT and return the first row only (or None).

        Example::
            count = ctx.sql.query_one("SELECT COUNT(*) AS cnt FROM user_stats")
            print(count["cnt"])  # 150
        """
        result = self._t.call("sql.query_one", {"sql": str(sql), "params": list(params or [])})
        if isinstance(result, dict):
            return result.get("row")
        return None

    def scalar(self, sql: str, params: Optional[list] = None) -> Any:
        """Run a SELECT and return the first column of the first row.

        Convenience wrapper around query_one() for single-value queries.

        Example::
            total = ctx.sql.scalar("SELECT COUNT(*) FROM user_stats")
            print(total)  # 150
        """
        row = self.query_one(sql, params)
        if row and isinstance(row, dict):
            # Return first value from the dict
            return next(iter(row.values()), None)
        return None


class _EphemeralApi:
    """ctx.ephemeral — fast, short-lived state for rate limiting, cooldowns, and dedup.

    Redis-backed with automatic in-process fallback. All keys are scoped to
    your plugin + server. TTL max is 24 hours — this is NOT persistent storage
    (use ctx.kv for that).

    No capability required — available to all plugins.
    """

    def __init__(self, transport: "Transport") -> None:
        self._t = transport

    def counter(self, key: str, window_seconds: int = 60) -> int:
        """Increment a sliding-window counter. Returns the current count within the window.

        Use for rate limiting: ``if ctx.ephemeral.counter("spam:" + user_id, 60) > 5: ...``

        Args:
            key: Counter name (max 256 chars).
            window_seconds: Sliding window size (1-86400, default 60).
        """
        result = self._t.call("ephemeral.counter", {
            "key": str(key), "window_seconds": int(window_seconds),
        })
        return int(result.get("count", 0)) if isinstance(result, dict) else 0

    def cooldown_set(self, key: str, ttl_seconds: int = 60) -> None:
        """Start a cooldown. Check with cooldown_check().

        Args:
            key: Cooldown name (max 256 chars).
            ttl_seconds: How long the cooldown lasts (1-86400).
        """
        self._t.call("ephemeral.cooldown_set", {
            "key": str(key), "ttl_seconds": int(ttl_seconds),
        })

    def cooldown_check(self, key: str) -> Dict[str, Any]:
        """Check if a cooldown is active. Returns {"active": bool, "remaining_seconds": float}."""
        result = self._t.call("ephemeral.cooldown_check", {"key": str(key)})
        return result if isinstance(result, dict) else {"active": False, "remaining_seconds": 0}

    def dedup(self, key: str, ttl_seconds: int = 3600) -> bool:
        """Check if this is the first time seeing this key. Returns True if new, False if seen before.

        Use for deduplication: ``if ctx.ephemeral.dedup(f"welcome:{user_id}"): send_welcome()``

        Args:
            key: Dedup key (max 256 chars).
            ttl_seconds: How long to remember (1-86400, default 3600).
        """
        result = self._t.call("ephemeral.dedup", {
            "key": str(key), "ttl_seconds": int(ttl_seconds),
        })
        return bool(result.get("is_new")) if isinstance(result, dict) else True

    def flag_set(self, key: str, ttl_seconds: int = 3600) -> None:
        """Set a boolean flag with TTL. Check with flag_check().

        Args:
            key: Flag name (max 256 chars).
            ttl_seconds: How long the flag stays set (1-86400).
        """
        self._t.call("ephemeral.flag_set", {
            "key": str(key), "ttl_seconds": int(ttl_seconds),
        })

    def flag_check(self, key: str) -> bool:
        """Check if a flag is set. Returns True if active, False if expired or unset."""
        result = self._t.call("ephemeral.flag_check", {"key": str(key)})
        return bool(result.get("active")) if isinstance(result, dict) else False
