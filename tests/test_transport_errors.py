"""Transport error classification tests.

Newer hosts ship a structured JSON-RPC error object
``{"message": str, "code": str?, "retry_after": float?}``; older hosts send
message-only errors. These tests pin the precedence contract:

  1. structured ``code`` field wins,
  2. then a ``(retry in <N>s)`` message parse supplies retry_after (and is a
     rate-limit classification signal on its own),
  3. then the legacy substring heuristics,
  4. then the plain RuntimeError fallback.

Run from the repo root so the local package is imported:
    python -m pytest tests/test_transport_errors.py
"""
from __future__ import annotations

import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


def test_local_package_imported():
    """Sanity: these tests must exercise THIS tree's package, not an installed
    copy (the monorepo editable install shadows it when run from elsewhere)."""
    import yourbot_sdk._transport as m
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert os.path.abspath(m.__file__).lower().startswith(repo_root.lower()), m.__file__


def _raise_for_error(error_obj):
    """Drive one call() to completion against an arbitrary RPC error payload."""
    from yourbot_sdk._transport import Transport
    with patch("sys.stdout", MagicMock()):
        t = Transport()
        box = {}

        def run():
            try:
                t.call("kv.get", {"key": "k"})
            except Exception as e:  # noqa: BLE001
                box["e"] = e

        th = threading.Thread(target=run)
        th.start()
        rid = None
        for _ in range(2000):
            with t._state_lock:
                if t._pending:
                    rid = next(iter(t._pending))
                    break
            time.sleep(0.001)
        assert rid is not None, "call() never registered a pending id"
        t._dispatch_message({"jsonrpc": "2.0", "id": rid, "error": error_obj})
        th.join(timeout=3)
    return box["e"], t


def _raise_for(message):
    e, _t = _raise_for_error({"message": message})
    return e


class TestStructuredErrors:
    def test_structured_code_wins_over_unmatchable_message(self):
        from yourbot_sdk import RateLimitError
        e, _ = _raise_for_error({
            "message": "schema change budget reached for this tenant",
            "code": "RATE_LIMITED",
            "retry_after": 3540.0,
        })
        assert isinstance(e, RateLimitError)
        assert e.code == "RATE_LIMITED"
        assert e.retry_after == 3540.0

    def test_structured_code_beats_conflicting_message_heuristic(self):
        from yourbot_sdk import RateLimitError
        e, _ = _raise_for_error({
            "message": "ddl capability throttled",
            "code": "RATE_LIMITED",
            "retry_after": 60,
        })
        assert isinstance(e, RateLimitError)
        assert e.retry_after == 60.0

    def test_retry_after_field_beats_message_parse(self):
        from yourbot_sdk import RateLimitError
        e, _ = _raise_for_error({
            "message": "rate limit exceeded: remaining=0/min (retry in 30s)",
            "retry_after": 120,
        })
        assert isinstance(e, RateLimitError)
        assert e.retry_after == 120.0

    def test_retry_in_message_parse_sets_retry_after(self):
        from yourbot_sdk import RateLimitError
        e = _raise_for("DDL rate limit exceeded (retry in 3600s)")
        assert isinstance(e, RateLimitError)
        assert e.code == "RATE_LIMITED"
        assert e.retry_after == 3600.0

    def test_retry_in_classifies_without_legacy_substrings(self):
        from yourbot_sdk import RateLimitError
        e = _raise_for("schema changes temporarily blocked (retry in 900s)")
        assert isinstance(e, RateLimitError)
        assert e.retry_after == 900.0

    def test_structured_quota_exceeded_code(self):
        from yourbot_sdk import RateLimitError
        e, _ = _raise_for_error({"message": "tenant out of actions", "code": "QUOTA_EXCEEDED"})
        assert isinstance(e, RateLimitError)
        assert e.code == "QUOTA_EXCEEDED"
        assert e.retry_after == 5.0  # no field, no parse — historical default

    def test_structured_capability_code_keeps_manifest_hint(self):
        from yourbot_sdk import CapabilityError
        e, _ = _raise_for_error({"message": "denied: storage:kv", "code": "CAPABILITY_DENIED"})
        assert isinstance(e, CapabilityError)
        assert "manifest" in str(e)

    def test_structured_kv_quota_code(self):
        from yourbot_sdk import KvQuotaError
        e, _ = _raise_for_error({"message": "too many keys", "code": "KV_QUOTA_EXCEEDED"})
        assert isinstance(e, KvQuotaError)

    def test_structured_discord_api_code_parses_status(self):
        from yourbot_sdk import DiscordApiError
        e, _ = _raise_for_error({
            "message": "Discord API error 404: Unknown Channel",
            "code": "DISCORD_API_ERROR",
        })
        assert isinstance(e, DiscordApiError)
        assert e.status_code == 404

    def test_structured_permission_and_validation_codes(self):
        from yourbot_sdk import SdkPermissionError, ValidationError
        e, _ = _raise_for_error({"message": "nope", "code": "BOT_MISSING_PERMISSION"})
        assert isinstance(e, SdkPermissionError)
        e, _ = _raise_for_error({"message": "nope", "code": "VALIDATION_ERROR"})
        assert isinstance(e, ValidationError)

    def test_unknown_structured_code_falls_back_to_heuristics(self):
        from yourbot_sdk import RateLimitError
        e, _ = _raise_for_error({
            "message": "quota exceeded: outbound_actions", "code": "SOMETHING_NEW",
        })
        assert isinstance(e, RateLimitError)
        assert e.code == "QUOTA_EXCEEDED"

    def test_unknown_structured_code_unmatchable_message_is_runtime_error(self):
        from yourbot_sdk import SdkError
        e, _ = _raise_for_error({"message": "mystery failure", "code": "SOMETHING_NEW"})
        assert isinstance(e, RuntimeError)
        assert not isinstance(e, SdkError)  # plain RuntimeError fallback path
        assert "mystery failure" in str(e)

    def test_bad_typed_structured_fields_ignored(self):
        from yourbot_sdk import RateLimitError
        e, _ = _raise_for_error({
            "message": "rate limit exceeded",
            "code": 429,            # not a str — ignored
            "retry_after": "soon",  # not numeric — ignored
        })
        assert isinstance(e, RateLimitError)
        assert e.retry_after == 5.0

    def test_error_meta_does_not_leak(self):
        from yourbot_sdk import RateLimitError
        e, t = _raise_for_error({"message": "x", "code": "RATE_LIMITED", "retry_after": 1})
        assert isinstance(e, RateLimitError)
        assert t._error_meta == {}
        assert t._errors == {}


