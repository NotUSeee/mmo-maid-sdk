# mmo-maid-sdk (deprecated alias)

This package is the legacy name for the **YourBot SDK**. It installs
[`yourbot-sdk`](https://pypi.org/project/yourbot-sdk/) and does nothing else.

```bash
pip install yourbot-sdk
```

```python
from yourbot_sdk import Plugin, Context
```

Existing code that does `import mmo_maid_sdk` keeps working (the `yourbot-sdk` wheel
ships a compatibility shim), but new plugins should import `yourbot_sdk`.
