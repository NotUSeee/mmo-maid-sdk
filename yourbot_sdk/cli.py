"""yourbot-sdk CLI — scaffold and locally test plugins.

Two subcommands:

  yourbot new <plugin-id>   Scaffold a new plugin directory with __main__.py,
                        manifest, README, .gitignore, requirements.txt,
                        events.yaml (sample test events), and tests/.

  yourbot dev               Run the plugin in the current directory against a
                        local mock host. Auto-fires events from events.yaml
                        once on start, then watches for file changes and
                        reloads. Prints every action your plugin took
                        (messages sent, KV writes, log lines).

Both subcommands work without ever talking to the platform — the dev loop
is pure local Python so iteration takes seconds, not the minutes of
"push, pull, dev-install, test, repeat".
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import re
import sys
import textwrap
import time
from pathlib import Path
from typing import Any


# ── Scaffold templates ──────────────────────────────────────────────────────
# Each template is plain text written to the new plugin directory.
# `{name}` is replaced with the human-readable plugin name (derived from
# the plugin_id by titling/replacing underscores).

_TEMPLATE_MAIN_PY = '''\
"""
{name}
{underline}

This is the entry point for your plugin. The SDK handles all communication
with the YourBot runner automatically.

Local development:
    pip install -e ../yourbot-sdk        # if you cloned the SDK
    pip install yourbot-sdk              # from PyPI
    yourbot dev                                # run against a mock host

Docs: https://yourbot.gg/dev/docs
"""
from yourbot_sdk import Plugin, Context
from yourbot_sdk.events import MessageCreate

plugin = Plugin()


@plugin.on_ready
def on_ready(ctx: Context):
    """Called once when your plugin boots."""
    ctx.log("{name} is ready!")


@plugin.on_event("message_create")
def on_message(ctx: Context, event: MessageCreate):
    """Reply to !ping with Pong!. Replace this with your own logic.

    The `MessageCreate` type hint gives you IDE autocomplete on the event
    fields (event.content, event.channel_id, etc.). It's a TypedDict, so
    you can still `event.get(...)` for safety.
    """
    content = event.get("content", "")
    if content.strip() == "!ping":
        ctx.discord.send_message(
            channel_id=event["channel_id"],
            content="Pong!",
        )


if __name__ == "__main__":
    plugin.run()
'''

_TEMPLATE_MANIFEST_JSON = '''\
{{
  "id": "{plugin_id}",
  "name": "{name}",
  "version": "0.1.0",
  "description": "A new YourBot plugin.",
  "capabilities_required": [
    "discord:send_message",
    "events:message_content"
  ]
}}
'''

_TEMPLATE_README_MD = '''\
# {name}

A plugin for [YourBot](https://yourbot.gg).

## Quick start

```bash
pip install yourbot-sdk
yourbot dev                # run locally against a mock host
```

Edit `__main__.py`, save, and `yourbot dev` reloads automatically.

## Capabilities

This plugin requests:

- `discord:send_message` — send messages to channels
- `events:message_content` — read message text (privacy-sensitive)

Update `manifest.json` and re-submit if you add or remove capabilities.

## Publishing

1. Push this directory to a GitHub repo.
2. In the [YourBot dev portal](https://yourbot.gg/dev), create a new plugin
   and link the repo.
3. Pull a version, confirm the auto-detected capabilities, and submit for review.
'''

_TEMPLATE_GITIGNORE = '''\
__pycache__/
*.py[cod]
.venv/
venv/
.pytest_cache/
.mypy_cache/
*.egg-info/
build/
dist/
.DS_Store
'''

_TEMPLATE_REQUIREMENTS = '''\
yourbot-sdk>=0.5.0
'''

_TEMPLATE_EVENTS_YAML = '''\
# Test events for `yourbot dev` to fire on start.
# Each entry is one event delivered to your plugin's matching @plugin.on_event handler.
# `yourbot dev` fires these top-to-bottom on launch and reload.

- type: message_create
  channel_id: "111111111111111111"
  user_id: "222222222222222222"
  username: testuser
  content: "!ping"
  guild_id: "333333333333333333"

- type: message_create
  channel_id: "111111111111111111"
  user_id: "222222222222222222"
  username: testuser
  content: "hello world"
  guild_id: "333333333333333333"
'''

_TEMPLATE_TEST_PY = '''\
"""Unit tests for the plugin handlers — run with: python -m pytest"""
from yourbot_sdk.testing import MockContext, make_event
from plugin_main import on_message


def test_ping_replies_pong():
    ctx = MockContext()
    event = make_event("message_create", content="!ping", channel_id="999")
    on_message(ctx, event)
    assert len(ctx.messages_sent) == 1
    assert ctx.messages_sent[0]["channel_id"] == "999"
    assert ctx.messages_sent[0]["content"] == "Pong!"


def test_other_messages_ignored():
    ctx = MockContext()
    on_message(ctx, make_event("message_create", content="hello"))
    assert ctx.messages_sent == []
'''


# ── Slash-command starter ────────────────────────────────────────────────────

_TEMPLATE_SLASH_MAIN = '''\
"""
{name}
{underline}

A slash-command plugin starter. Registers `/hello` which replies with a
personalised greeting. Extend by adding more @plugin.on_slash_command handlers.
"""
from yourbot_sdk import Plugin, Context

plugin = Plugin()


@plugin.on_ready
def on_ready(ctx: Context):
    ctx.log("{name} ready - /hello is registered")


@plugin.on_slash_command("hello")
def cmd_hello(ctx: Context, event: dict):
    user = event.get("user_name") or "there"
    ctx.interaction.respond(content=f"Hi, {{user}}!", ephemeral=True)


if __name__ == "__main__":
    plugin.run()
'''

_TEMPLATE_SLASH_MANIFEST = '''\
{{
  "id": "{plugin_id}",
  "name": "{name}",
  "version": "0.1.0",
  "description": "Slash-command starter for YourBot.",
  "capabilities_required": [
    "interaction:respond"
  ],
  "slash_commands": [
    {{
      "name": "hello",
      "description": "Get a friendly hello from the bot."
    }}
  ]
}}
'''

_TEMPLATE_SLASH_EVENTS = '''\
- type: interaction_create
  interaction_id: "10000000000000001"
  interaction_type: 2
  command_name: hello
  user_id: "222222222222222222"
  user_name: testuser
  channel_id: "111111111111111111"
  guild_id: "333333333333333333"
'''

_TEMPLATE_SLASH_TEST = '''\
"""Tests for the /hello slash command."""
from yourbot_sdk.testing import MockContext, make_event
from plugin_main import cmd_hello


def test_hello_replies_personalised():
    ctx = MockContext()
    event = make_event("interaction_create",
                       interaction_type=2, command_name="hello",
                       user_name="alice")
    cmd_hello(ctx, event)
    assert ctx.interaction.responses
    last = ctx.interaction.responses[-1]
    assert "alice" in last["content"]
    assert last.get("ephemeral") is True
'''


# ── Welcome-greeter starter ─────────────────────────────────────────────────

_TEMPLATE_WELCOME_MAIN = '''\
"""
{name}
{underline}

