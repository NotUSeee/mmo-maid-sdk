"""SDK exception hierarchy — typed errors for actionable handling."""


class SdkError(Exception):
    """Base class for all SDK errors."""


class CapabilityError(SdkError):
    """Raised when a plugin calls an API it doesn't have capability for."""


class RateLimitError(SdkError):
    """Raised when a quota/rate limit is exceeded.

    Attributes:
        retry_after: Suggested seconds to wait before retrying.
    """
    def __init__(self, message: str = "Rate limit exceeded", retry_after: int = 0):
        self.retry_after = retry_after
        super().__init__(message)


class DiscordApiError(SdkError):
    """Raised when the Discord REST API returns an error.

    Attributes:
        status_code: The HTTP status code from Discord (e.g., 403, 404).
    """
    def __init__(self, message: str = "Discord API error", status_code: int = 0):
        self.status_code = status_code
        super().__init__(message)


class SdkPermissionError(SdkError):
    """Raised when the bot lacks a required Discord permission.

    Attributes:
        permission: The missing permission name.
    """
    def __init__(self, message: str = "Missing permission", permission: str = ""):
        self.permission = permission
        super().__init__(message)


# Backwards-compatible alias
PermissionError = SdkPermissionError


class ValidationError(SdkError):
    """Raised when input validation fails (e.g., invalid channel_id, empty key)."""


class KvQuotaError(SdkError):
    """Raised when the KV key limit is exceeded."""


class RpcTimeoutError(SdkError):
    """Raised when an RPC call times out."""


# Backwards-compatible alias
TimeoutError = RpcTimeoutError
