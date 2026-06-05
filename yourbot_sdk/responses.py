"""Typed return shapes for ``ctx.discord`` read methods.

The Discord read APIs (``get_member``, ``get_channel``, ``get_guild``,
``list_roles``, ``list_channels``, ``get_messages``, ``list_members``,
``search_members``) return plain dicts. These TypedDicts describe those dicts so
IDEs autocomplete the fields and type checkers catch typos::

    from yourbot_sdk import Plugin, Context
    from yourbot_sdk.responses import Member

    @plugin.on_event("member_join")
    def on_join(ctx: Context, event):
        member: Member = ctx.discord.get_member(user_id=event["user_id"])
        if "admin" in member["roles"]:
            ...

Compatibility notes (same as ``events.py``):
  * Every TypedDict uses ``total=False`` (Python 3.10 has no ``NotRequired``), so
    every field is optional at the type level — a defensive ``.get(...)`` is fine.
  * The shapes mirror what the host returns (see the ``discord.*`` handlers in
    ``mmo_maid/apps/plugin_runner/rpc.py``). Snowflake IDs are strings.
"""
from __future__ import annotations

from typing import TypedDict, List, Optional


class Member(TypedDict, total=False):
    """Returned by ``ctx.discord.get_member`` / ``list_members`` / ``search_members``."""
    user_id: str
    username: str
    display_name: str
    nick: Optional[str]
    avatar: Optional[str]
    roles: List[str]            # role snowflakes the member has
    joined_at: Optional[str]    # ISO-8601 timestamp
    bot: bool


class Role(TypedDict, total=False):
    """Returned by ``ctx.discord.list_roles``."""
    id: str
    name: str
    color: int
    position: int
    managed: bool               # True for integration/bot-managed roles
    mentionable: bool
    permissions: str            # string-encoded integer permission bitfield


class Channel(TypedDict, total=False):
    """Returned by ``ctx.discord.get_channel`` / ``list_channels``."""
    id: str
    name: str
    type: int                   # 0=text, 2=voice, 4=category, 13=stage, 15=forum
    topic: Optional[str]
    parent_id: Optional[str]    # category snowflake, if any
    position: int
    nsfw: bool


class Guild(TypedDict, total=False):
    """Returned by ``ctx.discord.get_guild``."""
    id: str
    name: str
    icon: Optional[str]
    member_count: int
    premium_tier: int
    features: List[str]
    owner_id: str
    description: Optional[str]


class Message(TypedDict, total=False):
    """Returned by ``ctx.discord.get_messages``."""
    id: str
    channel_id: str
    author_id: str
    author_username: str
    author_bot: bool
    content: str
    timestamp: Optional[str]            # ISO-8601 timestamp
    edited_timestamp: Optional[str]
    attachments: int                    # number of file attachments (count, not a list)
    embeds: int                         # number of embeds (count, not a list)
    pinned: bool
