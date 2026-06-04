"""Typed event payloads for plugin handlers.

Each ``@plugin.on_event(...)`` handler receives an ``event: dict`` whose shape
depends on the event type. This module ships TypedDicts that describe those
shapes so IDEs autocomplete the fields and type checkers can catch typos.

Quick start::

    from yourbot_sdk import Plugin, Context
    from yourbot_sdk.events import MessageCreate

    plugin = Plugin()

    @plugin.on_event("message_create")
    def on_msg(ctx: Context, event: MessageCreate):
        if event["content"].startswith("!ping"):
            ctx.discord.send_message(channel_id=event["channel_id"], content="pong")

Compatibility notes:
  * Python 3.10 doesn't have ``typing.NotRequired``, so every TypedDict in this
    module uses ``total=False`` — i.e. **every field is optional at the type
    level**. In practice the gateway always populates the fields documented
    below, but a defensive ``event.get("...")`` is a fine pattern.
  * The field names match what the gateway publishes (see
    ``mmo_maid/apps/gateway/publisher.py`` for the canonical serializers).
  * The values are strings unless otherwise noted — Discord snowflakes are
    returned as strings to avoid precision loss in JSON.

If a field you expect isn't here, add it to the appropriate TypedDict; the
gateway always passes the dict through unchanged so unknown fields don't
break anything.
"""
from __future__ import annotations

from typing import TypedDict, Optional


# ── Message events ──────────────────────────────────────────────────────────


class MessageCreate(TypedDict, total=False):
    """Payload for ``@plugin.on_event("message_create")`` handlers.

    Fired when a message is sent in a server channel the bot can see.
    """
    message_id: str             # snowflake of the message
    channel_id: str             # snowflake of the channel
    channel_name: Optional[str] # channel display name (e.g. "general")
    author_id: str              # snowflake of the author user
    author_name: Optional[str]  # author's Discord username
    author_bot: bool            # True if the author is a bot
    content: str                # message body text (may be empty if only embeds/attachments)
    created_at: Optional[str]   # ISO-8601 timestamp
    embed_count: int            # number of embeds attached
    attachment_count: int       # number of file attachments
    mention_count: int          # number of users mentioned
    # Present only on replies; absent for normal messages.
    reference_message_id: Optional[str]
    # Aliases / extras populated by some surfaces (testing harness, older gateway).
    author_username: Optional[str]
    guild_id: Optional[str]
    timestamp: Optional[str]


class MessageEdit(TypedDict, total=False):
    """Payload for ``@plugin.on_event("message_edit")`` handlers."""
    message_id: str
    channel_id: str
    author_id: Optional[str]
    content: str               # post-edit content
    embed_count: int
    attachment_count: int
    mention_count: int
    guild_id: Optional[str]


class MessageDelete(TypedDict, total=False):
    """Payload for ``@plugin.on_event("message_delete")`` handlers.

    Note: ``content`` is only present when the message was in the gateway
    cache at deletion time. Treat it as best-effort.
    """
    message_id: str
    channel_id: str
    author_id: Optional[str]
    content: Optional[str]
    guild_id: Optional[str]


# ── Member events ───────────────────────────────────────────────────────────


class MemberJoin(TypedDict, total=False):
    """Payload for ``@plugin.on_event("member_join")`` handlers."""
    user_id: str
    username: str
    display_name: Optional[str]
    bot: bool
    joined_at: Optional[str]   # ISO-8601 timestamp
    roles: list[str]           # role IDs as strings
    nick: Optional[str]
    guild_id: Optional[str]
    guild_name: Optional[str]
    member_count: int          # server's total member count after join


class MemberLeave(TypedDict, total=False):
    """Payload for ``@plugin.on_event("member_leave")`` handlers.

    Fired for both voluntary leaves and kicks/bans (Discord doesn't
    distinguish in the gateway event).
    """
    user_id: str
    username: str
    display_name: Optional[str]
    bot: bool
    joined_at: Optional[str]
    roles: list[str]
    nick: Optional[str]
    guild_id: Optional[str]


class MemberUpdate(TypedDict, total=False):
    """Payload for ``@plugin.on_event("member_update")`` handlers.

    Fired on nick change, role change, timeout, avatar change. The new state
    is in the top-level fields; ``before_roles`` carries the prior role set.
    """
    user_id: str
    username: str
    display_name: Optional[str]
    bot: bool
    roles: list[str]           # current roles
    before_roles: list[str]    # roles before this update
    nick: Optional[str]
    before_nick: Optional[str]
    guild_id: Optional[str]


# ── Reaction events ─────────────────────────────────────────────────────────


class ReactionAdd(TypedDict, total=False):
    """Payload for ``@plugin.on_event("reaction_add")`` handlers."""
    message_id: str
    channel_id: str
    user_id: str
    emoji: str                 # ``"👍"`` or ``"<a:name:id>"`` for custom
    user_bot: bool
    guild_id: Optional[str]


class ReactionRemove(TypedDict, total=False):
    """Payload for ``@plugin.on_event("reaction_remove")`` handlers."""
    message_id: str
    channel_id: str
    user_id: str
    emoji: str
    user_bot: bool
    guild_id: Optional[str]


# ── Voice events ────────────────────────────────────────────────────────────


