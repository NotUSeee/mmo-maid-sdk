"""Plugin artifact validation — vendored from the platform.

This module is a **vendored copy** of the validation logic the YourBot platform
runs on a plugin artifact before accepting a version. Shipped in the SDK so devs
can run the exact same checks locally with `mmo validate` instead of discovering
errors only after a failed upload.

Contract: this file's output must match the platform's output byte-for-byte for
the same input. A CI parity test on the platform side diffs this against the
source-of-truth at `mmo_maid/core/plugin_validation.py` and the helpers at
`mmo_maid/core/artifact_store.py` so drift is caught early.

Sources of truth:
  - `mmo_maid/core/plugin_validation.py` — the validator itself
  - `mmo_maid/core/artifact_store.py`     — `_detect_capabilities`,
    `manifest_capabilities`, `write_manifest_capabilities`,
    `_CAP_PATTERNS`, `_CAPABILITIES_KEYS`

When upgrading, regenerate this file rather than editing it by hand.
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
) -> ValidationResult:
    """Run all pre-submit checks on a zipped plugin artifact.

    Args:
        plugin_id:   the platform's plugin id (must match ``manifest.id``).
        version:     the platform-assigned version (must match ``manifest.version``).
        artifact_bytes: raw .zip content as fetched from artifact storage.
        effective_manifest: optional override for capability checks (when the
            caller has a merged view of declared + auto-detected caps).

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
    py_paths = [n for n in namelist
                if n.endswith(".py") and not _is_excluded_path(n)]
    if py_paths:
        py_sources: list[bytes] = []
        for p in py_paths:
            try:
                py_sources.append(zf.read(p))
            except Exception:
                continue
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

    # 6. Forbidden patterns (would actually fail at sandbox runtime).
    for path in py_paths:
        try:
            data = zf.read(path)
        except Exception:
            continue
        for finding in _scan_forbidden_patterns(path, data):
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
    "group-admin", "group-alerts", "history", "kick", "leaderboard",
    "lockdown", "maid-bug-report", "music", "note", "poll", "purge",
    "quarantine", "quests", "raidmode", "report", "slowmode", "stats",
    "temp-role", "ticket", "tickets", "timeout", "unban", "unlockdown",
    "unquarantine", "untimeout", "warn", "welcome",
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
