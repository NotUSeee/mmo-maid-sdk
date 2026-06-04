"""
YourBot Plugin SDK
===================

Write a plugin in three steps::

    from yourbot_sdk import Plugin, Context, Button, ActionRow

    plugin = Plugin()

    @plugin.on_ready
    def ready(ctx: Context):
        ctx.log("My plugin is alive!")

    @plugin.on_event("message_create")
    def on_message(ctx: Context, event: dict):
        if "!hello" in event.get("content", ""):
            ctx.discord.send_message(
                channel_id=event["channel_id"],
                content="Hello from my plugin!",
            )

    # For IDE autocomplete on event fields, import the typed shape:
    #     from yourbot_sdk.events import MessageCreate
    #     def on_message(ctx: Context, event: MessageCreate): ...

    @plugin.on_slash_command("greet")
    def greet(ctx: Context, event: dict):
        ctx.interaction.respond(
            content="Hey there!",
            components=[ActionRow(Button("Click me", custom_id="btn_hi"))],
        )

    @plugin.on_component("btn_hi")
    def btn_hi(ctx: Context, event: dict):
        ctx.interaction.respond(content="You clicked!", ephemeral=True)

    plugin.run()

The SDK handles JSON-RPC, event acking, boot handshake, and error
recovery so you don't have to.
"""

from ._plugin import Plugin
from ._context import Context
from ._exceptions import (
    SdkError,
    CapabilityError,
    RateLimitError,
    DiscordApiError,
    SdkPermissionError,
    PermissionError,      # alias for SdkPermissionError (backwards compat)
    ValidationError,
    KvQuotaError,
    RpcTimeoutError,
    TimeoutError,         # alias for RpcTimeoutError (backwards compat)
)
from ._components import (
    ActionRow,
    Button,
    SelectMenu,
    SelectOption,
    TextInput,
)

__all__ = [
    "Plugin",
    "Context",
    "SdkError",
    "CapabilityError",
    "RateLimitError",
    "DiscordApiError",
    "SdkPermissionError",
    "PermissionError",       # alias
    "ValidationError",
    "KvQuotaError",
    "RpcTimeoutError",
    "TimeoutError",          # alias
    "ActionRow",
    "Button",
    "SelectMenu",
    "SelectOption",
    "TextInput",
]
__version__ = "0.6.0"