class VoiceStateUpdate(TypedDict, total=False):
    """Payload for ``@plugin.on_event("voice_state_update")`` handlers.

    Fired on join voice, leave voice, channel switch, mute/deafen, stream
    start/stop. The transition is encoded as ``before_channel_id`` (where
    they were, ``None`` = nowhere) and ``after_channel_id`` (where they are
    now, ``None`` = left voice entirely).
    """
    user_id: str
    username: Optional[str]
    bot: bool
    member_bot: bool
    before_channel_id: Optional[str]
    before_channel_name: Optional[str]
    after_channel_id: Optional[str]
    after_channel_name: Optional[str]
    before_self_stream: bool
    after_self_stream: bool
    self_mute: bool
    self_deaf: bool
    self_stream: bool
    self_video: bool
    guild_id: Optional[str]
    # Convenience alias used by some handlers; same as after_channel_id.
    channel_id: Optional[str]


# ── Interaction events ──────────────────────────────────────────────────────


class InteractionCreate(TypedDict, total=False):
    """Payload for ``@plugin.on_event("interaction_create")`` handlers.

    Fired when a user clicks a button, submits a modal, or invokes a slash
    command. For slash commands you usually want
    ``@plugin.on_slash_command("name")`` instead — it receives the same
    payload but only fires for the matching command.
    """
    interaction_id: str
    type: int                  # Discord interaction type (2=slash, 3=component, 5=modal)
    interaction_type: int      # alias used by the testing harness
    channel_id: Optional[str]
    user_id: Optional[str]
    user_name: Optional[str]
    command_name: Optional[str]      # for slash commands
    command_type: Optional[int]      # for slash commands
    custom_id: Optional[str]         # for component/modal interactions
    modal_values: dict               # for modal submissions
    guild_id: Optional[str]


# ── Channel events ──────────────────────────────────────────────────────────


class ChannelCreate(TypedDict, total=False):
    """Payload for ``@plugin.on_event("channel_create")`` handlers."""
    channel_id: str
    id: str                    # alias for channel_id
    name: str
    type: int                  # Discord channel type
    category_id: Optional[str]
    position: Optional[int]
    guild_id: Optional[str]


class ChannelDelete(TypedDict, total=False):
    """Payload for ``@plugin.on_event("channel_delete")`` handlers."""
    channel_id: str
    id: str
    name: str
    type: int
    category_id: Optional[str]
    position: Optional[int]
    guild_id: Optional[str]


class ChannelUpdate(TypedDict, total=False):
    """Payload for ``@plugin.on_event("channel_update")`` handlers.

    Top-level fields are the current state. The previous state, when
    available, lives under the ``before_*`` keys.
    """
    channel_id: str
    id: str
    name: str
    before_name: Optional[str]
    type: int
    category_id: Optional[str]
    position: Optional[int]
    guild_id: Optional[str]


# ── Role events ─────────────────────────────────────────────────────────────


class RoleCreate(TypedDict, total=False):
    """Payload for ``@plugin.on_event("role_create")`` handlers."""
    role_id: str
    id: str
    name: str
    color: int                 # RGB integer
    position: int
    permissions: str           # Discord permission integer as string (precision-safe)
    managed: bool
    mentionable: bool
    guild_id: Optional[str]


class RoleDelete(TypedDict, total=False):
    """Payload for ``@plugin.on_event("role_delete")`` handlers."""
    role_id: str
    id: str
    name: str
    color: int
    position: int
    permissions: str
    managed: bool
    mentionable: bool
    guild_id: Optional[str]


class RoleUpdate(TypedDict, total=False):
    """Payload for ``@plugin.on_event("role_update")`` handlers."""
    role_id: str
    id: str
    name: str
    before_name: Optional[str]
    color: int
    position: int
    permissions: str
    managed: bool
    mentionable: bool
    guild_id: Optional[str]


# ── Catalog ─────────────────────────────────────────────────────────────────


# Mapping of event_type string → TypedDict class. Useful when you want to
# look up the schema for an event type programmatically, e.g. in test factories.
EVENT_TYPE_TO_SCHEMA: dict[str, type] = {
    "message_create":      MessageCreate,
    "message_edit":        MessageEdit,
    "message_delete":      MessageDelete,
    "member_join":         MemberJoin,
    "member_leave":        MemberLeave,
    "member_update":       MemberUpdate,
    "reaction_add":        ReactionAdd,
    "reaction_remove":     ReactionRemove,
    "voice_state_update":  VoiceStateUpdate,
    "interaction_create":  InteractionCreate,
    "channel_create":      ChannelCreate,
    "channel_delete":      ChannelDelete,
    "channel_update":      ChannelUpdate,
    "role_create":         RoleCreate,
    "role_delete":         RoleDelete,
    "role_update":         RoleUpdate,
}


__all__ = [
    "MessageCreate", "MessageEdit", "MessageDelete",
    "MemberJoin", "MemberLeave", "MemberUpdate",
    "ReactionAdd", "ReactionRemove",
    "VoiceStateUpdate",
    "InteractionCreate",
    "ChannelCreate", "ChannelDelete", "ChannelUpdate",
    "RoleCreate", "RoleDelete", "RoleUpdate",
    "EVENT_TYPE_TO_SCHEMA",
]
