"""Plugin artifact validation — vendored from the platform.

This module is a **vendored copy** of the validation logic the YourBot platform
runs on a plugin artifact before accepting a version. Shipped in the SDK so devs
can run the exact same checks locally with `yourbot validate` instead of
discovering errors only after a failed upload.

Contract: for the same artifact bytes this file must emit the SAME finding
codes, with the SAME messages/hints, in the SAME order as the platform gate. A
differential harness zips each plugin version and diffs the two finding
sequences. It ships as a **hand-maintained** vendored copy — it is edited in
place when the platform validator changes, because it cannot import any
`mmo_maid.*` module (the platform does not exist inside the SDK wheel), so
several platform helpers are inlined here rather than imported.

Sources of truth (all inlined below, none importable at runtime):
  - `mmo_maid/core/plugin_validation.py`  — the validator itself
  - `mmo_maid/core/artifact_store.py`     — `_detect_capabilities`,
    `manifest_capabilities`, `write_manifest_capabilities`,
    `_CAP_PATTERNS`, `_CAPABILITIES_KEYS`, `_SLASH_CMD_RE`
  - `mmo_maid/core/command_registry.py`   — `reserved_command_names()`, frozen
    into `_RESERVED_COMMAND_NAMES`
  - `mmo_maid/core/dashboard_manifest.py` — `validate_dashboard_manifest`,
    `iter_manifest_widgets`, `manifest_widget_rpc_names`,
    `manifest_declared_rpc_names`
  - `mmo_maid/core/api_sandbox.py`        — `_validate_plugin_sql` and its
    helpers (`_strip_sql_comments`, `_assert_single_statement`,
    `_mask_sql_string_literals`, `_SQL_ALLOWED_STATEMENTS`,
    `_SQL_BLOCKED_PATTERNS`)
  - `mmo_maid/core/cron_match.py`         — via the SDK's own `_parse_cron_spec`

Checks this module runs (finding codes, in emission order):
  1.  zip integrity ............ artifact_unreadable
  2.  entry point .............. missing_entry_point
  3.  manifest parse ........... missing_manifest, manifest_encoding,
                                 manifest_not_object, manifest_invalid_json
  4.  manifest fields .......... manifest_missing_field,
                                 manifest_plugin_id_mismatch,
                                 manifest_version_mismatch,
                                 description_too_long, description_placeholder,
                                 capabilities_not_list, capability_malformed
  5.  capability cross-check ... capability_used_not_declared,
                                 capability_declared_not_used
  5b. bundled imports .......... import_not_bundled_or_declared
      dashboard wiring ......... dashboard_handler_without_manifest,
                                 dashboard_manifest_unparseable,
                                 dashboard_manifest_invalid,
                                 dashboard_rpc_method_missing_handler,
                                 dashboard_declared_rpc_no_handler
  5c. slash commands ........... command_defs_not_list,
                                 command_handler_not_lowercase,
                                 command_def_malformed,
                                 command_name_not_lowercase,
                                 command_name_invalid, command_name_duplicate,
                                 command_options_not_list,
                                 command_option_malformed,
                                 command_option_missing_name,
                                 command_option_name_invalid,
                                 command_option_missing_type,
                                 command_option_type_invalid,
                                 command_name_reserved,
                                 command_missing_handler,
                                 command_handlers_not_found,
                                 command_not_declared
  5d. cron schedules ........... cron_not_list, cron_too_many,
                                 cron_entry_malformed, cron_name_invalid,
                                 cron_name_duplicate, cron_spec_invalid,
                                 cron_spec_too_frequent,
                                 cron_task_not_in_manifest,
                                 schedule_task_never_runs, cron_missing_task,
                                 cron_tasks_not_found
  6.  per-file AST scans ....... syntax_error, forbidden_eval, forbidden_exec,
                                 forbidden_compile, forbidden_dunder_import,
                                 forbidden_import_subprocess,
                                 forbidden_import_ctypes,
                                 forbidden_import_socket,
                                 forbidden_import_multiprocessing,
                                 ctx_call_positional_kwonly,
                                 ctx_call_unknown_kwarg,
                                 ctx_call_missing_required,
                                 plugin_sql_rejected
  7.  junk files ............... bytecode_in_artifact, git_metadata_in_artifact

Two checks depend on the environment rather than the artifact and disable
themselves (emitting nothing) if their dependency is unavailable, exactly as
the platform's do:
  * the ctx call-signature lint introspects the installed SDK's own
    ``_context`` module — if that import fails, the lint is skipped;
  * the SQL dry-check runs the inlined sandbox validator with
    ``active_schema=None``, so the cross-schema reference check (which needs a
    live per-server schema name) never fires here — same as on the platform.
"""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, Optional


_log = logging.getLogger(__name__)


# Cap on body strings shown to the dev — keeps friendly errors readable.
_MAX_HINT_LEN = 240
_MAX_DESCRIPTION_LEN = 2000

# Required manifest fields. Values are short human-readable descriptions used
# in error messages.
_REQUIRED_MANIFEST_FIELDS = {
    "id": "plugin id (e.g. \"my_plugin\")",
    "name": "human-readable name shown on the marketplace",
    "version": "version string (e.g. \"1.0.0\")",
}

# Patterns that almost always indicate a placeholder description that
# slipped into a real submission. Matched case-insensitively against the
# whole description.
_PLACEHOLDER_DESCRIPTIONS = {
    "todo", "tbd", "fill me in", "lorem ipsum", "placeholder",
    "description goes here", "edit this",
}


# Historical key names for the requested-capabilities list in manifest.json.
# All four are accepted on read via manifest_capabilities(); writes go to the
# canonical key plus a mirror, via write_manifest_capabilities().
_CAPABILITIES_KEYS = (
    "capabilities_required",
    "capabilities_requested",
    "requested_capabilities",
    "capabilities",
)


def manifest_capabilities(manifest: dict) -> list[str]:
    """Read capabilities from a manifest dict, accepting any historical key name.
    Returns the deduped sorted list (empty if no key has a list value)."""
    if not isinstance(manifest, dict):
        return []
    seen: set[str] = set()
    for key in _CAPABILITIES_KEYS:
        v = manifest.get(key)
        if isinstance(v, list):
            for c in v:
                if isinstance(c, str):
                    s = c.strip()
                    if s:
                        seen.add(s)
    return sorted(seen)


def write_manifest_capabilities(manifest: dict, caps) -> None:
    """Write capabilities to the canonical key (capabilities_required) AND mirror
    to capabilities_requested so legacy reads keep working during transition.
    Idempotent. Accepts any iterable of strings."""
    if not isinstance(manifest, dict):
        return
    final = sorted({c.strip() for c in (caps or []) if isinstance(c, str) and c.strip()})
    manifest["capabilities_required"] = final
    manifest["capabilities_requested"] = final


# SDK method patterns → capability mapping for auto-detection.
# **Keep byte-for-byte in sync with `artifact_store._CAP_PATTERNS`.**
_CAP_PATTERNS: list[tuple[bytes, str]] = [
    # storage:kv — key-value store
    (b".kv.get(",          "storage:kv"),
    (b".kv.set(",          "storage:kv"),
    (b".kv.delete(",       "storage:kv"),
    (b".kv.list(",         "storage:kv"),
    (b".kv.increment(",    "storage:kv"),
    (b".kv.get_many(",     "storage:kv"),
    (b".kv.set_many(",     "storage:kv"),
    (b".kv.list_values(",  "storage:kv"),
    # Legacy names kept for old uploads; the SDK's real batch methods are
    # get_many/set_many.
    (b".kv.mget(",         "storage:kv"),
    (b".kv.mput(",         "storage:kv"),
    # storage:sql — sandboxed SQL
    (b".sql.execute(",     "storage:sql"),
    (b".sql.query(",       "storage:sql"),
    (b".sql.query_one(",   "storage:sql"),
    # storage:secrets — encrypted per-plugin secrets (ctx.secrets.*)
    (b".secrets.get(",     "storage:secrets"),
    (b".secrets.set(",     "storage:secrets"),
    (b".secrets.delete(",  "storage:secrets"),
    # discord:send_message / edit / delete / react
    (b".send_message(",    "discord:send_message"),
    (b".edit_message(",    "discord:edit_message"),
    (b".delete_message(",  "discord:delete_message"),
    (b".add_reaction(",    "discord:add_reaction"),
    # discord:read — member/channel/role lookups
    (b".get_member(",      "discord:read"),
    (b".get_channel(",     "discord:read"),
    (b".get_guild(",       "discord:read"),
    (b".list_roles(",      "discord:read"),
    (b".list_members(",    "discord:read"),
    (b".list_channels(",   "discord:read"),
    (b".search_members(",  "discord:read"),
    (b".get_messages(",    "discord:read"),
    # discord:manage_channels
    (b".create_channel(",  "discord:manage_channels"),
    (b".edit_channel(",    "discord:manage_channels"),
    (b".delete_channel(",  "discord:manage_channels"),
    (b".create_thread(",   "discord:manage_channels"),
    # discord:moderate_members
    (b".timeout_member(",  "discord:moderate_members"),
    (b".timeout_bulk(",    "discord:moderate_members"),
    # discord:ban_members
    (b".ban_member(",      "discord:ban_members"),
    # discord:kick_members
    (b".kick_member(",     "discord:kick_members"),
    (b".kick_bulk(",       "discord:kick_members"),
    # discord:manage_roles
    (b".add_role(",        "discord:manage_roles"),
    (b".remove_role(",     "discord:manage_roles"),
    (b".add_role_bulk(",   "discord:manage_roles"),
    (b".remove_role_bulk(", "discord:manage_roles"),
    # interaction:respond — slash commands, buttons, modals
    (b".interaction.respond(", "interaction:respond"),
    (b".interaction.defer(",   "interaction:respond"),
    (b".interaction.send_modal(", "interaction:respond"),
    # proxy:http — outbound HTTP
    (b".http.get(",        "proxy:http"),
    (b".http.post(",       "proxy:http"),
    (b".http.request(",    "proxy:http"),
    # proxy:websocket — persistent WebSocket connections (ctx.ws.*)
    (b".ws.ensure(",       "proxy:websocket"),
    (b".ws.send(",         "proxy:websocket"),
    (b".ws.connect(",      "proxy:websocket"),
    # events:message_content — accessing message text
    (b'("content"',        "events:message_content"),
    (b"('content'",        "events:message_content"),
    (b'.get("content"',    "events:message_content"),
    (b".get('content'",    "events:message_content"),
]


# Manifest fields whose non-empty value implies a capability requirement.
_CAPABILITY_IMPLIED_BY: dict[str, str] = {
    "proxy:http": "proxy_domains_requested",
    "interaction:respond": "slash_commands",
}


def _detect_capabilities(python_sources: list[bytes]) -> list[str]:
    """Scan Python source files for SDK method calls and return detected capabilities."""
    found: set[str] = set()
    for src in python_sources:
        for pattern, cap in _CAP_PATTERNS:
            if pattern in src:
                found.add(cap)
    return sorted(found)


def _detect_capabilities_with_locations(
    sources_by_path: list[tuple[str, bytes]],
) -> list[dict]:
    """Like ``_detect_capabilities``, but also reports (file, line, pattern)
    for the first occurrence of each detected capability. Mirrors the
    platform helper of the same name."""
    found: dict[str, tuple[str, int, str]] = {}
    for path, src in sources_by_path:
        for pattern, cap in _CAP_PATTERNS:
            if cap in found:
                continue
            idx = src.find(pattern)
            if idx == -1:
                continue
            line_no = src.count(b"\n", 0, idx) + 1
            found[cap] = (path, line_no, pattern.decode("utf-8", errors="replace"))
    return sorted(
        [
            {"capability": cap, "file": file, "line": line, "pattern": pat}
            for cap, (file, line, pat) in found.items()
        ],
        key=lambda d: (d["capability"], d["file"]),
    )


# ── Dashboard manifest schema (vendored from core/dashboard_manifest.py) ───
#
# The platform validator imports this module; the SDK cannot, so the schema is
# inlined verbatim. Error strings are surfaced to the dev as
# ``dashboard_manifest_invalid`` messages, so they must stay byte-identical.

