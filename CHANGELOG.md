# Changelog

All notable changes to the YourBot SDK are documented here. This project follows
[Keep a Changelog](https://keepachangelog.com/) and [Semantic Versioning](https://semver.org/).

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
