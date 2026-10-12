import os
import io
import time as _time_module
import telebot
import random
import re
import json
import unicodedata
import hashlib
import secrets as secrets_module
from google import genai
from faker import Faker
from datetime import datetime as _dt, timedelta
from werkzeug.security import generate_password_hash, check_password_hash as _wz_check
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from threading import Thread, Lock
from functools import wraps
from collections import deque

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Persistent KV Store (PostgreSQL ▶ JSON file fallback)
# Set DATABASE_URL env var to enable cross-deploy persistence.
# Without it the app works normally using local JSON files.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
_db_conn = None
_db_lock = Lock()

def _get_db():
    """Return a live psycopg2 connection, or None if DATABASE_URL not set."""
    global _db_conn
    if not _DATABASE_URL:
        return None
    with _db_lock:
        try:
            if _db_conn is None or _db_conn.closed:
                import psycopg2
                try:
                    _db_conn = psycopg2.connect(_DATABASE_URL, sslmode='require')
                except Exception as ssl_error:
                    # Local development PostgreSQL may not expose SSL, while
                    # hosted PostgreSQL connections generally do.
                    if "does not support SSL" not in str(ssl_error):
                        raise
                    _db_conn = psycopg2.connect(_DATABASE_URL, sslmode='disable')
                _db_conn.autocommit = True
                with _db_conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS kv_store (
                            key   TEXT PRIMARY KEY,
                            value TEXT NOT NULL
                        )
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS bot_file_blobs (
                            key         TEXT PRIMARY KEY,
                            data        BYTEA NOT NULL,
                            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                    """)
        except Exception as e:
            print(f"[DB] connect error: {e}")
            _db_conn = None
    return _db_conn

def db_get(key: str):
    """Return parsed JSON for key, or None (no DB or key missing)."""
    conn = _get_db()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM kv_store WHERE key = %s", (key,))
            row = cur.fetchone()
            return json.loads(row[0]) if row else None
    except Exception as e:
        print(f"[DB] get({key}) error: {e}")
        return None

def db_set(key: str, value) -> bool:
    """Upsert JSON-serialised value. Returns True on success."""
    conn = _get_db()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO kv_store (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (key, json.dumps(value, ensure_ascii=False)))
        return True
    except Exception as e:
        print(f"[DB] set({key}) error: {e}")
        return False

def db_set_blob(key: str, value: bytes) -> bool:
    """Store binary data in PostgreSQL. Returns False when DB is unavailable."""
    conn = _get_db()
    if not conn:
        return False
    try:
        import psycopg2
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_file_blobs (key, data, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE
                SET data = EXCLUDED.data, updated_at = NOW()
            """, (key, psycopg2.Binary(value)))
        return True
    except Exception as e:
        print(f"[DB] blob set({key}) error: {e}")
        return False

def db_get_blob(key: str):
    """Return binary data for a key, or None when unavailable/missing."""
    conn = _get_db()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM bot_file_blobs WHERE key = %s", (key,))
            row = cur.fetchone()
            return bytes(row[0]) if row else None
    except Exception as e:
        print(f"[DB] blob get({key}) error: {e}")
        return None

def db_delete_blob(key: str) -> bool:
    conn = _get_db()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bot_file_blobs WHERE key = %s", (key,))
        return True
    except Exception as e:
        print(f"[DB] blob delete({key}) error: {e}")
        return False

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Config — loaded from PostgreSQL then config.json
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONFIG_FILE = "config.json"
config_lock = Lock()

def _hash_password(password: str) -> str:
    """Return a Werkzeug scrypt/PBKDF2 hash of the password."""
    return generate_password_hash(password)

NAMES_DEFAULT_FILE = "names_default.json"

# ── Profile field definitions ──────────────────
FIELD_KEYS = ['full_name', 'nickname', 'facebook_id', 'google', 'whatsapp', 'email', 'mobile', 'username']
FIELD_LABELS = {
    'full_name':   'Withdrawer Name',
    'nickname':    'Nickname',
    'facebook_id': 'Facebook ID',
    'google':      'Google',
    'whatsapp':    'WhatsApp',
    'email':       'Email',
    'mobile':      'Mobile',
    'username':    'Username',
}
FIELD_EMOJI = {
    'full_name':   '👤',
    'nickname':    '🏷️',
    'facebook_id': '📘',
    'google':      '🔵',
    'whatsapp':    '💬',
    'email':       '📧',
    'mobile':      '📞',
    'username':    '🔗',
}

PROFILE_COPY_KEYS = ('full_name', 'google', 'email', 'mobile', 'username')

def build_profile_copy_keyboard(profile: dict, enabled_fields) -> object | None:
    """Return one protected Copy Text button for each useful profile field."""
    enabled = set(enabled_fields or FIELD_KEYS)
    buttons = []
    for key in PROFILE_COPY_KEYS:
        value = str(profile.get(key, '') or '').strip()
        if key not in enabled or not value:
            continue
        buttons.append(
            telebot.types.InlineKeyboardButton(
                f"📋 {FIELD_LABELS[key]}",
                copy_text=telebot.types.CopyTextButton(text=value[:256]),
            )
        )
    if not buttons:
        return None
    keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(*buttons)
    return keyboard

# Minimal emergency fallback — used only when names_default.json is missing or corrupt.
_EMERGENCY_DEFAULTS: dict = {
    "bd_first_names": ["Md"],
    "bd_last_names":  ["Rahman"],
    "bd_prefixes":    ["Md."],
}

def load_name_defaults() -> dict:
    """Load and validate default name lists from names_default.json.

    Returns a dict with keys bd_first_names, bd_last_names, bd_prefixes.
    Each value is guaranteed to be a non-empty list of non-empty strings.
    Falls back to _EMERGENCY_DEFAULTS (and prints a warning) on any error.
    """
    def _valid_str_list(val) -> bool:
        return (
            isinstance(val, list)
            and len(val) > 0
            and all(isinstance(s, str) and s.strip() for s in val)
        )

    try:
        with open(NAMES_DEFAULT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        result = {}
        for key in ("bd_first_names", "bd_last_names", "bd_prefixes"):
            val = data.get(key)
            if not _valid_str_list(val):
                raise ValueError(
                    f"'{key}' in {NAMES_DEFAULT_FILE} must be a non-empty list of strings"
                )
            result[key] = val
        return result

    except Exception as e:
        print(f"⚠️  Could not load {NAMES_DEFAULT_FILE}: {e} — using emergency defaults.")
        return {k: v[:] for k, v in _EMERGENCY_DEFAULTS.items()}

NAME_DEFAULTS = load_name_defaults()

# Fail fast at startup if we are stuck with the emergency defaults and the file
# is simply absent (common misconfiguration), so the operator notices immediately.
if NAME_DEFAULTS == {k: v[:] for k, v in _EMERGENCY_DEFAULTS.items()}:
    import warnings
    warnings.warn(
        f"{NAMES_DEFAULT_FILE} could not be loaded — "
        "name generation will be severely limited. "
        "Provide a valid names_default.json to restore full functionality.",
        RuntimeWarning,
        stacklevel=1,
    )

# Default AI prompt templates — placeholders filled at runtime:
#   BD prompt   : {prefixes}, {first_names}, {last_names}
#   Intl prompt : {country}
DEFAULT_AI_PROMPT_BD = (
    "তুমি একটি Bangladeshi নাম জেনারেটর। নিচে আমাদের নামের ডেটাবেজ থেকে কিছু উদাহরণ দেওয়া হলো:\n\n"
    "উপসর্গ (Prefix): {prefixes}\n"
    "প্রথম নাম উদাহরণ: {first_names}\n"
    "শেষ নাম উদাহরণ: {last_names}\n\n"
    "এই ডেটার ধরন ও ছন্দ বজায় রেখে একটি সম্পূর্ণ নতুন, realistic Bangladeshi পুরুষের নাম তৈরি করো।\n"
    "নামটি হুবহু উদাহরণের মতো না হয়ে নতুন ও স্বাভাবিক হতে হবে।\n"
    "ফরম্যাট: [উপসর্গ] [প্রথম নাম] [শেষ নাম]\n"
    "শুধু নামটি দাও — আর কিছু নয়, কোনো ব্যাখ্যা নয়।"
)

DEFAULT_AI_PROMPT_INTL = (
    "Generate a single realistic male full name from {country}.\n"
    "The name should sound natural and authentic for that country.\n"
    "Return only the name — no explanation, no extra text."
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Default Bot Text Templates (all dashboard-editable)
# Placeholders use Python str.format() syntax: {variable_name}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEFAULT_BOT_TEXTS = {
    # /start
    "welcome": (
        "👋 ফেক প্রোফাইল জেনারেটর বটে স্বাগতম!\n\n"
        "যেকোনো দেশের নাম লিখুন:\n{country_keys}\n\n"
        "🇧🇩 এখন পর্যন্ত {used_count} টি নাম ব্যবহৃত হয়েছে।\n"
        "✅ আরও {remaining} টি ইউনিক নাম বাকি আছে।\n\n"
        "📋 /history — আগের প্রোফাইল দেখুন\n"
        "🤖 /ainame [দেশ] — AI দিয়ে real-style নাম তৈরি করুন"
    ),
    "user_menu": (
        "📱 *বট মেনু*\n\n"
        "নিচের বাটন থেকে একটি অপশন বেছে নিন।"
    ),
    "country_menu": "{icon} কোন দেশের প্রোফাইল চান? একটি দেশ নির্বাচন করুন:",
    "help": (
        "❓ *কীভাবে ব্যবহার করবেন*\n\n"
        "⚡ Generate Profile চাপুন, তারপর দেশ নির্বাচন করুন।\n"
        "🤖 AI Name চাপলে AI দিয়ে নামসহ প্রোফাইল তৈরি হবে।\n"
        "📋 My History দিয়ে আগের প্রোফাইল দেখুন।\n"
        "📂 Files দিয়ে আপনার জন্য উন্মুক্ত ফাইল দেখুন।\n\n"
        "আপনি চাইলে সরাসরি দেশের নাম লিখেও প্রোফাইল তৈরি করতে পারেন।"
    ),
    # admin notification — new user joined
    "new_user_notify": (
        "🆕 *নতুন ইউজার জয়েন করেছে!*\n\n"
        "👤 নাম: {fullname}\n"
        "🔗 Username: {uname}\n"
        "🆔 ID: `{user_id}`\n"
        "📊 মোট ইউজার: {total_users}"
    ),
    "approval_pending": (
        "⏳ আপনার access এখনও অ্যাডমিন approval-এর অপেক্ষায় আছে।\n\n"
        "অ্যাডমিন approve করলে আপনি বট ব্যবহার করতে পারবেন।"
    ),
    "approval_rejected": (
        "❌ আপনার access request approve করা হয়নি।\n\n"
        "প্রয়োজনে অ্যাডমিনের সঙ্গে যোগাযোগ করুন।"
    ),
    "approval_granted": (
        "✅ আপনার access approve করা হয়েছে!\n\n"
        "এখন আপনি বট ব্যবহার করতে পারবেন।"
    ),
    "approval_admin_notify": (
        "🆕 *নতুন ইউজার approval-এর অপেক্ষায়!*\n\n"
        "👤 নাম: {fullname}\n"
        "🔗 Username: {uname}\n"
        "🆔 ID: `{user_id}`\n"
        "📊 মোট ইউজার: {total_users}\n\n"
        "নিচের button থেকে Approve বা Reject করুন।"
    ),
    "approval_admin_approved": "✅ ইউজার `{user_id}` approve করা হয়েছে।",
    "approval_admin_rejected": "🚫 ইউজার `{user_id}` reject করা হয়েছে।",
    "approval_admin_not_found": "❌ এই User ID-এর কোনো registered user পাওয়া যায়নি।",
    "approval_admin_usage": "⚠️ ব্যবহার: `/{action} <user_id>`",
    "approval_none_pending": "✅ কোনো pending approval নেই।",
    "approval_pending_list": "⏳ *Pending approvals* — মোট `{count}` জন\n\n{users}",
    "approval_admin_menu": (
        "\n\n✅ *ইউজার approval*\n"
        "• /pending — pending user list\n"
        "• /approve `<user_id>` — approve করুন\n"
        "• /reject `<user_id>` — reject করুন"
    ),
    # /panel (admin)
    "panel_reply": (
        "⚙️ কন্ট্রোল প্যানেল:\n\n"
        "বট চালু আছে ✅\n"
        "ডেভলপার: {developer_name}\n"
        "মোট সম্ভাব্য নাম: {total_combinations}\n"
        "ব্যবহৃত নাম: {used_count}\n"
        "বাকি নাম: {remaining}"
    ),
    "panel_not_admin":    "❌ আপনি এই বটের অ্যাডমিন নন!",
    # /reset (admin)
    "reset_success":      "✅ সমস্ত ব্যবহৃত নাম রিসেট করা হয়েছে!",
    "reset_not_admin":    "❌ শুধু অ্যাডমিন এই কমান্ড ব্যবহার করতে পারবেন!",
    # /history
    "history_empty": (
        "📭 আপনি এখনও কোনো প্রোফাইল জেনারেট করেননি।\n"
        "যেকোনো দেশের নাম লিখলেই প্রোফাইল পাবেন!"
    ),
    "history_copy_hint":  "💡 টেক্সটে ট্যাপ করলেই কপি হয়ে যাবে।",
    # ban reply
    "banned_reply": (
        "🚫 আপনাকে এই বট থেকে ব্যান করা হয়েছে।\n"
        "আপনি আর এই বট ব্যবহার করতে পারবেন না।"
    ),
    # /ainame
    "ai_unavailable":     "⚠️ AI name generator এখন উপলব্ধ নেই।",
    "ai_unknown_country": "⚠️ অজানা দেশ।\nলিখুন: {keys}",
    "ai_generating":      "🤖 AI দিয়ে নাম তৈরি হচ্ছে, একটু অপেক্ষা করুন…",
    "ai_profile_failed":  "⚠️ প্রোফাইল তৈরি করা সম্ভব হয়নি।",
    # AI reply template — placeholders: dev_line, ai_badge, tg_mention, country, field_lines
    "ai_reply": (
        "{dev_line}"
        "{ai_badge}\n"
        "🆔 Your Telegram: {tg_mention}\n"
        "🌍 দেশ: {country}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "{field_lines}"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 নিচের field button চাপলে শুধু ওই তথ্য কপি হবে।"
    ),
    # regular profile reply
    "unknown_country": (
        "⚠️ দুঃখিত, এটি কোনো সঠিক দেশের নাম নয়।\n\n"
        "লিখুন: {keys}"
    ),
    # profile reply template — placeholders: dev_line, tg_mention, country, field_lines
    "profile_reply": (
        "{dev_line}"
        "🆔 Your Telegram: {tg_mention}\n"
        "🌍 দেশ: {country}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "{field_lines}"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 নিচের field button চাপলে শুধু ওই তথ্য কপি হবে।"
    ),
    "profile_failed":     "⚠️ দুঃখিত, প্রোফাইল ডাটা জেনারেট করা সম্ভব হয়নি।",
    # admin notification — bot crashed
    "bot_crash_notify": (
        "⚠️ বট unexpected ভাবে বন্ধ হয়ে গেছে!\n\n"
        "🔴 Error: {error}\n\n"
        "Dashboard থেকে বট রিস্টার্ট করুন।"
    ),
}

def load_config():
    defaults = {
        "bot_token": "", "password_hash": "",
        "bot_reply_fields": FIELD_KEYS[:],
        "bd_first_names": NAME_DEFAULTS["bd_first_names"][:],
        "bd_last_names":  NAME_DEFAULTS["bd_last_names"][:],
        "bd_prefixes":    NAME_DEFAULTS["bd_prefixes"][:],
        "admin_id":       int(os.environ.get("ADMIN_ID", "0") or "0"),
        "developer_name": os.environ.get("DEVELOPER_NAME", ""),
        "ai_prompt_bd":   DEFAULT_AI_PROMPT_BD,
        "ai_prompt_intl": DEFAULT_AI_PROMPT_INTL,
        "bot_texts":      {k: v for k, v in DEFAULT_BOT_TEXTS.items()},
        "nickname_sfx": ["07", "Official", "Gamer", "Pro", "Boss", "Real", "King", "BD", "X", ""],
        "countries": {
            "bangladesh": {"locale": "en_US", "code": "+880", "digits": ["13XXXXXXXX", "14XXXXXXXX", "15XXXXXXXX", "16XXXXXXXX", "19XXXXXXXX"], "is_bd": True},
            "bd":         {"locale": "en_US", "code": "+880", "digits": ["13XXXXXXXX", "14XXXXXXXX", "15XXXXXXXX", "16XXXXXXXX", "19XXXXXXXX"], "is_bd": True},
            "india":      {"locale": "en_IN", "code": "+91",  "digits": "XXXXXXXXXX", "is_bd": False},
            "usa":        {"locale": "en_US", "code": "+1",   "digits": "XXXXXXXXXX", "is_bd": False},
            "uk":         {"locale": "en_GB", "code": "+44",  "digits": "XXXXXXXXXX", "is_bd": False},
            "canada":     {"locale": "en_CA", "code": "+1",   "digits": "XXXXXXXXXX", "is_bd": False},
            "france":     {"locale": "fr_FR", "code": "+33",  "digits": "XXXXXXXXXX", "is_bd": False},
            "germany":    {"locale": "de_DE", "code": "+49",  "digits": "XXXXXXXXXX", "is_bd": False},
            "japan":      {"locale": "ja_JP", "code": "+81",  "digits": "XXXXXXXXXX", "is_bd": False},
        },
    }
    db_data = db_get("config")
    if db_data:
        saved_texts = db_data.pop("bot_texts", {})
        defaults.update(db_data)
        if saved_texts:
            defaults["bot_texts"].update(saved_texts)  # deep merge — new keys keep defaults
    return defaults

def save_config(cfg: dict):
    with config_lock:
        db_set("config", cfg)

def get_text(key: str, **kwargs) -> str:
    """Fetch a bot message template from CONFIG (dashboard-editable) and format it.
    Falls back to DEFAULT_BOT_TEXTS if the key is missing from config.
    Format errors are silently caught — the raw template is returned.
    """
    texts = CONFIG.get("bot_texts", {})
    template = texts.get(key) or DEFAULT_BOT_TEXTS.get(key, key)
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, ValueError, IndexError):
        return template