VALID_WIDGET_TYPES = {"stat_card", "chart", "table", "form", "text", "alert",
                      "progress_bar", "list", "markdown"}
# Canonical width set = union of the SDK builder's set and the customer
# renderer's grid spans (quarter=3, third=4, half=6, two_thirds=8, full=12).
VALID_WIDTHS = {"quarter", "third", "half", "two_thirds", "full"}

# ── schema: 2 vocabulary ─────────────────────────────────────────────────────
V2_WIDGET_TYPES = {"image", "key_value", "timeline", "badge_list", "user_list",
                   "heading", "divider", "action_button"}
V2_CONTAINER_TYPES = {"section", "tabs"}
# Widget types that fetch data on page load (and therefore need rpc_method or
# a v2 `source` binding). heading/divider/image are static; action_button only
# fires on click; the rest render an empty state until data arrives but don't
# REQUIRE a binding.
_TYPES_REQUIRING_DATA = {"stat_card", "chart", "table"}
_STATIC_TYPES = {"heading", "divider", "image", "action_button"}
V2_ACTION_STYLES = {"default", "primary", "danger"}
V2_COLUMN_FORMATS = {"badge", "timestamp", "number", "percent", "link",
                     "avatar", "code"}
V2_BADGE_COLOR_TOKENS = {"green", "red", "gold", "cyan", "gray"}
V2_CHART_HEIGHTS = {"sm", "md", "lg"}
V2_DENSITIES = {"cozy", "compact"}
_PERMISSION_VALUES = {"viewer", "manager", "owner"}  # api_rbac._ROLE_ORDER keys
_REFRESH_MIN, _REFRESH_MAX = 5, 3600

_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
# Handler names registered via @plugin.on_dashboard("<name>").
_RPC_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def _is_safe_asset_path(path: object) -> bool:
    """Zip-relative dashboard asset path: must live under dashboard/, no
    traversal, no absolute paths, no backslashes."""
    if not isinstance(path, str) or not path:
        return False
    if ".." in path or path.startswith(("/", "\\")) or "\\" in path:
        return False
    return path.startswith("dashboard/") and len(path) <= 200


def _validate_rpc_block(manifest: dict, errors: list) -> None:
    rpc = manifest.get("rpc")
    if rpc is None:
        return
    if not isinstance(rpc, dict):
        errors.append("'rpc' must be an object mapping method name -> {\"writes\": true|false}")
        return
    for name, spec in rpc.items():
        if not isinstance(name, str) or not _RPC_NAME_RE.match(name):
            errors.append(
                f"'rpc' key {name!r} is not a valid method name "
                "(letters, digits, _ or -, max 64 chars)")
            continue
        if not isinstance(spec, dict):
            errors.append(f"'rpc.{name}' must be an object — use {{}} or {{\"writes\": true}}")
            continue
        writes = spec.get("writes")
        if writes is not None and not isinstance(writes, bool):
            errors.append(f"'rpc.{name}.writes' must be true or false")


def _validate_theme(theme: object, errors: list) -> None:
    if not isinstance(theme, dict):
        errors.append("'theme' must be an object of brand tokens")
        return
    for key in ("accent", "accent_secondary"):
        v = theme.get(key)
        if v is not None and (not isinstance(v, str) or not _HEX_COLOR_RE.match(v)):
            errors.append(f"'theme.{key}' must be a hex color like #7c5cff")
    palette = theme.get("chart_palette")
    if palette is not None:
        if (not isinstance(palette, list) or not palette or len(palette) > 8
                or not all(isinstance(c, str) and _HEX_COLOR_RE.match(c) for c in palette)):
            errors.append("'theme.chart_palette' must be a list of 1-8 hex colors")
    logo = theme.get("logo")
    if logo is not None and not _is_safe_asset_path(logo):
        errors.append("'theme.logo' must be a zip-relative path under dashboard/ (no traversal)")
    header = theme.get("header")
    if header is not None:
        if not isinstance(header, dict):
            errors.append("'theme.header' must be an object {title, subtitle}")
        else:
            for key in ("title", "subtitle"):
                v = header.get(key)
                if v is not None and (not isinstance(v, str) or len(v) > 120):
                    errors.append(f"'theme.header.{key}' must be a string of at most 120 chars")
    density = theme.get("density")
    if density is not None and density not in V2_DENSITIES:
        errors.append(f"'theme.density' must be one of {sorted(V2_DENSITIES)}")


def _validate_refresh(value: object, where: str, errors: list) -> None:
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) \
            or not (_REFRESH_MIN <= value <= _REFRESH_MAX):
        errors.append(f"{where}: 'refresh_seconds' must be an integer between "
                      f"{_REFRESH_MIN} and {_REFRESH_MAX}")


def _validate_permission(item: dict, where: str, errors: list) -> None:
    perm = item.get("permission")
    if perm is not None and (not isinstance(perm, str)
                             or perm.strip().lower() not in _PERMISSION_VALUES):
        errors.append(f"{where}: 'permission' must be one of {sorted(_PERMISSION_VALUES)}")


def _validate_data_sources(page: dict, where: str, errors: list,
                           seen_global: set | None = None) -> set:
    """Validate page.data_sources; returns the set of declared source ids.

    ``seen_global`` enforces MANIFEST-WIDE id uniqueness: the source endpoint
    resolves a source id by scanning pages in order, so two pages declaring
    the same id would silently shadow the later one."""
    sources = page.get("data_sources")
    ids: set = set()
    if sources is None:
        return ids
    if not isinstance(sources, list):
        errors.append(f"{where}: 'data_sources' must be a list")
        return ids
    for i, src in enumerate(sources):
        w = f"{where} data_source {i}"
        if not isinstance(src, dict):
            errors.append(f"{w} must be an object")
            continue
        sid = src.get("id")
        if not isinstance(sid, str) or not _ID_RE.match(sid):
            errors.append(f"{w}: 'id' is required (letters, digits, _ or -, max 64)")
        elif sid in ids or (seen_global is not None and sid in seen_global):
            errors.append(f"{w}: duplicate data_source id '{sid}' "
                          "(ids must be unique across the whole manifest)")
        else:
            ids.add(sid)
            if seen_global is not None:
                seen_global.add(sid)
        m = src.get("rpc_method")
        if not isinstance(m, str) or not _RPC_NAME_RE.match(m):
            errors.append(f"{w}: 'rpc_method' is required")
        params = src.get("rpc_params")
        if params is not None and not isinstance(params, dict):
            errors.append(f"{w}: 'rpc_params' must be an object")
        _validate_refresh(src.get("refresh_seconds"), w, errors)
    return ids


def _validate_columns_v2(widget: dict, where: str, errors: list) -> None:
    """v2 deep validation of table columns (v1 kept its shallow check)."""
    for j, col in enumerate(widget.get("columns") or []):
        w = f"{where} column {j}"
        if isinstance(col, str):
            continue
        if not isinstance(col, dict):
            errors.append(f"{w} must be a string key or an object")
            continue
        if not col.get("key"):
            errors.append(f"{w}: missing 'key'")
        fmt = col.get("format")
        if fmt is not None:
            if fmt not in V2_COLUMN_FORMATS:
                errors.append(f"{w}: invalid format '{fmt}' (valid: {sorted(V2_COLUMN_FORMATS)})")
            elif fmt == "badge":
                colors = (col.get("format_options") or {}).get("colors") \
                    if isinstance(col.get("format_options"), dict) else None
                if colors is not None:
                    if not isinstance(colors, dict) or not all(
                            isinstance(k, str) and v in V2_BADGE_COLOR_TOKENS
                            for k, v in colors.items()):
                        errors.append(
                            f"{w}: 'format_options.colors' must map values to one of "
                            f"{sorted(V2_BADGE_COLOR_TOKENS)}")


def _validate_plain_widget(widget: dict, where: str, schema: int,
                           source_ids: set, errors: list) -> None:
    """One non-container widget. v1 semantics are frozen; v2 adds vocabulary."""
    wtype = widget.get("type", "")
    valid_types = VALID_WIDGET_TYPES | (V2_WIDGET_TYPES if schema >= 2 else set())
    if wtype not in valid_types:
        if schema < 2 and wtype in (V2_WIDGET_TYPES | V2_CONTAINER_TYPES):
            errors.append(f"{where}: widget type '{wtype}' requires \"schema\": 2")
        else:
            # Sorted, not the raw set: set repr order is not stable across
            # processes, which made this message non-deterministic and broke
            # byte-parity between this validator and its vendored twin.
            errors.append(
                f"{where}: invalid type '{wtype}' "
                f"(valid: {', '.join(sorted(valid_types))})"
            )
    if not widget.get("id"):
        errors.append(f"{where} missing 'id'")
    width = widget.get("width", "full")
    if width not in VALID_WIDTHS:
        errors.append(f"{where}: invalid width '{width}' (valid: {VALID_WIDTHS})")

    if schema < 2:
        # v1 checks, FROZEN byte-for-byte: the fleet's schema-1 manifests may
        # carry arbitrary stray keys (source/height/format included) that were
        # always inert — erroring on them now would block re-saves of
        # previously-valid manifests.
        if wtype in _TYPES_REQUIRING_DATA and not widget.get("rpc_method"):
            errors.append(f"{where}: '{wtype}' requires 'rpc_method'")
        if wtype == "table" and not widget.get("columns"):
            errors.append(f"{where}: 'table' requires 'columns'")
        return

    # ── v2 widget checks ────────────────────────────────────────────────────
    source = widget.get("source")
    if source is not None:
        if not isinstance(source, str) or source not in source_ids:
            errors.append(f"{where}: 'source' must name a data_source id declared on this page")
    if wtype in _TYPES_REQUIRING_DATA and not widget.get("rpc_method") and not source:
        errors.append(f"{where}: '{wtype}' requires 'rpc_method' or a 'source' binding")
    if wtype == "table":
        if not widget.get("columns"):
            errors.append(f"{where}: 'table' requires 'columns'")
        else:
            _validate_columns_v2(widget, where, errors)
    height = widget.get("height")
    if height is not None and height not in V2_CHART_HEIGHTS:
        errors.append(f"{where}: 'height' must be one of {sorted(V2_CHART_HEIGHTS)}")
    if wtype == "image" and not _is_safe_asset_path(widget.get("src")):
        errors.append(f"{where}: 'image' requires 'src' — a zip-relative path under "
                      "dashboard/ (no traversal)")
    if wtype == "heading":
        text = widget.get("text")
        if not isinstance(text, str) or not text or len(text) > 200:
            errors.append(f"{where}: 'heading' requires 'text' (string, max 200 chars)")
        level = widget.get("level")
        if level is not None and level not in (1, 2, 3):
            errors.append(f"{where}: 'heading' level must be 1, 2 or 3")
    if wtype == "action_button":
        # Deliberately a DIFFERENT field than rpc_method: the widget-data
        # route serves widget.rpc_method to VIEWERS from cache, and an action
        # is a write — action_rpc_method is only reachable through the
        # manager-gated, never-cached direct RPC route.
        action = widget.get("action_rpc_method")
        if not isinstance(action, str) or not _RPC_NAME_RE.match(action or ""):
            errors.append(f"{where}: 'action_button' requires 'action_rpc_method' "
                          "(letters, digits, _ or -, max 64)")
        if widget.get("rpc_method"):
            errors.append(f"{where}: 'action_button' must use 'action_rpc_method', "
                          "not 'rpc_method' (actions are writes — never viewer-fetchable)")
        label = widget.get("label")
        if not isinstance(label, str) or not label or len(label) > 80:
            errors.append(f"{where}: 'action_button' requires 'label' (string, max 80 chars)")
        style = widget.get("style")
        if style is not None and style not in V2_ACTION_STYLES:
            errors.append(f"{where}: 'action_button' style must be one of {sorted(V2_ACTION_STYLES)}")
        for _k, _cap in (("confirm", 300), ("success_message", 200)):
            _v = widget.get(_k)
            if _v is not None and (not isinstance(_v, str) or len(_v) > _cap):
                errors.append(f"{where}: 'action_button' {_k} must be a string of at most {_cap} chars")
    vw = widget.get("visible_when")
    if vw is not None:
        # Cosmetic display toggle driven by a page data source. NOT access
        # control — the widget still renders in the DOM and its data is still
        # fetchable; use 'permission' to actually restrict who sees data.
        if not isinstance(vw, dict):
            errors.append(f"{where}: 'visible_when' must be an object "
                          "{{source, path, equals?}}")
        else:
            vw_src = vw.get("source")
            if not isinstance(vw_src, str) or vw_src not in source_ids:
                errors.append(f"{where}: 'visible_when.source' must name a data_source "
                              "id declared on this page")
            vw_path = vw.get("path")
            if not isinstance(vw_path, str) or not vw_path or len(vw_path) > 200:
                errors.append(f"{where}: 'visible_when.path' is required (dot path into "
                              "the source response)")
            if not isinstance(vw.get("equals", None),
                              (str, int, float, bool, type(None))):
                errors.append(f"{where}: 'visible_when.equals' must be a scalar")
    _validate_refresh(widget.get("refresh_seconds"), where, errors)
    _validate_permission(widget, where, errors)