Greets new members in a designated welcome channel. Channel ID is stored
per-server in plugin KV so different servers can configure different channels.

To set the welcome channel for a server, an admin runs `/welcome-channel`
inside that server (see the slash command below).
"""
from yourbot_sdk import Plugin, Context
from yourbot_sdk.events import MemberJoin

plugin = Plugin()


@plugin.on_event("member_join")
def on_member_join(ctx: Context, event: MemberJoin):
    channel_id = ctx.kv.get("welcome_channel_id")
    if not channel_id:
        return  # admin hasn't configured a channel yet
    user_name = event.get("display_name") or event.get("username") or "friend"
    ctx.discord.send_message(
        channel_id=str(channel_id),
        content=f"Welcome to the server, **{{user_name}}**! :wave:",
    )


@plugin.on_slash_command("welcome-channel")
def cmd_set_channel(ctx: Context, event: dict):
    """Admin sets the welcome channel by running /welcome-channel in it."""
    channel_id = event.get("channel_id")
    if not channel_id:
        ctx.interaction.respond(content="Could not detect channel.", ephemeral=True)
        return
    ctx.kv.set("welcome_channel_id", str(channel_id))
    ctx.interaction.respond(
        content=f"Got it - I'll greet new members in <#{{channel_id}}>.",
        ephemeral=True,
    )


if __name__ == "__main__":
    plugin.run()
'''

_TEMPLATE_WELCOME_MANIFEST = '''\
{{
  "id": "{plugin_id}",
  "name": "{name}",
  "version": "0.1.0",
  "description": "Greets new members in a configurable channel.",
  "capabilities_required": [
    "discord:send_message",
    "interaction:respond",
    "events:member_join",
    "storage:kv"
  ],
  "slash_commands": [
    {{
      "name": "welcome-channel",
      "description": "Set the current channel as the welcome channel for new members."
    }}
  ]
}}
'''

_TEMPLATE_WELCOME_EVENTS = '''\
- type: member_join
  user_id: "555555555555555555"
  username: newuser
  display_name: New User
  guild_id: "333333333333333333"

- type: interaction_create
  interaction_id: "10000000000000002"
  interaction_type: 2
  command_name: welcome-channel
  user_id: "222222222222222222"
  user_name: admin
  channel_id: "777777777777777777"
  guild_id: "333333333333333333"
'''

_TEMPLATE_WELCOME_TEST = '''\
"""Tests for the welcome greeter."""
from yourbot_sdk.testing import MockContext, make_event
from plugin_main import on_member_join, cmd_set_channel


def test_no_greet_until_channel_configured():
    ctx = MockContext()
    on_member_join(ctx, make_event("member_join", display_name="Alice"))
    assert ctx.messages_sent == []


def test_greet_once_channel_configured():
    ctx = MockContext()
    cmd_set_channel(ctx, make_event("interaction_create",
                                     interaction_type=2,
                                     command_name="welcome-channel",
                                     channel_id="111"))
    on_member_join(ctx, make_event("member_join", display_name="Alice"))
    assert len(ctx.messages_sent) == 1
    assert ctx.messages_sent[0]["channel_id"] == "111"
    assert "Alice" in ctx.messages_sent[0]["content"]
'''


# ── Cron-task starter ───────────────────────────────────────────────────────

_TEMPLATE_CRON_MAIN = '''\
"""
{name}
{underline}

Demonstrates @plugin.cron scheduled tasks. Posts a daily summary in a
configured channel and increments a counter every 15 minutes.

