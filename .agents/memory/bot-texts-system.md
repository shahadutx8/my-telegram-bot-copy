---
name: Bot texts system
description: How bot reply strings are stored, retrieved, and edited from dashboard.
---

# Bot Texts System

All bot message strings are extracted from handler code into `DEFAULT_BOT_TEXTS` (dict, defined before `load_config()`).

## Storage
- `DEFAULT_BOT_TEXTS` — the source-of-truth defaults in `main.py`
- `CONFIG["bot_texts"]` — the live (dashboard-editable) values, persisted to DB
- Deep merge in `load_config()`: new keys in `DEFAULT_BOT_TEXTS` auto-appear even on old configs

## Retrieval
Use `get_text(key, **kwargs)` everywhere in handlers. It:
1. Looks up `CONFIG["bot_texts"][key]`
2. Falls back to `DEFAULT_BOT_TEXTS[key]`
3. Formats with `str.format(**kwargs)` (catches KeyError/ValueError silently)

## Template placeholders (Python `.format()` style)
- `welcome`: `{country_keys}`, `{used_count}`, `{remaining}`
- `profile_reply` / `ai_reply`: `{dev_line}`, `{tg_mention}`, `{country}`, `{field_lines}`
- `ai_reply` extra: `{ai_badge}`
- `panel_reply`: `{developer_name}`, `{total_combinations}`, `{used_count}`, `{remaining}`
- `new_user_notify`: `{fullname}`, `{uname}`, `{user_id}`, `{total_users}`
- `bot_crash_notify`: `{error}`
- `ai_unknown_country` / `unknown_country`: `{keys}`

## API routes
- `GET /api/bot-texts` — returns `{texts, defaults}`
- `POST /api/bot-texts` — body `{texts: {key: val, ...}}` — saves individual or all keys
- `POST /api/bot-texts/reset` — body `{key: "all" | "<specific_key>"}` — resets to defaults

## Dashboard
- Tab: ✏️ Texts (between Log and Settings)
- JS: `BOT_TEXT_META`, `loadBotTexts()`, `renderBotTexts()`, `saveSingleBotText(key)`, `saveAllBotTexts()`, `resetSingleBotText(key)`, `resetAllBotTexts()`
- `switchTab` calls `loadBotTexts()` when `name === 'texts'`

**Why:** Bot admins need to tweak reply wording (especially Bangla text) without touching code.