def _validate_container(entry: dict, where: str, source_ids: set, errors: list) -> None:
    ctype = entry.get("type")
    if not entry.get("id"):
        errors.append(f"{where} missing 'id'")
    if ctype == "section":
        heading = entry.get("heading")
        if heading is not None and (not isinstance(heading, str) or len(heading) > 120):
            errors.append(f"{where}: section 'heading' must be a string of at most 120 chars")
        desc = entry.get("description")
        if desc is not None and (not isinstance(desc, str) or len(desc) > 500):
            errors.append(f"{where}: section 'description' must be a string of at most 500 chars")
        if entry.get("collapsible") is not None and not isinstance(entry.get("collapsible"), bool):
            errors.append(f"{where}: section 'collapsible' must be true or false")
        children = entry.get("widgets")
        if not isinstance(children, list) or not children:
            errors.append(f"{where}: section requires a non-empty 'widgets' list")
            children = []
        for j, child in enumerate(children):
            cw = f"{where} widget {j}"
            if not isinstance(child, dict):
                errors.append(f"{cw} must be an object")
            elif child.get("type") in V2_CONTAINER_TYPES:
                errors.append(f"{cw}: containers cannot nest (one level only)")
            else:
                _validate_plain_widget(child, cw, 2, source_ids, errors)
        _validate_permission(entry, where, errors)
    elif ctype == "tabs":
        tabs = entry.get("tabs")
        if not isinstance(tabs, list) or not tabs:
            errors.append(f"{where}: tabs requires a non-empty 'tabs' list")
            tabs = []
        seen: set = set()
        for t, tab in enumerate(tabs):
            tw = f"{where} tab {t}"
            if not isinstance(tab, dict):
                errors.append(f"{tw} must be an object")
                continue
            tid = tab.get("id")
            if not isinstance(tid, str) or not _ID_RE.match(tid):
                errors.append(f"{tw}: 'id' is required (letters, digits, _ or -, max 64)")
            elif tid in seen:
                errors.append(f"{tw}: duplicate tab id '{tid}'")
            else:
                seen.add(tid)
            if not tab.get("title"):
                errors.append(f"{tw} missing 'title'")
            for j, child in enumerate(tab.get("widgets") or []):
                cw = f"{tw} widget {j}"
                if not isinstance(child, dict):
                    errors.append(f"{cw} must be an object")
                elif child.get("type") in V2_CONTAINER_TYPES:
                    errors.append(f"{cw}: containers cannot nest (one level only)")
                else:
                    _validate_plain_widget(child, cw, 2, source_ids, errors)
        _validate_permission(entry, where, errors)


def validate_dashboard_manifest(manifest: dict) -> list:
    """Validate a dashboard manifest. Returns a list of error strings (empty = valid)."""
    errors: list = []
    if not isinstance(manifest, dict):
        return ["Manifest must be a JSON object"]

    schema = manifest.get("schema", 1)
    if schema not in (1, 2):
        errors.append("'schema' must be 1 or 2")
        return errors

    mode = manifest.get("mode", "manifest")
    if mode not in ("manifest", "iframe"):
        errors.append("'mode' must be 'manifest' or 'iframe'")
        return errors

    pages = manifest.get("pages")
    if pages is not None and not isinstance(pages, list):
        errors.append("'pages' must be a list")
        return errors

    _validate_rpc_block(manifest, errors)

    # theme is validated (and rendered) only under schema 2; in a v1 manifest a
    # stray "theme" key stays inert like every other unknown key (v1 freeze).
    if schema >= 2 and manifest.get("theme") is not None:
        _validate_theme(manifest.get("theme"), errors)

    if mode == "iframe":
        # Iframe mode: pages need 'src' pointing to HTML files, no widgets
        for i, page in enumerate(pages or []):
            if not isinstance(page, dict):
                errors.append(f"Page {i} must be an object")
                continue
            if not page.get("id"):
                errors.append(f"Page {i} missing 'id'")
            if not page.get("title"):
                errors.append(f"Page {i} missing 'title'")
            src = page.get("src", "")
            if not src:
                errors.append(f"Page {i}: iframe mode requires 'src' field")
            elif ".." in src or src.startswith("/") or src.startswith("\\"):
                errors.append(f"Page {i}: invalid 'src' path (no traversal allowed)")
        return errors

    # Manifest mode: validate page + widget structure
    _all_source_ids: set = set()
    for i, page in enumerate(pages or []):
        where = f"Page {i}"
        if not isinstance(page, dict):
            errors.append(f"{where} must be an object")
            continue
        if not page.get("id"):
            errors.append(f"{where} missing 'id'")
        if not page.get("title"):
            errors.append(f"{where} missing 'title'")

        # data_sources are validated (and fetched) only under schema 2; in v1 a
        # stray key stays inert (v1 freeze).
        source_ids = _validate_data_sources(page, where, errors, seen_global=_all_source_ids) \
            if schema >= 2 else set()
        if schema >= 2:
            _validate_permission(page, where, errors)

        for j, widget in enumerate(page.get("widgets", [])):
            wwhere = f"{where} widget {j}"
            if not isinstance(widget, dict):
                errors.append(f"{wwhere} must be an object")
                continue
            wtype = widget.get("type", "")
            if wtype in V2_CONTAINER_TYPES:
                if schema < 2:
                    errors.append(f"{wwhere}: widget type '{wtype}' requires \"schema\": 2")
                else:
                    _validate_container(widget, wwhere, source_ids, errors)
                continue
            _validate_plain_widget(widget, wwhere, schema, source_ids, errors)

    # Schema 2 only (v1 freeze): widget ids must be unique across the WHOLE
    # manifest — mock maps, the widget-data route and the copilot's diff keys
    # all resolve by id, and a duplicate silently resolves to whichever page
    # declared it first (or last, for mocks).
    if schema >= 2:
        _seen_widget_ids: set = set()
        _action_names: set = set()
        _readable_names: set = set()
        for page in pages or []:
            if not isinstance(page, dict):
                continue
            for src in page.get("data_sources") or []:
                if isinstance(src, dict) and isinstance(src.get("rpc_method"), str):
                    _readable_names.add(src["rpc_method"])
            for w in iter_manifest_widgets(page):
                wid = w.get("id")
                if wid:
                    if wid in _seen_widget_ids:
                        errors.append(f"duplicate widget id '{wid}' "
                                      "(ids must be unique across the whole manifest)")
                    else:
                        _seen_widget_ids.add(wid)
                if w.get("type") == "action_button" and isinstance(w.get("action_rpc_method"), str):
                    _action_names.add(w["action_rpc_method"])
                elif isinstance(w.get("rpc_method"), str):
                    _readable_names.add(w["rpc_method"])
        # An action's method must never ALSO be bound as a readable
        # rpc_method / data source — that alias would make the write
        # viewer-fetchable (and 5s-cached) through the widget-data and source
        # endpoints, defeating the whole point of the separate field.
        for name in sorted(_action_names & _readable_names):
            errors.append(
                f"action_rpc_method '{name}' is also bound as a readable "
                "rpc_method/data source — actions are writes and need a "
                "dedicated handler name")

    return errors


def iter_manifest_widgets(page: dict):
    """Yield every PLAIN widget on a manifest-mode page, recursing one level
    into v2 containers (section/tabs)."""
    if not isinstance(page, dict):
        return
    for entry in page.get("widgets") or []:
        if not isinstance(entry, dict):
            continue
        etype = entry.get("type")
        if etype == "section":
            for child in entry.get("widgets") or []:
                if isinstance(child, dict):
                    yield child
        elif etype == "tabs":
            for tab in entry.get("tabs") or []:
                if isinstance(tab, dict):
                    for child in tab.get("widgets") or []:
                        if isinstance(child, dict):
                            yield child
        else:
            yield entry


def manifest_widget_rpc_names(manifest: dict) -> set:
    """RPC methods that manifest-mode widgets INVOKE on page load — widget
    ``rpc_method`` / ``save_rpc_method`` fields (recursing containers) plus v2
    page-level ``data_sources``. Each must have a matching
    ``@plugin.on_dashboard("<name>")`` handler or the widget renders an error
    box for every customer."""
    names: set = set()
    if not isinstance(manifest, dict):
        return names
    for page in manifest.get("pages") or []:
        if not isinstance(page, dict):
            continue
        for src in page.get("data_sources") or []:
            if isinstance(src, dict):
                m = src.get("rpc_method")
                if isinstance(m, str) and m:
                    names.add(m)
        for widget in iter_manifest_widgets(page):
            # action_rpc_method only counts on REAL actions (schema-2
            # action_button widgets) — v1 fleet manifests may carry stray
            # inert keys of that name.
            _schema2 = manifest.get("schema", 1) == 2
            _is_action = _schema2 and widget.get("type") == "action_button"
            for field_name in (("rpc_method", "save_rpc_method", "action_rpc_method")
                               if _is_action else ("rpc_method", "save_rpc_method")):
                m = widget.get(field_name)
                if isinstance(m, str) and m:
                    names.add(m)
    return names


def manifest_declared_rpc_names(manifest: dict) -> set:
    """Keys of the ``rpc`` declaration block. A declaration is an ALLOWLIST
    grant, not a promise of implementation, so a declared-but-unimplemented
    name is a WARNING in the artifact validator, never an error."""
    names: set = set()
    if not isinstance(manifest, dict):
        return names
    rpc = manifest.get("rpc")
    if isinstance(rpc, dict):
        names.update(n for n in rpc.keys() if isinstance(n, str))
    return names


# ── Sandbox SQL validator (vendored from core/api_sandbox.py) ───────────────
#
# The platform's SQL dry-check imports ``_validate_plugin_sql`` from
# api_sandbox. That module is platform-only, so the function and its helpers
# are inlined here. Error strings are surfaced verbatim inside the
# ``plugin_sql_rejected`` message and must stay byte-identical.

_SQL_ALLOWED_STATEMENTS = {"SELECT", "INSERT", "UPDATE", "DELETE", "CREATE TABLE", "ALTER TABLE", "DROP TABLE", "CREATE INDEX", "DROP INDEX"}
_SQL_BLOCKED_PATTERNS = [
    "CREATE FUNCTION", "CREATE TRIGGER", "CREATE EXTENSION", "CREATE ROLE", "CREATE USER",
    "COPY ", "pg_dump", "pg_restore", "GRANT ", "REVOKE ", "ALTER ROLE", "ALTER SYSTEM",
    "SET ROLE", "SET SESSION", "RESET ROLE", "LISTEN ", "NOTIFY ", "PREPARE ",
    "DO $$", "DO $", "LOAD ", "lo_import", "lo_export",
    "pg_catalog", "pg_stat", "information_schema",
    # Function-based role/privilege manipulation and file/network access that
    # can ride *inside* an otherwise-allowlisted SELECT.
    "set_config", "current_setting", "pg_read_file", "pg_read_binary_file",
    "pg_ls_dir", "pg_logdir_ls", "pg_stat_file", "dblink",
    "lo_get", "lo_put", "lo_from_bytea", "DROP SCHEMA", "TRUNCATE",
    "SECURITY DEFINER",
]