Cron specs are 5-field standard: "minute hour day month dow", UTC.
See SDK docs for the full syntax.
"""
from yourbot_sdk import Plugin, Context

plugin = Plugin()


@plugin.on_ready
def on_ready(ctx: Context):
    ctx.log("{name} ready - cron tasks will fire on schedule")


@plugin.cron("0 9 * * *")          # every day at 09:00 UTC
def daily_summary(ctx: Context):
    """Send a summary message at 9am UTC every day."""
    channel_id = ctx.kv.get("summary_channel_id")
    if not channel_id:
        ctx.log("daily_summary: no channel configured, skipping", level="info")
        return
    ctx.discord.send_message(
        channel_id=str(channel_id),
        content="Good morning! :sunrise: Daily summary coming through.",
    )


@plugin.cron("*/15 * * * *")        # every 15 minutes
def heartbeat(ctx: Context):
    """Tick a counter so we can prove the scheduler ran."""
    n = int(ctx.kv.get("heartbeat_count") or 0) + 1
    ctx.kv.set("heartbeat_count", n)
    ctx.log(f"heartbeat tick #{{n}}", level="debug")


@plugin.on_slash_command("summary-channel")
def cmd_set_channel(ctx: Context, event: dict):
    channel_id = event.get("channel_id")
    if not channel_id:
        ctx.interaction.respond(content="Could not detect channel.", ephemeral=True)
        return
    ctx.kv.set("summary_channel_id", str(channel_id))
    ctx.interaction.respond(
        content=f"Daily summary will post to <#{{channel_id}}>.", ephemeral=True,
    )


if __name__ == "__main__":
    plugin.run()
'''

_TEMPLATE_CRON_MANIFEST = '''\
{{
  "id": "{plugin_id}",
  "name": "{name}",
  "version": "0.1.0",
  "description": "Cron-scheduled tasks (daily summary + 15-min heartbeat).",
  "capabilities_required": [
    "discord:send_message",
    "interaction:respond",
    "storage:kv"
  ],
  "slash_commands": [
    {{
      "name": "summary-channel",
      "description": "Set the channel where the daily summary will post."
    }}
  ]
}}
'''

_TEMPLATE_CRON_EVENTS = '''\
# `yourbot dev` does NOT fire cron tasks - they run on real wall-clock time.
# This file lets you exercise the slash command in the local loop.

- type: interaction_create
  interaction_id: "10000000000000003"
  interaction_type: 2
  command_name: summary-channel
  channel_id: "111111111111111111"
  user_id: "222222222222222222"
  user_name: admin
  guild_id: "333333333333333333"
'''

_TEMPLATE_CRON_TEST = '''\
"""Tests for the cron starter handlers."""
from yourbot_sdk.testing import MockContext, make_event
from plugin_main import daily_summary, heartbeat


def test_summary_skips_with_no_channel():
    ctx = MockContext()
    daily_summary(ctx)
    assert ctx.messages_sent == []


def test_summary_posts_to_configured_channel():
    ctx = MockContext()
    ctx.kv.set("summary_channel_id", "111")
    daily_summary(ctx)
    assert len(ctx.messages_sent) == 1
    assert ctx.messages_sent[0]["channel_id"] == "111"


def test_heartbeat_increments_counter():
    ctx = MockContext()
    heartbeat(ctx)
    heartbeat(ctx)
    heartbeat(ctx)
    assert int(ctx.kv.get("heartbeat_count") or 0) == 3
'''


# ── Shared test conftest ─────────────────────────────────────────────────────
# All templates use the same conftest — loads __main__.py by file path and
# exposes it as ``plugin_main`` so tests don't have to fight pytest's own
# __main__ resolution. Templates' tests/test_plugin.py do
# ``from plugin_main import ...``.

_TEMPLATE_CONFTEST = '''\
"""Test bootstrap — loads __main__.py as a module named `plugin_main` so
tests can import the plugin's handlers without colliding with pytest's
own __main__ resolution.
"""
import importlib.util
import sys
from pathlib import Path

_main_py = Path(__file__).resolve().parent.parent / "__main__.py"
spec = importlib.util.spec_from_file_location("plugin_main", _main_py)
plugin_main = importlib.util.module_from_spec(spec)
sys.modules["plugin_main"] = plugin_main
spec.loader.exec_module(plugin_main)
'''


# ── Template registry ───────────────────────────────────────────────────────

TEMPLATES: dict = {
    "basic": {
        "summary": "Minimal `!ping`-replies-`Pong!` example. Good for poking around.",
        "main": _TEMPLATE_MAIN_PY,
        "manifest": _TEMPLATE_MANIFEST_JSON,
        "events": _TEMPLATE_EVENTS_YAML,
        "test": _TEMPLATE_TEST_PY,
    },
    "slash": {
        "summary": "Slash-command bot - registers `/hello` with an ephemeral reply.",
        "main": _TEMPLATE_SLASH_MAIN,
        "manifest": _TEMPLATE_SLASH_MANIFEST,
        "events": _TEMPLATE_SLASH_EVENTS,
        "test": _TEMPLATE_SLASH_TEST,
    },
    "welcome": {
        "summary": "Greeter for new members; channel configurable per-server via `/welcome-channel`.",
        "main": _TEMPLATE_WELCOME_MAIN,
        "manifest": _TEMPLATE_WELCOME_MANIFEST,
        "events": _TEMPLATE_WELCOME_EVENTS,
        "test": _TEMPLATE_WELCOME_TEST,
    },
    "cron": {
        "summary": "Scheduled tasks demo - daily 09:00 UTC summary + 15-min heartbeat counter.",
        "main": _TEMPLATE_CRON_MAIN,
        "manifest": _TEMPLATE_CRON_MANIFEST,
        "events": _TEMPLATE_CRON_EVENTS,
        "test": _TEMPLATE_CRON_TEST,
    },
}


# ── `yourbot new` ──────────────────────────────────────────────────────────────

_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")


def _print_template_catalog() -> None:
    print("Available templates:")
    print()
    width = max(len(k) for k in TEMPLATES.keys())
    for key in sorted(TEMPLATES.keys()):
        summary = TEMPLATES[key]["summary"]
        print(f"  {key:<{width}}  {summary}")
    print()
    print("Use:  yourbot new <plugin_id> --template <name>")


def cmd_new(args: argparse.Namespace) -> int:
    """Scaffold a new plugin directory."""
    if getattr(args, "list_templates", False):
        _print_template_catalog()
        return 0

    if not args.plugin_id:
        print("error: plugin_id is required (or pass --list-templates).", file=sys.stderr)
        return 2

    plugin_id = args.plugin_id.strip().lower()
    if not _PLUGIN_ID_RE.match(plugin_id):
        print(
            f"error: plugin id {plugin_id!r} is invalid.\n"
            "  must be 3-32 chars, lowercase letters/digits/underscores, starting with a letter.",
            file=sys.stderr,
        )
        return 2

    template_key = (getattr(args, "template", None) or "basic").strip().lower()
    if template_key not in TEMPLATES:
        print(
            f"error: unknown template {template_key!r}. Run `yourbot new --list-templates` to see options.",
            file=sys.stderr,
        )
        return 2
    tpl = TEMPLATES[template_key]

    target = Path(args.target or plugin_id).resolve()
    if target.exists() and any(target.iterdir()):
        print(f"error: target directory {target} is not empty.", file=sys.stderr)
        return 2
    target.mkdir(parents=True, exist_ok=True)

    name = plugin_id.replace("_", " ").title()
    files = {
        "__main__.py":     tpl["main"].format(name=name, underline="=" * len(name)),
        "manifest.json":   tpl["manifest"].format(plugin_id=plugin_id, name=name),
        "README.md":       _TEMPLATE_README_MD.format(name=name),
        ".gitignore":      _TEMPLATE_GITIGNORE,
        "requirements.txt": _TEMPLATE_REQUIREMENTS,
        "events.yaml":     tpl["events"],
    }
    for relpath, content in files.items():
        (target / relpath).write_text(content, encoding="utf-8")
    tests_dir = target / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_plugin.py").write_text(tpl["test"], encoding="utf-8")
    (tests_dir / "conftest.py").write_text(_TEMPLATE_CONFTEST, encoding="utf-8")

    print(f"[ok] Scaffolded {plugin_id} ({template_key} template) in {target}")
    print()
    print("Next steps:")
    print(f"  cd {target.relative_to(Path.cwd()) if target.is_relative_to(Path.cwd()) else target}")
    print("  pip install -r requirements.txt")
    print("  yourbot dev                  # local test loop with hot-reload")
    print("  python -m pytest         # unit tests via yourbot_sdk.testing")
    return 0


# ── `yourbot init` (interactive wizard) ─────────────────────────────────────────


def _prompt(prompt_text: str, *, default: str = "", required: bool = False) -> str:
    """Prompt the user for input. Returns the trimmed answer or the default.

    Re-prompts if the answer is empty and ``required`` is True.
    """
    while True:
        suffix = f" [{default}]" if default else ""
        try:
            raw = input(f"{prompt_text}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[yourbot init] cancelled.", file=sys.stderr)
            sys.exit(1)
        value = raw or default
        if value or not required:
            return value
        print("  this value is required.")


def _prompt_yes_no(prompt_text: str, *, default: bool = False) -> bool:
    """Yes/no prompt; falls back to ``default`` on empty input."""
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            raw = input(f"{prompt_text} {suffix}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[yourbot init] cancelled.", file=sys.stderr)
            sys.exit(1)
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  please answer y or n.")


def _prompt_template() -> str:
    """Show the template catalog and prompt the user to pick one."""
    keys = sorted(TEMPLATES.keys())
    print()
    print("Templates:")
    width = max(len(k) for k in keys)
    for i, k in enumerate(keys, start=1):
        print(f"  {i}. {k:<{width}}  {TEMPLATES[k]['summary']}")
    print()
    while True:
        choice = _prompt("Choose a template (number or name)", default="basic")
        if choice in TEMPLATES:
            return choice
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(keys):
                return keys[idx]
        print(f"  unknown template {choice!r}.")


def _open_in_editor_if_possible(path: Path) -> bool:
    """Try to open the scaffolded project in VS Code if it's installed.

    Returns True if the editor was launched, False otherwise.
    """
    import shutil as _shutil
    import subprocess as _subprocess
    code_bin = _shutil.which("code")
    if not code_bin:
        return False
    try:
        # `code <path>` opens VS Code with that folder as the workspace.
        # Run detached so it doesn't block our terminal.
        if sys.platform == "win32":
            _subprocess.Popen([code_bin, str(path)], creationflags=0x00000008)  # DETACHED_PROCESS
        else:
            _subprocess.Popen([code_bin, str(path)], start_new_session=True)
        return True
    except Exception:
        return False


def cmd_init(args: argparse.Namespace) -> int:
    """Interactive wizard for starting a new plugin.

    Equivalent to ``yourbot new`` with all flags prompted for. Designed for
    people who just installed the SDK and want a first plugin scaffold
    without learning the flag syntax.
    """
    print("[yourbot init] Let's create your first plugin.")
    print()

    # Plugin id — must match the platform regex. Default = sanitized cwd name.
    cwd_name = Path.cwd().name.lower()
    suggested_id = ""
    if _PLUGIN_ID_RE.match(cwd_name):
        suggested_id = cwd_name
    while True:
        plugin_id = _prompt(
            "Plugin id (lowercase, 3-32 chars, letters/digits/underscores, starts with a letter)",
            default=suggested_id,
            required=True,
        ).strip().lower()
        if _PLUGIN_ID_RE.match(plugin_id):
            break
        print(f"  invalid plugin id {plugin_id!r}. Must be 3-32 chars, lowercase letters/digits/underscores, starts with a letter.")

    # Template — defaults to basic; shows the catalog.
    template_key = _prompt_template()

    # Target directory — defaults to ./plugin_id, but allow current dir.
    target_default = plugin_id
    raw_target = _prompt("Target directory", default=target_default)
    target = Path(raw_target).resolve()

    if target.exists() and any(target.iterdir()):
        print(f"error: target directory {target} is not empty.", file=sys.stderr)
        return 2

    # Optional: open in VS Code if available.
    import shutil as _shutil
    has_code = _shutil.which("code") is not None
    open_editor = False
    if has_code:
        open_editor = _prompt_yes_no("Open in VS Code when done?", default=True)

    # Delegate to cmd_new by constructing the equivalent argparse Namespace.
    new_args = argparse.Namespace(
        plugin_id=plugin_id,
        template=template_key,
        target=str(target),
        list_templates=False,
    )
    rc = cmd_new(new_args)
    if rc != 0:
        return rc

    if open_editor:
        opened = _open_in_editor_if_possible(target)
        if opened:
            print()
            print(f"[ok] opened {target} in VS Code")
        else:
            print()
            print(f"[note] couldn't launch VS Code automatically. Open {target} manually.")

    return 0


# ── `yourbot dev` ──────────────────────────────────────────────────────────────

def _load_plugin_module(plugin_dir: Path) -> Any:
    """Import the plugin's __main__.py and return the loaded module.

    Each call gets a fresh module instance so handler decorators run again —
    enables hot-reload by re-importing after file changes.
    """
    main_py = plugin_dir / "__main__.py"
    if not main_py.exists():
        raise FileNotFoundError(f"no __main__.py in {plugin_dir}")
    # Drop any previously-loaded version + its cached children
    for mod_name in list(sys.modules):
        if mod_name == "__main__" or mod_name.startswith("__main__."):
            del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location("__main__", main_py)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build import spec for {main_py}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["__main__"] = module
    spec.loader.exec_module(module)
    return module


def _print_actions(ctx: Any, label: str) -> None:
    """Print every action the plugin took during one event delivery."""
    msgs = getattr(ctx, "messages_sent", []) or []
    logs = getattr(ctx, "log_lines", []) or []
    kv_writes = getattr(ctx, "kv_writes", []) or []  # MockContext may not track these

    print(f"  + {label}: ", end="")
    parts = []
    if msgs:
        parts.append(f"{len(msgs)} message{'' if len(msgs) == 1 else 's'} sent")
    if logs:
        parts.append(f"{len(logs)} log line{'' if len(logs) == 1 else 's'}")
    if kv_writes:
        parts.append(f"{len(kv_writes)} kv write{'' if len(kv_writes) == 1 else 's'}")
    if not parts:
        parts.append("no actions")
    print(" / ".join(parts))

    for m in msgs:
        chan = m.get("channel_id", "?")
        body = m.get("content", "")
        if len(body) > 70:
            body = body[:70] + "..."
        print(f"     -> send_message(channel={chan}): {body!r}")
    for line in logs:
        print(f"     * log: {line}")


def _load_events_yaml(plugin_dir: Path) -> list:
    """Read events.yaml. If yaml isn't installed, fall back to a tiny parser
    that handles the simple format we ship by default."""
    events_path = plugin_dir / "events.yaml"
    if not events_path.exists():
        return []
    raw = events_path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(raw) or []
        if isinstance(data, list):
            return data
        return []
    except ImportError:
        # Minimal fallback parser: handles `- key: value` blocks separated by
        # `- ` lines. Sufficient for the scaffolded events.yaml; for anything
        # fancier, the dev should `pip install pyyaml`.
        return _tiny_yaml_list(raw)


def _tiny_yaml_list(text: str) -> list:
    """Very small YAML subset parser — list of dicts only, no nested structures.

    Stops at any line it can't handle and returns what it has. Real YAML is
    available via pyyaml when the dev needs anything beyond key: value pairs.
    """
    items: list = []
    cur: dict | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("- "):
            if cur is not None:
                items.append(cur)
            cur = {}
            tail = line[2:].strip()
            if ":" in tail:
                k, _, v = tail.partition(":")
                cur[k.strip()] = _yaml_scalar(v.strip())
            continue
        if cur is None:
            continue
        if line.startswith("  ") and ":" in line:
            k, _, v = line.lstrip().partition(":")
            cur[k.strip()] = _yaml_scalar(v.strip())
    if cur is not None:
        items.append(cur)
    return items


def _yaml_scalar(s: str) -> Any:
    """Parse a YAML scalar — strips quotes, recognises bare ints / true/false."""
    s = s.strip()
    if not s:
        return ""
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low == "null":
        return None
    if s.lstrip("-").isdigit():
        try: return int(s)
        except Exception: pass
    return s


def _file_signature(plugin_dir: Path) -> tuple:
    """Tuple of (path, mtime) for every .py file under plugin_dir.

    Used as a cheap change-detector for the watch loop — a tuple inequality
    means "something changed, reload".
    """
    sig = []
    for p in plugin_dir.rglob("*.py"):
        if any(part in {"__pycache__", ".venv", "venv", ".git"} for part in p.parts):
            continue
        try:
            sig.append((str(p.relative_to(plugin_dir)), p.stat().st_mtime))
        except OSError:
            pass
    return tuple(sorted(sig))


def cmd_dev(args: argparse.Namespace) -> int:
    """Run the plugin against a mock host with optional file-watching."""
    plugin_dir = Path(args.path or ".").resolve()
    if not (plugin_dir / "__main__.py").exists():
        print(f"error: no __main__.py in {plugin_dir}.", file=sys.stderr)
        print("       run `yourbot new <plugin-id>` to scaffold a new plugin.", file=sys.stderr)
        return 2

    # Make `from yourbot_sdk.testing import MockContext` work
    from yourbot_sdk.testing import MockContext, make_event

    def fire_all() -> None:
        print(f"\n[yourbot dev] loading {plugin_dir / '__main__.py'}")
        try:
            mod = _load_plugin_module(plugin_dir)
        except Exception as exc:
            print(f"  [FAIL] import failed: {type(exc).__name__}: {exc}")
            return
        plugin_obj = getattr(mod, "plugin", None)
        if plugin_obj is None:
            print("  [FAIL] no `plugin = Plugin()` found at module level.")
            return

        # on_ready
        if getattr(plugin_obj, "_on_ready_handlers", None) or hasattr(plugin_obj, "_ready_handlers"):
            ctx = MockContext()
            try:
                # Try the most likely API shapes — versions of the SDK have
                # used both `_on_ready_handlers` and `_ready_handlers`.
                handlers = (
                    getattr(plugin_obj, "_on_ready_handlers", None)
                    or getattr(plugin_obj, "_ready_handlers", None)
                    or []
                )
                for h in handlers:
                    h(ctx)
                _print_actions(ctx, "on_ready")
            except Exception as exc:
                print(f"  [FAIL] on_ready raised: {type(exc).__name__}: {exc}")

        # events.yaml
        events = _load_events_yaml(plugin_dir)
        if not events:
            print("  * no events.yaml (or empty) - nothing to fire. add events.yaml to test more.")
            return
        print(f"  * firing {len(events)} event(s) from events.yaml...")
        for i, evt in enumerate(events, 1):
            etype = evt.get("type") or evt.get("event_type") or "unknown"
            ctx = MockContext()
            try:
                event = make_event(etype, **{k: v for k, v in evt.items() if k != "type"})
                # Find the matching handler (SDK API may differ)
                handlers = _resolve_event_handlers(plugin_obj, etype)
                if not handlers:
                    print(f"  [{i}] {etype} - no handler registered, skipping")
                    continue
                for h in handlers:
                    h(ctx, event)
                _print_actions(ctx, f"[{i}] {etype}")
            except Exception as exc:
                print(f"  [{i}] {etype} [FAIL] {type(exc).__name__}: {exc}")

    # First fire
    fire_all()

    if not args.watch:
        print("\n[yourbot dev] done. add --watch to reload on file changes.")
        return 0

    print(f"\n[yourbot dev] watching {plugin_dir} (Ctrl+C to stop)…")
    last_sig = _file_signature(plugin_dir)
    try:
        while True:
            time.sleep(0.5)
            sig = _file_signature(plugin_dir)
            if sig != last_sig:
                last_sig = sig
                print("\n[yourbot dev] change detected — reloading…")
                fire_all()
    except KeyboardInterrupt:
        print("\n[yourbot dev] stopped.")
        return 0


def _resolve_event_handlers(plugin_obj: Any, event_type: str) -> list:
    """Find registered handlers for an event type, tolerating SDK API drift."""
    # Try common shapes in order of likelihood.
    by_type = getattr(plugin_obj, "_event_handlers", None)
    if isinstance(by_type, dict):
        return list(by_type.get(event_type, []) or [])
    by_type = getattr(plugin_obj, "_on_event_handlers", None)
    if isinstance(by_type, dict):
        return list(by_type.get(event_type, []) or [])
    handlers = getattr(plugin_obj, "_handlers", None)
    if isinstance(handlers, dict):
        return list(handlers.get(event_type, []) or [])
    return []


# ── validate ────────────────────────────────────────────────────────────────

# Paths to exclude when zipping a plugin directory for validation. Mirrors the
# server-side artifact_store behavior so what `yourbot validate` checks is what
# the platform will check.
_VALIDATE_EXCLUDE_DIRS = {
    ".git", ".github", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "node_modules", "dist", "build", "tests", "test",
}
_VALIDATE_EXCLUDE_NAMES = {".DS_Store", "Thumbs.db"}
_VALIDATE_EXCLUDE_SUFFIXES = (".pyc", ".pyo")


def _zip_plugin_dir(plugin_dir: Path) -> bytes:
    """Zip a plugin directory in-memory, excluding dev junk. Returns zip bytes."""
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(plugin_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(plugin_dir)
            parts = rel.parts
            if any(p in _VALIDATE_EXCLUDE_DIRS for p in parts):
                continue
            if rel.name in _VALIDATE_EXCLUDE_NAMES:
                continue
            if rel.suffix in _VALIDATE_EXCLUDE_SUFFIXES:
                continue
            try:
                zf.write(path, rel.as_posix())
            except OSError:
                continue
    return buf.getvalue()


def cmd_validate(args: argparse.Namespace) -> int:
    """Run the same pre-submit validation the platform runs, locally.

    Mirrors what `dev_version_submit_for_review` does on the server side.
    Exits 0 if clean, 1 if any errors, 2 if the directory looks wrong.
    Warnings never fail the command — only errors do.
    """
    plugin_dir = Path(args.path or ".").resolve()
    if not plugin_dir.is_dir():
        print(f"error: {plugin_dir} is not a directory.", file=sys.stderr)
        return 2
    if not (plugin_dir / "__main__.py").exists():
        print(f"error: no __main__.py in {plugin_dir}.", file=sys.stderr)
        print("       run `yourbot new <plugin-id>` to scaffold a new plugin, or use --path.", file=sys.stderr)
        return 2

    # Read manifest.json to default the plugin_id/version to whatever is in it
    # — the validator will catch mismatches between that and platform values.
    manifest_path = plugin_dir / "manifest.json"
    manifest_plugin_id = ""
    manifest_version = ""
    if manifest_path.exists():
        try:
            with open(manifest_path, "r", encoding="utf-8") as fh:
                _m = json.load(fh)
            if isinstance(_m, dict):
                manifest_plugin_id = str(_m.get("id") or "").strip()
                manifest_version = str(_m.get("version") or "").strip()
        except Exception:
            # The validator will surface a useful "manifest invalid" error
            # — don't fail here.
            pass

    plugin_id = args.id or manifest_plugin_id or plugin_dir.name
    version = args.version or manifest_version or "0.0.0"

    # Zip the directory in-memory and run the validator.
    try:
        artifact_bytes = _zip_plugin_dir(plugin_dir)
    except Exception as exc:
        print(f"error: failed to zip {plugin_dir}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    from ._validation import validate_artifact
    result = validate_artifact(
        plugin_id=plugin_id,
        version=version,
        artifact_bytes=artifact_bytes,
    )

    # Format findings — mimics how the platform displays them on the version row.
    errors = result.errors
    warnings = result.warnings

    def _fmt(f) -> str:
        loc = f"{f.path}:{f.line}" if (f.path and f.line) else (f.path or "")
        prefix = f"[{loc}] " if loc else ""
        body = f"  • {prefix}{f.message}"
        if f.hint:
            body += f"\n      hint: {f.hint}"
        return body

    if errors:
        print(f"\n{len(errors)} error(s):", file=sys.stderr)
        for f in errors:
            print(_fmt(f), file=sys.stderr)
    if warnings:
        print(f"\n{len(warnings)} warning(s):", file=sys.stderr)
        for f in warnings:
            print(_fmt(f), file=sys.stderr)

    if errors:
        print(f"\nFAILED: {len(errors)} error(s), {len(warnings)} warning(s). "
              f"Fix the errors and re-run.", file=sys.stderr)
        return 1

    if warnings:
        print(f"\nOK with {len(warnings)} warning(s). "
              f"Warnings don't block submission but should be fixed.", file=sys.stderr)
    else:
        print(f"\nOK — no issues found in {plugin_dir}.", file=sys.stderr)
    return 0


# ── yourbot doctor ──────────────────────────────────────────────────────────────


_DoctorCheck = tuple  # (status: 'ok' | 'warn' | 'error', short_label: str, detail: str | None)


def _doctor_status_glyph(status: str) -> str:
    return {"ok": "[OK]", "warn": "[!!]", "error": "[XX]"}.get(status, "[??]")


def _doctor_check_sdk_install() -> _DoctorCheck:
    """Is the SDK importable and is it a recognized version?"""
    try:
        import yourbot_sdk
        version = getattr(yourbot_sdk, "__version__", "unknown")
        return ("ok", "SDK installed", f"version {version}")
    except Exception as exc:
        return ("error", "SDK import failed",
                f"{type(exc).__name__}: {exc}. Run: pip install yourbot-sdk")


def _doctor_check_main_py(plugin_dir: Path) -> _DoctorCheck:
    """Does __main__.py exist and parse as valid Python?"""
    main_py = plugin_dir / "__main__.py"
    if not main_py.exists():
        return ("error", "__main__.py missing",
                f"Expected {main_py}. Run `yourbot new <plugin_id>` to scaffold.")
    try:
        import ast
        with open(main_py, "r", encoding="utf-8") as fh:
            data = fh.read()
        ast.parse(data, filename=str(main_py))
        return ("ok", "__main__.py parses", f"{len(data)} bytes")
    except SyntaxError as exc:
        return ("error", "__main__.py has SyntaxError",
                f"{exc.msg} at line {exc.lineno}. Fix the syntax before deploying.")
    except Exception as exc:
        return ("error", "__main__.py unreadable", f"{type(exc).__name__}: {exc}")


def _doctor_check_manifest(plugin_dir: Path) -> _DoctorCheck:
    """Does manifest.json exist and have the required fields?"""
    manifest_path = plugin_dir / "manifest.json"
    if not manifest_path.exists():
        return ("error", "manifest.json missing",
                f"Expected {manifest_path}. Required fields: id, name, version.")
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            m = json.load(fh)
    except json.JSONDecodeError as exc:
        return ("error", "manifest.json invalid JSON",
                f"{exc.msg} at line {exc.lineno}.")
    except Exception as exc:
        return ("error", "manifest.json unreadable", f"{type(exc).__name__}: {exc}")
    missing = [f for f in ("id", "name", "version") if not str(m.get(f) or "").strip()]
    if missing:
        return ("error", "manifest.json missing fields",
                f"Required: {', '.join(missing)}")
    return ("ok", "manifest.json valid",
            f"id={m.get('id')!r}, version={m.get('version')!r}")


def _doctor_check_capabilities(plugin_dir: Path) -> _DoctorCheck:
    """Are declared and detected capabilities consistent?"""
    main_py = plugin_dir / "__main__.py"
    manifest_path = plugin_dir / "manifest.json"
    if not main_py.exists() or not manifest_path.exists():
        return ("warn", "capabilities skipped", "needs __main__.py + manifest.json")
    try:
        from ._validation import _detect_capabilities, manifest_capabilities
        with open(main_py, "rb") as fh:
            src = fh.read()
        # Also scan other .py files at root
        sources = [src]
        for p in plugin_dir.glob("*.py"):
            if p.name == "__main__.py":
                continue
            try:
                with open(p, "rb") as fh:
                    sources.append(fh.read())
            except OSError:
                continue
        detected = set(_detect_capabilities(sources))
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        declared = set(manifest_capabilities(manifest))
        missing_in_manifest = detected - declared
        if missing_in_manifest:
            # The platform auto-adds these on upload, but a local heads-up is friendlier.
            return ("warn", "Some used caps not declared in manifest.json",
                    f"Detected {sorted(missing_in_manifest)} — the platform will auto-add these, but declaring upfront is cleaner.")
        unused = declared - detected - {"storage:kv", "storage:sql", "interaction:respond"}
        if unused:
            return ("warn", "Some declared caps appear unused",
                    f"Unused: {sorted(unused)}. Customers prefer plugins that ask for fewer permissions.")
        return ("ok", "Capabilities consistent",
                f"{len(declared)} declared, all match detected usage")
    except Exception as exc:
        return ("warn", "Capability check failed", f"{type(exc).__name__}: {exc}")


def _doctor_check_tests(plugin_dir: Path) -> _DoctorCheck:
    """Is there a tests/ directory with at least one test file?"""
    tests_dir = plugin_dir / "tests"
    if not tests_dir.is_dir():
        return ("warn", "No tests/ directory",
                "Add tests/test_plugin.py with MockContext. `yourbot new` scaffolds this.")
    test_files = list(tests_dir.glob("test_*.py")) + list(tests_dir.glob("*_test.py"))
    if not test_files:
        return ("warn", "tests/ has no test files",
                "Tests should be named test_*.py or *_test.py for pytest to collect.")
    return ("ok", f"{len(test_files)} test file(s)",
            ", ".join(p.name for p in test_files[:3])
            + (f" (+{len(test_files) - 3} more)" if len(test_files) > 3 else ""))


def _doctor_check_requirements(plugin_dir: Path) -> _DoctorCheck:
    """Does requirements.txt include yourbot-sdk?"""
    req_path = plugin_dir / "requirements.txt"
    if not req_path.exists():
        return ("warn", "requirements.txt missing",
                "Add `yourbot-sdk` so deploys install the SDK.")
    try:
        content = req_path.read_text(encoding="utf-8").lower()
    except OSError:
        return ("warn", "requirements.txt unreadable", "")
    # Accept the legacy mmo-maid-sdk / mmo_maid_sdk names too — older plugins
    # pin the pre-rename package and the platform still resolves it via the alias.
    if not any(s in content for s in ("yourbot-sdk", "yourbot_sdk", "mmo-maid-sdk", "mmo_maid_sdk")):
        return ("warn", "yourbot-sdk not in requirements.txt",
                "Add it so plugin runtime installs the SDK.")
    return ("ok", "requirements.txt OK", "yourbot-sdk pinned")


def _doctor_check_git_ignore(plugin_dir: Path) -> _DoctorCheck:
    """Is .gitignore sane? Catches __pycache__/.venv/etc."""
    gi = plugin_dir / ".gitignore"
    expected_lines = ("__pycache__", "*.pyc", ".venv", "venv")
    if not gi.exists():
        return ("warn", ".gitignore missing",
                "Recommended: ignore __pycache__/, *.pyc, .venv/, venv/.")
    try:
        content = gi.read_text(encoding="utf-8")
    except OSError:
        return ("warn", ".gitignore unreadable", "")
    missing = [pat for pat in expected_lines if pat not in content]
    if missing:
        return ("warn", ".gitignore missing common entries",
                f"Consider adding: {', '.join(missing)}")
    return ("ok", ".gitignore OK", "common patterns present")


def _doctor_check_dangerous_imports(plugin_dir: Path) -> _DoctorCheck:
    """Catch sandbox-forbidden imports locally so the dev doesn't hit them at deploy."""
    forbidden = {"subprocess", "ctypes", "socket", "multiprocessing"}
    py_files = list(plugin_dir.glob("**/*.py"))
    hits: list[tuple[str, int, str]] = []
    for p in py_files:
        parts = p.parts
        if any(part in (".venv", "venv", "tests", "test", "__pycache__") for part in parts):
            continue
        try:
            data = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for i, line in enumerate(data.splitlines(), start=1):
            stripped = line.lstrip()
            for mod in forbidden:
                if (stripped == f"import {mod}"
                        or stripped.startswith(f"import {mod} ")
                        or stripped.startswith(f"import {mod}.")
                        or stripped.startswith(f"from {mod} ")):
                    hits.append((str(p.relative_to(plugin_dir)), i, mod))
                    break
    if hits:
        sample = "; ".join(f"{path}:{lineno} ({mod})" for path, lineno, mod in hits[:3])
        more = f" (+{len(hits) - 3} more)" if len(hits) > 3 else ""
        return ("error", "Sandbox-forbidden imports detected",
                f"{sample}{more}. Remove these — they'll be blocked at runtime.")
    return ("ok", "No forbidden imports",
            f"Scanned {len(py_files)} .py file{'s' if len(py_files) != 1 else ''}")


