# MMO Maid SDK

SDK for building plugins for the [MMO Maid](https://mmomaid.com) Discord bot platform.

MMO Maid runs marketplace plugins in sandboxed Docker containers. This SDK is the
official Python interface plugins use to receive Discord events, call the Discord
API, store data, render dashboards, and emit metrics — all routed through the
platform so plugins never need direct network access or credentials.

## Install

```bash
pip install mmo-maid-sdk
```

Python 3.10 or newer.

## Hello, plugin

```python
from mmo_maid_sdk import Plugin, Context

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
via the [Developer Portal](https://mmomaid.com/dev).

## CLI

The package installs an `mmo` command for scaffolding and a local dev loop:

```bash
mmo new my_plugin     # scaffold a new plugin from the template
mmo dev               # run your plugin locally against a mock host
```

## Documentation

Full plugin contract, capability reference, and publishing guide:

- **Docs:** https://mmomaid.com/dev/docs
- **Developer Portal:** https://mmomaid.com/dev

## License

MIT — see [LICENSE](LICENSE).