_DOLLAR_TAG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Schema-qualified reference to a plugin sandbox schema:
#   plugin_<safe_pid>_<srv>.<identifier>
_RE_PLUGIN_SCHEMA_QUALIFIED = re.compile(
    r'(?<![A-Za-z0-9_."])"?(plugin_[a-z0-9_]+)"?\s*\.',
    re.IGNORECASE,
)


def _assert_single_statement(sql: str) -> None:
    """Reject SQL containing more than one top-level statement.

    psycopg uses the simple query protocol for parameter-less queries, which
    executes *every* semicolon-separated statement in one round-trip, so the
    sandbox enforces exactly one statement per call.

    The scan is string-, identifier- and dollar-quote-aware so that semicolons
    inside literals are not treated as separators. Expects comment-stripped
    input (caller strips ``--`` and ``/* */`` first).
    """
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "$":
            # Possible dollar-quoted string: $tag$ ... $tag$ (tag may be empty).
            close_tag = sql.find("$", i + 1)
            if close_tag != -1:
                inner = sql[i + 1:close_tag]
                if inner == "" or _DOLLAR_TAG_RE.match(inner):
                    tag = sql[i:close_tag + 1]
                    end = sql.find(tag, close_tag + 1)
                    if end == -1:
                        return  # unterminated dollar-quote -> invalid SQL anyway
                    i = end + len(tag)
                    continue
            i += 1
            continue
        if c == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2  # '' escaped quote, stay in string
                        continue
                    i += 1
                    break
                i += 1
            continue
        if c == '"':
            i += 1
            while i < n:
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if c == ";":
            # Trailing semicolons / whitespace are fine; real content after a
            # top-level ';' means a second statement.
            if sql[i + 1:].strip(" \t\r\n;"):
                raise ValueError(
                    "Only one SQL statement is allowed per call; "
                    "remove the ';'-separated statement(s)"
                )
            return
        i += 1


def _strip_sql_comments(sql: str) -> str:
    """Remove SQL comments (``--`` line and ``/* */`` block) that Postgres
    would treat as comments, leaving comment markers that live INSIDE a string
    literal / quoted identifier / dollar-quoted body untouched.

    Uses the SAME quote/dollar-quote state machine as
    ``_assert_single_statement`` so the validator's notion of "inside a string"
    matches Postgres's real lexer. Removed comment spans are replaced with a
    single space so adjacent tokens never fuse.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "$":
            # Possible dollar-quoted string: $tag$ ... $tag$ (tag may be empty).
            close_tag = sql.find("$", i + 1)
            if close_tag != -1:
                inner = sql[i + 1:close_tag]
                if inner == "" or _DOLLAR_TAG_RE.match(inner):
                    tag = sql[i:close_tag + 1]
                    end = sql.find(tag, close_tag + 1)
                    if end == -1:
                        # Unterminated dollar-quote -> rest is "inside" the
                        # string; copy verbatim (invalid SQL anyway).
                        out.append(sql[i:])
                        return "".join(out)
                    out.append(sql[i:end + len(tag)])
                    i = end + len(tag)
                    continue
            out.append(c)
            i += 1
            continue
        if c == "'":
            out.append(c)
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        out.append(sql[i:i + 2])  # '' escaped quote, stay in string
                        i += 2
                        continue
                    out.append(sql[i])
                    i += 1
                    break
                out.append(sql[i])
                i += 1
            continue
        if c == '"':
            out.append(c)
            i += 1
            while i < n:
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        out.append(sql[i:i + 2])
                        i += 2
                        continue
                    out.append(sql[i])
                    i += 1
                    break
                out.append(sql[i])
                i += 1
            continue
        # Outside any quoted region: real comments start here.
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            # -- line comment: drop through end of line (the newline is kept).
            nl = sql.find("\n", i + 2)
            if nl == -1:
                out.append(" ")
                return "".join(out)
            out.append(" ")
            i = nl
            continue
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            # /* block comment */: drop through the closing */.
            end = sql.find("*/", i + 2)
            if end == -1:
                # Unterminated block comment -> invalid SQL; drop the rest.
                out.append(" ")
                return "".join(out)
            out.append(" ")
            i = end + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _mask_sql_string_literals(sql: str) -> str:
    """Return `sql` with single-quoted string and dollar-quoted bodies replaced
    by spaces, preserving length-neutral structure for token scans.

    Double-quoted identifiers are copied through verbatim: a quoted identifier
    such as ``"plugin_x_999"`` IS a real schema reference (case-preserving
    identifier, not data), so it must stay visible to the check. Expects
    comment-stripped input.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "$":
            close_tag = sql.find("$", i + 1)
            if close_tag != -1:
                inner = sql[i + 1:close_tag]
                if inner == "" or _DOLLAR_TAG_RE.match(inner):
                    tag = sql[i:close_tag + 1]
                    end = sql.find(tag, close_tag + 1)
                    if end == -1:
                        # Unterminated dollar-quote -> rest is "inside" the
                        # string; mask it all (invalid SQL anyway).
                        out.append(" " * (n - i))
                        return "".join(out)
                    out.append(" " * (end + len(tag) - i))
                    i = end + len(tag)
                    continue
            out.append(c)
            i += 1
            continue
        if c == "'":
            # Mask the whole single-quoted string (including the quotes) so its
            # contents can never be read as identifiers.
            out.append(" ")
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        out.append("  ")  # '' escaped quote, stay in string
                        i += 2
                        continue
                    out.append(" ")
                    i += 1
                    break
                out.append(" ")
                i += 1
            continue
        if c == '"':
            # Double-quoted identifier: real identifier, copy through verbatim.
            out.append(c)
            i += 1
            while i < n:
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        out.append(sql[i:i + 2])
                        i += 2
                        continue
                    out.append(sql[i])
                    i += 1
                    break
                out.append(sql[i])
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _validate_plugin_sql(sql: str, active_schema: Optional[str] = None) -> str:
    """Validate and classify a SQL statement. Returns the statement type or raises ValueError."""
    # Strip SQL comments to prevent blocklist bypass. String/identifier/
    # dollar-quote-aware so comment markers INSIDE a literal are preserved
    # (Postgres executes them as data, not as comments).
    stripped = _strip_sql_comments(sql)
    # Strip Unicode zero-width characters
    stripped = re.sub(r"[​‌‍﻿]", "", stripped)

    # Reject multiple statements BEFORE the blocklist/allowlist, so a
    # benign-looking allowlisted prefix can't smuggle a second statement past
    # psycopg's simple (multi-statement) protocol.
    _assert_single_statement(stripped)

    # Cross-tenant SQL: any reference to a `plugin_`-prefixed schema OTHER than
    # the active schema is a cross-tenant access attempt. Only checkable when
    # the caller knows the live schema name — the artifact validator does not,
    # so it passes active_schema=None and this check stays off there (same as
    # on the platform).
    if active_schema is not None:
        masked = _mask_sql_string_literals(stripped)
        for m in _RE_PLUGIN_SCHEMA_QUALIFIED.finditer(masked):
            referenced = m.group(1).lower()
            if referenced != active_schema.lower():
                raise ValueError(
                    "Cross-schema reference not allowed: "
                    f"{referenced!r}. Plugin SQL must be unqualified (it runs "
                    "in your server's schema via search_path)."
                )

    normalized = " ".join(stripped.strip().split()).upper()

    if not normalized:
        raise ValueError("Empty SQL statement")

    # Check blocklist
    for pattern in _SQL_BLOCKED_PATTERNS:
        if pattern.upper() in normalized:
            raise ValueError(f"Blocked SQL pattern: {pattern}")

    # CREATE UNIQUE INDEX is CREATE INDEX plus a uniqueness constraint — same
    # security profile, same sandbox schema. Normalize the returned type so the
    # DDL rate limiter treats it as CREATE INDEX.
    if normalized.startswith("CREATE UNIQUE INDEX"):
        return "CREATE INDEX"

    # Check allowlist
    for allowed in _SQL_ALLOWED_STATEMENTS:
        if normalized.startswith(allowed):
            return allowed

    raise ValueError(f"SQL statement type not allowed. Permitted: {', '.join(sorted(_SQL_ALLOWED_STATEMENTS))}")


@dataclass
class Finding:
    """One validation result.

    ``severity`` is either ``"error"`` (blocks submission) or ``"warning"``
    (allows submission but surfaced to the dev as a fix-this-soon hint).
    ``path`` and ``line`` are populated when the issue is locatable in a
    specific file.
    """
    severity: str  # "error" | "warning"
    code: str      # short stable identifier (e.g. "manifest_missing_field")
    message: str   # human-readable
    path: Optional[str] = None
    line: Optional[int] = None
    hint: Optional[str] = None


@dataclass
class ValidationResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def has_errors(self) -> bool:
        return any(f.severity == "error" for f in self.findings)

    def to_payload(self) -> dict[str, Any]:
        """JSON-serializable shape for storage / UI rendering."""
        return {
            "errors": [_finding_to_dict(f) for f in self.errors],
            "warnings": [_finding_to_dict(f) for f in self.warnings],
            "has_errors": self.has_errors,
        }

    def add(
        self, severity: str, code: str, message: str,
        *, path: Optional[str] = None, line: Optional[int] = None,
        hint: Optional[str] = None,
    ) -> None:
        self.findings.append(Finding(
            severity=severity, code=code, message=message,
            path=path, line=line, hint=hint,
        ))


def _finding_to_dict(f: Finding) -> dict[str, Any]:
    out = {"severity": f.severity, "code": f.code, "message": f.message}
    if f.path: out["path"] = f.path
    if f.line is not None: out["line"] = f.line
    if f.hint: out["hint"] = f.hint
    return out


# ── Validator ──────────────────────────────────────────────────────────────

