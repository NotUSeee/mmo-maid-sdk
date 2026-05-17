# Changelog

All notable changes to the MMO Maid SDK are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.3] — 2026-05-17

This release rolls up bug fixes and additive improvements driven by
real-world plugin-developer feedback (four marketplace plugins running
against 0.5.2 in production). Nothing here breaks an existing plugin.

### Fixed (platform-side — no SDK code change but visible to plugins on upgrade)

- **Slash command args on the bot path are now available under the documented `event["options"]` key.** Previously, the bot websocket dispatch sent args under `event["command_options"]` while the gateway HTTP webhook used `event["options"]`. Plugins following the docs read `options` and silently broke on the bot path. Both paths now publish `options`; `command_options` is retained as a legacy alias and will be removed in a future release.
- **`event["member"]` is now included on `interaction_create`** when the interaction occurred in a guild. The dict contains `permissions` (string-encoded integer bitfield, Discord convention), `roles` (list of role-ID strings, excludes `@everyone`), and `nick` (or `None`). Plugins can now do admin gates without an extra `ctx.discord.get_member()` REST round-trip. The key is absent for DM interactions.
- **`ctx.discord.list_roles()` now returns the `permissions` field** for each role (was previously stripped). Use this for role-based admin detection without an extra REST call per role.
- **`ctx.discord.*` calls that fail with an HTTP error now reliably raise `DiscordApiError`** with `.status_code` set, instead of escaping as a plain `RuntimeError`. Affected methods that were previously inconsistent include `get_guild`, `create_channel`, `delete_channel`, `edit_channel`, `bulk_delete_messages`, `unban_member`, `set_channel_permissions`, `delete_channel_permission`, `create_thread`, `edit_thread`, `pin_message`, `unpin_message`, `list_channels`, `set_nickname`, `create_webhook`, `execute_webhook`, `delete_webhook`, `timeout_member`, `ban_member`, `kick_member`, `add_role`, `remove_role`.
- **`ctx.sql.execute()` / `query()` / `query_one()` error responses now include the underlying psycopg detail** (e.g., "relation does not exist", "column ... does not exist"), truncated to 300 characters. Previously every runtime SQL error returned the same opaque "SQL execution failed. Check your query syntax and parameters." string.
- **`ctx.sql.query()` responses now include a `truncated: bool` flag** indicating whether the 1000-row cap clipped the result set. Plugins can detect silent truncation and re-issue with windowing (`ROW_NUMBER() OVER (PARTITION BY ...)`).
- **`ctx.version` is now populated correctly in pool-mode workers.** Previously empty in handlers running under pool workers.

### Added

- **`@plugin.on_component(prefix="...")`** — register a handler that matches any `custom_id` starting with the given prefix. Use this when your buttons or select menus encode dynamic state (e.g. `"page:next:5"`, `"vote:yes:42"`). The original `@plugin.on_component("exact_id")` form still works unchanged; exactly one of `custom_id=` or `prefix=` must be provided.
- **`ctx.request_id`** — a `@property` exposing the current event's correlation ID (the Discord interaction ID inside `interaction_create` handlers, empty string outside). Pass through `ctx.log(..., request_id=ctx.request_id)` to correlate log lines across handler entry, RPC calls, and downstream work.
- **`ctx.http.request(..., params=...)`** — pass a dict of query-string parameters; values may be strings or lists of strings (lists encode as repeated keys, `doseq=True`). The kwarg is also accepted by `ctx.http.get()` and `ctx.http.post()`. Replaces the manual `urllib.parse.urlencode(...)` boilerplate plugins were writing.
- **`ctx.discord.edit_message(..., components=...)`** — pass an updated list of `ActionRow` / `Button` / `SelectMenu` objects (or raw dicts) to replace the message's components. Pass `components=[]` to clear all buttons. Previously you could only edit `content` and `embeds`.
- **`ctx.interaction.followup(...)` now returns a dict** with at least `{"message_id": str, "channel_id": str}` so you can later edit or delete the followup. Previously typed as `-> None` and the Discord response was discarded. Existing code that called `followup(...)` without using the return value continues to work.
- **`ctx.secrets`** API surface — read/write encrypted per-plugin secrets configured in the dev portal. (Previously available only in the in-tree bundled SDK copy; now part of the published package.)

### Deprecated

- **`event["command_options"]`** — read from `event["options"]` instead. The legacy key continues to be populated for at least one minor release.

### Notes

- The 0.5.2 → 0.5.3 transition is intended to be drop-in: `pip install --upgrade mmo-maid-sdk` and your existing plugin should continue to work. Workaround code your plugin wrote against the bugs listed under **Fixed** can be deleted; see the per-fix paragraph for guidance.

## [0.5.2] — earlier

Initial PyPI release as a standalone package, extracted from the MMO Maid
monorepo. See git history (`git log --oneline`) for the change list prior
to 0.5.3.