CONFIG = load_config()

# Bootstrap password hash from env var on first run (no stored hash yet)
if not CONFIG.get("password_hash"):
    _env_pass = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if not _env_pass:
        raise RuntimeError(
            "DASHBOARD_PASSWORD environment variable must be set on first run "
            "to initialise the dashboard password."
        )
    CONFIG["password_hash"] = _hash_password(_env_pass)
    save_config(CONFIG)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Gemini AI Setup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

def generate_ai_name(country_key: str = "bangladesh") -> str | None:
    """Use Gemini to generate a realistic name, guided by the bot's own name lists.
    Prompts are loaded from CONFIG so they can be customised from the dashboard.
    Supported placeholders:
      BD prompt   — {prefixes}, {first_names}, {last_names}
      Intl prompt — {country}
    """
    if not _GEMINI_KEY:
        return None
    is_bd = country_key.lower() in ("bangladesh", "bd")

    if is_bd:
        sample_firsts = random.sample(get_first_names(), min(15, len(get_first_names())))
        sample_lasts  = random.sample(get_last_names(),  min(15, len(get_last_names())))
        prefixes      = get_prefixes()
        template = CONFIG.get("ai_prompt_bd") or DEFAULT_AI_PROMPT_BD
        prompt = template.format(
            prefixes    = ", ".join(prefixes),
            first_names = ", ".join(sample_firsts),
            last_names  = ", ".join(sample_lasts),
            country     = country_key.capitalize(),   # available but rarely used in BD template
        )
    else:
        template = CONFIG.get("ai_prompt_intl") or DEFAULT_AI_PROMPT_INTL
        prompt = template.format(
            country     = country_key.capitalize(),
            prefixes    = "",   # available but not meaningful for intl
            first_names = "",
            last_names  = "",
        )

    try:
        client   = genai.Client(api_key=_GEMINI_KEY)
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
        )
        name = response.text.strip().strip("\"'").strip()
        # Sanity check: must be 2–5 words, no special chars beyond periods/spaces
        if name and 2 <= len(name.split()) <= 5:
            return name
        return None
    except Exception as e:
        print(f"[generate_ai_name] error: {e}")
        return None

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Flask App Setup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
app = Flask('')
app.secret_key = os.environ.get("SESSION_SECRET", "")
if not app.secret_key:
    raise RuntimeError("SESSION_SECRET environment variable must be set.")

# Recent profile log (in-memory, last 50)
recent_log = deque(maxlen=50)

# ── Jinja filter ──
@app.template_filter('format_num')
def format_num(value):
    try:
        return f"{int(value):,}"
    except Exception:
        return value

# ── Auth helpers ──
def _is_legacy_hash(h: str) -> bool:
    """Detect the old static-salt SHA-256 hex digest (64 hex chars, no $ prefix)."""
    return bool(h) and len(h) == 64 and all(c in '0123456789abcdef' for c in h)

def _legacy_hash(password: str) -> str:
    salt = "bot_dashboard_salt_v1"
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()

def check_password(plain: str) -> bool:
    stored = CONFIG.get("password_hash", "")
    if _is_legacy_hash(stored):
        if _legacy_hash(plain) != stored:
            return False
        # Migrate to secure hash on successful login
        CONFIG["password_hash"] = _hash_password(plain)
        save_config(CONFIG)
        return True
    return _wz_check(stored, plain)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify(success=False, error='Unauthorized'), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Telegram Bot — hot-swappable
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
bot = None
bot_thread = None
bot_lock = Lock()
bot_status = {"running": False, "token_preview": "", "error": ""}

# Telegram content protection is enabled for every message and media sent by this bot.
class ProtectedTeleBot(telebot.TeleBot):
    """TeleBot that always marks outgoing content as protected."""

    @staticmethod
    def _protected_kwargs(kwargs):
        kwargs["protect_content"] = True
        return kwargs

    def send_message(self, *args, **kwargs):
        return super().send_message(*args, **self._protected_kwargs(kwargs))

    def reply_to(self, *args, **kwargs):
        return super().reply_to(*args, **self._protected_kwargs(kwargs))

    def send_document(self, *args, **kwargs):
        return super().send_document(*args, **self._protected_kwargs(kwargs))

    def send_photo(self, *args, **kwargs):
        return super().send_photo(*args, **self._protected_kwargs(kwargs))

    def send_video(self, *args, **kwargs):
        return super().send_video(*args, **self._protected_kwargs(kwargs))

    def send_audio(self, *args, **kwargs):
        return super().send_audio(*args, **self._protected_kwargs(kwargs))

    def send_voice(self, *args, **kwargs):
        return super().send_voice(*args, **self._protected_kwargs(kwargs))

    def send_animation(self, *args, **kwargs):
        return super().send_animation(*args, **self._protected_kwargs(kwargs))

    def send_sticker(self, *args, **kwargs):
        return super().send_sticker(*args, **self._protected_kwargs(kwargs))

    def send_video_note(self, *args, **kwargs):
        return super().send_video_note(*args, **self._protected_kwargs(kwargs))

    def send_media_group(self, *args, **kwargs):
        return super().send_media_group(*args, **self._protected_kwargs(kwargs))

    def copy_message(self, *args, **kwargs):
        return super().copy_message(*args, **self._protected_kwargs(kwargs))

# Admin ID and developer name are now stored in config.json and managed via the dashboard.

def make_bot(token: str):
    """Create, validate (via get_me), and return a TeleBot instance, or None on error."""
    token = token.strip()
    if not token:
        return None, "No token provided."
    try:
        b = ProtectedTeleBot(token)
        b.get_me()          # synchronous API call — raises if token is invalid
        register_handlers(b)
        return b, ""
    except Exception as e:
        return None, str(e)

def start_bot_polling(b):
    """Run infinity_polling in a daemon thread."""
    try:
        b.remove_webhook()
        b.infinity_polling(skip_pending=True)
    except Exception as e:
        bot_status["running"] = False
        bot_status["error"] = str(e)
        # Notify admin about the crash
        admin_id = get_admin_id()
        if admin_id:
            try:
                b.send_message(
                    admin_id,
                    get_text("bot_crash_notify", error=str(e)[:300])
                )
            except Exception:
                pass

def launch_bot(token: str):
    """Stop any existing bot and start a new one."""
    global bot, bot_thread
    with bot_lock:
        # Stop existing bot
        if bot:
            try:
                bot.stop_polling()
            except Exception:
                pass
            bot = None
        bot_status["running"] = False
        bot_status["error"] = ""

        new_bot, err = make_bot(token)
        if not new_bot:
            bot_status["error"] = err or "Invalid token."
            return False, bot_status["error"]

        bot = new_bot
        bot_status["token_preview"] = token[:10] + "..." if len(token) > 10 else token
        t = Thread(target=start_bot_polling, args=(new_bot,), daemon=True)
        t.start()
        bot_thread = t
        bot_status["running"] = True
        return True, ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Dynamic config accessors — all read from CONFIG (managed via dashboard API)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_first_names():     return CONFIG.get("bd_first_names", NAME_DEFAULTS["bd_first_names"])
def get_last_names():      return CONFIG.get("bd_last_names",  NAME_DEFAULTS["bd_last_names"])
def get_prefixes():        return CONFIG.get("bd_prefixes",    NAME_DEFAULTS["bd_prefixes"])
def get_country_details(): return CONFIG.get("countries", {})
def get_nickname_sfx():    return CONFIG.get("nickname_sfx", [""])
def get_admin_id():
    try:
        return int(CONFIG.get("admin_id") or 0)
    except (TypeError, ValueError):
        return 0
def get_developer_name():  return CONFIG.get("developer_name", "")
def get_total_combinations():
    return max(1, len(get_first_names()) * len(get_last_names()) * len(get_prefixes()))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Banned Users
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BANNED_FILE  = "banned_users.json"
banned_lock  = Lock()

def load_banned() -> dict:
    db_data = db_get("banned")
    if db_data is not None:
        return {int(k): v for k, v in db_data.items()}
    return {}

def save_banned(data: dict):
    db_set("banned", data)

BANNED_USERS: dict = load_banned()   # {user_id(int): {username, first_name, reason, timestamp}}

def is_banned(user_id: int) -> bool:
    return user_id in BANNED_USERS

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Persistent Used Names  (BD + non-BD, all in one set)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
USED_NAMES_FILE  = "used_names.json"
used_names_lock  = Lock()

def load_used_names():
    db_data = db_get("used_names")
    return set(db_data) if db_data is not None else set()

def save_used_names(used: set):
    db_set("used_names", list(used))

USED_NAMES = load_used_names()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Persistent Used Phones  (global uniqueness)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
used_phones_lock = Lock()

def load_used_phones() -> set:
    db_data = db_get("used_phones")
    return set(db_data) if db_data is not None else set()

def save_used_phones(used: set):
    db_set("used_phones", list(used))

USED_PHONES: set = load_used_phones()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Persistent Used Usernames  (global uniqueness)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
used_usernames_lock = Lock()

def load_used_usernames() -> set:
    db_data = db_get("used_usernames")
    return set(db_data) if db_data is not None else set()

def save_used_usernames(used: set):
    db_set("used_usernames", list(used))

USED_USERNAMES: set = load_used_usernames()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Name-Usage Log  (who got which BD name + when)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NAME_LOG_FILE = "name_log.json"
name_log_lock = Lock()

def load_name_log() -> list:
    db_data = db_get("name_log")
    return db_data if isinstance(db_data, list) else []

def save_name_log(log: list):
    db_set("name_log", log)

NAME_LOG: list = load_name_log()