def validate_artifact(
    *,
    plugin_id: str,
    version: str,
    artifact_bytes: bytes,
    effective_manifest: Optional[dict] = None,
    stored_dashboard_manifest: Optional[dict] = None,
) -> ValidationResult:
    """Run all pre-submit checks on a zipped plugin artifact.

    Args:
        plugin_id:   the platform's plugin id (must match ``manifest.id``).
        version:     the platform-assigned version (must match ``manifest.version``).
        artifact_bytes: raw .zip content as fetched from artifact storage.
        effective_manifest: optional override for capability checks (when the
            caller has a merged view of declared + auto-detected caps).
        stored_dashboard_manifest: a dashboard manifest that lives OUTSIDE the
            zip but is known to survive onto this version — the version row's
            ``dashboard_manifest`` column (Dev Portal Dashboard Editor), or the
            carry-forward candidate for a version being ingested. Suppresses the
            false ``dashboard_handler_without_manifest`` warning for
            editor-authored dashboards. Only manifest-mode counts (iframe
            manifests render zip-shipped files, so a zip without ``dashboard/``
            genuinely has no iframe dashboard). The `yourbot validate` CLI has
            no access to platform state and leaves this None.

    Returns a :class:`ValidationResult`. ``result.has_errors`` is the gate
    for blocking submission; ``result.warnings`` are surfaced to the dev as
    "fix this soon" but don't block.
    """
    result = ValidationResult()

    # 1. Zip integrity
    try:
        zf = zipfile.ZipFile(io.BytesIO(artifact_bytes), "r")
    except zipfile.BadZipFile as e:
        result.add(
            "error", "artifact_unreadable",
            f"The plugin artifact is not a valid zip file: {e}",
            hint="Re-pull from GitHub. If this persists, file a bug report.",
        )
        return result

    namelist = zf.namelist()

    # 2. __main__.py entry point
    if "__main__.py" not in namelist:
        result.add(
            "error", "missing_entry_point",
            "Required file __main__.py not found at the root of your plugin.",
            hint="Every plugin needs a top-level __main__.py that creates a "
                 "Plugin() instance and registers handlers.",
        )

    # 3. manifest.json parse + required fields
    manifest_dict: Optional[dict] = None
    if "manifest.json" not in namelist:
        result.add(
            "error", "missing_manifest",
            "Required file manifest.json not found at the root of your plugin.",
            hint="Add a manifest.json with {id, name, version, capabilities_requested}.",
        )
    else:
        try:
            manifest_text = zf.read("manifest.json").decode("utf-8")
        except UnicodeDecodeError:
            result.add(
                "error", "manifest_encoding",
                "manifest.json is not valid UTF-8.",
                hint="Save manifest.json as UTF-8 (no BOM).",
            )
        else:
            try:
                parsed = json.loads(manifest_text)
                if not isinstance(parsed, dict):
                    result.add(
                        "error", "manifest_not_object",
                        "manifest.json must be a JSON object at the top level.",
                        path="manifest.json",
                    )
                else:
                    manifest_dict = parsed
            except json.JSONDecodeError as e:
                result.add(
                    "error", "manifest_invalid_json",
                    f"manifest.json is not valid JSON: {e.msg} (line {e.lineno}).",
                    path="manifest.json", line=e.lineno,
                    hint="Run a JSON linter or `python -m json.tool manifest.json` "
                         "to find the issue.",
                )

    # 4. Manifest field-by-field checks (only if manifest parsed cleanly)
    if manifest_dict is not None:
        for field_name, descr in _REQUIRED_MANIFEST_FIELDS.items():
            val = manifest_dict.get(field_name)
            if not val or not str(val).strip():
                result.add(
                    "error", "manifest_missing_field",
                    f"manifest.json is missing required field \"{field_name}\".",
                    path="manifest.json",
                    hint=f"Add \"{field_name}\": <{descr}>.",
                )

        # Plugin id must match what the platform expects.
        m_id = str(manifest_dict.get("id") or "").strip()
        if m_id and m_id != plugin_id:
            result.add(
                "error", "manifest_plugin_id_mismatch",
                f"manifest.json id is \"{m_id}\", but this plugin was created "
                f"as \"{plugin_id}\" on the platform.",
                path="manifest.json",
                hint=f"Change \"id\": \"{plugin_id}\" in manifest.json — "
                     "the platform-side id is authoritative.",
            )

        # Version must match the platform-assigned version
        m_ver = str(manifest_dict.get("version") or "").strip()
        if m_ver and m_ver != version:
            result.add(
                "error", "manifest_version_mismatch",
                f"manifest.json version is \"{m_ver}\", but this version is "
                f"\"{version}\" on the platform.",
                path="manifest.json",
                hint=f"Update manifest.json with \"version\": \"{version}\" "
                     "before pulling, or pull from a tag matching the manifest.",
            )

        # Description quality (warning, not blocking)
        descr = str(manifest_dict.get("description") or "").strip()
        if descr:
            if len(descr) > _MAX_DESCRIPTION_LEN:
                result.add(
                    "warning", "description_too_long",
                    f"Description is {len(descr)} chars (cap is {_MAX_DESCRIPTION_LEN}).",
                    path="manifest.json",
                    hint="Shorten the description or move detail to README.md.",
                )
            elif descr.lower() in _PLACEHOLDER_DESCRIPTIONS:
                result.add(
                    "warning", "description_placeholder",
                    f"Description appears to be a placeholder: \"{descr}\".",
                    path="manifest.json",
                    hint="Write a one-paragraph description so customers know "
                         "what your plugin does.",
                )

        # Capability declaration sanity
        declared_caps = manifest_dict.get("capabilities_requested") or []
        if not isinstance(declared_caps, list):
            result.add(
                "error", "capabilities_not_list",
                "manifest.json field \"capabilities_requested\" must be a JSON array.",
                path="manifest.json",
                hint='Use ["discord:send_message", "events:message_content"] format.',
            )
        else:
            for cap in declared_caps:
                if not isinstance(cap, str) or ":" not in cap:
                    result.add(
                        "error", "capability_malformed",
                        f"Capability {cap!r} is not in the expected format.",
                        path="manifest.json",
                        hint="Capabilities look like \"namespace:action\" "
                             "(e.g. \"discord:send_message\").",
                    )

    # 5. Capability-declared-vs-used cross check
    # Pull all .py sources from the artifact
    py_paths = [n for n in namelist
                if n.endswith(".py") and not _is_excluded_path(n)]
    py_sources: list[bytes] = []
    for p in py_paths:
        try:
            py_sources.append(zf.read(p))
        except Exception:
            continue
    if py_paths:
        try:
            detected = set(_detect_capabilities(py_sources))
        except Exception:
            detected = set()

        if manifest_dict is not None:
            cap_source = effective_manifest if effective_manifest is not None else manifest_dict
            declared = set(manifest_capabilities(cap_source))
            undeclared_used = detected - declared
            for cap in sorted(undeclared_used):
                result.add(
                    "error", "capability_used_not_declared",
                    f"Your code calls SDK methods that need \"{cap}\", but "
                    f"this capability isn't in manifest.json's "
                    f"\"capabilities_requested\".",
                    path="manifest.json",
                    hint=f"Add \"{cap}\" to capabilities_requested, or stop using "
                         "the corresponding SDK method.",
                )
            unused_declared = declared - detected - {"storage:kv", "storage:sql", "interaction:respond"}
            for cap in sorted(unused_declared):
                result.add(
                    "warning", "capability_declared_not_used",
                    f"Capability \"{cap}\" is declared in manifest.json but "
                    f"your code doesn't appear to use it.",
                    path="manifest.json",
                    hint="Remove unused capabilities — customers are more "
                         "likely to install plugins that ask for fewer permissions.",
                )

    # 5b. Bundled-import + dashboard-wiring sanity. Catches two common (esp.
    # AI-generated) mistakes that pass every other check but break silently at
    # runtime: importing a module that was never shipped/declared, and writing an
    # @on_dashboard handler without the manifest section that renders it.
    if py_paths:
        import ast as _ast
        import re as _re
        import sys as _sys

        # Imports must come from real ast.Import / ast.ImportFrom nodes —
        # a raw-text scan also matches prose inside docstrings and strings
        # (customer-reported: a docstring line "from _begin until every
        # statement has landed." flagged a phantom "_begin" dependency).
        # Relative imports (level > 0) are bundled by definition and skipped.
        import_roots: set[str] = set()
        for _src in py_sources:
            _txt = _src.decode("utf-8", "replace")
            try:
                _tree = _ast.parse(_txt)
            except (SyntaxError, ValueError):
                # Unparseable source can't be AST-scanned; keep the line-based
                # scan for that file so broken code doesn't lose coverage.
                for _m in _re.finditer(r"(?m)^[ \t]*(?:import|from)[ \t]+([A-Za-z_]\w*)", _txt):
                    import_roots.add(_m.group(1))
                continue
            for _node in _ast.walk(_tree):
                if isinstance(_node, _ast.Import):
                    for _imp_alias in _node.names:
                        import_roots.add(_imp_alias.name.split(".")[0])
                elif isinstance(_node, _ast.ImportFrom) and not _node.level and _node.module:
                    import_roots.add(_node.module.split(".")[0])

        _stdlib = set(getattr(_sys, "stdlib_module_names", set()))
        _bundled = {n[:-3] for n in namelist if n.endswith(".py") and "/" not in n}
        _bundled |= {n.split("/")[0] for n in namelist if n.endswith("/__init__.py")}
        _always_ok = {"yourbot_sdk", "mmo_maid_sdk", "__future__", "__main__"}
        # import root -> PyPI distribution name for the common mismatches
        _alias = {"google": "protobuf", "yaml": "pyyaml", "PIL": "pillow",
                  "cv2": "opencv-python", "bs4": "beautifulsoup4",
                  "dateutil": "python-dateutil", "dotenv": "python-dotenv"}
        _req_roots: set[str] = set()
        if "requirements.txt" in namelist:
            try:
                for _line in zf.read("requirements.txt").decode("utf-8", "replace").splitlines():
                    _line = _line.strip()
                    if not _line or _line.startswith("#"):
                        continue
                    _name = _re.split(r"[=<>!~;\[ ]", _line, maxsplit=1)[0].strip().lower().replace("_", "-")
                    if _name:
                        _req_roots.add(_name)
            except Exception:
                pass
        for _root in sorted(import_roots):
            if _root in _stdlib or _root in _bundled or _root in _always_ok:
                continue
            _dist = _alias.get(_root, _root).lower().replace("_", "-")
            if _dist in _req_roots or _root.lower().replace("_", "-") in _req_roots:
                continue
            result.add(
                "warning", "import_not_bundled_or_declared",
                f"Your code imports \"{_root}\", but it is neither bundled in the zip "
                f"nor listed in requirements.txt. The sandbox has no network and a "
                f"read-only filesystem, so this import will fail at runtime (and if it "
                f"is wrapped in try/except, the feature silently does nothing).",
                hint=f"Ship {_root}.py inside the plugin zip, or add its package to "
                     "requirements.txt (e.g. a *_pb2 module needs \"protobuf\").",
            )

        _has_dash_handler = any(b".on_dashboard(" in s for s in py_sources)
        # The dashboard is surfaced by a dashboard_manifest.json file at the zip root,
        # a dashboard/ directory (iframe mode), a manifest-embedded key, OR by the
        # Dev Portal Dashboard Editor (stored on the version row, not in the zip) —
        # callers pass that last one via stored_dashboard_manifest.
        _has_dash_manifest = (
            any(n == "dashboard_manifest.json" or n.endswith("/dashboard_manifest.json") for n in namelist)
            or any("/dashboard/" in n or n.startswith("dashboard/") for n in namelist)
            or bool(isinstance(manifest_dict, dict) and manifest_dict.get("dashboard_manifest"))
            or bool(
                isinstance(stored_dashboard_manifest, dict)
                and stored_dashboard_manifest
                and str(stored_dashboard_manifest.get("mode", "manifest")) == "manifest"
            )
        )
        if _has_dash_handler and not _has_dash_manifest:
            result.add(
                "warning", "dashboard_handler_without_manifest",
                "Your code has an @plugin.on_dashboard handler, but nothing surfaces it: "
                "there is no dashboard_manifest.json in the zip and no dashboard/ directory. "
                "Without one, the platform renders no dashboard and the handler is never called.",
                hint="Ship a dashboard_manifest.json at the zip root (with pages/widgets whose "
                     "rpc_method names match your handlers), or configure it in the Dev Portal "
                     "Dashboard Editor. See reference/dashboards.md.",
            )

        # 5b-2. Dashboard manifest schema + handler wiring — ERROR level, so a
        # broken dashboard blocks submit-for-review instead of shipping widgets
        # that render a 10s-timeout error box for every customer. The schema is
        # vendored above from core/dashboard_manifest.py (single source for
        # every ingestion path).
        _dash_zip_name = next(
            (n for n in namelist
             if n == "dashboard_manifest.json" or n.endswith("/dashboard_manifest.json")),
            None,
        )
        if _dash_zip_name is not None:
            _vdm = validate_dashboard_manifest
            _dash_widget_rpc_names = manifest_widget_rpc_names
            _dash_declared_rpc_names = manifest_declared_rpc_names
            _dash_obj = None
            try:
                _dash_obj = json.loads(zf.read(_dash_zip_name))
            except Exception:
                result.add(
                    "error", "dashboard_manifest_unparseable",
                    f"{_dash_zip_name} is not valid JSON — the platform cannot render "
                    "a dashboard from it, so it would be dropped for every customer.",
                    hint="Fix the JSON (a trailing comma is the usual culprit) and re-upload.",
                )
            if _dash_obj is not None:
                if not isinstance(_dash_obj, dict):
                    _dash_errors = ["dashboard_manifest.json must be a JSON object"]
                else:
                    _dash_errors = _vdm(_dash_obj)
                for _msg in _dash_errors[:10]:
                    result.add(
                        "error", "dashboard_manifest_invalid",
                        f"dashboard_manifest.json: {_msg}",
                        hint="See reference/dashboards.md for the manifest schema.",
                    )
                if not _dash_errors and isinstance(_dash_obj, dict):
                    # Handler cross-check. Matches both decorator and
                    # direct-call registration styles.
                    _handler_re = re.compile(
                        rb"\.on_dashboard\(\s*[\"']([A-Za-z0-9_\-]{1,64})[\"']")
                    _handler_names = set()
                    _literal_calls = 0
                    for _src in py_sources:
                        for _hm in _handler_re.finditer(_src):
                            _literal_calls += 1
                            _handler_names.add(_hm.group(1).decode("ascii", "replace"))
                    # The regex only sees literal-string registration. Code that
                    # registers dynamically (plugin.on_dashboard(name)(fn) in a
                    # loop) has more .on_dashboard( call sites than literal
                    # matches — in that case a "missing" handler may exist at
                    # runtime, so downgrade to warning instead of blocking.
                    _total_calls = sum(_src.count(b".on_dashboard(") for _src in py_sources)
                    _dynamic_reg = _total_calls > _literal_calls
                    # Widget-invoked methods fire on page load — a missing
                    # handler errors for every customer, so it blocks submit
                    # (unless registration is dynamic and unprovable here).
                    for _wanted in sorted(_dash_widget_rpc_names(_dash_obj)):
                        if _wanted not in _handler_names:
                            result.add(
                                "warning" if _dynamic_reg else "error",
                                "dashboard_rpc_method_missing_handler",
                                f'dashboard_manifest.json widget references RPC method '
                                f'"{_wanted}", but no @plugin.on_dashboard("{_wanted}") '
                                "handler exists in the code — that widget errors for "
                                "every customer on page load."
                                + (" (Registration looks dynamic, so this may resolve "
                                   "at runtime — verify on a test server.)" if _dynamic_reg else ""),
                                hint=f'Add @plugin.on_dashboard("{_wanted}") in __main__.py, '
                                     "or remove the widget's reference to it.",
                            )
                    # Declared-but-unimplemented rpc-block names are only a
                    # warning: the declaration is an allowlist grant, and the
                    # canned iframe dashboard probes optional methods
                    # (get_settings) then degrades gracefully.
                    for _wanted in sorted(_dash_declared_rpc_names(_dash_obj)
                                          - _handler_names
                                          - _dash_widget_rpc_names(_dash_obj)):
                        result.add(
                            "warning", "dashboard_declared_rpc_no_handler",
                            f'dashboard_manifest.json declares RPC method "{_wanted}" in its '
                            f'"rpc" block, but no @plugin.on_dashboard("{_wanted}") handler '
                            "exists — calls to it will fail at runtime.",
                            hint="Implement the handler, or drop the declaration if the "
                                 "dashboard treats it as optional.",
                        )

    # 5c. Slash-command consistency — the checks that keep "what the dev
    # reviewed" equal to "what Discord runs". Discord registration is driven
    # solely by manifest.slash_commands (names lowercased by the platform),
    # while runtime dispatch is driven solely by @plugin.on_slash_command
    # decorators (matched case-sensitively by the SDK). A drifted name ships a
    # command that registers but never answers. Mirrors the platform validator
    # and its publish-time reserved/duplicate/charset gate.
    # NOTE: the publish-time auto-merge scans EVERY .py in the zip (no test/
    # vendor exclusion), so this check must scan the same set.
    if manifest_dict is not None:
        cmd_scan_sources: list[bytes] = []
        for n in namelist:
            if not n.endswith(".py"):
                continue
            try:
                cmd_scan_sources.append(zf.read(n))
            except Exception:
                continue
        _validate_slash_commands(result, manifest_dict, cmd_scan_sources)
        # 5d. Cron schedule consistency — in production (pool mode) scheduled
        # work fires ONLY from manifest "cron" entries; the SDK's own
        # cron/schedule threads never start. Drift between the manifest and
        # the @plugin.cron decorators means schedules that silently never
        # run. Scans the same unfiltered .py set as the command scan.
        _validate_cron_entries(result, manifest_dict, cmd_scan_sources)

    # 6. Forbidden patterns (would actually fail at sandbox runtime).
    for path in py_paths:
        try:
            data = zf.read(path)
        except Exception:
            continue
        for finding in _scan_forbidden_patterns(path, data):
            result.findings.append(finding)
        # 6b. Contract checks that look INSIDE the call — the classes that
        # actually kill plugins in production and that nothing else here sees.
        for finding in _scan_ctx_call_signatures(path, data):
            result.findings.append(finding)
        for finding in _scan_plugin_sql(path, data):
            result.findings.append(finding)

    # 7. Junk files that shouldn't be in a published plugin
    for n in namelist:
        if "__pycache__" in n or n.endswith(".pyc"):
            result.add(
                "warning", "bytecode_in_artifact",
                f"Compiled bytecode file in artifact: {n}",
                path=n,
                hint="Add __pycache__/ and *.pyc to your .gitignore so they "
                     "don't get committed.",
            )
            break
        if n.startswith(".git/") or n == ".git":
            result.add(
                "warning", "git_metadata_in_artifact",
                ".git directory included in artifact.",
                path=n,
                hint="GitHub archive downloads shouldn't include .git — check your "
                     "GitHub release/tag setup.",
            )
            break

    return result


