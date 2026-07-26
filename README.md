# YourBot SDK

SDK for building plugins for the [YourBot](https://yourbot.gg) Discord bot platform.

YourBot runs marketplace plugins in sandboxed Docker containers. This SDK is the
official Python interface plugins use to receive Discord events, call the Discord
API, store data, render dashboards, and emit metrics — all routed through the
platform so plugins never need direct network access or credentials.

## Install

```bash
pip install yourbot-sdk
```

Python 3.10 or newer.

## Hello, plugin

```python
from yourbot_sdk import Plugin, Context

plugin = Plugin()

@plugin.on_event("message_create")
def on_message(ctx: Context, event: dict):
    if "!ping" in event.get("content", ""):
        ctx.discord.send_message(
            channel_id=event["channel_id"],
            content="Pong!",
        )

plugin.run()  # must be the last line
```

Drop this in a folder named `my_plugin/` as `__main__.py`, zip it, and upload it
via the [Developer Portal](https://yourbot.gg/dev).

## CLI

The package installs a `yourbot` command for scaffolding and a local dev loop:

```bash
yourbot new my_plugin     # scaffold a new plugin from the template
yourbot dev               # run your plugin locally against a mock host
```

## Documentation

Full plugin contract, capability reference, and publishing guide:

- **Docs:** https://yourbot.gg/docs/developers
- **Developer Portal:** https://yourbot.gg/dev (sign-in required)

## License

MIT — see [LICENSE](LICENSE).
