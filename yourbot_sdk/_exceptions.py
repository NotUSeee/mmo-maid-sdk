"""SDK exception hierarchy — typed errors for actionable handling.

Every exception carries a machine-readable ``code`` (a stable string like
``"CAPABILITY_DENIED"``) alongside the human-readable message, so you can branch
on the failure type without string-matching::

    try:
        ctx.discord.send_message(channel_id=cid, content=text)
    except SdkError as e:
        if e.code == "RATE_LIMITED":
            time.sleep(getattr(e, "retry_after", 5))
        elif e.code == "CAPABILITY_DENIED":
            ctx.log(f"missing capability: {e}", level="error")
"""
from typing import Optional


class SdkError(Exception):
    """Base class for all SDK errors.

    Attributes:
        code: Stable, machine-readable error code (e.g. ``"CAPABILITY_DENIED"``).
    """
    code: str = "SDK_ERROR"

    def __init__(self, message: str = "", *, code: Optional[str] = None):
        if code is not None:
            self.code = code
        super().__init__(message)


class CapabilityError(SdkError):
    """Raised when a plugin calls an API it doesn't have capability for."""
    code = "CAPABILITY_DENIED"


class RateLimitError(SdkError):
    """Raised when a quota/rate limit is exceeded.

    Attributes:
        retry_after: Suggested seconds to wait before retrying.
        code: ``"RATE_LIMITED"`` for per-second limits or ``"QUOTA_EXCEEDED"``
            for exhausted tenant quotas.
    """
    code = "RATE_LIMITED"

    def __init__(self, message: str = "Rate limit exceeded", retry_after: float = 0,
                 *, code: Optional[str] = None):
        self.retry_after = retry_after
        super().__init__(message, code=code)


class DiscordApiError(SdkError):
    """Raised when the Discord REST API returns an error.

    Attributes:
        status_code: The HTTP status code from Discord (e.g., 403, 404).
    """
    code = "DISCORD_API_ERROR"

    def __init__(self, message: str = "Discord API error", status_code: int = 0,
                 *, code: Optional[str] = None):
        self.status_code = status_code
        super().__init__(message, code=code)


class SdkPermissionError(SdkError):
    """Raised when the bot lacks a required Discord permission.

    Attributes:
        permission: The missing permission name.
    """
    code = "BOT_MISSING_PERMISSION"

    def __init__(self, message: str = "Missing permission", permission: str = "",
                 *, code: Optional[str] = None):
        self.permission = permission
        super().__init__(message, code=code)


# Backwards-compatible alias
PermissionError = SdkPermissionError


class ValidationError(SdkError):
    """Raised when input validation fails (e.g., invalid channel_id, empty key)."""
    code = "VALIDATION_ERROR"


class KvQuotaError(SdkError):
    """Raised when the KV key limit is exceeded."""
    code = "KV_QUOTA_EXCEEDED"


class RpcTimeoutError(SdkError):
    """Raised when an RPC call times out."""
    code = "RPC_TIMEOUT"


# Backwards-compatible alias
TimeoutError = RpcTimeoutError