def cmd_doctor(args: argparse.Namespace) -> int:
    """Run a battery of pre-flight checks against a plugin directory.

    Exits 0 if no errors found (warnings allowed), 1 if any error, 2 if the
    directory itself is wrong. Useful before pushing to GitHub or zipping for
    upload — catches the common preventable mistakes locally.
    """
    plugin_dir = Path(args.path or ".").resolve()
    if not plugin_dir.is_dir():
        print(f"error: {plugin_dir} is not a directory.", file=sys.stderr)
        return 2

    print(f"[yourbot doctor] checking {plugin_dir}\n")

    checks = [
        ("SDK install",          lambda: _doctor_check_sdk_install()),
        ("__main__.py",          lambda: _doctor_check_main_py(plugin_dir)),
        ("manifest.json",        lambda: _doctor_check_manifest(plugin_dir)),
        ("capabilities",         lambda: _doctor_check_capabilities(plugin_dir)),
        ("forbidden imports",    lambda: _doctor_check_dangerous_imports(plugin_dir)),
        ("requirements.txt",     lambda: _doctor_check_requirements(plugin_dir)),
        ("tests/",               lambda: _doctor_check_tests(plugin_dir)),
        (".gitignore",           lambda: _doctor_check_git_ignore(plugin_dir)),
    ]

    err_count = 0
    warn_count = 0

    name_col = max(len(name) for name, _ in checks)
    for name, runner in checks:
        try:
            status, label, detail = runner()
        except Exception as exc:
            status, label, detail = "error", "check crashed", f"{type(exc).__name__}: {exc}"
        if status == "error":
            err_count += 1
        elif status == "warn":
            warn_count += 1
        glyph = _doctor_status_glyph(status)
        print(f"  {glyph} {name:<{name_col}}  {label}")
        if detail:
            print(f"        {detail}")

    print()
    if err_count:
        print(f"FAILED: {err_count} error(s), {warn_count} warning(s). Fix the errors.",
              file=sys.stderr)
        return 1
    if warn_count:
        print(f"OK with {warn_count} warning(s). Warnings don't block deployment but should be cleaned up.")
    else:
        print("OK — all checks pass.")
    return 0


