---
name: Config ordering rule
description: DEFAULT_* constants must be defined before load_config() in main.py.
---

# Config Ordering Rule

`load_config()` references `DEFAULT_AI_PROMPT_BD`, `DEFAULT_AI_PROMPT_INTL`, and `DEFAULT_BOT_TEXTS` inside the defaults dict literal.

**Rule:** All `DEFAULT_*` constants must be defined **before** `def load_config():` in `main.py`. If you define them after, you'll get a `NameError` at startup.

**Why:** Python executes the function body when called (not when defined), but the defaults dict inside `load_config()` is built each call — so the constants must exist when `load_config()` is first called (which is immediately at module level: `CONFIG = load_config()`).

**How to apply:** When adding any new dashboard-configurable default, define its `DEFAULT_*` constant in the "Default constants" block above `load_config()`, then reference it inside the `defaults` dict in `load_config()`, and add deep-merge logic for any nested dicts (like `bot_texts`).