class TestMessageOnlyFallback:
    """Old hosts send message-only errors — the legacy mapping is unchanged."""

    def test_capability_hint(self):
        from yourbot_sdk import CapabilityError
        e = _raise_for("capability storage:kv required")
        assert isinstance(e, CapabilityError)
        assert "manifest" in str(e)

    def test_kv_quota_beats_generic_quota(self):
        from yourbot_sdk import KvQuotaError
        e = _raise_for("plugin_kv quota exceeded: 50000 keys")
        assert isinstance(e, KvQuotaError)

    def test_default_retry_after_unchanged(self):
        from yourbot_sdk import RateLimitError
        e = _raise_for("rate limit exceeded")
        assert isinstance(e, RateLimitError)
        assert e.retry_after == 5.0

    def test_legacy_remaining_parse_unchanged(self):
        from yourbot_sdk import RateLimitError
        e = _raise_for("rate limit exceeded: remaining=0/min")
        assert isinstance(e, RateLimitError)
        assert e.retry_after == 60.0

    def test_discord_api_status_parse(self):
        from yourbot_sdk import DiscordApiError
        e = _raise_for("Discord API error 403: Missing Permissions")
        assert isinstance(e, DiscordApiError)
        assert e.status_code == 403

    def test_permission_mapping(self):
        from yourbot_sdk import SdkPermissionError
        e = _raise_for("bot lacks MANAGE_ROLES permission")
        assert isinstance(e, SdkPermissionError)

    def test_validation_mapping(self):
        from yourbot_sdk import ValidationError
        e = _raise_for("channel_id is required and must be a string")
        assert isinstance(e, ValidationError)

    def test_unmatched_message_is_runtime_error(self):
        e = _raise_for("mystery failure")
        assert isinstance(e, RuntimeError)

    def test_non_dict_error_payload_still_classified(self):
        from yourbot_sdk import RateLimitError
        e, _ = _raise_for_error("rate limit exceeded (retry in 12s)")
        assert isinstance(e, RateLimitError)
        assert e.retry_after == 12.0