def log_name_usage(bd_name: str, user_id: int, username: str | None, first_name: str | None):
    """Record which Telegram user received this BD name."""
    from datetime import datetime, timezone
    entry = {
        "name":       bd_name,
        "user_id":    user_id,
        "username":   username or "",
        "first_name": first_name or "",
        "timestamp":  datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    with name_log_lock:
        NAME_LOG.append(entry)
        save_name_log(NAME_LOG)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Users registry — all users who ever used the bot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
USERS_FILE = "users.json"
users_lock = Lock()

def load_users() -> dict:
    db_data = db_get("users")
    if db_data is not None:
        return {int(k): v for k, v in db_data.items()}
    return {}

def save_users(data: dict):
    db_set("users", data)

KNOWN_USERS: dict = load_users()  # {user_id: {username, first_name, last_seen, profile_count}}

def track_user(user_id: int, username: str | None, first_name: str | None, increment_count: bool = False) -> bool:
    """Upsert user into registry. Thread-safe. Returns True if this is a brand-new user."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with users_lock:
        is_new = user_id not in KNOWN_USERS
        existing = KNOWN_USERS.get(user_id, {})
        KNOWN_USERS[user_id] = {
            "username":      username or existing.get("username", ""),
            "first_name":    first_name or existing.get("first_name", ""),
            "last_seen":     now,
            "profile_count": existing.get("profile_count", 0) + (1 if increment_count else 0),
            # Existing users from before approval mode remain approved.
            "approval_status": existing.get(
                "approval_status",
                "pending" if is_new else "approved",
            ),
        }
        save_users(KNOWN_USERS)
    return is_new


def get_approval_status(user_id: int) -> str:
    """Return pending, approved, or rejected for a registered user."""
    if user_id == get_admin_id():
        return "approved"
    with users_lock:
        info = KNOWN_USERS.get(user_id)
        if not info:
            return "pending"
        status = info.get("approval_status")
        # Users created before approval mode was introduced are trusted.
        return status if status in ("pending", "approved", "rejected") else "approved"


def is_user_approved(user_id: int) -> bool:
    return get_approval_status(user_id) == "approved"


def set_approval_status(user_id: int, status: str) -> bool:
    """Persist an approval decision. Returns False if the user is unknown."""
    if status not in ("pending", "approved", "rejected"):
        return False
    with users_lock:
        if user_id not in KNOWN_USERS:
            return False
        KNOWN_USERS[user_id]["approval_status"] = status
        save_users(KNOWN_USERS)
    return True

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Admin Notifications
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def build_approval_keyboard(user_id: int):
    keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        telebot.types.InlineKeyboardButton(
            "✅ Approve",
            callback_data=f"approval:approve:{user_id}",
        ),
        telebot.types.InlineKeyboardButton(
            "❌ Reject",
            callback_data=f"approval:reject:{user_id}",
        ),
    )
    return keyboard


def notify_admin(text: str, reply_markup=None):
    """Send a notification to the admin user. Silently ignores errors."""
    admin_id = get_admin_id()
    if not admin_id:
        return
    b = bot
    if not b or not bot_status.get("running"):
        return
    try:
        kwargs = {"parse_mode": "Markdown"}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        b.send_message(admin_id, text, **kwargs)
    except Exception as e:
        print(f"[notify_admin] failed: {e}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Per-User Profile History
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MAX_HISTORY_PER_USER = 10
user_profiles_lock = Lock()

def load_user_profiles() -> dict:
    db_data = db_get("user_profiles")
    if db_data is not None:
        return {int(k): v for k, v in db_data.items()}
    return {}

def save_user_profiles(data: dict):
    db_set("user_profiles", data)

USER_PROFILES: dict = load_user_profiles()

def record_user_profile(user_id: int, profile: dict):
    """Append a profile snapshot to the user's history (keep last MAX_HISTORY_PER_USER)."""
    from datetime import datetime, timezone
    entry = {k: profile.get(k, "") for k in profile}
    entry["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with user_profiles_lock:
        history = USER_PROFILES.get(user_id, [])
        history = history[-(MAX_HISTORY_PER_USER - 1):]
        history.append(entry)
        USER_PROFILES[user_id] = history
        save_user_profiles(USER_PROFILES)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Admin File Library
# Files uploaded from the dashboard are kept on disk and sent as Telegram
# documents when a user requests them with /download <command>.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FILE_STORAGE_DIR = "bot_files"
FILE_INDEX_PATH = "uploaded_files.json"
FILE_MAX_BYTES = 50 * 1024 * 1024
stored_files_lock = Lock()

def load_stored_files() -> list:
    db_data = db_get("uploaded_files")
    if isinstance(db_data, list):
        return db_data
    try:
        with open(FILE_INDEX_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

def save_stored_files(files: list):
    db_set("uploaded_files", files)
    os.makedirs(FILE_STORAGE_DIR, exist_ok=True)
    temp_path = FILE_INDEX_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as fh:
        json.dump(files, fh, ensure_ascii=False, indent=2)
    os.replace(temp_path, FILE_INDEX_PATH)

STORED_FILES: list = load_stored_files()

def get_stored_file(command: str) -> dict | None:
    command = (command or "").strip().lstrip("/").lower()
    with stored_files_lock:
        return next((item for item in STORED_FILES if item.get("command") == command), None)

def file_is_available(item: dict, user_id: int) -> bool:
    """Check expiry and optional allow-list access for a stored file."""
    expires_at = (item.get("expires_at") or "").strip()
    if expires_at:
        try:
            if _dt.fromisoformat(expires_at) <= _dt.now():
                return False
        except ValueError:
            pass
    access_mode = item.get("access_mode", "all")
    if access_mode == "selected":
        allowed = {int(uid) for uid in item.get("allowed_users", []) if str(uid).lstrip("-").isdigit()}
        if user_id not in allowed and user_id != get_admin_id():
            return False
    return True

def get_available_stored_files(user_id: int) -> list:
    with stored_files_lock:
        return [item for item in STORED_FILES if file_is_available(item, user_id)]

def get_file_commands_text(user_id: int | None = None) -> str:
    with stored_files_lock:
        files = [item for item in STORED_FILES if user_id is None or file_is_available(item, user_id)]
    if not files:
        return "📂 এখন কোনো ফাইল সংরক্ষিত নেই।"
    lines = ["📂 ডাউনলোড করা যায় এমন ফাইল:\n"]
    for item in files:
        category = item.get("category", "General")
        lines.append(
            f"• [{category}] {item.get('title') or item.get('filename', 'File')} — "
            f"/download {item.get('command', '')}"
        )
    return "\n".join(lines)

def build_file_keyboard(user_id: int):
    files = get_available_stored_files(user_id)
    if not files:
        return None
    keyboard = telebot.types.InlineKeyboardMarkup(row_width=1)
    for item in files:
        title = item.get("title") or item.get("filename", "File")
        category = item.get("category", "General")
        version = f" v{item['version']}" if item.get("version") else ""
        keyboard.add(telebot.types.InlineKeyboardButton(
            f"📥 [{category}] {title}{version}"[:64],
            callback_data=f"file:{item.get('id', '')}",
        ))
    return keyboard

def record_file_download(item_id: str, user):
    with stored_files_lock:
        item = next((entry for entry in STORED_FILES if entry.get("id") == item_id), None)
        if not item:
            return
        item["downloads"] = int(item.get("downloads", 0) or 0) + 1
        recent = item.get("last_downloads", [])
        if not isinstance(recent, list):
            recent = []
        recent.insert(0, {
            "user_id": user.id,
            "username": user.username or "",
            "first_name": user.first_name or "",
            "timestamp": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        item["last_downloads"] = recent[:20]
        try:
            save_stored_files(STORED_FILES)
        except OSError as exc:
            print(f"[FileDownload] stats save error: {exc}")

def send_stored_file(bot_instance, chat_id: int, item: dict, user) -> tuple[bool, str]:
    if not file_is_available(item, user.id):
        return False, "এই ফাইলটি আপনার জন্য অনুমোদিত নয় অথবা এর মেয়াদ শেষ হয়েছে।"

    if item.get("storage") == "telegram_channel":
        source_chat_id = str(item.get("source_chat_id", "")).strip()
        try:
            source_message_id = int(item.get("source_message_id", 0))
        except (TypeError, ValueError):
            source_message_id = 0
        if not source_chat_id or source_message_id <= 0:
            return False, "Private channel file-এর source তথ্য অসম্পূর্ণ।"
        try:
            copy_kwargs = {"protect_content": True}
            if item.get("caption"):
                copy_kwargs["caption"] = item["caption"]
            bot_instance.copy_message(
                chat_id,
                source_chat_id,
                source_message_id,
                **copy_kwargs,
            )
            record_file_download(item.get("id", ""), user)
            return True, ""
        except Exception as exc:
            print(
                f"[ChannelFile] source={source_chat_id}/{source_message_id} "
                f"error: {exc}"
            )
            return False, (
                "Private channel থেকে file পাঠানো যায়নি। "
                "Bot-কে channel-এ member/admin করে channel ID ও message ID যাচাই করুন।"
            )

    file_path = item.get("path", "")
    file_handle = None
    should_close = False
    if file_path and os.path.isfile(file_path):
        try:
            file_handle = open(file_path, "rb")
            should_close = True
        except OSError:
            file_handle = None
    if file_handle is None and item.get("storage") == "database":
        blob = db_get_blob(item.get("id", ""))
        if blob:
            file_handle = io.BytesIO(blob)
            file_handle.name = item.get("filename", "download")
    if file_handle is None:
        return False, "ফাইলটি বর্তমানে পাওয়া যাচ্ছে না। অ্যাডমিনকে জানান।"
    try:
        bot_instance.send_document(
            chat_id,
            file_handle,
            caption=item.get("caption") or item.get("title") or item.get("filename", ""),
        )
        record_file_download(item.get("id", ""), user)
        return True, ""
    except Exception as exc:
        print(f"[FileDownload] id={item.get('id', '')} error: {exc}")
        return False, "ফাইল পাঠানো যায়নি। একটু পরে আবার চেষ্টা করুন।"
    finally:
        if should_close:
            file_handle.close()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Broadcast state (in-memory)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
broadcast_status = {"running": False, "total": 0, "sent": 0, "failed": 0, "done": False, "message": ""}
broadcast_lock = Lock()

def _do_broadcast(text: str, user_ids: list, bot_snapshot):
    """Run in a daemon thread. Sends message to each user_id."""
    import time
    for uid in user_ids:
        success = False
        try:
            if bot_snapshot:
                bot_snapshot.send_message(uid, text)
                success = True
        except Exception:
            pass
        with broadcast_lock:
            if success:
                broadcast_status["sent"] += 1
            else:
                broadcast_status["failed"] += 1
        time.sleep(0.05)  # ~20 msg/s — safe for Telegram limits
    with broadcast_lock:
        broadcast_status["running"] = False
        broadcast_status["done"] = True

def generate_unique_bd_name():
    global USED_NAMES
    with used_names_lock:
        total = get_total_combinations()
        if len(USED_NAMES) >= total:
            USED_NAMES = set()
            save_used_names(USED_NAMES)
        attempts = 0
        while True:
            prefix    = random.choice(get_prefixes())
            first     = random.choice(get_first_names())
            last      = random.choice(get_last_names())
            full_name = f"{prefix} {first} {last}"
            attempts += 1
            if full_name not in USED_NAMES:
                USED_NAMES.add(full_name)
                save_used_names(USED_NAMES)
                return full_name
            if attempts > total:
                USED_NAMES = set()
                save_used_names(USED_NAMES)

def generate_fake_phone(code, pattern_or_list):
    """Pick a random pattern from a list (or use the single string) then generate."""
    if isinstance(pattern_or_list, list) and pattern_or_list:
        pattern = random.choice(pattern_or_list)
    else:
        pattern = pattern_or_list
    return code + "".join(
        str(random.randint(0, 9)) if c == 'X' else c for c in pattern
    )

def clean_to_english(text):
    return "".join(
        c for c in unicodedata.normalize('NFD', text)
        if unicodedata.category(c) != 'Mn'
    )

def generate_profile(country_key):
    global USED_PHONES, USED_USERNAMES
    country_details = get_country_details()
    if country_key not in country_details:
        return None
    details = country_details[country_key]

    # ── 1. Unique full name ──────────────────────────────────────
    if details['is_bd']:
        full_name = generate_unique_bd_name()
    else:
        fake = Faker(details['locale'])
        # Retry until we get a name no other user has received
        with used_names_lock:
            for _ in range(200):
                candidate = clean_to_english(fake.name_male())
                if candidate not in USED_NAMES:
                    full_name = candidate
                    USED_NAMES.add(full_name)
                    save_used_names(USED_NAMES)
                    break
            else:
                # Safety valve: use as-is (extremely rare edge case)
                full_name = clean_to_english(fake.name_male())

    # ── 2. Derive base tokens ────────────────────────────────────
    tokens    = full_name.split()
    nick_base = re.sub(r'[^a-zA-Z]', '', tokens[-1] if len(tokens) > 1 else tokens[0])
    if not nick_base:
        nick_base = "User"

    clean_name = re.sub(r'[^a-zA-Z0-9]', '', full_name).lower()
    if not clean_name:
        clean_name = "user" + str(random.randint(1000, 9999))

    sfx_list = get_nickname_sfx()
    sfx      = random.choice(sfx_list) if sfx_list else ""
    nickname = f"{nick_base.capitalize()}{sfx}" if sfx else nick_base.capitalize()
    facebook_id = full_name

    # ── 3. Unique username (→ email) ─────────────────────────────
    # Base truncated to 12 chars + 4-digit suffix → total 5–16 chars (4–16 limit)
    _uname_base = (clean_name[:12] or "user")
    with used_usernames_lock:
        for _ in range(10_000):
            rnum     = random.randint(1000, 9999)   # 4-digit suffix
            username = f"{_uname_base}{rnum}"
            if username not in USED_USERNAMES:
                USED_USERNAMES.add(username)
                save_used_usernames(USED_USERNAMES)
                break
        else:
            rnum     = random.randint(1000, 9999)
            username = f"{_uname_base}{rnum}"

    email = f"{username}@gmail.com"

    # ── 4. Unique phone ──────────────────────────────────────────
    with used_phones_lock:
        for _ in range(10_000):
            phone = generate_fake_phone(details['code'], details['digits'])
            if phone not in USED_PHONES:
                USED_PHONES.add(phone)
                save_used_phones(USED_PHONES)
                break
        # If somehow all tried phones are taken, last generated value is used

    return {
        "country":     country_key,
        "full_name":   full_name,
        "nickname":    nickname,
        "username":    username,
        "facebook_id": facebook_id,
        "google":      email,
        "whatsapp":    phone,
        "email":       email,
        "mobile":      phone,
    }

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Telegram Handler Registration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def register_handlers(b: telebot.TeleBot):

    def _get_enabled_fields():
        return CONFIG.get("bot_reply_fields", FIELD_KEYS)

    USER_MENU_BUTTONS = {
        "generate": "⚡ Generate Profile",
        "ai":       "🤖 AI Name",
        "history":  "📋 My History",
        "files":    "📂 Files",
        "help":     "❓ Help",
    }

    def _user_menu_keyboard():
        keyboard = telebot.types.ReplyKeyboardMarkup(
            resize_keyboard=True,
            row_width=2,
        )
        keyboard.add(
            telebot.types.KeyboardButton(USER_MENU_BUTTONS["generate"]),
            telebot.types.KeyboardButton(USER_MENU_BUTTONS["ai"]),
        )
        keyboard.add(
            telebot.types.KeyboardButton(USER_MENU_BUTTONS["history"]),
            telebot.types.KeyboardButton(USER_MENU_BUTTONS["files"]),
        )
        keyboard.add(telebot.types.KeyboardButton(USER_MENU_BUTTONS["help"]))
        return keyboard

    def _country_keyboard(action: str):
        keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
        buttons = []
        seen = set()
        for country in get_country_details():
            # "bd" is an alias for Bangladesh; show it only once.
            canonical = "bangladesh" if country == "bd" else country
            if canonical in seen:
                continue
            seen.add(canonical)
            buttons.append(
                telebot.types.InlineKeyboardButton(
                    f"🌍 {canonical.capitalize()}",
                    callback_data=f"menu:{action}:{canonical}",
                )
            )
        keyboard.add(*buttons)
        return keyboard

    def _send_user_menu(message):
        b.reply_to(
            message,
            get_text("user_menu"),
            reply_markup=_user_menu_keyboard(),
            parse_mode="Markdown",
        )

    def _notify_new_pending_user(message):
        user = getattr(message, "from_user", message)
        uname = f"@{user.username}" if user.username else "N/A"
        fullname = f"{user.first_name or ''} {user.last_name or ''}".strip() or "N/A"
        Thread(target=notify_admin, args=(
            get_text(
                "approval_admin_notify",
                fullname=fullname,
                uname=uname,
                user_id=user.id,
                total_users=f"{len(KNOWN_USERS):,}",
            ),
            build_approval_keyboard(user.id),
        ), daemon=True).start()

    def _track_entry_user(subject):
        user = getattr(subject, "from_user", subject)
        is_new = track_user(
            user.id,
            user.username,
            user.first_name,
        )
        if is_new and user.id != get_admin_id():
            _notify_new_pending_user(subject)
        return is_new

    def _approval_guard(message) -> bool:
        """Stop unapproved users before any user-facing bot action."""
        user_id = message.from_user.id
        if is_user_approved(user_id):
            return True
        status = get_approval_status(user_id)
        key = "approval_rejected" if status == "rejected" else "approval_pending"
        b.reply_to(message, get_text(key))
        return False

    def _send_country_menu(message, action: str):
        icon = "🤖" if action == "ai" else "⚡"
        b.reply_to(
            message,
            get_text("country_menu", icon=icon),
            reply_markup=_country_keyboard(action),
        )

    def _send_generated_profile(chat_id, user, country_input):
        """Generate and send a regular profile from a menu or typed country."""
        if not is_user_approved(user.id):
            key = "approval_rejected" if get_approval_status(user.id) == "rejected" else "approval_pending"
            b.send_message(chat_id, get_text(key))
            return
        if is_banned(user.id):
            b.send_message(chat_id, get_text("banned_reply"))
            return
        try:
            country_details = get_country_details()
            profile = generate_profile(country_input)
            if not profile:
                b.send_message(chat_id, get_text("profile_failed"))
                return
            recent_log.append(profile)
            Thread(target=record_user_profile, args=(user.id, profile), daemon=True).start()
            if country_details[country_input].get("is_bd"):
                log_name_usage(
                    bd_name=profile["full_name"],
                    user_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                )
            track_user(user.id, user.username, user.first_name, increment_count=True)
            enabled = _get_enabled_fields()
            field_lines = ""
            for key in FIELD_KEYS:
                if key in enabled and key in profile:
                    field_lines += f"{FIELD_EMOJI[key]} {FIELD_LABELS[key]}: `{profile[key]}`\n"
            tg = user.username
            tg_mention = f"@{tg}" if tg else "Not Available"
            dev_name = get_developer_name()
            dev_line = f"👤 Developer: {dev_name}\n" if dev_name else ""
            b.send_message(
                chat_id,
                get_text(
                    "profile_reply",
                    dev_line=dev_line,
                    tg_mention=tg_mention,
                    country=country_input.capitalize(),
                    field_lines=field_lines,
                ),
                parse_mode="Markdown",
                reply_markup=build_profile_copy_keyboard(profile, enabled),
            )
        except Exception:
            b.send_message(chat_id, get_text("profile_failed"))

    def _run_ai_generation(chat_id, user, country_arg):
        """Generate an AI-assisted profile and send it asynchronously."""
        if not is_user_approved(user.id):
            key = "approval_rejected" if get_approval_status(user.id) == "rejected" else "approval_pending"
            b.send_message(chat_id, get_text(key))
            return
        if is_banned(user.id):
            b.send_message(chat_id, get_text("banned_reply"))
            return
        if not _GEMINI_KEY:
            b.send_message(chat_id, get_text("ai_unavailable"))
            return
        country_details = get_country_details()
        if country_arg not in country_details:
            keys = ", ".join(k.capitalize() for k in country_details.keys())
            b.send_message(chat_id, get_text("ai_unknown_country", keys=keys))
            return

        wait_msg = b.send_message(chat_id, get_text("ai_generating"))

        def _do_ainame():
            ai_name = generate_ai_name(country_arg)
            profile = generate_profile(country_arg)
            if not profile:
                try:
                    b.delete_message(chat_id, wait_msg.message_id)
                except Exception:
                    pass
                b.send_message(chat_id, get_text("ai_profile_failed"))
                return

            if ai_name:
                profile["full_name"] = ai_name
                profile["facebook_id"] = ai_name
                clean = re.sub(r"[^a-zA-Z0-9]", "", ai_name).lower()
                if clean:
                    rnum = random.randint(1000, 9999)
                    profile["username"] = f"{clean[:12]}{rnum}"
                    profile["email"] = f"{clean}{rnum}@gmail.com"
                    profile["google"] = profile["email"]

            Thread(target=record_user_profile, args=(user.id, profile), daemon=True).start()
            enabled = _get_enabled_fields()
            field_lines = ""
            for key in FIELD_KEYS:
                if key in enabled and key in profile:
                    field_lines += f"{FIELD_EMOJI[key]} {FIELD_LABELS[key]}: `{profile[key]}`\n"

            dev_name = get_developer_name()
            dev_line = f"👤 Developer: {dev_name}\n" if dev_name else ""
            tg = user.username
            tg_mention = f"@{tg}" if tg else "Not Available"
            ai_badge = "🤖 *AI Generated*" if ai_name else "⚡ *Auto Generated*"
            try:
                b.delete_message(chat_id, wait_msg.message_id)
            except Exception:
                pass
            b.send_message(
                chat_id,
                get_text(
                    "ai_reply",
                    dev_line=dev_line,
                    ai_badge=ai_badge,
                    tg_mention=tg_mention,
                    country=country_arg.capitalize(),
                    field_lines=field_lines,
                ),
                parse_mode="Markdown",
                reply_markup=build_profile_copy_keyboard(profile, enabled),
            )

        Thread(target=_do_ainame, daemon=True).start()

    @b.message_handler(commands=['start'])
    def send_welcome(message):
        _track_entry_user(message)
        if not _approval_guard(message):
            return
        used_count   = len(USED_NAMES)
        remaining    = get_total_combinations() - used_count
        country_keys = ", ".join(k.capitalize() for k in get_country_details().keys())
        b.reply_to(message, get_text("welcome",
            country_keys=country_keys,
            used_count=f"{used_count:,}",
            remaining=f"{remaining:,}",
        ), reply_markup=_user_menu_keyboard())
    @b.message_handler(commands=['panel'])
    def admin_panel(message):
        admin_id = get_admin_id()
        if admin_id and message.from_user.id == admin_id:
            total = get_total_combinations()
            b.reply_to(message, get_text("panel_reply",
                developer_name=get_developer_name(),
                total_combinations=f"{total:,}",
                used_count=f"{len(USED_NAMES):,}",
                remaining=f"{total - len(USED_NAMES):,}",
            ))
        else:
            b.reply_to(message, get_text("panel_not_admin"))

    @b.message_handler(commands=['reset'])
    def reset_names(message):
        admin_id = get_admin_id()
        if admin_id and message.from_user.id == admin_id:
            global USED_NAMES
            USED_NAMES = set()
            save_used_names(USED_NAMES)
            b.reply_to(message, get_text("reset_success"))
        else:
            b.reply_to(message, get_text("reset_not_admin"))

    @b.message_handler(commands=['history'])
    def send_history(message):
        _track_entry_user(message)
        if not _approval_guard(message):
            return
        with user_profiles_lock:
            history = list(USER_PROFILES.get(message.from_user.id, []))
        if not history:
            b.reply_to(message, get_text("history_empty"))
            return
        lines = [f"📋 আপনার শেষ *{len(history)}* টি প্রোফাইল:\n"]
        for i, p in enumerate(reversed(history), 1):
            ts = p.get("timestamp", "")[:10]
            country = p.get("country", "").capitalize()
            lines.append(f"━━━ #{i} ┃ {country} ┃ {ts} ━━━")
            lines.append(f"👤 `{p.get('full_name', '')}`")
            lines.append(f"📧 `{p.get('email', '')}`")
            lines.append(f"📞 `{p.get('mobile', '')}`")
            lines.append("")
        lines.append(get_text("history_copy_hint"))
        b.reply_to(message, "\n".join(lines), parse_mode="Markdown")

    @b.message_handler(commands=['files'])
    def list_downloadable_files(message):
        _track_entry_user(message)
        if not _approval_guard(message):
            return
        if is_banned(message.from_user.id):
            b.reply_to(message, get_text("banned_reply"))
            return
        keyboard = build_file_keyboard(message.from_user.id)
        b.reply_to(
            message,
            get_file_commands_text(message.from_user.id),
            reply_markup=keyboard,
        )

    @b.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("file:")))
    def download_file_button(call):
        user = call.from_user
        _track_entry_user(user)
        if not is_user_approved(user.id):
            try:
                b.answer_callback_query(
                    call.id,
                    get_text(
                        "approval_rejected"
                        if get_approval_status(user.id) == "rejected"
                        else "approval_pending",
                    )[:200],
                    show_alert=True,
                )
            except Exception:
                pass
            return
        try:
            b.answer_callback_query(call.id, "ফাইল পাঠানো হচ্ছে…")
        except Exception:
            pass
        file_id = call.data.split(":", 1)[1].strip()
        with stored_files_lock:
            item = next((entry.copy() for entry in STORED_FILES if entry.get("id") == file_id), None)
        if not item:
            b.send_message(call.message.chat.id, "❌ ফাইলটি আর পাওয়া যাচ্ছে না।")
            return
        ok, error = send_stored_file(b, call.message.chat.id, item, user)
        if not ok:
            b.send_message(call.message.chat.id, f"⚠️ {error}")

    @b.message_handler(commands=['download', 'getfile'])
    def download_stored_file(message):
        _track_entry_user(message)
        if not _approval_guard(message):
            return
        if is_banned(message.from_user.id):
            b.reply_to(message, get_text("banned_reply"))
            return
        parts = (message.text or "").strip().split(maxsplit=1)
        command = parts[1].strip().lstrip("/").lower() if len(parts) > 1 else ""
        if not command:
            b.reply_to(message, "📥 কমান্ড দিন: `/download command`", parse_mode="Markdown")
            return
        item = get_stored_file(command)
        if not item or not file_is_available(item, message.from_user.id):
            b.reply_to(
                message,
                f"❌ `{command}` নামে কোনো ফাইল পাওয়া যায়নি, অথবা আপনার access নেই/মেয়াদ শেষ।\n\n"
                f"{get_file_commands_text(message.from_user.id)}",
                parse_mode="Markdown",
            )
            return
        ok, error = send_stored_file(b, message.chat.id, item, message.from_user)
        if not ok:
            b.reply_to(message, f"⚠️ {error}")

    @b.message_handler(commands=['help'])
    def help_command(message):
        _track_entry_user(message)
        if not _approval_guard(message):
            return
        if is_banned(message.from_user.id):
            b.reply_to(message, get_text("banned_reply"))
            return
        b.reply_to(
            message,
            get_text("help"),
            reply_markup=_user_menu_keyboard(),
            parse_mode="Markdown",
        )

    @b.message_handler(func=lambda message: (message.text or "").strip() == USER_MENU_BUTTONS["generate"])
    def menu_generate(message):
        _track_entry_user(message)
        if not _approval_guard(message):
            return
        if is_banned(message.from_user.id):
            b.reply_to(message, get_text("banned_reply"))
            return
        _send_country_menu(message, "generate")

    @b.message_handler(func=lambda message: (message.text or "").strip() == USER_MENU_BUTTONS["ai"])
    def menu_ai(message):
        _track_entry_user(message)
        if not _approval_guard(message):
            return
        if is_banned(message.from_user.id):
            b.reply_to(message, get_text("banned_reply"))
            return
        _send_country_menu(message, "ai")

    @b.message_handler(func=lambda message: (message.text or "").strip() == USER_MENU_BUTTONS["history"])
    def menu_history(message):
        send_history(message)

    @b.message_handler(func=lambda message: (message.text or "").strip() == USER_MENU_BUTTONS["files"])
    def menu_files(message):
        list_downloadable_files(message)

    @b.message_handler(func=lambda message: (message.text or "").strip() == USER_MENU_BUTTONS["help"])
    def menu_help(message):
        help_command(message)

    @b.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("menu:")))
    def menu_callback(call):
        try:
            b.answer_callback_query(call.id)
        except Exception:
            pass
        parts = call.data.split(":", 2)
        if len(parts) != 3:
            return
        action, country = parts[1], parts[2].lower()
        user = call.from_user
        _track_entry_user(user)
        if not is_user_approved(user.id):
            try:
                b.answer_callback_query(
                    call.id,
                    get_text(
                        "approval_rejected"
                        if get_approval_status(user.id) == "rejected"
                        else "approval_pending",
                    )[:200],
                    show_alert=True,
                )
            except Exception:
                pass
            return
        if is_banned(user.id):
            b.send_message(call.message.chat.id, get_text("banned_reply"))
            return
        if action == "generate":
            _send_generated_profile(call.message.chat.id, user, country)
        elif action == "ai":
            _run_ai_generation(call.message.chat.id, user, country)

    @b.message_handler(commands=['ainame'])
    def send_ai_name(message):
        _track_entry_user(message)
        if not _approval_guard(message):
            return
        parts       = message.text.strip().split(maxsplit=1)
        country_arg = parts[1].strip().lower() if len(parts) > 1 else "bangladesh"
        _run_ai_generation(message.chat.id, message.from_user, country_arg)

    # ────────────────────────────────────────────
    # Admin Bot Commands — Dashboard Control via Bot
    # ────────────────────────────────────────────
    def _admin_only(msg) -> bool:
        """Returns True if sender is admin, otherwise replies and returns False."""
        admin_id = get_admin_id()
        if admin_id and msg.from_user.id == admin_id:
            return True
        b.reply_to(msg, "❌ এই কমান্ড শুধু অ্যাডমিন ব্যবহার করতে পারবেন!")
        return False

    @b.message_handler(commands=['admin', 'menu'])
    def admin_menu(message):
        if not _admin_only(message): return
        b.reply_to(message,
            "🔐 *অ্যাডমিন কন্ট্রোল প্যানেল*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "📊 *পরিসংখ্যান ও লগ*\n"
            "• /stats — বট স্ট্যাটস\n"
            "• /users — ইউজার লিস্ট\n"
            "• /usage — নেম ইউজেজ লগ\n\n"
            "🚫 *ব্যান ম্যানেজমেন্ট*\n"
            "• /banned — ব্যানড লিস্ট\n"
            "• /ban `<id>` [কারণ] — ব্যান করুন\n"
            "• /unban `<id>` — ব্যান তুলুন\n\n"
            "⚙️ *কনফিগ ও ফিল্ড*\n"
            "• /config — বর্তমান কনফিগ\n"
            "• /fields — প্রোফাইল ফিল্ড স্ট্যাটাস\n"
            "• /setdev `<নাম>` — ডেভলপার নাম\n"
            "• /setadminid `<id>` — অ্যাডমিন ID\n\n"
            "🧪 *জেনারেট ও টেস্ট*\n"
            "• /gen `<country>` — টেস্ট প্রোফাইল\n"
            "• /reset — ব্যবহৃত নাম রিসেট\n\n"
            "📢 *ব্রডকাস্ট*\n"
            "• /broadcast `<টেক্সট>` — সবাইকে মেসেজ\n\n"
            "📂 *ফাইল ডাউনলোড*\n"
            "• /files — সংরক্ষিত ফাইলের তালিকা\n"
            "• /download `<কমান্ড>` — ফাইল পাঠান\n\n"
            "🛑 *বট স্টপ*\n"
            "• /stopbot — পোলিং বন্ধ করুন"
            + get_text("approval_admin_menu"),
            parse_mode="Markdown"
        )

    @b.message_handler(commands=['pending'])
    def pending_users_cmd(message):
        if not _admin_only(message):
            return
        with users_lock:
            pending = [
                (uid, info.copy())
                for uid, info in KNOWN_USERS.items()
                if info.get("approval_status") == "pending"
            ]
        if not pending:
            b.reply_to(message, get_text("approval_none_pending"))
            return
        lines = []
        keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
        for uid, info in pending[:50]:
            username = f"@{info.get('username')}" if info.get("username") else "—"
            name = info.get("first_name") or "—"
            lines.append(f"• {name} {username} — ID: {uid}")
            keyboard.add(
                telebot.types.InlineKeyboardButton(
                    f"✅ {uid}",
                    callback_data=f"approval:approve:{uid}",
                ),
                telebot.types.InlineKeyboardButton(
                    f"❌ {uid}",
                    callback_data=f"approval:reject:{uid}",
                ),
            )
        if len(pending) > 50:
            lines.append(f"\n…আরও {len(pending) - 50} জন pending আছে।")
        b.reply_to(
            message,
            get_text(
                "approval_pending_list",
                count=len(pending),
                users="\n".join(lines),
            ),
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    def _set_user_approval_from_command(message, status: str):
        if not _admin_only(message):
            return
        parts = (message.text or "").strip().split()
        action = "approve" if status == "approved" else "reject"
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            b.reply_to(
                message,
                get_text("approval_admin_usage", action=action),
                parse_mode="Markdown",
            )
            return
        user_id = int(parts[1])
        if not set_approval_status(user_id, status):
            b.reply_to(message, get_text("approval_admin_not_found"))
            return
        key = "approval_admin_approved" if status == "approved" else "approval_admin_rejected"
        b.reply_to(message, get_text(key, user_id=user_id), parse_mode="Markdown")
        try:
            if status == "approved":
                b.send_message(
                    user_id,
                    get_text("approval_granted"),
                    reply_markup=_user_menu_keyboard(),
                )
            else:
                b.send_message(user_id, get_text("approval_rejected"))
        except Exception as e:
            print(f"[approval] user notification failed for {user_id}: {e}")

    @b.message_handler(commands=['approve'])
    def approve_user_cmd(message):
        _set_user_approval_from_command(message, "approved")

    @b.message_handler(commands=['reject'])
    def reject_user_cmd(message):
        _set_user_approval_from_command(message, "rejected")

    @b.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("approval:")))
    def approval_callback(call):
        if call.from_user.id != get_admin_id():
            try:
                b.answer_callback_query(call.id, "❌ শুধু অ্যাডমিন এই action করতে পারবেন।", show_alert=True)
            except Exception:
                pass
            return
        parts = call.data.split(":")
        if len(parts) != 3 or not parts[2].lstrip("-").isdigit():
            return
        action, user_id = parts[1], int(parts[2])
        status = "approved" if action == "approve" else "rejected" if action == "reject" else None
        if not status or not set_approval_status(user_id, status):
            try:
                b.answer_callback_query(call.id, get_text("approval_admin_not_found"), show_alert=True)
            except Exception:
                pass
            return
        key = "approval_admin_approved" if status == "approved" else "approval_admin_rejected"
        try:
            b.answer_callback_query(call.id, get_text(key, user_id=user_id))
            b.edit_message_reply_markup(
                call.message.chat.id,
                call.message.message_id,
                reply_markup=None,
            )
        except Exception:
            pass
        try:
            if status == "approved":
                b.send_message(
                    user_id,
                    get_text("approval_granted"),
                    reply_markup=_user_menu_keyboard(),
                )
            else:
                b.send_message(user_id, get_text("approval_rejected"))
        except Exception as e:
            print(f"[approval] user notification failed for {user_id}: {e}")

    @b.message_handler(commands=['stats'])
    def bot_stats(message):
        if not _admin_only(message): return
        total  = get_total_combinations()
        used   = len(USED_NAMES)
        remain = max(total - used, 0)
        b.reply_to(message,
            "📊 *বট স্ট্যাটস*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔤 মোট নাম সম্ভাবনা: `{total:,}`\n"
            f"✅ ব্যবহৃত নাম: `{used:,}`\n"
            f"🆕 বাকি নাম: `{remain:,}`\n\n"
            f"👥 মোট ইউজার: `{len(KNOWN_USERS):,}`\n"
            f"🚫 ব্যানড ইউজার: `{len(BANNED_USERS):,}`\n"
            f"🌍 সাপোর্টেড দেশ: `{len(get_country_details())}`\n\n"
            f"👤 ডেভলপার: {get_developer_name() or '_(সেট নেই)_'}\n"
            f"🆔 Admin ID: `{get_admin_id() or '(সেট নেই)'}`",
            parse_mode="Markdown"
        )

    @b.message_handler(commands=['users'])
    def users_list(message):
        if not _admin_only(message): return
        parts    = message.text.strip().split()
        page     = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        per_page = 10
        all_u    = sorted(KNOWN_USERS.items(),
                          key=lambda x: x[1].get("profile_count", 0), reverse=True)
        total        = len(all_u)
        total_pages  = max((total + per_page - 1) // per_page, 1)
        page         = max(1, min(page, total_pages))
        start        = (page - 1) * per_page
        chunk        = all_u[start:start + per_page]
        lines = [f"👥 *ইউজার লিস্ট* — পেজ {page}/{total_pages} (মোট {total:,})\n"]
        for rank, (uid, info) in enumerate(chunk, start + 1):
            uname = f"@{info['username']}" if info.get("username") else "—"
            name  = info.get("first_name") or "—"
            count = info.get("profile_count", 0)
            lines.append(f"{rank}. {name} {uname}\n   🆔 `{uid}` | 📋 {count} profiles")
        if page < total_pages:
            lines.append(f"\n➡️ পরের পেজ: /users {page + 1}")
        b.reply_to(message, "\n".join(lines), parse_mode="Markdown")

    @b.message_handler(commands=['usage'])
    def usage_list(message):
        if not _admin_only(message): return
        parts    = message.text.strip().split()
        page     = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        per_page = 15
        names    = sorted(USED_NAMES)
        total    = len(names)
        if not total:
            b.reply_to(message, "ℹ️ এখনও কোনো নাম ব্যবহৃত হয়নি।")
            return
        total_pages = max((total + per_page - 1) // per_page, 1)
        page        = max(1, min(page, total_pages))
        start       = (page - 1) * per_page
        chunk       = names[start:start + per_page]
        lines = [f"📋 *ব্যবহৃত নাম লগ* — পেজ {page}/{total_pages} (মোট {total:,})\n"]
        for i, name in enumerate(chunk, start + 1):
            lines.append(f"{i}. `{name}`")
        if page < total_pages:
            lines.append(f"\n➡️ পরের পেজ: /usage {page + 1}")
        b.reply_to(message, "\n".join(lines), parse_mode="Markdown")

    @b.message_handler(commands=['banned'])
    def banned_list_cmd(message):
        if not _admin_only(message): return
        if not BANNED_USERS:
            b.reply_to(message, "✅ কোনো ব্যানড ইউজার নেই।")
            return
        lines = [f"🚫 *ব্যানড ইউজার* (মোট {len(BANNED_USERS)})\n"]
        for uid, info in list(BANNED_USERS.items())[:20]:
            uname  = f"@{info.get('username')}" if info.get("username") else "—"
            reason = info.get("reason") or "—"
            lines.append(f"• `{uid}` {uname}\n  কারণ: {reason}")
        if len(BANNED_USERS) > 20:
            lines.append(f"\n…আরও {len(BANNED_USERS) - 20} জন (ড্যাশবোর্ড দেখুন)")
        b.reply_to(message, "\n".join(lines), parse_mode="Markdown")

    @b.message_handler(commands=['ban'])
    def ban_user_cmd(message):
        if not _admin_only(message): return
        parts = message.text.strip().split(maxsplit=2)
        if len(parts) < 2 or not parts[1].lstrip('-').isdigit():
            b.reply_to(message, "⚠️ ব্যবহার: /ban `<user_id>` [কারণ]", parse_mode="Markdown")
            return
        uid    = int(parts[1])
        reason = parts[2].strip() if len(parts) > 2 else "অ্যাডমিন কর্তৃক ব্যান"
        if uid in BANNED_USERS:
            b.reply_to(message, f"ℹ️ ইউজার `{uid}` ইতিমধ্যে ব্যান আছে।", parse_mode="Markdown")
            return
        info = KNOWN_USERS.get(uid, {})
        BANNED_USERS[uid] = {
            "username":   info.get("username"),
            "first_name": info.get("first_name"),
            "reason":     reason,
            "timestamp":  _dt.utcnow().isoformat(),
        }
        save_banned(BANNED_USERS)
        b.reply_to(message,
            f"✅ ইউজার `{uid}` ব্যান করা হয়েছে।\nকারণ: {reason}",
            parse_mode="Markdown"
        )

    @b.message_handler(commands=['unban'])
    def unban_user_cmd(message):
        if not _admin_only(message): return
        parts = message.text.strip().split()
        if len(parts) < 2 or not parts[1].lstrip('-').isdigit():
            b.reply_to(message, "⚠️ ব্যবহার: /unban `<user_id>`", parse_mode="Markdown")
            return
        uid = int(parts[1])
        if uid not in BANNED_USERS:
            b.reply_to(message, f"ℹ️ ইউজার `{uid}` ব্যানড নেই।", parse_mode="Markdown")
            return
        BANNED_USERS.pop(uid)
        save_banned(BANNED_USERS)
        b.reply_to(message, f"✅ ইউজার `{uid}` আনব্যান করা হয়েছে।", parse_mode="Markdown")

    @b.message_handler(commands=['broadcast'])
    def broadcast_cmd(message):
        if not _admin_only(message): return
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            b.reply_to(message, "⚠️ ব্যবহার: /broadcast `<মেসেজ টেক্সট>`", parse_mode="Markdown")
            return
        text     = parts[1].strip()
        user_ids = list(KNOWN_USERS.keys())
        if not user_ids:
            b.reply_to(message, "❌ কোনো ইউজার নেই।")
            return
        wait_msg = b.reply_to(message, f"📢 {len(user_ids):,} জনকে মেসেজ পাঠানো হচ্ছে…")
        def _do_bc():
            sent = failed = 0
            for uid in user_ids:
                try:
                    b.send_message(uid, text)
                    sent += 1
                    _time_module.sleep(0.05)   # rate-limit
                except Exception:
                    failed += 1
            try:
                b.edit_message_text(
                    f"✅ ব্রডকাস্ট সম্পন্ন!\n\n"
                    f"📤 পাঠানো: {sent}\n"
                    f"❌ ব্যর্থ: {failed}",
                    message.chat.id, wait_msg.message_id
                )
            except Exception:
                pass
        Thread(target=_do_bc, daemon=True).start()

    @b.message_handler(commands=['gen', 'generate'])
    def gen_profile_cmd(message):
        if not _admin_only(message): return
        parts       = message.text.strip().split(maxsplit=1)
        country_arg = parts[1].strip().lower() if len(parts) > 1 else "bangladesh"
        country_details = get_country_details()
        if country_arg not in country_details:
            keys = ", ".join(k.capitalize() for k in country_details.keys())
            b.reply_to(message, f"⚠️ অজানা দেশ। লিখুন: {keys}")
            return
        try:
            profile     = generate_profile(country_arg)
            enabled     = CONFIG.get("bot_reply_fields", FIELD_KEYS)
            field_lines = ""
            for key in FIELD_KEYS:
                if key in enabled and key in profile:
                    field_lines += f"{FIELD_EMOJI[key]} {FIELD_LABELS[key]}: `{profile[key]}`\n"
            b.reply_to(message,
                f"🧪 *টেস্ট প্রোফাইল* — {country_arg.capitalize()}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{field_lines}"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"_(লগ বা হিস্ট্রিতে সেভ হয়নি)_",
                parse_mode="Markdown",
                reply_markup=build_profile_copy_keyboard(profile, enabled),
            )
        except Exception:
            b.reply_to(message, "⚠️ প্রোফাইল তৈরি করা সম্ভব হয়নি।")

    @b.message_handler(commands=['config'])
    def show_config_cmd(message):
        if not _admin_only(message): return
        token = CONFIG.get("bot_token", "")
        if len(token) > 12:
            token_masked = f"{token[:8]}…{token[-4:]}"
        elif token:
            token_masked = token
        else:
            token_masked = "_(সেট নেই)_"
        enabled_fields = CONFIG.get("bot_reply_fields", FIELD_KEYS)
        sfx_sample     = ", ".join(CONFIG.get("nickname_sfx", [])[:5])
        b.reply_to(message,
            "⚙️ *বর্তমান কনফিগ*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🤖 Token: `{token_masked}`\n"
            f"🆔 Admin ID: `{get_admin_id() or '(সেট নেই)'}`\n"
            f"👤 Developer: {get_developer_name() or '_(সেট নেই)_'}\n\n"
            f"📋 এনাবল্ড ফিল্ড: {len(enabled_fields)}/{len(FIELD_KEYS)}\n"
            f"🔤 মোট নাম সম্ভাবনা: `{get_total_combinations():,}`\n"
            f"🏷️ Nickname suffix (প্রথম ৫): {sfx_sample}\n\n"
            f"সম্পাদনা: /setdev, /setadminid বা ড্যাশবোর্ড",
            parse_mode="Markdown"
        )

    @b.message_handler(commands=['fields'])
    def show_fields_cmd(message):
        if not _admin_only(message): return
        enabled = set(CONFIG.get("bot_reply_fields", FIELD_KEYS))
        lines   = ["📋 *প্রোফাইল ফিল্ড স্ট্যাটাস*\n"]
        for key in FIELD_KEYS:
            icon  = "✅" if key in enabled else "❌"
            label = FIELD_LABELS.get(key, key)
            emoji = FIELD_EMOJI.get(key, "")
            lines.append(f"{icon} {emoji} {label} (`{key}`)")
        lines.append("\n_ফিল্ড চালু/বন্ধ করতে ড্যাশবোর্ডের Settings ট্যাব ব্যবহার করুন।_")
        b.reply_to(message, "\n".join(lines), parse_mode="Markdown")

    @b.message_handler(commands=['setdev'])
    def set_developer_cmd(message):
        if not _admin_only(message): return
        parts = message.text.strip().split(maxsplit=1)
        name  = parts[1].strip() if len(parts) > 1 else ""
        CONFIG["developer_name"] = name
        save_config(CONFIG)
        if name:
            b.reply_to(message, f"✅ ডেভলপার নাম সেট হয়েছে: *{name}*", parse_mode="Markdown")
        else:
            b.reply_to(message, "✅ ডেভলপার নাম সরিয়ে দেওয়া হয়েছে।")

    @b.message_handler(commands=['setadminid'])
    def set_admin_id_cmd(message):
        if not _admin_only(message): return
        parts = message.text.strip().split()
        if len(parts) < 2 or not parts[1].lstrip('-').isdigit():
            b.reply_to(message, "⚠️ ব্যবহার: /setadminid `<telegram_id>`", parse_mode="Markdown")
            return
        new_id = int(parts[1])
        CONFIG["admin_id"] = new_id
        save_config(CONFIG)
        b.reply_to(message, f"✅ Admin ID সেট হয়েছে: `{new_id}`", parse_mode="Markdown")

    @b.message_handler(commands=['stopbot'])
    def stop_bot_cmd(message):
        if not _admin_only(message): return
        b.reply_to(message, "🛑 বট পোলিং বন্ধ করা হচ্ছে…")
        Thread(target=lambda: b.stop_polling(), daemon=True).start()

    @b.message_handler(func=lambda message: True)
    def handle_all_messages(message):
        _track_entry_user(message)

        if not _approval_guard(message):
            return

        if is_banned(message.from_user.id):
            b.reply_to(message, get_text("banned_reply"))
            return

        if not message.text:
            return

        country_input   = message.text.strip().lower()
        country_details = get_country_details()
        if country_input not in country_details:
            keys = ", ".join(k.capitalize() for k in country_details.keys())
            b.reply_to(message, get_text("unknown_country", keys=keys))
            return
        try:
            profile = generate_profile(country_input)
            recent_log.append(profile)
            Thread(target=record_user_profile, args=(message.from_user.id, profile), daemon=True).start()
            tg          = message.from_user.username
            tg_mention  = f"@{tg}" if tg else "Not Available"
            dev_name    = get_developer_name()
            if country_details[country_input].get('is_bd'):
                log_name_usage(
                    bd_name    = profile['full_name'],
                    user_id    = message.from_user.id,
                    username   = message.from_user.username,
                    first_name = message.from_user.first_name,
                )
            track_user(
                message.from_user.id,
                message.from_user.username,
                message.from_user.first_name,
                increment_count=True,
            )
            enabled = _get_enabled_fields()
            field_lines = ""
            for key in FIELD_KEYS:
                if key in enabled and key in profile:
                    emoji = FIELD_EMOJI[key]
                    label = FIELD_LABELS[key]
                    field_lines += f"{emoji} {label}: `{profile[key]}`\n"
            dev_line = f"👤 Developer: {dev_name}\n" if dev_name else ""
            b.reply_to(message,
                get_text("profile_reply",
                    dev_line=dev_line, tg_mention=tg_mention,
                    country=country_input.capitalize(), field_lines=field_lines,
                ),
                parse_mode="Markdown",
                reply_markup=build_profile_copy_keyboard(profile, enabled),
            )
        except Exception:
            b.reply_to(message, get_text("profile_failed"))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Flask Routes — Auth
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route('/')
def index():
    return redirect(url_for('dashboard') if session.get('logged_in') else url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if check_password(request.form.get('password', '')):
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        error = 'Wrong password. Try again.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Flask Routes — Dashboard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route('/dashboard')
@login_required
def dashboard():
    total = get_total_combinations()
    return render_template(
        'dashboard.html',
        total_combinations=total,
        used_count=len(USED_NAMES),
        remaining=total - len(USED_NAMES),
        recent_log=list(recent_log)[-20:][::-1],
        log_count=len(recent_log),
        bot_status=bot_status,
        bot_token_saved=bool(CONFIG.get("bot_token", "").strip()),
        first_names=sorted(get_first_names()),
        last_names=sorted(get_last_names()),
        prefixes=get_prefixes(),
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Flask Routes — API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route('/api/generate', methods=['POST'])
@login_required
def api_generate():
    data    = request.get_json(force=True)
    country = data.get('country', 'bangladesh').lower()
    profile = generate_profile(country)
    if not profile:
        return jsonify(success=False, error='Unknown country')
    recent_log.append(profile)
    return jsonify(success=True, profile=profile)

@app.route('/api/reset', methods=['POST'])
@login_required
def api_reset():
    global USED_NAMES
    USED_NAMES = set()
    save_used_names(USED_NAMES)
    return jsonify(success=True, message='✅ সমস্ত ব্যবহৃত নাম রিসেট করা হয়েছে!')

@app.route('/api/stats')
@login_required
def api_stats():
    total = get_total_combinations()
    return jsonify(
        total_combinations=total,
        used_count=len(USED_NAMES),
        remaining=total - len(USED_NAMES),
        log_count=len(recent_log),
        bot_running=bot_status["running"],
        bot_error=bot_status["error"],
        first_count=len(get_first_names()),
        last_count=len(get_last_names()),
        prefix_count=len(get_prefixes()),
    )

@app.route('/api/names', methods=['GET'])
@login_required
def api_names_get():
    return jsonify(
        bd_first_names=sorted(get_first_names()),
        bd_last_names=sorted(get_last_names()),
        bd_prefixes=get_prefixes(),
        total_combinations=get_total_combinations(),
    )

@app.route('/api/names/add', methods=['POST'])
@login_required
def api_names_add():
    data  = request.get_json(force=True)
    kind  = data.get('kind', '')
    names = [n.strip().title() for n in data.get('names', []) if n.strip()]
    if kind not in ('bd_first_names', 'bd_last_names', 'bd_prefixes'):
        return jsonify(success=False, error='Invalid list type.')
    if not names:
        return jsonify(success=False, error='No valid names provided.')
    if kind == 'bd_prefixes':
        names = [n.strip() for n in data.get('names', []) if n.strip()]  # preserve prefix case
    existing = set(CONFIG.get(kind, []))
    added = [n for n in names if n not in existing]
    CONFIG[kind] = list(existing) + added
    save_config(CONFIG)
    return jsonify(success=True, added=len(added),
                   total=len(CONFIG[kind]),
                   total_combinations=get_total_combinations(),
                   message=f'✅ {len(added)} টি নাম যোগ করা হয়েছে!')

@app.route('/api/names/delete', methods=['POST'])
@login_required
def api_names_delete():
    data  = request.get_json(force=True)
    kind  = data.get('kind', '')
    name  = data.get('name', '').strip()
    if kind not in ('bd_first_names', 'bd_last_names', 'bd_prefixes'):
        return jsonify(success=False, error='Invalid list type.')
    lst = CONFIG.get(kind, [])
    if len(lst) <= 1:
        return jsonify(success=False, error='Cannot delete — list must have at least 1 item.')
    if name not in lst:
        return jsonify(success=False, error='Name not found.')
    CONFIG[kind] = [n for n in lst if n != name]
    save_config(CONFIG)
    return jsonify(success=True,
                   total=len(CONFIG[kind]),
                   total_combinations=get_total_combinations(),
                   message=f'🗑️ "{name}" মুছে ফেলা হয়েছে।')

@app.route('/api/name-usage', methods=['GET'])
@login_required
def api_name_usage():
    """Return full name-usage log, newest first."""
    q = request.args.get('q', '').strip().lower()
    with name_log_lock:
        data = list(NAME_LOG)          # copy under lock
    data.reverse()                     # newest first
    if q:
        data = [
            e for e in data
            if q in e['name'].lower()
            or q in e['username'].lower()
            or q in e['first_name'].lower()
            or q in str(e['user_id'])
        ]
    return jsonify(entries=data, total=len(NAME_LOG))

@app.route('/api/name-usage/delete', methods=['POST'])
@login_required
def api_name_usage_delete():
    """Remove exactly the first matching entry by name+user_id combo, then free the name."""
    global USED_NAMES
    data = request.get_json(force=True)
    name = data.get('name', '').strip()
    if not name:
        return jsonify(success=False, error='Name required.')
    with name_log_lock:
        idx = next((i for i, e in enumerate(NAME_LOG) if e['name'] == name), None)
        if idx is None:
            return jsonify(success=False, error='Entry not found.')
        NAME_LOG.pop(idx)
        save_name_log(NAME_LOG)
    with used_names_lock:
        USED_NAMES.discard(name)
        save_used_names(USED_NAMES)
    return jsonify(success=True, message=f'✅ "{name}" লগ থেকে মুছে নাম মুক্ত করা হয়েছে।')

@app.route('/api/name-usage/reset-user', methods=['POST'])
@login_required
def api_name_usage_reset_user():
    """Remove all entries for one Telegram user_id (frees their names too)."""
    global USED_NAMES
    data = request.get_json(force=True)
    try:
        user_id = int(data.get('user_id', 0))
    except (TypeError, ValueError):
        return jsonify(success=False, error='Invalid user_id.')
    if not user_id:
        return jsonify(success=False, error='user_id required.')
    with name_log_lock:
        freed = [e['name'] for e in NAME_LOG if e['user_id'] == user_id]
        NAME_LOG[:] = [e for e in NAME_LOG if e['user_id'] != user_id]
        save_name_log(NAME_LOG)
    with used_names_lock:
        for n in freed:
            USED_NAMES.discard(n)
        save_used_names(USED_NAMES)
    return jsonify(success=True, freed=len(freed),
                   message=f'✅ এই ইউজারের {len(freed)} টি নাম মুক্ত করা হয়েছে।')

@app.route('/api/name-usage/reset-all', methods=['POST'])
@login_required
def api_name_usage_reset_all():
    """Wipe entire usage log and free all BD names."""
    global USED_NAMES
    with name_log_lock:
        NAME_LOG.clear()
        save_name_log(NAME_LOG)
    with used_names_lock:
        USED_NAMES = set()
        save_used_names(USED_NAMES)
    return jsonify(success=True, message='✅ সমস্ত ট্র্যাকিং ডেটা রিসেট হয়েছে!')

# ── Ban / Unban ──────────────────────────────
@app.route('/api/banned', methods=['GET'])
@login_required
def api_banned_list():
    with banned_lock:
        data = [
            {"user_id": uid, **info}
            for uid, info in BANNED_USERS.items()
        ]
    data.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return jsonify(entries=data, total=len(data))

@app.route('/api/banned/ban', methods=['POST'])
@login_required
def api_ban_user():
    from datetime import datetime, timezone
    global BANNED_USERS
    body     = request.get_json(force=True)
    try:
        user_id = int(body.get("user_id", 0))
    except (TypeError, ValueError):
        return jsonify(success=False, error="Invalid user_id.")
    if not user_id:
        return jsonify(success=False, error="user_id required.")
    reason   = str(body.get("reason", "")).strip() or "No reason given"
    username = str(body.get("username", "")).strip()
    first_name = str(body.get("first_name", "")).strip()
    with banned_lock:
        BANNED_USERS[user_id] = {
            "username":   username,
            "first_name": first_name,
            "reason":     reason,
            "timestamp":  datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
        save_banned(BANNED_USERS)
    label = f"@{username}" if username else first_name or str(user_id)
    return jsonify(success=True, message=f"🚫 {label} ব্যান করা হয়েছে।")

@app.route('/api/banned/unban', methods=['POST'])
@login_required
def api_unban_user():
    global BANNED_USERS
    body = request.get_json(force=True)
    try:
        user_id = int(body.get("user_id", 0))
    except (TypeError, ValueError):
        return jsonify(success=False, error="Invalid user_id.")
    if not user_id:
        return jsonify(success=False, error="user_id required.")
    with banned_lock:
        if user_id not in BANNED_USERS:
            return jsonify(success=False, error="User is not banned.")
        info = BANNED_USERS.pop(user_id)
        save_banned(BANNED_USERS)
    label = f"@{info.get('username')}" if info.get('username') else info.get('first_name') or str(user_id)
    return jsonify(success=True, message=f"✅ {label} আনব্যান করা হয়েছে।")

@app.route('/api/names/reset-defaults', methods=['POST'])
@login_required
def api_names_reset_defaults():
    data = request.get_json(force=True)
    kind = data.get('kind', '')
    if kind not in ('bd_first_names', 'bd_last_names', 'bd_prefixes'):
        return jsonify(success=False, error='Invalid list type.')
    # Reload from file each time so changes to names_default.json take effect
    fresh_defaults = load_name_defaults()
    new_list = fresh_defaults.get(kind, [])
    if not new_list:
        return jsonify(success=False, error=f'Could not load defaults from {NAMES_DEFAULT_FILE}.')
    CONFIG[kind] = new_list[:]
    save_config(CONFIG)
    return jsonify(success=True,
                   total=len(CONFIG[kind]),
                   total_combinations=get_total_combinations(),
                   message='✅ ডিফল্ট লিস্টে ফিরে যাওয়া হয়েছে!')

@app.route('/api/set-bot-token', methods=['POST'])
@login_required
def api_set_bot_token():
    data  = request.get_json(force=True)
    token = data.get('token', '').strip()
    if not token:
        return jsonify(success=False, error='Token cannot be empty.')
    ok, err = launch_bot(token)
    if not ok:
        return jsonify(success=False, error=f'Invalid token: {err}')
    CONFIG['bot_token'] = token
    save_config(CONFIG)
    return jsonify(success=True, message='✅ Bot connected successfully!')

@app.route('/api/stop-bot', methods=['POST'])
@login_required
def api_stop_bot():
    global bot
    with bot_lock:
        if bot:
            try:
                bot.stop_polling()
            except Exception:
                pass
            bot = None
        bot_status["running"] = False
        bot_status["error"]   = ""
        bot_status["token_preview"] = ""
    CONFIG['bot_token'] = ''
    save_config(CONFIG)
    return jsonify(success=True, message='🛑 Bot stopped.')

@app.route('/api/change-password', methods=['POST'])
@login_required
def api_change_password():
    data         = request.get_json(force=True)
    current_pass = data.get('current', '')
    new_pass     = data.get('new', '').strip()
    if not check_password(current_pass):
        return jsonify(success=False, error='Current password is incorrect.')
    if len(new_pass) < 6:
        return jsonify(success=False, error='New password must be at least 6 characters.')
    CONFIG['password_hash'] = _hash_password(new_pass)
    save_config(CONFIG)
    return jsonify(success=True, message='✅ Password changed! Please log in again.')

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Flask Routes — Countries API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route('/api/countries', methods=['GET'])
@login_required
def api_countries_get():
    return jsonify(countries=get_country_details())

@app.route('/api/countries/add', methods=['POST'])
@login_required
def api_countries_add():
    data   = request.get_json(force=True)
    key    = data.get('key', '').strip().lower()
    locale = data.get('locale', '').strip()
    code   = data.get('code', '').strip()
    is_bd  = bool(data.get('is_bd', False))
    # Accept either a list of patterns or a single string (backward compat)
    raw_patterns = data.get('patterns') or data.get('digits') or []
    if isinstance(raw_patterns, str):
        raw_patterns = [raw_patterns]
    patterns = [p.strip() for p in raw_patterns if str(p).strip()]
    if not key:
        return jsonify(success=False, error='Country key is required (e.g. "usa").')
    if not locale or not code or not patterns:
        return jsonify(success=False, error='locale, code এবং অন্তত একটি digit pattern দিতে হবে।')
    countries = CONFIG.get('countries', {})
    exists = key in countries
    # Store as list so generate_fake_phone can pick randomly
    countries[key] = {'locale': locale, 'code': code, 'digits': patterns, 'is_bd': is_bd}
    CONFIG['countries'] = countries
    save_config(CONFIG)
    verb = 'আপডেট' if exists else 'যোগ'
    return jsonify(success=True, total=len(countries),
                   message=f'✅ "{key}" {verb} করা হয়েছে!')

@app.route('/api/countries/delete', methods=['POST'])
@login_required
def api_countries_delete():
    data = request.get_json(force=True)
    key  = data.get('key', '').strip().lower()
    countries = CONFIG.get('countries', {})
    if key not in countries:
        return jsonify(success=False, error='Country not found.')
    if len(countries) <= 1:
        return jsonify(success=False, error='At least one country must remain.')
    del countries[key]
    CONFIG['countries'] = countries
    save_config(CONFIG)
    return jsonify(success=True, total=len(countries),
                   message=f'🗑️ "{key}" মুছে ফেলা হয়েছে।')

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Flask Routes — Bot Config API (admin_id, developer_name, nickname_sfx)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route('/api/bot-config', methods=['GET'])
@login_required
def api_bot_config_get():
    return jsonify(
        admin_id=CONFIG.get('admin_id', 0),
        developer_name=CONFIG.get('developer_name', ''),
        nickname_sfx=CONFIG.get('nickname_sfx', []),
    )

@app.route('/api/bot-config', methods=['POST'])
@login_required
def api_bot_config_set():
    data = request.get_json(force=True)
    if 'admin_id' in data:
        try:
            CONFIG['admin_id'] = int(data['admin_id']) if data['admin_id'] else 0
        except (TypeError, ValueError):
            return jsonify(success=False, error='admin_id must be a number.')
    if 'developer_name' in data:
        CONFIG['developer_name'] = str(data['developer_name']).strip()
    if 'nickname_sfx' in data:
        raw = data['nickname_sfx']
        if not isinstance(raw, list):
            return jsonify(success=False, error='nickname_sfx must be a list.')
        CONFIG['nickname_sfx'] = [str(s) for s in raw]
    save_config(CONFIG)
    return jsonify(success=True, message='✅ Bot config আপডেট হয়েছে!')

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Flask Routes — Fields config API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route('/api/fields-config', methods=['GET'])
@login_required
def api_fields_config_get():
    return jsonify(
        all_fields=[{"key": k, "label": FIELD_LABELS[k], "emoji": FIELD_EMOJI[k]} for k in FIELD_KEYS],
        enabled=CONFIG.get("bot_reply_fields", FIELD_KEYS),
    )

@app.route('/api/fields-config', methods=['POST'])
@login_required
def api_fields_config_set():
    data = request.get_json(force=True)
    enabled = data.get("enabled", [])
    if not isinstance(enabled, list):
        return jsonify(success=False, error="enabled must be a list.")
    valid = [k for k in enabled if k in FIELD_KEYS]
    if not valid:
        return jsonify(success=False, error="অন্তত একটি ফিল্ড চালু রাখতে হবে।")
    CONFIG["bot_reply_fields"] = valid
    save_config(CONFIG)
    return jsonify(success=True, enabled=valid, message="✅ ফিল্ড সেটিং সেভ হয়েছে!")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Flask Routes — AI Prompt Customizer API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route('/api/ai-prompts', methods=['GET'])
@login_required
def api_ai_prompts_get():
    return jsonify(
        bd=CONFIG.get('ai_prompt_bd', DEFAULT_AI_PROMPT_BD),
        intl=CONFIG.get('ai_prompt_intl', DEFAULT_AI_PROMPT_INTL),
        default_bd=DEFAULT_AI_PROMPT_BD,
        default_intl=DEFAULT_AI_PROMPT_INTL,
    )

@app.route('/api/ai-prompts', methods=['POST'])
@login_required
def api_ai_prompts_set():
    data = request.get_json(force=True)
    changed = False
    if 'bd' in data:
        val = str(data['bd']).strip()
        if not val:
            return jsonify(success=False, error='BD prompt খালি রাখা যাবে না।')
        CONFIG['ai_prompt_bd'] = val
        changed = True
    if 'intl' in data:
        val = str(data['intl']).strip()
        if not val:
            return jsonify(success=False, error='International prompt খালি রাখা যাবে না।')
        CONFIG['ai_prompt_intl'] = val
        changed = True
    if changed:
        save_config(CONFIG)
    return jsonify(success=True, message='✅ AI Prompt সেভ হয়েছে!')

@app.route('/api/bot-texts', methods=['GET'])
@login_required
def api_bot_texts_get():
    current = CONFIG.get("bot_texts", {})
    merged  = {k: current.get(k, v) for k, v in DEFAULT_BOT_TEXTS.items()}
    return jsonify(texts=merged, defaults={k: v for k, v in DEFAULT_BOT_TEXTS.items()})

@app.route('/api/bot-texts', methods=['POST'])
@login_required
def api_bot_texts_set():
    data        = request.get_json(force=True)
    texts_input = data.get("texts", {})
    if not isinstance(texts_input, dict):
        return jsonify(success=False, error="texts must be a dict.")
    bot_texts = CONFIG.get("bot_texts", {k: v for k, v in DEFAULT_BOT_TEXTS.items()})
    updated = 0
    for key, val in texts_input.items():
        if key not in DEFAULT_BOT_TEXTS:
            continue
        val = str(val).strip()
        if not val:
            return jsonify(success=False, error=f'"{key}" খালি রাখা যাবে না।')
        bot_texts[key] = val
        updated += 1
    CONFIG["bot_texts"] = bot_texts
    save_config(CONFIG)
    return jsonify(success=True, message=f'✅ {updated} টি text সেভ হয়েছে!')

@app.route('/api/bot-texts/reset', methods=['POST'])
@login_required
def api_bot_texts_reset():
    data = request.get_json(force=True)
    key  = data.get("key", "all")
    bot_texts = CONFIG.get("bot_texts", {k: v for k, v in DEFAULT_BOT_TEXTS.items()})
    if key == "all":
        CONFIG["bot_texts"] = {k: v for k, v in DEFAULT_BOT_TEXTS.items()}
        save_config(CONFIG)
        return jsonify(success=True, texts=CONFIG["bot_texts"], message="✅ সব text ডিফল্টে ফিরে গেছে!")
    if key not in DEFAULT_BOT_TEXTS:
        return jsonify(success=False, error="Unknown text key.")
    bot_texts[key] = DEFAULT_BOT_TEXTS[key]
    CONFIG["bot_texts"] = bot_texts
    save_config(CONFIG)
    return jsonify(success=True, default_value=DEFAULT_BOT_TEXTS[key],
                   message=f'✅ "{key}" ডিফল্টে ফিরে গেছে!')

@app.route('/api/ai-prompts/reset', methods=['POST'])
@login_required
def api_ai_prompts_reset():
    data = request.get_json(force=True)
    kind = data.get('kind', 'both')
    if kind in ('bd', 'both'):
        CONFIG['ai_prompt_bd'] = DEFAULT_AI_PROMPT_BD
    if kind in ('intl', 'both'):
        CONFIG['ai_prompt_intl'] = DEFAULT_AI_PROMPT_INTL
    save_config(CONFIG)
    label = {'bd': 'BD', 'intl': 'International', 'both': 'সব'}.get(kind, 'সব')
    return jsonify(success=True,
                   bd=CONFIG['ai_prompt_bd'],
                   intl=CONFIG['ai_prompt_intl'],
                   message=f'✅ {label} prompt ডিফল্টে ফিরে গেছে!')

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Flask Routes — User stats API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route('/api/user-stats', methods=['GET'])
@login_required
def api_user_stats():
    q = request.args.get('q', '').strip().lower()
    with users_lock:
        data = [
            {"user_id": uid, **info}
            for uid, info in KNOWN_USERS.items()
        ]
    data.sort(key=lambda x: x.get("profile_count", 0), reverse=True)
    if q:
        data = [
            e for e in data
            if q in str(e["user_id"])
            or q in (e.get("username") or "").lower()
            or q in (e.get("first_name") or "").lower()
        ]
    return jsonify(entries=data, total=len(KNOWN_USERS))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Flask Routes — Broadcast API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route('/api/broadcast', methods=['POST'])
@login_required
def api_broadcast():
    if not bot or not bot_status.get("running"):
        return jsonify(success=False, error="বট চালু নেই — আগে বট কানেক্ট করুন।")
    data = request.get_json(force=True)
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify(success=False, error="মেসেজ খালি রাখা যাবে না।")
    with users_lock:
        user_ids = list(KNOWN_USERS.keys())
    if not user_ids:
        return jsonify(success=False, error="কোনো ইউজার রেজিস্টার্ড নেই — বটে কেউ message পাঠালে ট্র্যাক হবে।")
    # Atomically check-and-mark running under lock, then snapshot bot reference
    with broadcast_lock:
        if broadcast_status.get("running"):
            return jsonify(success=False, error="একটি broadcast চলছে, শেষ হওয়া পর্যন্ত অপেক্ষা করুন।")
        broadcast_status.update({"running": True, "total": len(user_ids), "sent": 0, "failed": 0, "done": False})
        bot_snapshot = bot  # capture current bot so mid-flight swaps don't affect this run
    t = Thread(target=_do_broadcast, args=(text, user_ids, bot_snapshot), daemon=True)
    t.start()
    return jsonify(success=True, total=len(user_ids),
                   message=f"✅ {len(user_ids)} জনকে broadcast শুরু হয়েছে।")

@app.route('/api/broadcast-status', methods=['GET'])
@login_required
def api_broadcast_status():
    with broadcast_lock:
        return jsonify(**broadcast_status)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Scheduled Messages
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
schedules_lock = Lock()

def load_schedules() -> list:
    db_data = db_get("schedules")
    return db_data if isinstance(db_data, list) else []

def save_schedules(data: list):
    db_set("schedules", data)

SCHEDULES: list = load_schedules()

WEEKDAY_NAMES = ["সোমবার","মঙ্গলবার","বুধবার","বৃহস্পতিবার","শুক্রবার","শনিবার","রবিবার"]

def _compute_next_run(time_str: str, repeat: str, weekday: int = 0) -> str:
    """Return ISO timestamp of next run for this schedule."""
    now = _dt.now()
    h, m = map(int, time_str.split(':'))
    base = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if repeat in ('once', 'daily'):
        candidate = base if base > now else base + timedelta(days=1)
    elif repeat == 'weekly':
        days_ahead = (int(weekday) - now.weekday()) % 7
        candidate = base + timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(weeks=1)
    else:
        candidate = base if base > now else base + timedelta(days=1)
    return candidate.strftime('%Y-%m-%dT%H:%M:%S')

def _send_scheduled(sched: dict) -> bool:
    """Send a scheduled text message to all known users.
    Returns True if the send was attempted (bot running), False if skipped."""
    b = bot
    if not b or not bot_status.get('running'):
        return False          # bot down — do NOT consume the schedule
    text = sched.get('text', '').strip()
    if not text:
        return True           # nothing to send but schedule is valid
    with users_lock:
        user_ids = list(KNOWN_USERS.keys())
    for uid in user_ids:
        try:
            b.send_message(uid, text)
        except Exception:
            pass
        _time_module.sleep(0.05)
    return True

def _run_due_schedules():
    global SCHEDULES
    now_str = _dt.now().strftime('%Y-%m-%dT%H:%M:%S')

    # 1. Collect due schedules under lock, then release before sending
    with schedules_lock:
        due = [s for s in SCHEDULES
               if s.get('active', True)
               and s.get('next_run', '') <= now_str
               and s.get('next_run', '')]

    if not due:
        return

    # 2. Send outside the lock so API calls don't block schedule CRUD
    executed_ids = set()
    for sched in due:
        try:
            sent = _send_scheduled(sched)
        except Exception as e:
            print(f"[Scheduler] send error: {e}")
            sent = False
        if sent:
            executed_ids.add(sched['id'])

    if not executed_ids:
        return   # bot was down for all — don't advance any state

    # 3. Re-acquire lock to persist updated next_run / active
    with schedules_lock:
        changed = False
        for sched in SCHEDULES:
            if sched['id'] not in executed_ids:
                continue
            repeat = sched.get('repeat', 'once')
            if repeat == 'once':
                sched['active'] = False
            elif repeat == 'daily':
                sched['next_run'] = _compute_next_run(sched['time'], 'daily')
            elif repeat == 'weekly':
                sched['next_run'] = _compute_next_run(sched['time'], 'weekly', sched.get('weekday', 0))
            changed = True
        if changed:
            save_schedules(SCHEDULES)

def _scheduler_loop():
    """Background daemon: checks every 30 s for due schedules."""
    while True:
        _time_module.sleep(30)
        try:
            _run_due_schedules()
        except Exception as e:
            print(f"[Scheduler] loop error: {e}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Flask Routes — Schedules API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route('/api/schedules', methods=['GET'])
@login_required
def api_schedules_get():
    with schedules_lock:
        return jsonify(schedules=list(SCHEDULES))

@app.route('/api/schedules', methods=['POST'])
@login_required
def api_schedules_add():
    data      = request.get_json(force=True)
    text      = (data.get('text') or '').strip()
    time_str  = (data.get('time') or '').strip()
    repeat    = data.get('repeat', 'daily')
    weekday   = int(data.get('weekday', 0) or 0)
    if not text:
        return jsonify(success=False, error='মেসেজ খালি রাখা যাবে না।')
    if not time_str or ':' not in time_str:
        return jsonify(success=False, error='সময় সঠিকভাবে দিন (HH:MM)।')
    if repeat not in ('once', 'daily', 'weekly'):
        return jsonify(success=False, error='Invalid repeat type.')
    next_run = _compute_next_run(time_str, repeat, weekday)
    sched = {
        'id':         secrets_module.token_hex(8),
        'text':       text,
        'time':       time_str,
        'repeat':     repeat,
        'weekday':    weekday,
        'next_run':   next_run,
        'active':     True,
        'created_at': _dt.now().strftime('%Y-%m-%dT%H:%M:%S'),
    }
    with schedules_lock:
        SCHEDULES.append(sched)
        save_schedules(SCHEDULES)
    return jsonify(success=True, schedule=sched,
                   message=f'✅ Schedule যোগ হয়েছে! পরবর্তী রান: {next_run}')

@app.route('/api/schedules/delete', methods=['POST'])
@login_required
def api_schedules_delete():
    data = request.get_json(force=True)
    sid  = data.get('id', '')
    with schedules_lock:
        idx = next((i for i, s in enumerate(SCHEDULES) if s['id'] == sid), None)
        if idx is None:
            return jsonify(success=False, error='Schedule পাওয়া যায়নি।')
        SCHEDULES.pop(idx)
        save_schedules(SCHEDULES)
    return jsonify(success=True, message='🗑️ Schedule মুছে ফেলা হয়েছে।')

@app.route('/api/schedules/toggle', methods=['POST'])
@login_required
def api_schedules_toggle():
    data = request.get_json(force=True)
    sid  = data.get('id', '')
    with schedules_lock:
        sched = next((s for s in SCHEDULES if s['id'] == sid), None)
        if not sched:
            return jsonify(success=False, error='Schedule পাওয়া যায়নি।')
        sched['active'] = not sched.get('active', True)
        if sched['active']:
            sched['next_run'] = _compute_next_run(
                sched['time'], sched['repeat'], sched.get('weekday', 0))
        save_schedules(SCHEDULES)
    return jsonify(success=True, active=sched['active'],
                   message=f"✅ {'চালু' if sched['active'] else 'বন্ধ'} করা হয়েছে।")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Flask Routes — Admin File Library
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route('/api/files', methods=['GET'])
@login_required
def api_files():
    with stored_files_lock:
        entries = [
            {
                "id": item.get("id", ""),
                "title": item.get("title", ""),
                "command": item.get("command", ""),
                "filename": item.get("filename", ""),
                "caption": item.get("caption", ""),
                "category": item.get("category", "General"),
                "version": item.get("version", ""),
                "access_mode": item.get("access_mode", "all"),
                "allowed_users": item.get("allowed_users", []),
                "expires_at": item.get("expires_at", ""),
                "downloads": int(item.get("downloads", 0) or 0),
                "last_downloads": item.get("last_downloads", [])[-5:],
                "size": item.get("size", 0),
                "created_at": item.get("created_at", ""),
                "storage": item.get("storage", "disk"),
                "source_chat_id": item.get("source_chat_id", ""),
                "source_message_id": item.get("source_message_id", ""),
            }
            for item in STORED_FILES
        ]
    return jsonify(success=True, files=entries)

@app.route('/api/files/upload', methods=['POST'])
@login_required
def api_files_upload():
    title = request.form.get("title", "").strip()
    command = request.form.get("command", "").strip().lstrip("/").lower()
    caption = request.form.get("caption", "").strip()
    category = request.form.get("category", "").strip()[:40] or "General"
    version = request.form.get("version", "").strip()[:30]
    access_mode = request.form.get("access_mode", "all").strip().lower()
    allowed_raw = request.form.get("allowed_users", "").strip()
    expires_at = request.form.get("expires_at", "").strip()
    replace = request.form.get("replace", "").strip().lower() in {"1", "true", "yes", "on"}
    file_obj = request.files.get("file")

    if not title:
        return jsonify(success=False, error="ফাইলের একটি নাম দিন।")
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", command):
        return jsonify(
            success=False,
            error="কমান্ড 2–32 অক্ষরের হতে হবে; শুধু ইংরেজি ছোট হাতের অক্ষর, সংখ্যা ও _ ব্যবহার করুন।",
        )
    if access_mode not in {"all", "selected"}:
        return jsonify(success=False, error="Access mode সঠিক নয়।")
    allowed_users = []
    if access_mode == "selected":
        try:
            allowed_users = sorted({int(value.strip()) for value in allowed_raw.split(",") if value.strip()})
        except ValueError:
            return jsonify(success=False, error="Allowed User ID কমা দিয়ে সঠিকভাবে দিন।")
        if not allowed_users:
            return jsonify(success=False, error="Selected users access-এর জন্য অন্তত একটি User ID দিন।")
    if expires_at:
        try:
            if _dt.fromisoformat(expires_at) <= _dt.now():
                return jsonify(success=False, error="Expiry সময় ভবিষ্যতের হতে হবে।")
        except ValueError:
            return jsonify(success=False, error="Expiry সময় সঠিক নয়।")
    if not file_obj or not file_obj.filename:
        return jsonify(success=False, error="ফাইল সিলেক্ট করুন।")

    filename = os.path.basename(file_obj.filename).strip() or "download"
    raw = file_obj.read()
    if not raw:
        return jsonify(success=False, error="খালি ফাইল আপলোড করা যাবে না।")
    if len(raw) > FILE_MAX_BYTES:
        return jsonify(success=False, error="ফাইলের সর্বোচ্চ সাইজ 50 MB।")

    with stored_files_lock:
        existing_index = next(
            (i for i, item in enumerate(STORED_FILES) if item.get("command") == command),
            None,
        )
        if existing_index is not None and not replace:
            return jsonify(success=False, error=f"/download {command} ইতিমধ্যে ব্যবহার করা হয়েছে।")
        os.makedirs(FILE_STORAGE_DIR, exist_ok=True)
        stored_name = f"{secrets_module.token_hex(16)}_{filename}"
        file_path = os.path.join(FILE_STORAGE_DIR, stored_name)
        try:
            with open(file_path, "wb") as fh:
                fh.write(raw)
        except OSError as exc:
            print(f"[FileUpload] save error: {exc}")
            return jsonify(success=False, error="ফাইল সংরক্ষণ করা যায়নি।")

        existing = STORED_FILES[existing_index] if existing_index is not None else {}
        item = {
            "id": existing.get("id") or secrets_module.token_hex(8),
            "title": title,
            "command": command,
            "caption": caption,
            "category": category,
            "version": version,
            "access_mode": access_mode,
            "allowed_users": allowed_users,
            "expires_at": expires_at,
            "filename": filename,
            "path": file_path,
            "size": len(raw),
            "created_at": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
            "downloads": int(existing.get("downloads", 0) or 0),
            "last_downloads": existing.get("last_downloads", []),
        }
        item["storage"] = "database" if db_set_blob(item["id"], raw) else "disk"
        if existing_index is None:
            STORED_FILES.append(item)
        else:
            STORED_FILES[existing_index] = item
        try:
            save_stored_files(STORED_FILES)
        except OSError as exc:
            if existing_index is None:
                STORED_FILES.pop()
            else:
                STORED_FILES[existing_index] = existing
            try:
                os.remove(file_path)
            except OSError:
                pass
            print(f"[FileUpload] index save error: {exc}")
            return jsonify(success=False, error="ফাইলের তথ্য সংরক্ষণ করা যায়নি।")
        if existing_index is not None and existing.get("path") and existing.get("path") != file_path:
            try:
                os.remove(existing["path"])
            except OSError:
                pass

    action = "আপডেট" if existing_index is not None else "সেভ"
    return jsonify(success=True, message=f"✅ ফাইল {action} হয়েছে। ইউজার ব্যবহার করবে: /download {command}")

@app.route('/api/files/channel', methods=['POST'])
@login_required
def api_files_channel():
    """Register a file that will be copied from a private Telegram channel."""
    title = request.form.get("title", "").strip()
    command = request.form.get("command", "").strip().lstrip("/").lower()
    caption = request.form.get("caption", "").strip()
    category = request.form.get("category", "").strip()[:40] or "General"
    version = request.form.get("version", "").strip()[:30]
    access_mode = request.form.get("access_mode", "all").strip().lower()
    allowed_raw = request.form.get("allowed_users", "").strip()
    expires_at = request.form.get("expires_at", "").strip()
    replace = request.form.get("replace", "").strip().lower() in {"1", "true", "yes", "on"}
    source_chat_id = request.form.get("source_chat_id", "").strip()
    source_message_id = request.form.get("source_message_id", "").strip()

    if not title:
        return jsonify(success=False, error="ফাইলের একটি নাম দিন।")
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", command):
        return jsonify(
            success=False,
            error="কমান্ড 2–32 অক্ষরের হতে হবে; শুধু ইংরেজি ছোট হাতের অক্ষর, সংখ্যা ও _ ব্যবহার করুন।",
        )
    if not source_chat_id:
        return jsonify(success=False, error="Private channel ID দিন, যেমন -1001234567890।")
    if not (
        re.fullmatch(r"-?\d+", source_chat_id)
        or re.fullmatch(r"@[A-Za-z0-9_]{5,}", source_chat_id)
    ):
        return jsonify(success=False, error="Channel ID -100... অথবা @channelusername আকারে দিন।")
    try:
        source_message_id = int(source_message_id)
        if source_message_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify(success=False, error="সঠিক private channel message ID দিন।")
    if access_mode not in {"all", "selected"}:
        return jsonify(success=False, error="Access mode সঠিক নয়।")
    allowed_users = []
    if access_mode == "selected":
        try:
            allowed_users = sorted({int(value.strip()) for value in allowed_raw.split(",") if value.strip()})
        except ValueError:
            return jsonify(success=False, error="Allowed User ID কমা দিয়ে সঠিকভাবে দিন।")
        if not allowed_users:
            return jsonify(success=False, error="Selected users access-এর জন্য অন্তত একটি User ID দিন।")
    if expires_at:
        try:
            if _dt.fromisoformat(expires_at) <= _dt.now():
                return jsonify(success=False, error="Expiry সময় ভবিষ্যতের হতে হবে।")
        except ValueError:
            return jsonify(success=False, error="Expiry সময় সঠিক নয়।")

    with stored_files_lock:
        existing_index = next(
            (i for i, item in enumerate(STORED_FILES) if item.get("command") == command),
            None,
        )
        if existing_index is not None and not replace:
            return jsonify(success=False, error=f"/download {command} ইতিমধ্যে ব্যবহার করা হয়েছে।")

        existing = STORED_FILES[existing_index] if existing_index is not None else {}
        item = {
            "id": existing.get("id") or secrets_module.token_hex(8),
            "title": title,
            "command": command,
            "caption": caption,
            "category": category,
            "version": version,
            "access_mode": access_mode,
            "allowed_users": allowed_users,
            "expires_at": expires_at,
            "filename": f"channel-message-{source_message_id}",
            "path": "",
            "size": 0,
            "created_at": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
            "downloads": int(existing.get("downloads", 0) or 0),
            "last_downloads": existing.get("last_downloads", []),
            "storage": "telegram_channel",
            "source_chat_id": source_chat_id,
            "source_message_id": source_message_id,
        }
        if existing_index is None:
            STORED_FILES.append(item)
        else:
            STORED_FILES[existing_index] = item
        try:
            save_stored_files(STORED_FILES)
        except OSError as exc:
            if existing_index is None:
                STORED_FILES.pop()
            else:
                STORED_FILES[existing_index] = existing
            print(f"[ChannelFile] index save error: {exc}")
            return jsonify(success=False, error="Private channel file-এর তথ্য সংরক্ষণ করা যায়নি।")

        if existing_index is not None:
            if existing.get("storage") == "database":
                db_delete_blob(existing.get("id", ""))
            old_path = existing.get("path", "")
            if old_path and old_path != item["path"]:
                try:
                    os.remove(old_path)
                except OSError:
                    pass

    action = "আপডেট" if existing_index is not None else "সেভ"
    return jsonify(
        success=True,
        message=f"✅ Private channel file {action} হয়েছে। ইউজার ব্যবহার করবে: /download {command}",
    )

@app.route('/api/files/delete', methods=['POST'])
@login_required
def api_files_delete():
    data = request.get_json(force=True) or {}
    file_id = str(data.get("id", "")).strip()
    with stored_files_lock:
        index = next((i for i, item in enumerate(STORED_FILES) if item.get("id") == file_id), None)
        if index is None:
            return jsonify(success=False, error="ফাইল পাওয়া যায়নি।")
        item = STORED_FILES.pop(index)
        try:
            save_stored_files(STORED_FILES)
        except OSError as exc:
            STORED_FILES.insert(index, item)
            print(f"[FileDelete] index save error: {exc}")
            return jsonify(success=False, error="ফাইল মুছে ফেলা যায়নি।")
        try:
            os.remove(item.get("path", ""))
        except OSError:
            pass
        db_delete_blob(item.get("id", ""))
    return jsonify(success=True, message="🗑️ ফাইল মুছে ফেলা হয়েছে।")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Flask Routes — Send Media to User(s)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route('/api/send-media', methods=['POST'])
@login_required
def api_send_media():
    if not bot or not bot_status.get('running'):
        return jsonify(success=False, error='বট চালু নেই — আগে বট কানেক্ট করুন।')
    media_type   = request.form.get('type', 'text')
    target       = request.form.get('target', 'user')
    user_id_str  = request.form.get('user_id', '').strip()
    caption      = request.form.get('caption', '').strip() or None
    text_msg     = request.form.get('text', '').strip()
    file_obj     = request.files.get('file')

    if target == 'all':
        with users_lock:
            user_ids = list(KNOWN_USERS.keys())
        if not user_ids:
            return jsonify(success=False, error='কোনো ইউজার রেজিস্টার্ড নেই।')
    else:
        try:
            user_ids = [int(user_id_str)]
        except (ValueError, TypeError):
            return jsonify(success=False, error='Valid User ID দিন।')

    ALLOWED_TYPES = {'text', 'photo', 'video', 'audio', 'document'}
    if media_type not in ALLOWED_TYPES:
        return jsonify(success=False, error=f'অবৈধ মিডিয়া টাইপ: {media_type}')
    if media_type != 'text' and not file_obj:
        return jsonify(success=False, error='ফাইল সিলেক্ট করুন।')
    if media_type == 'text' and not text_msg:
        return jsonify(success=False, error='মেসেজ লিখুন।')

    # Read file into BytesIO so pyTelegramBotAPI can seek+reuse it
    # and gets a proper .name attribute for multipart upload
    file_buf = None
    if file_obj and media_type != 'text':
        raw = file_obj.read()
        file_buf = io.BytesIO(raw)
        file_buf.name = file_obj.filename or f'file.{media_type}'

    b = bot
    sent = failed = 0
    for uid in user_ids:
        try:
            if media_type == 'text':
                b.send_message(uid, text_msg)
            elif media_type == 'photo':
                file_buf.seek(0); b.send_photo(uid, file_buf, caption=caption)
            elif media_type == 'video':
                file_buf.seek(0); b.send_video(uid, file_buf, caption=caption)
            elif media_type == 'audio':
                file_buf.seek(0); b.send_audio(uid, file_buf, caption=caption)
            elif media_type == 'document':
                file_buf.seek(0); b.send_document(uid, file_buf, caption=caption)
            sent += 1
        except Exception as e:
            failed += 1
            print(f"[SendMedia] uid={uid} type={media_type} error: {e}")
        if target == 'all':
            _time_module.sleep(0.05)

    msg = f'✅ {sent} জনকে পাঠানো হয়েছে।'
    if failed:
        msg += f' ❌ {failed} টি ব্যর্থ।'
    return jsonify(success=True, sent=sent, failed=failed, message=msg)

@app.route('/health')
def health():
    return "Profile Generator Bot is alive 24/7!"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Entry Point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_web_server():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)

if __name__ == '__main__':
    # Start background scheduler
    _sched_thread = Thread(target=_scheduler_loop, daemon=True)
    _sched_thread.start()

    # Auto-start bot if token saved in config
    saved_token = CONFIG.get("bot_token", "").strip()
    if not saved_token:
        saved_token = os.environ.get("BOT_TOKEN", "").strip()
    if saved_token:
        ok, err = launch_bot(saved_token)
        if ok:
            print(f"🚀 Bot started | মোট সম্ভাব্য BD নাম: {get_total_combinations():,}")
        else:
            print(f"⚠️  Bot failed to start: {err}")
    else:
        print("ℹ️  No BOT_TOKEN — dashboard-only mode. Set token from the dashboard.")

    run_web_server()