# ── Slash-command consistency (vendored from plugin_validation.py) ─────────

# @<ident>.on_slash_command("name") with single or double quotes — vendored
# from artifact_store._SLASH_CMD_RE (the platform's publish-time auto-merge
# uses the same pattern, so local and platform scans can never disagree).
_SLASH_CMD_RE = re.compile(
    rb'@\w+\.on_slash_command\s*\(\s*["\']([a-zA-Z0-9_\-]+)["\']\s*\)'
)

# Discord command/option names: 1-32 chars of letters, digits, - and _
# (Discord additionally requires lowercase, checked separately so the error
# can say so).
_CMD_NAME_RE = re.compile(r"^[-_\w]{1,32}$")

# Discord ApplicationCommandOptionType values.
_VALID_OPTION_TYPES = set(range(1, 12))

# Top-level command names reserved by built-in YourBot plugins + platform
# commands. Vendored from the platform's command_registry.reserved_command_names()
# — a CI parity test on the platform side keeps this set in sync.
_RESERVED_COMMAND_NAMES = {
    "announce", "appeal", "ban", "beacon", "case", "giveaway", "group",
    "group-admin", "group-alerts", "help", "history", "kick", "leaderboard",
    "lockdown", "maid-bug-report", "music", "note", "pluginbugreport",
    "poll", "purge",
    "quarantine", "quests", "raidmode", "report", "slowmode", "stats",
    "temp-role", "ticket", "tickets", "timeout", "unban", "unlockdown",
    "unquarantine", "untimeout", "warn", "welcome", "yourbot",
}


def _walk_command_options(
    result: ValidationResult, cmd_name: str, options: Any, depth: int = 0,
) -> None:
    """Validate one command's options array (recursing into subcommands).

    Option names and types go to Discord verbatim (slash_sync only fills in
    empty descriptions) — one illegal option 400s the ENTIRE guild bulk PUT
    and wedges every marketplace command sync for that guild behind the error
    backoff, so these are blocking errors."""
    if options is None:
        return
    if not isinstance(options, list):
        result.add(
            "error", "command_options_not_list",
            f"Command \"/{cmd_name}\": \"options\" must be a JSON array.",
            path="manifest.json",
        )
        return
    if depth > 2:  # Discord allows at most group > subcommand > options
        return
    for opt in options:
        if not isinstance(opt, dict):
            result.add(
                "error", "command_option_malformed",
                f"Command \"/{cmd_name}\" has an option that is not a JSON object.",
                path="manifest.json",
            )
            continue
        oname = str(opt.get("name") or "").strip()
        if not oname:
            result.add(
                "error", "command_option_missing_name",
                f"Command \"/{cmd_name}\" has an option with no \"name\".",
                path="manifest.json",
            )
        elif oname != oname.lower() or not _CMD_NAME_RE.match(oname):
            result.add(
                "error", "command_option_name_invalid",
                f"Command \"/{cmd_name}\" option \"{oname}\" is not a valid "
                f"Discord option name.",
                path="manifest.json",
                hint="Option names must be 1-32 chars of lowercase letters, "
                     "digits, - or _. Discord rejects the whole command batch "
                     "for one bad name, which blocks every plugin command on "
                     "the server.",
            )
        otype = opt.get("type")
        if otype is None:
            result.add(
                "error", "command_option_missing_type",
                f"Command \"/{cmd_name}\" option \"{oname or '?'}\" has no \"type\".",
                path="manifest.json",
                hint="Set \"type\": 3=string, 4=integer, 5=boolean, 6=user, "
                     "7=channel, 8=role, 10=number.",
            )
        elif not isinstance(otype, int) or otype not in _VALID_OPTION_TYPES:
            result.add(
                "error", "command_option_type_invalid",
                f"Command \"/{cmd_name}\" option \"{oname or '?'}\" has invalid "
                f"type {otype!r} (must be an integer 1-11).",
                path="manifest.json",
            )
        if opt.get("options") is not None:
            _walk_command_options(result, cmd_name, opt.get("options"), depth + 1)


def _validate_slash_commands(
    result: ValidationResult, manifest_dict: dict, py_sources: list[bytes],
) -> None:
    """Cross-check manifest.slash_commands against @on_slash_command decorators.

    Failure modes this closes (all observed with AI-generated plugins):
      - manifest declares a command with no matching handler → the command
        registers on Discord, the bot auto-defers, no code ever answers, and
        the invoker stares at an eternal "thinking…";
      - a decorator name with uppercase → registration lowercases the name but
        SDK dispatch is a case-sensitive exact match, so the handler is dead;
      - reserved/duplicate/illegal names → the platform refuses the zip at
        publish time, AFTER the dev already reviewed a green draft.
    """
    declared = manifest_dict.get("slash_commands")
    if declared is None:
        declared = []
    if not isinstance(declared, list):
        result.add(
            "error", "command_defs_not_list",
            "manifest.json field \"slash_commands\" must be a JSON array.",
            path="manifest.json",
        )
        return

    handler_names_raw: list[str] = []
    for src in py_sources:
        for m in _SLASH_CMD_RE.finditer(src):
            handler_names_raw.append(m.group(1).decode("utf-8", errors="ignore").strip())
    handler_names = {n.lower() for n in handler_names_raw if n}

    # Handlers whose decorator name isn't lowercase are dead on arrival:
    # Discord registers the lowercased name, the event delivers that lowercase
    # name, and the SDK's dispatch filter compares case-sensitively.
    for raw in sorted({n for n in handler_names_raw if n and n != n.lower()}):
        result.add(
            "error", "command_handler_not_lowercase",
            f"@on_slash_command(\"{raw}\") will never run: Discord registers "
            f"the command as \"/{raw.lower()}\" and dispatch matches the "
            f"decorator name exactly.",
            hint=f"Rename the decorator (and any matching manifest entry) to "
                 f"\"{raw.lower()}\".",
        )

    reserved = _RESERVED_COMMAND_NAMES

    declared_names: list[str] = []
    seen: set[str] = set()
    for c in declared:
        if not isinstance(c, dict) or not str(c.get("name") or "").strip():
            result.add(
                "error", "command_def_malformed",
                "manifest.json has a slash_commands entry with no \"name\".",
                path="manifest.json",
                hint='Each entry needs at least {"name": "...", "description": "..."}.',
            )
            continue
        raw = str(c.get("name")).strip()
        name = raw.lower()
        declared_names.append(name)
        if raw != name:
            result.add(
                "warning", "command_name_not_lowercase",
                f"Command \"/{raw}\" will register as \"/{name}\" — Discord "
                f"command names are lowercase.",
                path="manifest.json",
                hint=f"Use \"{name}\" in manifest.json and in the "
                     f"@on_slash_command decorator so all three match.",
            )
        if not _CMD_NAME_RE.match(name) or re.search(r"\s", name):
            result.add(
                "error", "command_name_invalid",
                f"\"/{name}\" is not a valid Discord command name.",
                path="manifest.json",
                hint="Command names must be 1-32 chars of lowercase letters, "
                     "digits, - or _.",
            )
        if name in seen:
            result.add(
                "error", "command_name_duplicate",
                f"Command \"/{name}\" is declared more than once in "
                f"manifest.json.",
                path="manifest.json",
            )
        seen.add(name)
        _walk_command_options(result, name, c.get("options"))

    # Reserved names block publish outright (artifact_store_put raises), so
    # surface them here first — including handler-only names, which the
    # publish step auto-merges into the manifest before its gate runs.
    for name in sorted((set(declared_names) | handler_names) & reserved):
        result.add(
            "error", "command_name_reserved",
            f"Command name \"/{name}\" is reserved by a built-in YourBot "
            f"plugin and cannot be used by marketplace plugins.",
            path="manifest.json",
            hint="Pick a different name (e.g. a plugin-specific prefix) in "
                 "manifest.json and the @on_slash_command decorator.",
        )

    # Handler-only names get auto-merged into the manifest at publish, where
    # the gate applies the same shape rule — so an oversized decorator name
    # must fail here too, not first at commit.
    for name in sorted(handler_names - set(declared_names)):
        if not _CMD_NAME_RE.match(name):
            result.add(
                "error", "command_name_invalid",
                f"\"/{name}\" (from an @on_slash_command decorator) is not a "
                f"valid Discord command name.",
                hint="Command names must be 1-32 chars of lowercase letters, "
                     "digits, - or _.",
            )

    # Declared-but-no-handler: the command WILL appear in Discord and WILL
    # hang forever when invoked. Only enforceable when the scanner saw literal
    # decorator names; a plugin registering handlers dynamically gets a
    # warning instead of a hard block.
    unhandled = [n for n in declared_names if n not in handler_names]
    if unhandled and handler_names:
        for name in unhandled:
            result.add(
                "error", "command_missing_handler",
                f"Command \"/{name}\" is declared in manifest.json but no "
                f"@on_slash_command(\"{name}\") handler exists in your code. "
                f"It would appear in Discord and hang on \"thinking…\" forever.",
                path="manifest.json",
                hint="Add the handler, or remove the manifest entry. Decorator "
                     "names must be literal strings — the scanner (and the "
                     "publish-time auto-merge) cannot see names built from "
                     "variables.",
            )
    elif unhandled and py_sources:
        result.add(
            "warning", "command_handlers_not_found",
            "manifest.json declares slash commands but no literal "
            "@on_slash_command decorators were found in your code, so the "
            "names could not be verified against their handlers.",
            hint="Use literal string names in @on_slash_command decorators so "
                 "mismatches are caught before your users hit them.",
        )

    # Handler-but-not-declared: publish auto-adds it with an empty description
    # and NO options — it works, but shows "No description" in Discord and its
    # event["options"] is always empty. Worth declaring properly.
    for name in sorted(handler_names - set(declared_names)):
        if name in reserved:
            continue  # already reported as reserved above
        result.add(
            "warning", "command_not_declared",
            f"@on_slash_command(\"{name}\") has no manifest.json entry. It "
            f"will register with no description and no options (so "
            f"event[\"options\"] is always empty).",
            path="manifest.json",
            hint=f"Add {{\"name\": \"{name}\", \"description\": \"...\"}} (plus "
                 f"its options) to slash_commands in manifest.json.",
        )


