# Fake Profile Generator Bot

A Telegram bot + Flask web dashboard that generates fake profiles (name, phone, email, etc.) for multiple countries. The bot replies to country names with a generated profile; the dashboard lets you manage everything.

## Stack
- **Python 3** — single `main.py` entry point
- **pyTelegramBotAPI** — Telegram bot polling
- **Flask** — web dashboard (login-protected)
- **Faker** — profile generation for non-BD countries

## How to run
The `Start application` workflow runs `python main.py`.

On first start the app needs:
- `SESSION_SECRET` — Flask session key (already set)
- `DASHBOARD_PASSWORD` — dashboard login password (already set)

After login, set the Telegram bot token from the dashboard (Settings tab).

## Key files
- `main.py` — all application logic (bot + Flask)
- `names_default.json` — default BD first/last name lists and prefixes (edit this file to change defaults; **do not hardcode names in main.py**)
- `config.json` — runtime config saved by the dashboard (bot token, password hash, custom name lists)
- `templates/` — Jinja2 HTML templates for login and dashboard

## Data files (auto-created at runtime)
- `config.json` — bot token, password, name lists
- `banned_users.json` — banned Telegram user IDs
- `used_names.json` — set of already-used BD names
- `name_log.json` — log of which user received which BD name

## User preferences
- Name lists (BD first names, last names, prefixes) must NOT be hardcoded in `main.py`. They live in `names_default.json` and are managed via the dashboard.