# ── Entry point ─────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yourbot",
        description="YourBot plugin development CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              yourbot init                        # interactive wizard for first-time setup
              yourbot new my_plugin              # scaffold a new plugin in ./my_plugin
              yourbot new my_plugin --target .   # scaffold into the current directory
              yourbot dev                         # run plugin in cwd against mock host
              yourbot dev --watch                 # also reload on file changes
              yourbot validate                    # run the platform's pre-submit checks locally
              yourbot validate --id my_plugin     # validate against a specific plugin_id
              yourbot doctor                      # pre-flight: SDK install + manifest + caps + tests + env
        """),
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>")

    p_init = sub.add_parser(
        "init",
        help="interactive wizard for first-time plugin setup (prompts instead of flags)",
    )
    p_init.set_defaults(func=cmd_init)

    p_new = sub.add_parser("new", help="scaffold a new plugin directory")
    p_new.add_argument("plugin_id", nargs="?", default=None,
                       help="lowercase id (3-32 chars, letters/digits/underscore, starts with letter)")
    p_new.add_argument("--target", "-t", default=None,
                       help="directory to create (default: ./<plugin_id>)")
    p_new.add_argument("--template", default="basic",
                       choices=sorted(TEMPLATES.keys()),
                       help="starter template (default: basic). "
                            "Run `yourbot new --list-templates` to see what each one does.")
    p_new.add_argument("--list-templates", action="store_true",
                       help="print the catalog of available templates and exit")
    p_new.set_defaults(func=cmd_new)

    p_dev = sub.add_parser("dev", help="run plugin locally against a mock host")
    p_dev.add_argument("--path", "-p", default=None,
                       help="plugin directory (default: cwd)")
    p_dev.add_argument("--watch", "-w", action="store_true",
                       help="reload on file changes")
    p_dev.set_defaults(func=cmd_dev)

    p_validate = sub.add_parser(
        "validate",
        help="run pre-submit checks locally (the same ones the platform runs)",
    )
    p_validate.add_argument("--path", "-p", default=None,
                            help="plugin directory (default: cwd)")
    p_validate.add_argument("--id", default=None,
                            help="override the plugin_id for the cap-mismatch check "
                                 "(default: read from manifest.json, then directory name)")
    p_validate.add_argument("--version", default=None,
                            help="override the version for the version-mismatch check "
                                 "(default: read from manifest.json)")
    p_validate.set_defaults(func=cmd_validate)

    p_doctor = sub.add_parser(
        "doctor",
        help="pre-flight diagnostic: SDK install, manifest, capabilities, tests, env",
    )
    p_doctor.add_argument("--path", "-p", default=None,
                          help="plugin directory (default: cwd)")
    p_doctor.set_defaults(func=cmd_doctor)

    return p


def main(argv: list | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