# ── Cron schedule consistency (vendored from plugin_validation.py) ─────────

# Cap + floor for manifest-declared server-side cron schedules. The runner's
# dispatcher enforces both at fire time too — the validator exists so the dev
# finds out at build time, not by a schedule silently never firing.
_CRON_MAX_ENTRIES = 5
_CRON_MIN_INTERVAL_MIN = 5

# @<ident>.cron("spec") decorator — captures (spec, task_fn_name). Tolerates a
# trailing comment after the decorator and further stacked decorators before
# the def.
_CRON_TASK_RE = re.compile(
    rb"@\w+\.cron\s*\(\s*[\"']([^\"']*)[\"']\s*\)[^\r\n]*\r?\n"
    rb"(?:\s*@[^\r\n]*\r?\n)*"
    rb"\s*def\s+(\w+)"
)
# Any @<ident>.schedule( decorator — interval tasks are not cron-addressable.
_SCHEDULE_TASK_RE = re.compile(rb"@\w+\.schedule\s*\(")


def _cron_parse(spec: str) -> dict:
    """Parse a five-field cron spec (delegates to the SDK's own parser, which
    is semantics- and message-identical to the platform's shared matcher at
    ``mmo_maid/core/cron_match.py``)."""
    from ._plugin import _parse_cron_spec
    return _parse_cron_spec(spec)


def _cron_min_interval(parsed: dict) -> int:
    """Lower bound (minutes) between two consecutive firings of the spec.

    Vendored from ``mmo_maid/core/cron_match.py`` ``min_interval_minutes`` —
    keep byte-for-byte identical behavior.
    """
    minutes = sorted(parsed["minute"])
    if len(minutes) <= 1:
        return 60
    gaps = [b - a for a, b in zip(minutes, minutes[1:])]
    gaps.append(60 - minutes[-1] + minutes[0])  # wrap into the next firing hour
    return min(gaps)


def _validate_cron_entries(
    result: ValidationResult, manifest_dict: dict, py_sources: list[bytes],
) -> None:
    """Cross-check manifest ``cron`` entries against @plugin.cron decorators.

    In production, marketplace plugins run in pool mode where the SDK never
    starts @plugin.cron / @plugin.schedule background threads. Scheduled work
    fires ONLY via manifest ``"cron"`` entries — the platform delivers a
    synthetic ``cron`` event to every enabled install on schedule and the SDK
    routes it to the @plugin.cron task whose function name matches the entry's
    ``name``. So: an entry without a matching task no-ops, and a task without
    a matching entry never runs in production. Both are surfaced here.
    """
    declared = manifest_dict.get("cron")
    if declared is None:
        declared = []
    if not isinstance(declared, list):
        result.add(
            "error", "cron_not_list",
            "manifest.json field \"cron\" must be a JSON array.",
            path="manifest.json",
            hint='Use [{"spec": "0 9 * * *", "name": "daily_summary"}] format.',
        )
        return

    if len(declared) > _CRON_MAX_ENTRIES:
        result.add(
            "error", "cron_too_many",
            f"manifest.json declares {len(declared)} cron entries "
            f"(max {_CRON_MAX_ENTRIES}).",
            path="manifest.json",
            hint="Consolidate related work into fewer scheduled tasks.",
        )

    declared_names: list[str] = []
    seen: set[str] = set()
    for c in declared:
        if (not isinstance(c, dict)
                or not str(c.get("spec") or "").strip()
                or not str(c.get("name") or "").strip()):
            result.add(
                "error", "cron_entry_malformed",
                "manifest.json has a cron entry without both \"spec\" and \"name\".",
                path="manifest.json",
                hint='Each entry needs {"spec": "0 9 * * *", "name": "daily_summary"} '
                     "— name must equal the @plugin.cron function's name.",
            )
            continue
        name = str(c.get("name")).strip()
        spec = str(c.get("spec")).strip()
        if not name.isidentifier():
            result.add(
                "error", "cron_name_invalid",
                f"Cron entry name \"{name}\" is not a valid Python identifier.",
                path="manifest.json",
                hint="The name must match the @plugin.cron function's name exactly.",
            )
        if name in seen:
            result.add(
                "error", "cron_name_duplicate",
                f"Cron entry name \"{name}\" is declared more than once in "
                f"manifest.json.",
                path="manifest.json",
            )
        seen.add(name)
        declared_names.append(name)
        try:
            parsed = _cron_parse(spec)
        except (ValueError, TypeError) as e:
            result.add(
                "error", "cron_spec_invalid",
                f"Cron entry \"{name}\" has an invalid spec: {e}",
                path="manifest.json",
                hint='Five space-separated fields: "minute hour day month dow" '
                     "(UTC, day-of-week 0=Sunday).",
            )
            continue
        if _cron_min_interval(parsed) < _CRON_MIN_INTERVAL_MIN:
            result.add(
                "error", "cron_spec_too_frequent",
                f"Cron entry \"{name}\" (\"{spec}\") can fire more often than "
                f"every {_CRON_MIN_INTERVAL_MIN} minutes — the platform floor.",
                path="manifest.json",
                hint='Use "*/5 * * * *" or slower. For higher-frequency work, '
                     "react to events instead of polling on a schedule.",
            )

    # Code scan: literal @plugin.cron / @plugin.schedule registrations.
    task_names_raw: list[str] = []
    schedule_found = False
    for src in py_sources:
        for m in _CRON_TASK_RE.finditer(src):
            task_names_raw.append(m.group(2).decode("utf-8", errors="ignore").strip())
        if not schedule_found and _SCHEDULE_TASK_RE.search(src):
            schedule_found = True
    task_names = {n for n in task_names_raw if n}

    # @plugin.cron task with no manifest entry — dead code in production.
    for name in sorted(task_names - set(declared_names)):
        result.add(
            "warning", "cron_task_not_in_manifest",
            f"@plugin.cron task \"{name}\" has no matching manifest.json cron "
            f"entry, so it will never run in production (pooled plugins only "
            f"fire manifest-declared schedules).",
            path="manifest.json",
            hint=f'Add {{"spec": "<its cron spec>", "name": "{name}"}} to '
                 '"cron" in manifest.json.',
        )

    # @plugin.schedule is never cron-addressable — it simply doesn't run.
    if schedule_found:
        result.add(
            "warning", "schedule_task_never_runs",
            "@plugin.schedule interval tasks do not run in production (pooled "
            "plugins have no background threads) and cannot be addressed by "
            "manifest cron entries.",
            hint="Convert the task to @plugin.cron and add a matching "
                 '"cron" entry to manifest.json.',
        )

    # Manifest entry with no matching decorated task — the schedule fires but
    # (name-match failing) only runs a task whose raw spec matches exactly.
    missing = [n for n in declared_names if n not in task_names]
    if missing and task_names:
        for name in missing:
            result.add(
                "warning", "cron_missing_task",
                f"Cron entry \"{name}\" has no matching @plugin.cron function "
                f"named \"{name}\" — the schedule will fire but only run a "
                f"task whose spec matches exactly.",
                path="manifest.json",
                hint="Name the manifest entry after the decorated function, "
                     "or rename the function to match.",
            )
    elif missing and py_sources:
        result.add(
            "warning", "cron_tasks_not_found",
            "manifest.json declares cron entries but no literal @plugin.cron "
            "decorators were found in your code, so the names could not be "
            "verified against their tasks.",
            hint="Use literal string specs in @plugin.cron decorators so "
                 "mismatches are caught before the schedule silently no-ops.",
        )


# ── Forbidden-pattern AST scan ─────────────────────────────────────────────

_FORBIDDEN_AST_PATTERNS = [
    ("eval", "forbidden_eval",
     "eval() is forbidden in sandboxed plugins."),
    ("exec", "forbidden_exec",
     "exec() is forbidden in sandboxed plugins."),
    ("compile", "forbidden_compile",
     "compile() to runtime code-gen is forbidden in sandboxed plugins."),
    ("__import__", "forbidden_dunder_import",
     "__import__() bypasses normal imports and is forbidden."),
]
_FORBIDDEN_IMPORTS = {
    "subprocess": ("forbidden_import_subprocess",
                   "subprocess is unavailable in the sandbox (no fork/exec)."),
    "ctypes":     ("forbidden_import_ctypes",
                   "ctypes is unavailable in the sandbox (no FFI)."),
    "socket":     ("forbidden_import_socket",
                   "Direct socket use is unavailable; use SDK proxy.request() instead."),
    "multiprocessing": ("forbidden_import_multiprocessing",
                        "multiprocessing is unavailable in the sandbox."),
}


# ── ctx call-signature lint ────────────────────────────────────────────────
#
# Every other check in this module is about wiring — zip layout, manifest
# fields, declared-vs-used capabilities. None of them look INSIDE a ctx call, so
# the most common way a plugin dies in production was invisible here: the whole
# ctx surface is keyword-only, and a positional call raises TypeError on the
# first event. The SDK swallows handler exceptions, so the plugin looks
# installed while every event silently no-ops until the circuit breaker trips.
#
# The signature table is introspected from THIS SDK's own ``_context`` module
# rather than hard-coded, so it cannot drift the way a copied table would. (The
# platform introspects the installed ``yourbot_sdk`` for the same reason — same
# module, same table.)

