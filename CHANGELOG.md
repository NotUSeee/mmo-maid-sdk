# Changelog

All notable changes to the YourBot SDK are documented here. This project follows
[Keep a Changelog](https://keepachangelog.com/) and [Semantic Versioning](https://semver.org/).

## [0.9.0]

### Added

- **`ctx.sql.query()` now reports truncation.** It returns a `QueryResult` — a
  plain `list` of row dicts that also carries `.truncated`, True when the host
  clipped the result at the requested `limit`. The host has always computed the
  flag; the SDK dropped it, so a query that silently lost rows was
  indistinguishable from a complete one. `QueryResult` subclasses `list`, so
  iteration, indexing, `len()` and `==` against a plain list are unchanged and
  no existing code needs to be touched. Exported as `yourbot_sdk.QueryResult`.
- **`ctx.kv.list(start_after=...)` for pagination.** The host caps `list()` at
  100 keys per call, and the cursor that pages past that was implemented in the
  platform but never forwarded through the RPC layer, so keys 101+ were
  unreachable from inside a plugin. Pass the last key you received to get the
  next page.
- **`allowed_mentions` on `ctx.discord.send_message` and `edit_message`.** The
  host has always accepted and sanitized it on both actions, but the SDK did
  not expose the parameter, so passing it raised
  `TypeError: unexpected keyword argument 'allowed_mentions'`. Omit it and
  nothing pings (the host default is `{"parse": []}`); `everyone`/`here` are
  always stripped host-side and cannot be triggered from a plugin.

### Fixed

- **`ctx.kv.list` documented and mocked its real cap.** The docstring promised
  "up to 1000 results" while the host clamps to 100, so plugins that swept a
  prefix silently processed only the first 100 keys and reported success.
  `MockContext` now clamps to 100 as well, so a plugin that only works because
  the mock returned more keys fails in tests rather than in production.

## [0.8.5]

### Added

- **`RpcError`.** Host errors the SDK cannot map to a more specific exception
  (including "transport closed" failures) are now raised as
  `yourbot_sdk.RpcError` instead of bare `RuntimeError`, so `except SdkError`
  is a true catch-all as documented. `RpcError` also subclasses
  `RuntimeError`, so existing `except RuntimeError` handlers keep working
  unchanged. Note: code with BOTH an `except SdkError:` and an
  `except RuntimeError:` clause will now route these errors into whichever
  clause appears first — previously they could only match `RuntimeError`.
- **`SdkPermissionError.permission` is now populated.** Newer hosts ship the
  missing permission name in the structured error payload; against older
  hosts the SDK best-effort parses it from the error message. Empty string
  when unknown (previously it was always empty).

### Fixed

- **Reserved command names resynced with the platform.** `yourbot validate` now
  refuses the platform commands `help` and `yourbot` locally, matching the
  publish gate (previously the platform rejected `yourbot` at publish while
  local validation passed it, and `help` is newly reserved for the platform's
  `/help` command).
- **`yourbot_sdk.responses` importable through every shim.** The `responses`
  module (typed return shapes) was missing from the compat-shim submodule
  lists, so `from mmo_maid_sdk.responses import Member` (and the monorepo
  dev-tree import) failed with `ModuleNotFoundError` even though the module
  ships in the wheel.
- **`ctx.metrics.record` documents the enforced name rule.** The docstring
  claimed names up to 128 chars with dots; the platform actually enforces
  1-64 chars of `[a-zA-Z_][a-zA-Z0-9_]*` (no dots). Dotted names now also
  fail fast at the RPC layer with a clear message instead of a generic
  storage error.
- **No more leaked pending entries on a failed request write.** If
  serializing an RPC request failed (e.g. a non-JSON-serializable param), the
  request stayed in the pending table forever; it is now cleaned up and the
  original exception propagates unchanged.

### Changed

- **Testing mocks enforce production signatures.** `MockContext` sub-APIs now
  reject positional arguments exactly where production does:
  `ctx.interaction.respond/defer/followup/send_modal`, `ctx.http.request`
  options, `ctx.log` options, and `ctx.sql.query`'s `limit` are keyword-only;
  `ctx.http.get/post` no longer accept `secret_auth`/`auth` (production only
  accepts those on `ctx.http.request`), and `ctx.http.post` requires `body`.
  Tests that passed these positionally were already broken in production —
  the mock now catches it before you ship.
- **Reserved names are grandfathered on the platform.** If your plugin
  published a command before its name became reserved, the platform keeps
  accepting your uploads and registers the command on each server under the
  alias `/yourpluginid-name`; events still arrive under your manifest name.
  Local `yourbot validate` cannot see your publish history, so it may still flag
  such a name as reserved — the platform's publish gate is the authority.

## [0.8.4]

### Added

- **Server-side cron for pooled plugins.** `@plugin.cron` tasks now run in
  production: declare each task in `manifest.json` —
  `"cron": [{"spec": "0 9 * * *", "name": "daily_summary"}]` (`name` = the
  decorated function's name) — and the platform fires the schedule
  server-side, once per enabled server, delivering a normal plugin event with
  `event_type: "cron"`. The SDK routes it to the matching `@plugin.cron`
  function with a tenant-scoped `ctx` (same per-tenant Context and
  `on_ready`-before-first-event guarantees as any event); you can also consume
  the raw event with `@plugin.on_event("cron")`. Limits: max 5 entries,
  nothing more often than every 5 minutes, at-most-once per
  (server, schedule, minute), missed ticks not replayed.
- **Cron consistency checks in `yourbot validate`.** The manifest `"cron"` array
  is validated (shape, entry cap, 5-minute frequency floor, identifier names,
  duplicates, spec syntax — same 5-field UTC dialect as the decorator), and
  drift between the manifest and your code is surfaced: a `@plugin.cron` task
  with no manifest entry never runs in production (warning), and
  `@plugin.schedule` tasks never run in production at all (warning).
- The `cron` starter template (`yourbot new`) now ships a manifest with
  matching `"cron"` entries.

### Fixed

- **Lifecycle hooks get a per-tenant context in pool mode.**
  `@on_install` / `@on_enable` / `@on_disable` / `@on_uninstall` handlers now
  receive a Context scoped to the server the signal is for (previously the
  blank boot ctx in pool mode), and their RPCs carry the host-supplied
  correlation id so tenant resolution is exact even after the install row is
  gone.

## [0.8.3]

### Added

- **Slash-command consistency checks in `yourbot validate`.** The local validator
  now cross-checks `manifest.json` `slash_commands` against your
  `@plugin.on_slash_command` decorators, exactly like the platform does at
  upload and in the Plugin Builder preview: a declared command with no matching
  handler is a blocking error (it would appear in Discord and hang on
  "thinking…" forever), an uppercase decorator name is a blocking error
  (registration lowercases the name, dispatch matches exactly), reserved names
  owned by built-in YourBot plugins are refused, and command/option names must
  be 1-32 chars of lowercase letters, digits, `-` or `_` with a valid option
  `type`. A handler with no manifest entry warns (it registers with no
  description and no options). Fix mismatches locally instead of discovering
  them after a failed upload.
- **`proxy:websocket` capability detection.** `yourbot validate` and capability
  auto-detection now recognize `ctx.ws.*` usage, so WebSocket plugins no longer
  validate green locally while missing the capability at upload.

### Fixed

- **Pool-mode tenant resolution hardened (now actually shipped).** Every
  outbound RPC carries the correlation ID of the event that triggered it, so
  the host resolves the RPC's tenant from that trusted ID instead of "most
  recent event". This fix was documented for 0.7.1 but the code did not make
  it into the published wheel; 0.8.3 ships it. No public API change.
- **Dashboard handler error logs name the right handler.** With multiple
  `@plugin.on_dashboard` handlers, an error log previously always reported the
  last-registered method name instead of the one that failed.

## [0.8.2]

### Added

- **Wildcard WebSocket handlers.** `@plugin.on_ws_message("name:*")` (and `on_ws_open`/
  `on_ws_close`) now match any concrete connection whose name shares the prefix — e.g.
  `"rustplus:*"` handles `rustplus:eu1`, `rustplus:us-west`, etc. Lets a plugin manage many
  connections (one socket per game server) with a single handler set; the concrete `name` is
  in the frame so you can route per-connection. Exact-name registrations still take precedence.

## [0.8.1]

### Added

- **`ctx.ws.allow_host(host)` / `ctx.ws.revoke_host(host)`** — authorize a WebSocket
  destination the SERVER ADMIN supplies at setup time (e.g. their own game server's IP),
  which a static `proxy_domains_requested` allowlist can't express. Must be called from
  inside a slash-command handler run by a server admin (Manage Server); the platform verifies
  the invoking member is an admin and that the host is public, then remembers it for that
  server only. After approval, `ctx.ws.ensure(name, "wss://<host>:...")` to that host succeeds.
  `MockContext.ws` records `allowed_hosts` / `revoked_hosts`.

## [0.8.0]

### Added

- **Persistent WebSocket connections (`ctx.ws`).** A new `proxy:websocket` capability lets a
  plugin open and maintain a live two-way connection to a declared host. The platform's broker
  holds the socket (the sandbox still has no raw network) and reconnects automatically.
  - `ctx.ws.ensure(name, url, *, secret_auth=None, auth=None, subscribe=None, binary=False)` —
    idempotent; safe to call on every event or in `on_ready`.
  - `ctx.ws.send(name, data)` — `str` sends a text frame, `bytes` a binary frame.
  - `ctx.ws.close(name)`.
  - Inbound frames are delivered to `@plugin.on_ws_message(name)` (`(ctx, msg)` where
    `msg = {"name", "conn_id", "data", "binary"}`; binary `data` is base64), with
    `@plugin.on_ws_open(name)` and `@plugin.on_ws_close(name)` for lifecycle. Frames for one
    connection are serialized in order. Suitable for game-server feeds and the Rust+ companion
    protocol (bundle pure-Python protobuf in your ZIP).
- **Secret-backed auth injection for `ctx.http` and `ctx.ws`.** Pass `secret_auth="SECRET_NAME"`
  (or `auth={"scheme": "bearer"|"basic"|"token", "secret": "NAME"}`) and the platform injects the
  `Authorization` header from a **domain-bound** secret — the plugin never sees the value and
  cannot set `Authorization` itself. This unblocks Bearer-token APIs that were previously
  unreachable because `Authorization` is stripped. Requires `storage:secrets`.
- **`quarter` dashboard widget width** alongside `full` / `half` / `third` / `two_thirds`.
- **`MockContext.ws`** in the test harness records `ensure` / `send` / `close` calls and is
  capability-gated like `ctx.http`, so WebSocket plugins are unit-testable.

### Notes

- `proxy:websocket` is a dangerous-tier capability (staff-reviewed) and requires the **exact**
  host in `proxy_domains_requested` (no subdomain wildcard, unlike HTTP).

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
