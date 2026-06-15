# Changelog

All notable changes to the YourBot SDK are documented here. This project follows
[Keep a Changelog](https://keepachangelog.com/) and [Semantic Versioning](https://semver.org/).

## [0.7.1]

### Fixed

- **`ctx.kv.increment(key, amount)` accepts `amount` positionally.** It was
  keyword-only (`increment(key, *, amount=1)`), so the natural positional call —
  matching `ctx.kv.decrement(key, amount)` and Redis `INCRBY` — raised
  `TypeError`. The signature is now `increment(key, amount=1, *, path="")`;
  existing `amount=`/`path=` keyword calls are unchanged. `MockContext` mirrors it.

### Added

- **Accurate `RateLimitError.retry_after`.** When the host sends structured error
  metadata (`code`, `retry_after`) the SDK now surfaces the precise retry delay,
  falling back to parsing `retry in <N>s` from the message, then the legacy
  `remaining=/min` parse. `retry_after` is now a float to support sub-second and
  hour-scale limiter windows.

## [0.7.0]

### Added

- **In-place message updates from component handlers.**
  `ctx.interaction.respond(update_message=True)` edits the message the
  button/select menu is attached to (Discord `UPDATE_MESSAGE`) instead of
  sending a new reply — game boards, pagination, and live dashboards can now
  update in place. Component interactions only; `ephemeral` is ignored; the
  fields you pass replace the message's current content/embeds/components;
  may be called repeatedly within the 15-minute interaction window. On
  platform versions without support the flag is ignored and a normal reply
  is sent, so it degrades gracefully. `MockContext` records the new flag in
  `interaction.responses` for assertions.

## [0.6.1]

### Added

- **PEP 561 typing marker.** The wheel now ships `py.typed`, so type checkers
  (mypy, pyright) and IDEs pick up the SDK's inline type hints when it's installed
  from PyPI — previously the hints were ignored for installed (non-editable) users.
- **`MockContext` enforces capabilities by default.** Calling a gated method
  (e.g. `ctx.discord.send_message`) without the matching capability now raises
  `CapabilityError`, matching production — so a passing test means a working
  manifest. Pass `MockContext(strict_capabilities=False)` for the old behaviour.
  (`MockContext(capabilities=[])` now means "no capabilities" rather than "all".)
- **Typed Discord responses.** New `yourbot_sdk.responses` module with `Member`,
  `Role`, `Channel`, `Guild`, and `Message` TypedDicts; the `ctx.discord` read
  methods (`get_member`, `get_channel`, `get_guild`, `list_roles`,
  `list_channels`, `list_members`, `search_members`, `get_messages`) are now
  annotated with them for IDE autocomplete.
- **`ctx.discord.iter_messages(...)`** — a generator that pages through a
  channel's full history automatically (walks newest→oldest by default, or
  oldest→newest with `after=`), so you no longer manage `before`/`after` cursors
  by hand. The testing harness supports it via `ctx.discord.set_messages([...])`.
- **Machine-readable error codes.** Every SDK exception now carries a stable
  `.code` (e.g. `CAPABILITY_DENIED`, `RATE_LIMITED`, `QUOTA_EXCEEDED`,
  `DISCORD_API_ERROR`, `BOT_MISSING_PERMISSION`, `KV_QUOTA_EXCEEDED`,
  `VALIDATION_ERROR`, `RPC_TIMEOUT`) so you can branch on failures without
  string-matching. `CapabilityError` messages now include a manifest hint.

### Fixed

- KV-quota errors now raise `KvQuotaError` (code `KV_QUOTA_EXCEEDED`) instead of
  being misclassified as a generic `RateLimitError`.

### Fixed — testing harness fidelity

- `MockContext.kv.increment` now takes the keyword-only `path` argument, matching
  the real API (JSON-object increments). `ctx.kv.increment("k", 5)` becomes
  `ctx.kv.increment("k", amount=5)`.
- `ctx.ephemeral.counter` in the mock is now a real sliding-window counter (it was
  monotonic and never reset, making rate-limit tests false-pass).
- The HTTP mock now records the `params` query-string argument; the SQL mock
  `query` accepts `limit`; `metrics.query` accepts `aggregate` — all matching the
  real signatures.
- `yourbot dev` now reports log lines and KV writes (it read attributes that
  didn't exist on `MockContext`, so those counters were always zero). `MockContext`
  gained `kv_writes` and `log_lines` accessors.

## [0.6.0]

### Changed — package rename (`mmo-maid-sdk` → `yourbot-sdk`)

- The distribution is now **`yourbot-sdk`** (`pip install yourbot-sdk`) and the import
  package is **`yourbot_sdk`** (`from yourbot_sdk import Plugin, Context`).
- The CLI command is now **`yourbot`** (`yourbot new`, `yourbot dev`, `yourbot validate`).
- The dispatch-thread env var is now `YOURBOT_SDK_DISPATCH_THREADS`
  (the old `MMO_SDK_DISPATCH_THREADS` is still honored as a fallback).

### Backward compatibility (nothing breaks)

- The `yourbot-sdk` wheel still ships a `mmo_maid_sdk` compatibility package, so existing
  plugins that `import mmo_maid_sdk` (including submodule and legacy nested imports) keep
  working. Importing it emits a `DeprecationWarning` pointing to `yourbot_sdk`.
- `pip install mmo-maid-sdk` continues to resolve via a thin alias meta-package that depends
  on `yourbot-sdk` of the same version.
- No public API changed: class names, exceptions (incl. the `PermissionError`/`TimeoutError`
  aliases), decorators, and `Context` sub-APIs are identical.

Prior releases (0.5.x and earlier) were published under the `mmo-maid-sdk` name; their history
lives in that line.