_CTX_CLASS_TO_ATTR = {
    "_SecretsApi": "secrets",
    "_KvApi": "kv",
    "_DiscordApi": "discord",
    "_HttpApi": "http",
    "_WsApi": "ws",
    "_InteractionApi": "interaction",
    "_MetricsApi": "metrics",
    "_SqlApi": "sql",
    "_EphemeralApi": "ephemeral",
}
# Names a handler's Context argument realistically goes by. Anything else (a
# local alias, an attribute on a helper object) is skipped rather than guessed
# at — a false ERROR here blocks a legitimate submission.
_CTX_BASE_NAMES = {"ctx", "context"}
_SQL_EXEC_METHODS = {"execute", "query", "query_one", "scalar"}

_ctx_signature_cache: Optional[dict] = None


def _ctx_signatures() -> dict:
    """Map ``(api_attr, method) -> signature spec`` from this SDK's _context.

    Returns an empty dict if the module is not importable, which disables the
    lint rather than failing a submission over an environment problem.
    """
    global _ctx_signature_cache
    if _ctx_signature_cache is not None:
        return _ctx_signature_cache

    table: dict = {}
    try:
        import inspect
        from . import _context as _ctx_mod

        for cls_name, api_attr in _CTX_CLASS_TO_ATTR.items():
            cls = getattr(_ctx_mod, cls_name, None)
            if cls is None:
                continue
            for meth_name, meth in vars(cls).items():
                if meth_name.startswith("_") or not callable(meth):
                    continue
                try:
                    sig = inspect.signature(meth)
                except (TypeError, ValueError):
                    continue
                params = list(sig.parameters.values())[1:]  # drop self
                positional = [
                    p.name for p in params
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                ]
                keyword_only = [p.name for p in params if p.kind == p.KEYWORD_ONLY]
                required_kw = [
                    p.name for p in params
                    if p.kind == p.KEYWORD_ONLY and p.default is p.empty
                ]
                table[(api_attr, meth_name)] = {
                    "positional": positional,
                    "keyword_only": keyword_only,
                    "required_kw": required_kw,
                    "accepts_var_kw": any(p.kind == p.VAR_KEYWORD for p in params),
                    "accepts_var_pos": any(p.kind == p.VAR_POSITIONAL for p in params),
                }
    except Exception:  # pragma: no cover - environment-dependent
        _log.debug("ctx signature lint disabled: _context not importable", exc_info=True)
        table = {}

    _ctx_signature_cache = table
    return table


def _attr_chain(node) -> Optional[list[str]]:
    """Flatten ``a.b.c`` into ``["a", "b", "c"]``; None for anything else."""
    import ast

    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    parts.reverse()
    return parts


def _resolve_ctx_call(chain: list[str]) -> Optional[tuple[str, str]]:
    """``["ctx","discord","send_message"] -> ("discord", "send_message")``.

    Also accepts ``self.ctx.<api>.<method>``, the other common shape.
    """
    if len(chain) == 3 and chain[0] in _CTX_BASE_NAMES:
        return chain[1], chain[2]
    if len(chain) == 4 and chain[0] == "self" and chain[1] in _CTX_BASE_NAMES:
        return chain[2], chain[3]
    return None


def _typeerror_guarded(tree) -> set:
    """ids of nodes lexically inside a ``try:`` that catches TypeError.

    A plugin that probes for an SDK method shape it is not sure exists writes
    ``try: ctx.x.y(new_kwarg=...) except TypeError: <fallback>``. The TypeError
    is the point of the code, not a defect, so those calls must not be reported.

    Deliberately narrow: only a handler naming TypeError counts. A bare
    ``except:`` or ``except Exception`` is ordinary defensive error handling, not
    a signature probe — nearly every handler that touches the Discord API is
    wrapped in one, so treating those as intent would silence real bugs.
    """
    import ast

    guarded: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches = False
        for handler in node.handlers:
            if handler.type is None:  # bare except — too broad to imply intent
                continue
            exc = handler.type
            names: list[str] = []
            if isinstance(exc, ast.Name):
                names = [exc.id]
            elif isinstance(exc, ast.Attribute):
                names = [exc.attr]
            elif isinstance(exc, ast.Tuple):
                for elt in exc.elts:
                    if isinstance(elt, ast.Name):
                        names.append(elt.id)
                    elif isinstance(elt, ast.Attribute):
                        names.append(elt.attr)
            if "TypeError" in names:
                catches = True
                break
        if catches:
            for stmt in node.body:
                for child in ast.walk(stmt):
                    guarded.add(id(child))
    return guarded


def _scan_ctx_call_signatures(path: str, data: bytes) -> list[Finding]:
    """Bind every literal ``ctx.<api>.<method>(...)`` call against the real SDK
    signature. Only flags calls that are certain to raise at runtime."""
    import ast

    table = _ctx_signatures()
    if not table:
        return []
    try:
        tree = ast.parse(data, filename=path)
    except SyntaxError:
        return []  # reported separately by _scan_forbidden_patterns

    guarded = _typeerror_guarded(tree)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        chain = _attr_chain(node.func)
        if not chain:
            continue
        resolved = _resolve_ctx_call(chain)
        if resolved is None or resolved not in table:
            continue
        api, meth = resolved
        spec = table[resolved]
        call = f"ctx.{api}.{meth}()"

        # A *args splat makes the positional count unknowable — skip.
        if any(isinstance(a, ast.Starred) for a in node.args):
            continue
        n_pos = len(node.args)
        if n_pos > len(spec["positional"]) and not spec["accepts_var_pos"]:
            allowed = ", ".join(spec["positional"]) or "none"
            findings.append(Finding(
                severity="error", code="ctx_call_positional_kwonly",
                message=(
                    f"{call} was called with {n_pos} positional argument(s) but "
                    f"accepts at most {len(spec['positional'])}."
                ),
                path=path, line=getattr(node, "lineno", None),
                hint=(
                    f"Arguments to {call} are keyword-only. Positional: {allowed}. "
                    f"Keyword-only: {', '.join(spec['keyword_only']) or 'none'}. "
                    "Pass them by name, e.g. ctx.discord.get_channel(channel_id=cid). "
                    "This raises TypeError on the first call at runtime."
                ),
            ))
            continue  # the kwarg checks below are meaningless once binding fails

        # A **kwargs splat makes the keyword set unknowable — skip those checks.
        if any(k.arg is None for k in node.keywords):
            continue
        supplied = {k.arg for k in node.keywords if k.arg}
        if not spec["accepts_var_kw"]:
            known = set(spec["positional"]) | set(spec["keyword_only"])
            unknown = sorted(supplied - known)
            if unknown:
                # WARNING, not error. An unknown kwarg is the one shape here that
                # is sometimes deliberate: the ctx surface gains arguments over
                # time, so a plugin that must run against several SDK versions
                # probes for the new one and falls back. _typeerror_guarded()
                # already drops the in-line `try:` form of that, but the probe is
                # just as often a list of lambdas called inside a try elsewhere,
                # which no lexical rule can see. Blocking those would reject a
                # correct plugin, so surface it and let the dev judge. It still
                # reaches the AI builder's repair loop, which consumes warnings
                # as well as errors.
                findings.append(Finding(
                    severity="warning", code="ctx_call_unknown_kwarg",
                    message=f"{call} does not accept {', '.join(unknown)}.",
                    path=path, line=getattr(node, "lineno", None),
                    hint=(
                        f"Valid arguments: {', '.join(sorted(known)) or 'none'}. "
                        "This raises TypeError at runtime unless you are probing "
                        "for it inside a try/except TypeError."
                    ),
                ))
        bound_positionally = set(spec["positional"][:n_pos])
        missing = [
            name for name in spec["required_kw"]
            if name not in supplied and name not in bound_positionally
        ]
        if missing:
            findings.append(Finding(
                severity="error", code="ctx_call_missing_required",
                message=f"{call} is missing required argument(s): {', '.join(missing)}.",
                path=path, line=getattr(node, "lineno", None),
                hint="These are keyword-only with no default; the call raises TypeError without them.",
            ))
    return findings


def _scan_plugin_sql(path: str, data: bytes) -> list[Finding]:
    """Dry-run every literal SQL string through the sandbox's own validator.

    The runtime rejects statements the corpus never warned about — most often a
    multi-statement migration string, or a CTE (``WITH ...``), whose leading
    keyword is not on the allowlist. Both are invisible to every other check
    here and both take the plugin's whole schema down at first run.

    Only string literals passed straight to a ``*.sql.execute/query(...)`` call
    (directly, or via a module-level constant) are checked, so dynamically built
    SQL is never guessed at.
    """
    import ast

    try:
        tree = ast.parse(data, filename=path)
    except SyntaxError:
        return []

    # Module-level `NAME = "...sql..."` so `self.sql.execute(_MIGRATION)` resolves.
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    constants[tgt.id] = node.value.value

    findings: list[Finding] = []
    seen: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _SQL_EXEC_METHODS or not node.args:
            continue
        # Receiver must be a `.sql` handle — ctx.sql, self.sql, self._sql, db.sql.
        chain = _attr_chain(node.func.value)
        if not chain or not chain[-1].lstrip("_").endswith("sql"):
            continue

        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            sql_text = arg.value
        elif isinstance(arg, ast.Name) and arg.id in constants:
            sql_text = constants[arg.id]
        else:
            continue
        if not sql_text.strip():
            continue

        try:
            _validate_plugin_sql(sql_text)
        except ValueError as exc:
            line = getattr(node, "lineno", None)
            key = (line or 0, str(exc))
            if key in seen:
                continue
            seen.add(key)
            findings.append(Finding(
                severity="error", code="plugin_sql_rejected",
                message=f"This SQL is rejected by the sandbox: {exc}",
                path=path, line=line,
                hint=(
                    "The sandbox runs one statement per call and only allows "
                    "SELECT, INSERT, UPDATE, DELETE, CREATE/ALTER/DROP TABLE and "
                    "CREATE/DROP INDEX as the leading keyword. Split multi-statement "
                    "migrations into one execute() per statement, and rewrite CTEs "
                    "(WITH ...) as subqueries."
                ),
            ))
        except Exception:  # pragma: no cover - never fail validation over a lint
            continue
    return findings


def _scan_forbidden_patterns(path: str, data: bytes) -> list[Finding]:
    """Walk a Python file's AST looking for patterns that won't work in the
    sandbox. Reports each one as an error with a line number."""
    findings: list[Finding] = []
    try:
        import ast
        tree = ast.parse(data, filename=path)
    except SyntaxError as e:
        findings.append(Finding(
            severity="error", code="syntax_error",
            message=f"{path}: {e.msg} (line {e.lineno})",
            path=path, line=e.lineno,
            hint="Fix the SyntaxError — your plugin won't import as-is.",
        ))
        return findings
    except Exception:
        return findings

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name: Optional[str] = None
            if isinstance(fn, ast.Name):
                name = fn.id
            for forbidden, code, msg in _FORBIDDEN_AST_PATTERNS:
                if name == forbidden:
                    findings.append(Finding(
                        severity="error", code=code, message=msg,
                        path=path, line=getattr(node, "lineno", None),
                        hint="Remove this call — it will be blocked at sandbox runtime.",
                    ))
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = (alias.name or "").split(".")[0]
                if mod in _FORBIDDEN_IMPORTS:
                    code, msg = _FORBIDDEN_IMPORTS[mod]
                    findings.append(Finding(
                        severity="error", code=code, message=msg,
                        path=path, line=getattr(node, "lineno", None),
                        hint=f"Remove the `import {mod}` statement.",
                    ))
        if isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in _FORBIDDEN_IMPORTS:
                code, msg = _FORBIDDEN_IMPORTS[mod]
                findings.append(Finding(
                    severity="error", code=code, message=msg,
                    path=path, line=getattr(node, "lineno", None),
                    hint=f"Remove the `from {mod} import ...` statement.",
                ))
    return findings


def _is_excluded_path(name: str) -> bool:
    """True for paths we shouldn't scan (test code, vendored deps, bytecode)."""
    parts = name.replace("\\", "/").split("/")
    excluded_dirs = {"tests", "test", "__pycache__", ".venv", "venv",
                     "site-packages", ".git"}
    return any(p in excluded_dirs for p in parts) or name.endswith(".pyc")
