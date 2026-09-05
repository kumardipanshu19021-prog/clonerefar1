import asyncio
import base64
import csv
import html
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import time
import threading
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import telegram
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, RetryAfter, Forbidden, BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:
    AESGCM = None

# =========================================================
# Configuration
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_RAW = os.getenv("OWNER_ID", os.getenv("ADMIN_ID", "0")).strip()
try:
    OWNER_ID = int(OWNER_RAW)
except ValueError:
    OWNER_ID = 0

DB_PATH = os.getenv("DB_PATH", "bot2.db").strip() or "bot2.db"
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "backups")); BACKUP_DIR.mkdir(parents=True, exist_ok=True)
SECRET_ENCRYPTION_KEY = os.getenv("SECRET_ENCRYPTION_KEY", "").strip()
BOT_VERSION = "4.0.0-multibot"
DB_VERSION = 10
STARTED_AT = time.monotonic()
LAST_ERROR = ""
ERROR_LOG_MAX = 300
PREMIUM_EMOJI_ENABLED = True
AUTO_BACKUP_TASK = None
MASTER_BOT_REF = None

BTN1, BTN2, BTN3 = "🎯 Claim Agent", "📊 Statistics", "🤝 Refer & Earn"
EMOJI = {
    "🎯": "5228855127892327218", "📊": "6093382540784046658", "🤝": "6086990448331592466",
    "📣": "6095891759462617671", "💬": "6095865895169560113", "📝": "6010292709066019210",
    "🖼️": "5341285075210224047", "➕": "6093406373557571574", "❌": "6010471186432005118",
    "⚙️": "6010355840790303830", "✅": "6246537187614005254", "🌟": "5783170625090622777",
    "📌": "6089019283508040459", "🔔": "6093852083788715042", "👑": "6247039939305808563",
    "💰": "5785325680765965100",
}

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("multibot_force_join")

# Legacy states + new master states.
(
    S_CH_ID, S_CH_NAME, S_CH_LINK, S_WELCOME, S_WELCOME_PHOTO, S_POSTJOIN, S_TOP,
    S_BTN1, S_BTN2, S_BTN3, S_BCAST, S_RESTORE, S_SEARCH, S_USERMSG, S_EDITNAME,
    S_EDITLINK, S_EMOJI,
    S_CREATE_CONFIRM, S_REJECT_REASON, S_CREATE_TOKEN, S_CREATE_ADMIN_ID,
    S_MASTER_BCAST_CONTENT, S_MASTER_BCAST_BUTTON_NAME, S_MASTER_BCAST_BUTTON_URL,
    S_MASTER_BCAST_BUTTON_ROW, S_MASTER_GLOBAL_CONTENT,
    S_CHILD_ADMIN_ID, S_CHILD_CHANNEL_ID, S_CHILD_CHANNEL_NAME, S_CHILD_CHANNEL_LINK,
    S_CHILD_MSG, S_CHILD_EMOJI, S_CHILD_SEARCH, S_CHILD_USERMSG,
) = range(34)

# =========================================================
# Utility / secret handling
# =========================================================

def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def esc(value):
    return html.escape(str(value or ""))


def sanitize_error(message, secret=None):
    text = str(message or "")
    if secret:
        text = text.replace(secret, "<redacted-token>")
    # Bot tokens normally look like 123456:ABC..., mask defensively.
    text = re.sub(r"\b\d{5,12}:[A-Za-z0-9_-]{20,}\b", "<redacted-token>", text)
    return text[:2000]


def get_crypto():
    if AESGCM is None:
        raise RuntimeError("cryptography package is required for child bot token encryption.")
    if not SECRET_ENCRYPTION_KEY:
        raise RuntimeError("SECRET_ENCRYPTION_KEY is required before registering child bots.")
    raw = SECRET_ENCRYPTION_KEY.encode()
    try:
        key = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
    except Exception as e:
        raise RuntimeError("SECRET_ENCRYPTION_KEY must be URL-safe base64 encoded 32-byte key.") from e
    if len(key) != 32:
        raise RuntimeError("SECRET_ENCRYPTION_KEY must decode to exactly 32 bytes.")
    return AESGCM(key)


def encrypt_secret(secret):
    aes = get_crypto()
    nonce = secrets.token_bytes(12)
    data = aes.encrypt(nonce, secret.encode(), None)
    return base64.urlsafe_b64encode(nonce + data).decode()


def decrypt_secret(blob):
    aes = get_crypto()
    raw = base64.urlsafe_b64decode(blob.encode())
    return aes.decrypt(raw[:12], raw[12:], None).decode()

# =========================================================
# Database
# =========================================================

@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def scalar(sql, args=(), default=0):
    try:
        with db() as con:
            row = con.execute(sql, args).fetchone()
            return row[0] if row else default
    except Exception as e:
        logger.error("DB scalar: %s", sanitize_error(e))
        return default


def gset(key, default=""):
    try:
        with db() as con:
            row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else default
    except Exception as e:
        logger.error("gset: %s", sanitize_error(e))
        return default


def sset(key, value):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))


def log_error(level, message):
    global LAST_ERROR
    safe = sanitize_error(message)
    LAST_ERROR = safe
    try:
        with db() as con:
            con.execute(
                "INSERT INTO error_logs(created_at,level,message) VALUES(?,?,?)",
                (now_iso(), level, safe),
            )
            con.execute(
                "DELETE FROM error_logs WHERE id NOT IN "
                "(SELECT id FROM error_logs ORDER BY id DESC LIMIT ?)",
                (ERROR_LOG_MAX,),
            )
    except Exception:
        pass


class DBHandler(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.ERROR:
            log_error(record.levelname, record.getMessage())


logger.addHandler(DBHandler())


def init_db():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                first_name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                joined_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE IF NOT EXISTS channels(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT UNIQUE NOT NULL,
                channel_name TEXT NOT NULL,
                channel_link TEXT NOT NULL,
                position INTEGER DEFAULT 0,
                order_num INTEGER DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS join_requests(
                user_id INTEGER NOT NULL, channel_id TEXT NOT NULL,
                requested_at TEXT, status TEXT DEFAULT 'active',
                PRIMARY KEY(user_id,channel_id)
            );
            CREATE TABLE IF NOT EXISTS broadcast_msgs(
                bcast_id TEXT NOT NULL,user_id INTEGER NOT NULL,message_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS broadcasts(
                bcast_id TEXT PRIMARY KEY,created_at TEXT,source_chat_id INTEGER,
                source_message_id INTEGER,kind TEXT,total INTEGER DEFAULT 0,sent INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0,cancelled INTEGER DEFAULT 0,status TEXT DEFAULT 'running',
                last_error TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS error_logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT,level TEXT,message TEXT
            );

            CREATE TABLE IF NOT EXISTS child_bots(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER UNIQUE NOT NULL,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                display_name TEXT DEFAULT '',
                encrypted_token TEXT NOT NULL,
                owner_user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'CONFIGURING',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_started_at TEXT DEFAULT '',
                last_stopped_at TEXT DEFAULT '',
                last_error TEXT DEFAULT '',
                last_heartbeat TEXT DEFAULT '',
                bot_api_meta TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS child_bot_admins(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                child_bot_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(child_bot_id,user_id),
                FOREIGN KEY(child_bot_id) REFERENCES child_bots(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS clone_requests(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT UNIQUE NOT NULL,
                requester_user_id INTEGER NOT NULL,
                requester_name TEXT DEFAULT '',
                requester_username TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                reason TEXT DEFAULT '',
                reviewed_by INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                approved_at TEXT DEFAULT '',
                rejected_at TEXT DEFAULT '',
                child_bot_id INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS child_bot_users(
                child_bot_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                first_name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                joined_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                last_seen TEXT DEFAULT '',
                PRIMARY KEY(child_bot_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS child_bot_channels(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                child_bot_id INTEGER NOT NULL,
                channel_id TEXT NOT NULL,
                channel_name TEXT NOT NULL,
                channel_link TEXT NOT NULL,
                position INTEGER DEFAULT 0,
                order_num INTEGER DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                UNIQUE(child_bot_id,channel_id)
            );
            CREATE TABLE IF NOT EXISTS child_bot_settings(
                child_bot_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY(child_bot_id,key)
            );
            CREATE TABLE IF NOT EXISTS child_bot_buttons(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                child_bot_id INTEGER NOT NULL,
                button_key TEXT NOT NULL,
                text TEXT NOT NULL,
                callback_data TEXT DEFAULT '',
                url TEXT DEFAULT '',
                style TEXT DEFAULT 'primary',
                emoji_id TEXT DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                row_num INTEGER DEFAULT 0,
                position INTEGER DEFAULT 0,
                UNIQUE(child_bot_id,button_key)
            );
            CREATE TABLE IF NOT EXISTS child_bot_broadcasts(
                broadcast_id TEXT PRIMARY KEY,
                child_bot_id INTEGER NOT NULL,
                source_chat_id INTEGER DEFAULT 0,
                source_message_id INTEGER DEFAULT 0,
                kind TEXT NOT NULL,
                total INTEGER DEFAULT 0,
                sent INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0,
                cancelled INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running',
                created_at TEXT NOT NULL,
                completed_at TEXT DEFAULT '',
                last_error TEXT DEFAULT '',
                payload_json TEXT DEFAULT '',
                buttons_json TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS child_bot_broadcast_messages(
                broadcast_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY(broadcast_id,user_id,message_id)
            );
            CREATE TABLE IF NOT EXISTS child_bot_logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                child_bot_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL
            );
            """
        )
        # Legacy safe migrations.
        cols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
        if "username" not in cols: con.execute("ALTER TABLE users ADD COLUMN username TEXT DEFAULT ''")
        if "status" not in cols: con.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        cols = {r[1] for r in con.execute("PRAGMA table_info(channels)")}
        if "enabled" not in cols: con.execute("ALTER TABLE channels ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
        cols = {r[1] for r in con.execute("PRAGMA table_info(join_requests)")}
        if "requested_at" not in cols: con.execute("ALTER TABLE join_requests ADD COLUMN requested_at TEXT")
        if "status" not in cols: con.execute("ALTER TABLE join_requests ADD COLUMN status TEXT DEFAULT 'active'")
        con.execute("UPDATE join_requests SET requested_at=COALESCE(requested_at,?)", (now_iso(),))

        cols = {r[1] for r in con.execute("PRAGMA table_info(child_bots)")}
        if "bot_api_meta" not in cols: con.execute("ALTER TABLE child_bots ADD COLUMN bot_api_meta TEXT NOT NULL DEFAULT '{}'")
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", ("db_version", str(DB_VERSION)))
        cols = {r[1] for r in con.execute("PRAGMA table_info(child_bot_broadcasts)")}
        if "payload_json" not in cols: con.execute("ALTER TABLE child_bot_broadcasts ADD COLUMN payload_json TEXT DEFAULT ''")
        if "buttons_json" not in cols: con.execute("ALTER TABLE child_bot_broadcasts ADD COLUMN buttons_json TEXT DEFAULT ''")

        defaults = {
            "welcome": "👋 <b>Welcome!</b>\n\n🛑 Join all required channels below.\n\n💣 Then click <b>✅ Joined</b>",
            "welcome_photo": "",
            "postjoin": "🏛️ <b>Welcome!</b>\n\n📋 <b>Rules</b>\n• One agent per user\n• Permanent assignment",
            "top": "",
            "btn1_msg": "🎯 Agent claim coming soon!",
            "btn2_msg": "📊 Statistics coming soon!",
            "btn3_msg": "🤝 Refer & Earn coming soon!",
            "maintenance_mode": "0", "force_join_enabled": "1", "welcome_photo_enabled": "1",
            "broadcast_enabled": "1", "auto_backup_enabled": "0", "auto_backup_frequency": "daily",
            "auto_backup_keep": "7", "debug_logging": "0", "last_backup": "", "last_restore": "",
            "button_style_btn1": "primary", "button_style_btn2": "success", "button_style_btn3": "primary",
            "button_emoji_btn1": EMOJI["🎯"], "button_emoji_btn2": EMOJI["📊"], "button_emoji_btn3": EMOJI["🤝"],
            "button_emoji_enabled_btn1": "1", "button_emoji_enabled_btn2": "1", "button_emoji_enabled_btn3": "1",
        }
        for key, value in defaults.items():
            con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, str(value)))


def migration_info():
    return {"db_version": DB_VERSION, "bot_version": BOT_VERSION}

# =========================================================
# Legacy master bot data layer — preserved
# =========================================================

def is_owner(uid):
    return bool(OWNER_ID and uid == OWNER_ID)


def add_user(user):
    with db() as con:
        row = con.execute("SELECT 1 FROM users WHERE user_id=?", (user.id,)).fetchone()
        if row:
            con.execute("UPDATE users SET first_name=?,username=? WHERE user_id=?", (user.first_name or "", user.username or "", user.id))
            return False
        con.execute("INSERT INTO users VALUES(?,?,?,?,?)", (user.id, user.first_name or "", user.username or "", now_iso(), "active"))
        return True


def user_status(uid):
    return scalar("SELECT status FROM users WHERE user_id=?", (uid,), "active")


def set_status(uid, status):
    with db() as con:
        con.execute("UPDATE users SET status=? WHERE user_id=?", (status, uid))


def delete_user(uid):
    with db() as con:
        con.execute("DELETE FROM users WHERE user_id=?", (uid,))
        con.execute("DELETE FROM join_requests WHERE user_id=?", (uid,))


def users(include_blocked=False):
    q = "SELECT user_id FROM users" if include_blocked else "SELECT user_id FROM users WHERE status!='blocked'"
    with db() as con:
        return [r[0] for r in con.execute(q)]


def search_users(query):
    q = str(query).strip()
    with db() as con:
        if q.isdigit():
            return con.execute("SELECT user_id,first_name,username,joined_at,status FROM users WHERE user_id=?", (int(q),)).fetchall()
        x = f"%{q}%"
        return con.execute(
            "SELECT user_id,first_name,username,joined_at,status FROM users WHERE first_name LIKE ? OR username LIKE ? LIMIT 25",
            (x, x),
        ).fetchall()


def channels(all_rows=True):
    q = "SELECT id,channel_id,channel_name,channel_link,position,order_num,enabled FROM channels"
    q += " ORDER BY order_num,id" if all_rows else " WHERE enabled=1 ORDER BY order_num,id"
    with db() as con:
        return con.execute(q).fetchall()


def channel(cid):
    with db() as con:
        return con.execute(
            "SELECT id,channel_id,channel_name,channel_link,position,order_num,enabled FROM channels WHERE id=?",
            (cid,),
        ).fetchone()


def add_channel(cid, name, link):
    with db() as con:
        n = con.execute("SELECT COALESCE(MAX(order_num),0)+1 FROM channels").fetchone()[0]
        con.execute("INSERT INTO channels(channel_id,channel_name,channel_link,order_num,enabled) VALUES(?,?,?,?,1)", (str(cid), name, link, n))


def update_channel(cid, name=None, link=None):
    with db() as con:
        if name is not None: con.execute("UPDATE channels SET channel_name=? WHERE id=?", (name, cid))
        if link is not None: con.execute("UPDATE channels SET channel_link=? WHERE id=?", (link, cid))


def delete_channel(cid):
    with db() as con: con.execute("DELETE FROM channels WHERE id=?", (cid,))


def toggle_channel(cid):
    with db() as con: con.execute("UPDATE channels SET enabled=1-enabled WHERE id=?", (cid,))


def move_channel(cid, direction):
    rows = channels(True); ids = [r[0] for r in rows]
    if cid not in ids: return
    i = ids.index(cid); j = i - 1 if direction == "left" else i + 1
    if j < 0 or j >= len(ids): return
    with db() as con:
        con.execute("UPDATE channels SET order_num=? WHERE id=?", (rows[j][5], rows[i][0]))
        con.execute("UPDATE channels SET order_num=? WHERE id=?", (rows[i][5], rows[j][0]))


def record_req(uid, cid):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO join_requests VALUES(?,?,?,?)", (uid, str(cid), now_iso(), "active"))


def mark_req(uid, cid):
    with db() as con:
        con.execute("UPDATE join_requests SET status='joined' WHERE user_id=? AND channel_id=?", (uid, str(cid)))


def has_req(uid, cid):
    return bool(scalar("SELECT 1 FROM join_requests WHERE user_id=? AND channel_id=? AND status IN ('active','joined')", (uid, str(cid)), 0))


async def check_joined(bot, uid):
    if gset("force_join_enabled", "1") != "1": return True, set()
    rows = channels(False)
    if not rows: return True, set()
    joined = set()
    for row in rows:
        cid = row[1]
        try:
            m = await bot.get_chat_member(cid, uid)
            if m.status in ("member", "administrator", "creator", "restricted"):
                joined.add(cid); mark_req(uid, cid); continue
        except TelegramError as e:
            logger.warning("Join check %s/%s: %s", cid, uid, sanitize_error(e))
        if has_req(uid, cid): joined.add(cid)
    return len(joined) == len(rows), joined


async def channel_status(bot, cid):
    try:
        me = await bot.get_me(); ch = await bot.get_chat(cid); member = await bot.get_chat_member(cid, me.id)
        return True, member.status in ("administrator", "creator"), ch.title or str(cid), member.status
    except TelegramError as e:
        return False, False, sanitize_error(e), "error"

# =========================================================
# Buttons / keyboards
# =========================================================

def ib(text, callback_data=None, url=None, style=None, emoji_id=None):
    kw = {"text": str(text)}
    if not PREMIUM_EMOJI_ENABLED: emoji_id = None
    if callback_data is not None:
        cb = str(callback_data)
        if len(cb.encode("utf-8")) > 64:
            raise ValueError("callback_data exceeds Telegram 64-byte limit")
        kw["callback_data"] = cb
    if url is not None: kw["url"] = str(url)
    if style in ("primary", "success", "danger"): kw["style"] = style
    if emoji_id: kw["icon_custom_emoji_id"] = str(emoji_id)
    try:
        return InlineKeyboardButton(**kw)
    except (TypeError, ValueError):
        kw.pop("style", None); kw.pop("icon_custom_emoji_id", None)
        return InlineKeyboardButton(**kw)


def bstyle(k):
    x = gset("button_style_" + k, "primary")
    return x if x in ("primary", "success", "danger") else "primary"


def bemoji(k):
    return gset("button_emoji_" + k, "") if gset("button_emoji_enabled_" + k, "1") == "1" else None


def back_kb(cb="a_back"):
    return InlineKeyboardMarkup([[ib("🔙 Back", cb, style="primary", emoji_id=EMOJI["📌"])]] )


def cancel_kb(cb="create_cancel"):
    return InlineKeyboardMarkup([[ib("❌ Cancel", cb, style="danger", emoji_id=EMOJI["❌"])]] )


def confirm_cancel_keyboard(confirm_cb, cancel_cb="create_cancel"):
    return InlineKeyboardMarkup([[ib("✅ Confirm", confirm_cb, style="success", emoji_id=EMOJI["✅"]), ib("❌ Cancel", cancel_cb, style="danger", emoji_id=EMOJI["❌"])]] )


def join_kb(rows, joined):
    out = []
    for row in rows:
        if row[6] and row[1] not in joined:
            out.append([ib("📢 " + row[2], url=row[3], style="primary", emoji_id=EMOJI["📣"])])
    out.append([ib("✅ Joined", "check_joined", style="success", emoji_id=EMOJI["✅"])])
    return InlineKeyboardMarkup(out)


def main_kb():
    return InlineKeyboardMarkup([
        [ib(BTN1, "btn1", style=bstyle("btn1"), emoji_id=bemoji("btn1")), ib(BTN2, "btn2", style=bstyle("btn2"), emoji_id=bemoji("btn2"))],
        [ib(BTN3, "btn3", style=bstyle("btn3"), emoji_id=bemoji("btn3"))],
    ])

# =========================================================
# Legacy normal flow
# =========================================================

async def send_welcome(bot, chat, text, kb):
    try:
        photo = gset("welcome_photo")
        if photo and gset("welcome_photo_enabled", "1") == "1":
            await bot.send_photo(chat, photo, caption=text, reply_markup=kb, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(chat, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        logger.error("Welcome send failed: %s", sanitize_error(e))


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u: return
    add_user(u)
    if user_status(u.id) == "blocked": return await update.message.reply_text("🚫 You are blocked from using this bot.")
    if gset("maintenance_mode", "0") == "1" and not is_owner(u.id): return await update.message.reply_text("🛠 Bot is under maintenance.")
    rows = channels(False); ok, joined = await check_joined(ctx.bot, u.id)
    if not rows or ok:
        return await update.message.reply_text(gset("postjoin"), reply_markup=main_kb(), parse_mode=ParseMode.HTML)
    top = gset("top"); text = f"{top}\n\n{gset('welcome')}" if top else gset("welcome")
    await send_welcome(ctx.bot, u.id, text, join_kb(rows, joined))


async def cb_check(update, ctx):
    q = update.callback_query; uid = q.from_user.id
    if user_status(uid) == "blocked": return await q.answer("🚫 You are blocked.", show_alert=True)
    await q.answer(); rows = channels(False); ok, joined = await check_joined(ctx.bot, uid)
    if ok:
        try: await q.edit_message_text(gset("postjoin"), reply_markup=main_kb(), parse_mode=ParseMode.HTML)
        except TelegramError: await ctx.bot.send_message(q.message.chat_id, gset("postjoin"), reply_markup=main_kb(), parse_mode=ParseMode.HTML)
    else:
        await q.answer("🚫 Join every required channel, then press ✅ Joined again.", show_alert=True)
        try: await q.message.delete()
        except TelegramError: pass
        top = gset("top"); text = f"{top}\n\n{gset('welcome')}" if top else gset("welcome")
        await send_welcome(ctx.bot, q.message.chat_id, text, join_kb(rows, joined))


async def cb_btn(update, ctx):
    q = update.callback_query
    if user_status(q.from_user.id) == "blocked": return await q.answer("🚫 You are blocked.", show_alert=True)
    await q.answer(); key = {"btn1":"btn1_msg","btn2":"btn2_msg","btn3":"btn3_msg"}.get(q.data)
    if not key: return
    try: await q.edit_message_text(gset(key), reply_markup=back_kb("back_main"), parse_mode=ParseMode.HTML)
    except TelegramError: await ctx.bot.send_message(q.message.chat_id, gset(key), reply_markup=back_kb("back_main"), parse_mode=ParseMode.HTML)


async def cb_back(update, ctx):
    q = update.callback_query; await q.answer()
    try: await q.edit_message_text(gset("postjoin"), reply_markup=main_kb(), parse_mode=ParseMode.HTML)
    except TelegramError: await ctx.bot.send_message(q.message.chat_id, gset("postjoin"), reply_markup=main_kb(), parse_mode=ParseMode.HTML)


async def join_request(update, ctx):
    r = update.chat_join_request
    if r: record_req(r.from_user.id, r.chat.id)

# =========================================================
# Child storage
# =========================================================

DEFAULT_CHILD_SETTINGS = {
    "welcome": "👋 <b>Welcome!</b>\n\n🛑 Join all required channels below.\n\n💣 Then click <b>✅ Joined</b>",
    "welcome_photo": "",
    "postjoin": "🏛️ <b>Welcome!</b>\n\n📋 <b>Rules</b>\n• One agent per user\n• Permanent assignment",
    "top": "",
    "maintenance_mode": "0", "force_join_enabled": "1", "welcome_photo_enabled": "1", "broadcast_enabled": "1",
    "btn1_msg": "🎯 Agent claim coming soon!", "btn2_msg": "📊 Statistics coming soon!", "btn3_msg": "🤝 Refer & Earn coming soon!",
    "button_style_btn1": "primary", "button_style_btn2": "success", "button_style_btn3": "primary",
    "button_emoji_btn1": EMOJI["🎯"], "button_emoji_btn2": EMOJI["📊"], "button_emoji_btn3": EMOJI["🤝"],
    "button_emoji_enabled_btn1": "1", "button_emoji_enabled_btn2": "1", "button_emoji_enabled_btn3": "1",
}


def child_setting(child_id, key, default=""):
    with db() as con:
        row = con.execute("SELECT value FROM child_bot_settings WHERE child_bot_id=? AND key=?", (child_id,key)).fetchone()
        return row[0] if row else default


def set_child_setting(child_id, key, value):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO child_bot_settings(child_bot_id,key,value) VALUES(?,?,?)", (child_id,key,str(value)))


def init_child_defaults(child_id):
    source=dict(DEFAULT_CHILD_SETTINGS)
    for key in ("welcome","postjoin","top","btn1_msg","btn2_msg","btn3_msg"):
        source[key]=gset(key,source[key])
    for key in ("button_style_btn1","button_style_btn2","button_style_btn3","button_emoji_btn1","button_emoji_btn2","button_emoji_btn3","button_emoji_enabled_btn1","button_emoji_enabled_btn2","button_emoji_enabled_btn3"):
        source[key]=gset(key,source[key])
    with db() as con:
        for k,v in source.items():
            con.execute("INSERT OR IGNORE INTO child_bot_settings(child_bot_id,key,value) VALUES(?,?,?)", (child_id,k,str(v)))
        for key,text,style,emoji in (("btn1",BTN1,"primary",EMOJI["🎯"]),("btn2",BTN2,"success",EMOJI["📊"]),("btn3",BTN3,"primary",EMOJI["🤝"])):
            con.execute(
                "INSERT OR IGNORE INTO child_bot_buttons(child_bot_id,button_key,text,callback_data,url,style,emoji_id,enabled,row_num,position) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (child_id,key,text,key,"",style,emoji,1,0,0 if key=="btn1" else 1 if key=="btn2" else 0),
            )


def child_rows(child_id, enabled_only=False):
    q = "SELECT id,channel_id,channel_name,channel_link,position,order_num,enabled FROM child_bot_channels WHERE child_bot_id=?"
    q += " AND enabled=1" if enabled_only else ""
    q += " ORDER BY order_num,id"
    with db() as con: return con.execute(q,(child_id,)).fetchall()


def child_users(child_id, include_blocked=False):
    q = "SELECT user_id FROM child_bot_users WHERE child_bot_id=?"
    if not include_blocked: q += " AND status!='blocked'"
    with db() as con: return [r[0] for r in con.execute(q,(child_id,))]


def child_add_user(child_id, user):
    with db() as con:
        old = con.execute("SELECT 1 FROM child_bot_users WHERE child_bot_id=? AND user_id=?",(child_id,user.id)).fetchone()
        if old:
            con.execute("UPDATE child_bot_users SET first_name=?,username=?,last_seen=? WHERE child_bot_id=? AND user_id=?",(user.first_name or "",user.username or "",now_iso(),child_id,user.id)); return
        con.execute("INSERT INTO child_bot_users VALUES(?,?,?,?,?,?,?)",(child_id,user.id,user.first_name or "",user.username or "",now_iso(),"active",now_iso()))


def child_user_status(child_id, uid):
    return scalar("SELECT status FROM child_bot_users WHERE child_bot_id=? AND user_id=?",(child_id,uid),"active")


def child_set_status(child_id, uid, status):
    with db() as con: con.execute("UPDATE child_bot_users SET status=? WHERE child_bot_id=? AND user_id=?",(status,child_id,uid))


def child_search_users(child_id, query):
    q = str(query).strip()
    with db() as con:
        if q.isdigit(): return con.execute("SELECT user_id,first_name,username,joined_at,status FROM child_bot_users WHERE child_bot_id=? AND user_id=?",(child_id,int(q))).fetchall()
        x=f"%{q}%"
        return con.execute("SELECT user_id,first_name,username,joined_at,status FROM child_bot_users WHERE child_bot_id=? AND (first_name LIKE ? OR username LIKE ?) LIMIT 25",(child_id,x,x)).fetchall()


def child_button_rows(child_id):
    with db() as con:
        return con.execute("SELECT button_key,text,callback_data,url,style,emoji_id,enabled,row_num,position FROM child_bot_buttons WHERE child_bot_id=? ORDER BY row_num,position,id",(child_id,)).fetchall()


def child_build_main_kb(child_id):
    rows = child_button_rows(child_id)
    grouped = {}
    for k,text,cb,url,style,eid,en,row,pos in rows:
        if not en: continue
        grouped.setdefault(row,[]).append(ib(text,callback_data=cb or None,url=url or None,style=style,emoji_id=eid or None))
    return InlineKeyboardMarkup([grouped[k] for k in sorted(grouped)]) if grouped else InlineKeyboardMarkup([])

# =========================================================
# Clone request management
# =========================================================

def active_clone_request(uid):
    with db() as con:
        return con.execute("SELECT id,request_id,status FROM clone_requests WHERE requester_user_id=? AND status IN ('pending','approved_waiting_token','configuring') ORDER BY id DESC LIMIT 1",(uid,)).fetchone()


def get_request(request_id):
    with db() as con:
        return con.execute("SELECT id,request_id,requester_user_id,requester_name,requester_username,status,reason,reviewed_by,created_at,updated_at,approved_at,rejected_at,child_bot_id FROM clone_requests WHERE request_id=?",(request_id,)).fetchone()


def create_clone_request(user):
    existing = active_clone_request(user.id)
    if existing: return existing[1], False
    rid = secrets.token_urlsafe(8).replace("-","").replace("_","")[:12]
    with db() as con:
        con.execute(
            "INSERT INTO clone_requests(request_id,requester_user_id,requester_name,requester_username,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (rid,user.id,user.full_name or user.first_name or "",user.username or "", "pending", now_iso(), now_iso()),
        )
    return rid, True


def set_request_status(request_id, status, reviewer=0, reason="", child_id=0):
    with db() as con:
        con.execute(
            "UPDATE clone_requests SET status=?,reason=?,reviewed_by=?,child_bot_id=COALESCE(NULLIF(?,0),child_bot_id),updated_at=?,approved_at=CASE WHEN ?='approved_waiting_token' THEN ? ELSE approved_at END,rejected_at=CASE WHEN ?='rejected' THEN ? ELSE rejected_at END WHERE request_id=?",
            (status, reason, reviewer, child_id, now_iso(), status, now_iso(), status, now_iso(), request_id),
        )


def child_get(child_id):
    with db() as con:
        return con.execute("SELECT id,bot_id,username,first_name,display_name,encrypted_token,owner_user_id,status,created_at,updated_at,last_started_at,last_stopped_at,last_error,last_heartbeat,bot_api_meta FROM child_bots WHERE id=?",(child_id,)).fetchone()


def child_get_by_botid(bot_id):
    with db() as con:
        return con.execute("SELECT id,bot_id,username,first_name,display_name,encrypted_token,owner_user_id,status,created_at,updated_at,last_started_at,last_stopped_at,last_error,last_heartbeat,bot_api_meta FROM child_bots WHERE bot_id=?",(bot_id,)).fetchone()


def child_admins(child_id):
    with db() as con: return con.execute("SELECT id,user_id,role,created_at FROM child_bot_admins WHERE child_bot_id=? ORDER BY id",(child_id,)).fetchall()


def child_is_authorized(child_id, uid, roles=("CHILD_OWNER","CHILD_ADMIN","MASTER_OWNER")):
    if is_owner(uid): return True
    with db() as con:
        row=con.execute("SELECT role FROM child_bot_admins WHERE child_bot_id=? AND user_id=?",(child_id,uid)).fetchone()
        return bool(row and row[0] in roles)


def upsert_child_admin(child_id, uid, role):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO child_bot_admins(child_bot_id,user_id,role,created_at) VALUES(?,?,?,COALESCE((SELECT created_at FROM child_bot_admins WHERE child_bot_id=? AND user_id=?),?))",(child_id,uid,role,child_id,uid,now_iso()))


def remove_child_admin(child_id, uid):
    with db() as con: con.execute("DELETE FROM child_bot_admins WHERE child_bot_id=? AND user_id=? AND role!='CHILD_OWNER'",(child_id,uid))


def create_child_record(bot_info, encrypted_token, owner_uid, admin_uid):
    existing = child_get_by_botid(bot_info.id)
    if existing: return existing[0], False
    ts=now_iso()
    display=(bot_info.first_name or "").strip()
    meta={
        "can_join_groups":getattr(bot_info,"can_join_groups",None),
        "can_read_all_group_messages":getattr(bot_info,"can_read_all_group_messages",None),
        "supports_inline_queries":getattr(bot_info,"supports_inline_queries",None),
    }
    with db() as con:
        cur=con.execute(
            "INSERT INTO child_bots(bot_id,username,first_name,display_name,encrypted_token,owner_user_id,status,created_at,updated_at,last_heartbeat,bot_api_meta) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (bot_info.id,bot_info.username or "",bot_info.first_name or "",display,encrypted_token,owner_uid,"STOPPED",ts,ts,ts,json.dumps(meta,separators=(",",":"))),
        )
        child_id=cur.lastrowid
        con.execute("INSERT INTO child_bot_admins(child_bot_id,user_id,role,created_at) VALUES(?,?,?,?)",(child_id,owner_uid,"CHILD_OWNER",ts))
        if admin_uid and admin_uid!=owner_uid:
            con.execute("INSERT OR IGNORE INTO child_bot_admins(child_bot_id,user_id,role,created_at) VALUES(?,?,?,?)",(child_id,admin_uid,"CHILD_ADMIN",ts))
    init_child_defaults(child_id)
    return child_id, True


def update_child_status(child_id,status,error=""):
    with db() as con:
        con.execute("UPDATE child_bots SET status=?,updated_at=?,last_error=? WHERE id=?",(status,now_iso(),sanitize_error(error),child_id))


def touch_child(child_id):
    with db() as con: con.execute("UPDATE child_bots SET last_heartbeat=?,updated_at=? WHERE id=?",(now_iso(),now_iso(),child_id))

# =========================================================
# Permission + child runtime
# =========================================================

class ChildBotManager:
    def __init__(self):
        self.apps = {}
        self.tasks = {}
        self.broadcast_tasks = {}
        self.locks = {}

    def get_child_bot(self, child_id):
        return child_get(child_id)

    def get_all_child_bots(self):
        with db() as con: return con.execute("SELECT id,bot_id,username,first_name,display_name,owner_user_id,status,created_at,last_error,last_heartbeat FROM child_bots WHERE status!='REMOVED' ORDER BY id DESC").fetchall()

    async def register_child_bot(self, child_id):
        if child_id in self.apps: return self.apps[child_id]
        record=child_get(child_id)
        if not record: raise ValueError("Child bot not found")
        token=decrypt_secret(record[5])
        app=(Application.builder().token(token).concurrent_updates(False).build())
        app.bot_data["child_id"]=child_id
        self._register_handlers(app)
        self.apps[child_id]=app
        self.locks.setdefault(child_id,asyncio.Lock())
        return app

    def _register_handlers(self,app):
        app.add_handler(CommandHandler("start",child_start))
        app.add_handler(CommandHandler("admin",child_admin_cmd))
        app.add_handler(CommandHandler("cancel",child_cancel))
        app.add_handler(CallbackQueryHandler(child_check_joined,pattern=r"^cj$"))
        app.add_handler(CallbackQueryHandler(child_main_callback,pattern=r"^cbtn:"))
        app.add_handler(CallbackQueryHandler(child_admin_callback,pattern=r"^ca:"))
        app.add_handler(ChatJoinRequestHandler(child_join_request))
        app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, child_message_handler), group=10)
        app.add_error_handler(child_error_handler)

    async def start_child_bot(self, child_id):
        record=self.get_child_bot(child_id)
        if not record:
            raise ValueError("Child bot not found")
        if child_id in self.apps:
            app=self.apps[child_id]
        else:
            app=await self.register_child_bot(child_id)
        if app.running: return True
        try:
            await app.initialize()
            await app.start()
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
            self.apps[child_id]=app
            with db() as con: con.execute("UPDATE child_bots SET status='RUNNING',last_started_at=?,last_heartbeat=?,last_error='',updated_at=? WHERE id=?",(now_iso(),now_iso(),now_iso(),child_id))
            return True
        except TelegramError as e:
            text=sanitize_error(e)
            status="TOKEN_INVALID" if any(x in text.lower() for x in ("unauthorized","invalid bot token","token is invalid","not found")) else "ERROR"
            update_child_status(child_id,status,text)
            if status=="TOKEN_INVALID" and MASTER_BOT_REF and OWNER_ID:
                try:
                    await MASTER_BOT_REF.send_message(OWNER_ID, f"🔐 <b>CHILD BOT TOKEN INVALID</b>\n\nChild ID: <code>{child_id}</code>\nBot ID: <code>{record[1]}</code>\nUsername: <b>@{esc(record[2]) if record[2] else '—'}</b>\n\nThe child runtime was stopped. Reconfigure with a new BotFather token.", parse_mode=ParseMode.HTML)
                except TelegramError: pass
            await self._safe_close_app(child_id)
            return False
        except Exception as e:
            update_child_status(child_id,"ERROR",e)
            await self._safe_close_app(child_id)
            return False

    async def _safe_close_app(self,child_id):
        app=self.apps.pop(child_id,None)
        if not app: return
        try:
            if app.updater and app.updater.running: await app.updater.stop()
        except Exception: pass
        try:
            if app.running: await app.stop()
        except Exception: pass
        try: await app.shutdown()
        except Exception: pass

    async def stop_child_bot(self, child_id, status="STOPPED"):
        app=self.apps.get(child_id)
        if app:
            try:
                if app.updater and app.updater.running: await app.updater.stop()
            except Exception as e: logger.warning("Child updater stop %s: %s",child_id,sanitize_error(e))
            try:
                if app.running: await app.stop()
            except Exception as e: logger.warning("Child app stop %s: %s",child_id,sanitize_error(e))
            try: await app.shutdown()
            except Exception as e: logger.warning("Child shutdown %s: %s",child_id,sanitize_error(e))
            self.apps.pop(child_id,None)
        with db() as con: con.execute("UPDATE child_bots SET status=?,last_stopped_at=?,updated_at=? WHERE id=?",(status,now_iso(),now_iso(),child_id))
        return True

    async def restart_child_bot(self, child_id):
        await self.stop_child_bot(child_id,"STOPPED")
        return await self.start_child_bot(child_id)

    async def remove_child_bot(self, child_id):
        await self.stop_child_bot(child_id,"REMOVED")
        with db() as con:
            con.execute("UPDATE child_bots SET status='REMOVED',updated_at=? WHERE id=?",(now_iso(),child_id))
        return True

    async def reload_child_config(self, child_id):
        # Configuration is database-backed and read per update; restart is only required for token changes.
        touch_child(child_id)
        return True

    async def health_check(self):
        results=[]
        for row in self.get_all_child_bots():
            cid=row[0]
            if cid in self.apps:
                try:
                    await self.apps[cid].bot.get_me()
                    touch_child(cid)
                    results.append((cid,"RUNNING"))
                except Exception as e:
                    text=sanitize_error(e)
                    status="TOKEN_INVALID" if any(x in text.lower() for x in ("unauthorized","invalid bot token","token is invalid","not found")) else "ERROR"
                    update_child_status(cid,status,text)
                    if status=="TOKEN_INVALID" and MASTER_BOT_REF and OWNER_ID:
                        try:
                            rr=self.get_child_bot(cid)
                            await MASTER_BOT_REF.send_message(OWNER_ID, f"🔐 <b>CHILD BOT TOKEN INVALID</b>\n\nChild ID: <code>{cid}</code>\nBot ID: <code>{rr[1]}</code>\nUsername: <b>@{esc(rr[2]) if rr[2] else '—'}</b>", parse_mode=ParseMode.HTML)
                        except TelegramError: pass
                    await self._safe_close_app(cid)
                    results.append((cid,status))
            else:
                results.append((cid,row[6]))
        return results

    async def broadcast_to_child_bot(self, child_id, payload, buttons):
        return await child_broadcast(self,child_id,payload,buttons)

    async def start_broadcast_task(self, child_id, payload, buttons, status_msg):
        if child_id not in self.apps:
            ok=await self.start_child_bot(child_id)
            if not ok: raise RuntimeError("Child bot could not be started")
        recipients=child_users(child_id)
        bid=bcast_create(child_id,payload_kind(payload),len(recipients),payload=payload,buttons=buttons)
        task=asyncio.create_task(child_broadcast(self,child_id,payload,buttons,bid=bid,status_msg=status_msg))
        self.broadcast_tasks[bid]=task
        def _done(_): self.broadcast_tasks.pop(bid,None)
        task.add_done_callback(_done)
        return bid

    async def cancel_broadcast(self,bid):
        task=self.broadcast_tasks.get(bid)
        if task and not task.done():
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
            return True
        return False

    def failed_recipients(self,bid):
        with db() as con:
            row=con.execute("SELECT child_bot_id,payload_json,buttons_json FROM child_bot_broadcasts WHERE broadcast_id=?",(bid,)).fetchone()
            sent={r[0] for r in con.execute("SELECT user_id FROM child_bot_broadcast_messages WHERE broadcast_id=?",(bid,)).fetchall()}
        if not row: return None
        child_id,payload_json,buttons_json=row
        return child_id,json.loads(payload_json or "{}"),json.loads(buttons_json or "[]"),[uid for uid in child_users(child_id) if uid not in sent]

    async def shutdown_all(self):
        for cid in list(self.apps):
            await self.stop_child_bot(cid,"STOPPED")

MANAGER = ChildBotManager()

# =========================================================
# Child normal bot features
# =========================================================

async def child_check_joined(update,ctx):
    q=update.callback_query; cid=ctx.application.bot_data.get("child_id"); uid=q.from_user.id
    if not cid: return await q.answer("Unavailable",show_alert=True)
    if child_user_status(cid,uid)=="blocked": return await q.answer("🚫 You are blocked.",show_alert=True)
    await q.answer(); rows=child_rows(cid,True)
    if not rows:
        return await q.edit_message_text(child_setting(cid,"postjoin",DEFAULT_CHILD_SETTINGS["postjoin"]),reply_markup=child_build_main_kb(cid),parse_mode=ParseMode.HTML)
    joined=set()
    for r in rows:
        try:
            m=await ctx.bot.get_chat_member(r[1],uid)
            if m.status in ("member","administrator","creator","restricted"): joined.add(r[1])
        except TelegramError: pass
    if len(joined)==len(rows):
        await q.edit_message_text(child_setting(cid,"postjoin"),reply_markup=child_build_main_kb(cid),parse_mode=ParseMode.HTML)
    else:
        await q.answer("🚫 Please join every required channel.",show_alert=True)


async def child_start(update,ctx):
    cid=ctx.application.bot_data.get("child_id"); u=update.effective_user
    if not cid or not u: return
    child_add_user(cid,u)
    if child_user_status(cid,u.id)=="blocked": return await update.message.reply_text("🚫 You are blocked from using this bot.")
    if child_setting(cid,"maintenance_mode","0")=="1" and not child_is_authorized(cid,u.id): return await update.message.reply_text("🛠 Bot is under maintenance.")
    rows=child_rows(cid,True)
    if not rows:
        return await update.message.reply_text(child_setting(cid,"postjoin"),reply_markup=child_build_main_kb(cid),parse_mode=ParseMode.HTML)
    joined=set()
    for r in rows:
        try:
            m=await ctx.bot.get_chat_member(r[1],u.id)
            if m.status in ("member","administrator","creator","restricted"): joined.add(r[1])
        except TelegramError: pass
    if len(joined)==len(rows):
        return await update.message.reply_text(child_setting(cid,"postjoin"),reply_markup=child_build_main_kb(cid),parse_mode=ParseMode.HTML)
    top=child_setting(cid,"top"); text=f"{top}\n\n{child_setting(cid,'welcome')}" if top else child_setting(cid,"welcome")
    kb=[]
    for r in rows:
        if r[1] not in joined: kb.append([ib("📢 "+r[2],url=r[3],style="primary",emoji_id=EMOJI["📣"])])
    kb.append([ib("✅ Joined","cj",style="success",emoji_id=EMOJI["✅"])])
    photo=child_setting(cid,"welcome_photo")
    if photo and child_setting(cid,"welcome_photo_enabled","1")=="1": await update.message.reply_photo(photo,caption=text,reply_markup=InlineKeyboardMarkup(kb),parse_mode=ParseMode.HTML)
    else: await update.message.reply_text(text,reply_markup=InlineKeyboardMarkup(kb),parse_mode=ParseMode.HTML)


async def child_main_callback(update,ctx):
    q=update.callback_query; cid=ctx.application.bot_data.get("child_id"); uid=q.from_user.id
    if not cid: return
    if child_user_status(cid,uid)=="blocked": return await q.answer("🚫 You are blocked.",show_alert=True)
    await q.answer(); key=(q.data or "").split(":",1)[-1]
    if key=="back":
        return await q.edit_message_text(child_setting(cid,"postjoin"),reply_markup=child_build_main_kb(cid),parse_mode=ParseMode.HTML)
    setting={"btn1":"btn1_msg","btn2":"btn2_msg","btn3":"btn3_msg"}.get(key)
    if setting:
        return await q.edit_message_text(child_setting(cid,setting),reply_markup=InlineKeyboardMarkup([[ib("🔙 Back","cbtn:back",style="primary",emoji_id=EMOJI["📌"])]]),parse_mode=ParseMode.HTML)


async def child_join_request(update,ctx):
    r=update.chat_join_request; cid=ctx.application.bot_data.get("child_id")
    if cid and r:
        with db() as con:
            con.execute("INSERT OR IGNORE INTO child_bot_users(child_bot_id,user_id,first_name,username,joined_at,status,last_seen) VALUES(?,?,?,?,?,?,?)",(cid,r.from_user.id,r.from_user.first_name or "",r.from_user.username or "",now_iso(),"active",now_iso()))


async def child_cancel(update,ctx):
    ctx.user_data.clear(); await update.message.reply_text("❌ Cancelled.")

# =========================================================
# Master create flow
# =========================================================

async def create_start(update,ctx):
    u=update.effective_user
    if not u: return ConversationHandler.END
    existing=active_clone_request(u.id)
    if existing:
        return await update.message.reply_text(f"⏳ You already have an active child-bot request.\n\nRequest ID: <code>{esc(existing[1])}</code>\nStatus: <b>{esc(existing[2])}</b>",reply_markup=InlineKeyboardMarkup([[ib("❌ Cancel", "create_cancel", style="danger", emoji_id=EMOJI["❌"])]]),parse_mode=ParseMode.HTML)
    text=("🤖 <b>CREATE BOT REQUEST</b>\n\nYou are requesting permission to create a managed child bot.\n\n"
          "Your request will be reviewed by the owner.")
    await update.message.reply_text(text,reply_markup=InlineKeyboardMarkup([[ib("✅ Confirm","create_confirm",style="success",emoji_id=EMOJI["✅"]),ib("❌ Cancel","create_cancel",style="danger",emoji_id=EMOJI["❌"])]]),parse_mode=ParseMode.HTML)
    return S_CREATE_CONFIRM


async def create_confirm_cb(update,ctx):
    q=update.callback_query; uid=q.from_user.id; await q.answer()
    if q.data=="create_cancel":
        await q.edit_message_text("❌ Request cancelled.",reply_markup=InlineKeyboardMarkup([[ib("🏠 Main Menu","create_done",style="primary")]])); return ConversationHandler.END
    rid,created=create_clone_request(q.from_user)
    if not created: return await q.edit_message_text(f"⏳ Active request already exists.\nRequest ID: <code>{esc(rid)}</code>",parse_mode=ParseMode.HTML)
    await q.edit_message_text(f"✅ Request submitted.\n\n🆔 Request ID: <code>{esc(rid)}</code>\nStatus: <b>PENDING</b>",parse_mode=ParseMode.HTML)
    if OWNER_ID:
        await ctx.bot.send_message(OWNER_ID,
            "🤖 <b>NEW CHILD BOT REQUEST</b>\n\n"
            f"👤 Name: <b>{esc(q.from_user.full_name)}</b>\n"
            f"Username: <b>@{esc(q.from_user.username) if q.from_user.username else '—'}</b>\n"
            f"User ID: <code>{q.from_user.id}</code>\n\n🆔 Request ID: <code>{esc(rid)}</code>\nStatus: <b>PENDING</b>",
            reply_markup=InlineKeyboardMarkup([[ib("✅ Approve",f"clone:approve:{rid}",style="success",emoji_id=EMOJI["✅"]),ib("❌ Reject",f"clone:reject:{rid}",style="danger",emoji_id=EMOJI["❌"])]]),
            parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def create_done_cb(update,ctx):
    await update.callback_query.answer(); return ConversationHandler.END


async def owner_clone_callback(update,ctx,callback_data=None):
    q=update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("❌ Not authorized.",show_alert=True)
    data = callback_data if callback_data is not None else (q.data or "")
    parts=data.split(":")
    if len(parts)!=3: return await q.answer("Invalid request.",show_alert=True)
    action,rid=parts[1],parts[2]
    req=get_request(rid)
    if not req: return await q.answer("Request not found.",show_alert=True)
    if req[5] != "pending" and action in ("approve","reject"):
        return await q.answer(f"Request is already {req[5]}.",show_alert=True)
    if action=="approve":
        set_request_status(rid,"approved_waiting_token",OWNER_ID)
        await q.answer("Approved")
        try:
            await q.edit_message_text("✅ <b>REQUEST APPROVED</b>\n\nRequester has been asked for their BotFather token.",parse_mode=ParseMode.HTML)
        except TelegramError: pass
        await ctx.bot.send_message(req[2],"✅ <b>Your request has been approved!</b>\n\nPlease send your Telegram Bot Token.\n\n⚠️ Send the complete token generated by @BotFather.\n\nThe token will be encrypted at rest and never shown back to you.",reply_markup=cancel_kb("create_cancel"),parse_mode=ParseMode.HTML)
        return
    # Reject starts a reason flow; store target in owner's user_data only.
    ctx.user_data["reject_request_id"]=rid
    await q.answer(); await q.edit_message_text("❌ <b>REJECT CHILD BOT</b>\n\nPlease enter the reason for rejection.",reply_markup=cancel_kb("reject_cancel"),parse_mode=ParseMode.HTML)
    return S_REJECT_REASON


async def reject_cancel(update,ctx):
    if update.callback_query:
        await update.callback_query.answer(); await update.callback_query.edit_message_text("❌ Rejection cancelled.",reply_markup=back_kb("a_child_requests"))
    ctx.user_data.pop("reject_request_id",None)
    return ConversationHandler.END


async def reject_reason_message(update,ctx):
    if not is_owner(update.effective_user.id): return ConversationHandler.END
    rid=ctx.user_data.get("reject_request_id")
    if not rid: return ConversationHandler.END
    reason=(update.message.text or "").strip()
    if not reason: return S_REJECT_REASON
    req=get_request(rid)
    if not req or req[5]!="pending":
        ctx.user_data.pop("reject_request_id",None); await update.message.reply_text("⚠️ Request is no longer pending.",reply_markup=back_kb("a_child_requests")); return ConversationHandler.END
    set_request_status(rid,"rejected",OWNER_ID,reason)
    ctx.user_data.pop("reject_request_id",None)
    await update.message.reply_text("✅ Request rejected.",reply_markup=back_kb("a_child_requests"))
    try:
        await update.get_bot().send_message(req[2],f"❌ <b>Your child bot request was rejected.</b>\n\n<b>Reason:</b>\n{esc(reason)}\n\n🆔 Request ID: <code>{esc(rid)}</code>",reply_markup=back_kb("child_end"),parse_mode=ParseMode.HTML)
    except TelegramError as e: logger.warning("Reject notice failed: %s",sanitize_error(e))
    return ConversationHandler.END


async def create_cancel_cb(update,ctx):
    if update.callback_query:
        await update.callback_query.answer(); await update.callback_query.edit_message_text("❌ Cancelled.")
    ctx.user_data.clear(); return ConversationHandler.END


async def requester_token_message(update,ctx):
    if not update.message or not update.message.text: return ConversationHandler.END
    uid=update.effective_user.id
    req=active_clone_request(uid)
    if not req or req[2] != "approved_waiting_token": return ConversationHandler.END
    token=update.message.text.strip()
    if len(token)>300:
        return await update.message.reply_text("❌ Invalid token format. Please send the complete BotFather token.",reply_markup=cancel_kb("create_cancel"))
    # Validate token by getMe before any DB record is created.
    test_bot=telegram.Bot(token)
    try:
        me=await test_bot.get_me()
    except Exception as e:
        _safe_error=sanitize_error(e,token)
        await update.message.reply_text("❌ <b>Invalid Bot Token</b>\n\nPlease send a valid token generated by @BotFather.",reply_markup=cancel_kb("create_cancel"),parse_mode=ParseMode.HTML)
        return S_CREATE_TOKEN
    finally:
        try: await test_bot.shutdown()
        except Exception: pass
    existing=child_get_by_botid(me.id)
    if existing:
        return await update.message.reply_text(f"⚠️ <b>THIS BOT IS ALREADY REGISTERED</b>\n\n@{esc(existing[2]) or '—'}\nStatus: <b>{esc(existing[7])}</b>\nBot ID: <code>{existing[1]}</code>",reply_markup=cancel_kb("create_cancel"),parse_mode=ParseMode.HTML)
    # Token is kept in memory only until admin confirmation.
    ctx.user_data["create_token"]=token
    ctx.user_data["create_bot_info"]={
        "id":me.id,"username":me.username or "","first_name":me.first_name or "",
        "can_join_groups":getattr(me,"can_join_groups",None),
        "can_read_all_group_messages":getattr(me,"can_read_all_group_messages",None),
        "supports_inline_queries":getattr(me,"supports_inline_queries",None),
    }
    set_request_status(req[1],"configuring",OWNER_ID)
    await update.message.reply_text(
        "🤖 <b>BOT VERIFIED</b>\n\n"
        f"Name: <b>{esc(me.first_name or '—')}</b>\n"
        f"Username: <b>@{esc(me.username) if me.username else '—'}</b>\n"
        f"Bot ID: <code>{me.id}</code>\n\n"
        "Token: <code>********</code>\n\n👑 Now send your Admin ID.",
        reply_markup=cancel_kb("create_cancel"),parse_mode=ParseMode.HTML)
    return S_CREATE_ADMIN_ID


async def requester_admin_id_message(update,ctx):
    if not update.message or not update.message.text: return ConversationHandler.END
    uid=update.effective_user.id; req=active_clone_request(uid)
    if not req or req[2]!="configuring": return ConversationHandler.END
    text=update.message.text.strip()
    if not text.isdigit() or not (5 <= len(text) <= 15):
        return await update.message.reply_text("❌ Admin ID must be a valid numeric Telegram user ID.",reply_markup=cancel_kb("create_cancel"))
    admin_uid=int(text)
    info=ctx.user_data.get("create_bot_info")
    token=ctx.user_data.get("create_token")
    if not info or not token: return ConversationHandler.END
    ctx.user_data["create_admin_id"]=admin_uid
    ctx.user_data["create_request_id"]=req[1]
    await update.message.reply_text(
        "👑 <b>CHILD BOT CONFIGURATION</b>\n\n"
        f"Bot: <b>@{esc(info.get('username') or '—')}</b>\n"
        f"Bot ID: <code>{info['id']}</code>\n"
        f"Admin ID: <code>{admin_uid}</code>\n\n"
        "[✅ Confirm & Launch] [✏️ Change Admin ID] [❌ Cancel]",
        reply_markup=InlineKeyboardMarkup([[ib("✅ Confirm & Launch","create:launch",style="success",emoji_id=EMOJI["✅"]),ib("✏️ Change Admin ID","create:change_admin",style="primary",emoji_id=EMOJI["📝"])],[ib("❌ Cancel","create_cancel",style="danger",emoji_id=EMOJI["❌"])]]),
        parse_mode=ParseMode.HTML)
    return S_CREATE_ADMIN_ID


async def requester_create_callback(update,ctx):
    q=update.callback_query; await q.answer(); uid=q.from_user.id
    req=active_clone_request(uid)
    if q.data=="create_cancel":
        if req and req[2] in ("approved_waiting_token","configuring"):
            set_request_status(req[1],"rejected",uid,"Cancelled by requester")
        ctx.user_data.clear(); await q.edit_message_text("❌ Cancelled."); return ConversationHandler.END
    if q.data=="create:change_admin":
        await q.edit_message_text("👑 Send the new Admin ID.",reply_markup=cancel_kb("create_cancel")); return S_CREATE_ADMIN_ID
    if q.data=="create:launch":
        if not req: return ConversationHandler.END
        token=ctx.user_data.get("create_token"); info=ctx.user_data.get("create_bot_info"); admin_uid=int(ctx.user_data.get("create_admin_id",0));
        if not token or not info: return ConversationHandler.END
        try:
            encrypted=encrypt_secret(token)
            # Duplicate protection at DB boundary too.
            child_id,created=create_child_record(type("BotInfo",(),info),encrypted,uid,admin_uid)
            if not created:
                ctx.user_data.clear(); return await q.edit_message_text("⚠️ This bot is already registered.",parse_mode=ParseMode.HTML)
            set_request_status(req[1],"approved",OWNER_ID,child_id=child_id)
            ok=await MANAGER.start_child_bot(child_id)
            status="RUNNING" if ok else "ERROR"
            await q.edit_message_text(("✅ <b>CHILD BOT LAUNCHED</b>" if ok else "⚠️ <b>CHILD BOT CREATED, BUT COULD NOT START</b>")+f"\n\nBot: <b>@{esc(info.get('username') or '—')}</b>\nStatus: <b>{status}</b>",parse_mode=ParseMode.HTML)
            if OWNER_ID:
                try: await ctx.bot.send_message(OWNER_ID,f"🤖 Child bot created.\n\nBot: <b>@{esc(info.get('username') or '—')}</b>\nBot ID: <code>{info['id']}</code>\nChild ID: <code>{child_id}</code>\nStatus: <b>{status}</b>",parse_mode=ParseMode.HTML)
                except TelegramError: pass
        except Exception as e:
            await q.edit_message_text("❌ Child bot creation failed. The secret was not exposed.",parse_mode=ParseMode.HTML)
            log_error("ERROR",f"Child create failed: {sanitize_error(e,token)}")
        finally:
            ctx.user_data.clear()
        return ConversationHandler.END
    return S_CREATE_ADMIN_ID

# =========================================================
# Broadcast payload / keyboard builder
# =========================================================

def entity_dicts(entities):
    return [e.to_dict() for e in (entities or [])]


def entity_objs(data):
    return [telegram.MessageEntity.de_json(e, None) for e in (data or [])]


def snapshot_message(msg):
    if not msg: raise ValueError("Missing message")
    payload={"type":"unknown","text":msg.text or "","entities":entity_dicts(msg.entities),"caption":msg.caption or "","caption_entities":entity_dicts(msg.caption_entities)}
    if msg.photo:
        payload.update(type="photo",file_id=msg.photo[-1].file_id)
    elif msg.video:
        payload.update(type="video",file_id=msg.video.file_id)
    elif msg.document:
        payload.update(type="document",file_id=msg.document.file_id)
    elif msg.audio:
        payload.update(type="audio",file_id=msg.audio.file_id)
    elif msg.voice:
        payload.update(type="voice",file_id=msg.voice.file_id)
    elif msg.animation:
        payload.update(type="animation",file_id=msg.animation.file_id)
    elif msg.sticker:
        payload.update(type="sticker",file_id=msg.sticker.file_id)
    elif msg.video_note:
        payload.update(type="video_note",file_id=msg.video_note.file_id)
    elif msg.location:
        payload.update(type="location",latitude=msg.location.latitude,longitude=msg.location.longitude)
    elif msg.venue:
        payload.update(type="venue",latitude=msg.venue.location.latitude,longitude=msg.venue.location.longitude,title=msg.venue.title,address=msg.venue.address)
    elif msg.contact:
        payload.update(type="contact",phone_number=msg.contact.phone_number,first_name=msg.contact.first_name,last_name=msg.contact.last_name)
    elif msg.poll:
        payload.update(type="poll",question=msg.poll.question,options=[o.text for o in msg.poll.options],is_anonymous=msg.poll.is_anonymous,type_=msg.poll.type,allows_multiple_answers=msg.poll.allows_multiple_answers,correct_option_id=getattr(msg.poll,"correct_option_id",None),explanation=getattr(msg.poll,"explanation",None))
    return payload


def button_validate_url(url):
    return bool(re.fullmatch(r"https://(?:t\.me|telegram\.me)(?:/[^\s]+)?|https://[^\s]+",url or ""))


def build_inline_keyboard(buttons):
    if not buttons: return None
    rows=[]
    for row in buttons:
        out=[]
        for b in row:
            text=str(b.get("text","")).strip(); url=str(b.get("url","")).strip()
            if not text or not url or not button_validate_url(url): continue
            out.append(ib(text,url=url,style=b.get("style"),emoji_id=b.get("emoji_id")))
        if out: rows.append(out)
    return InlineKeyboardMarkup(rows) if rows else None


def add_payload_button(ctx, text, url, row):
    buttons=ctx.user_data.setdefault("broadcast_buttons",[])
    while len(buttons)<row: buttons.append([])
    buttons[row-1].append({"text":text,"url":url,"style":"primary"})


def payload_kind(payload):
    return payload.get("type","unknown")

async def send_payload(bot, chat_id, payload, buttons=None):
    kb=build_inline_keyboard(buttons or [])
    typ=payload.get("type")
    kwargs={}
    if typ=="text": return await bot.send_message(chat_id,payload.get("text","") or " ",entities=entity_objs(payload.get("entities")),reply_markup=kb)
    cap=payload.get("caption",""); cents=entity_objs(payload.get("caption_entities"))
    if typ=="photo": return await bot.send_photo(chat_id,payload["file_id"],caption=cap or None,caption_entities=cents or None,reply_markup=kb)
    if typ=="video": return await bot.send_video(chat_id,payload["file_id"],caption=cap or None,caption_entities=cents or None,reply_markup=kb)
    if typ=="document": return await bot.send_document(chat_id,payload["file_id"],caption=cap or None,caption_entities=cents or None,reply_markup=kb)
    if typ=="audio": return await bot.send_audio(chat_id,payload["file_id"],caption=cap or None,caption_entities=cents or None,reply_markup=kb)
    if typ=="voice": return await bot.send_voice(chat_id,payload["file_id"],caption=cap or None,caption_entities=cents or None,reply_markup=kb)
    if typ=="animation": return await bot.send_animation(chat_id,payload["file_id"],caption=cap or None,caption_entities=cents or None,reply_markup=kb)
    if typ=="sticker":
        m=await bot.send_sticker(chat_id,payload["file_id"])
        if kb: await bot.send_message(chat_id,"‎",reply_markup=kb)
        return m
    if typ=="video_note":
        m=await bot.send_video_note(chat_id,payload["file_id"])
        if kb: await bot.send_message(chat_id,"‎",reply_markup=kb)
        return m
    if typ=="location": return await bot.send_location(chat_id,payload["latitude"],payload["longitude"],reply_markup=kb)
    if typ=="venue": return await bot.send_venue(chat_id,payload["latitude"],payload["longitude"],payload["title"],payload["address"],reply_markup=kb)
    if typ=="contact": return await bot.send_contact(chat_id,payload["phone_number"],payload["first_name"],last_name=payload.get("last_name") or None,reply_markup=kb)
    if typ=="poll":
        kwargs={"chat_id":chat_id,"question":payload.get("question","Poll"),"options":payload.get("options",[]),"is_anonymous":bool(payload.get("is_anonymous",True)),"allows_multiple_answers":bool(payload.get("allows_multiple_answers",False))}
        if payload.get("type_") and payload.get("type_")=="quiz":
            kwargs["type"]="quiz"; kwargs["correct_option_id"]=payload.get("correct_option_id")
            if payload.get("explanation"): kwargs["explanation"]=payload.get("explanation")
        return await bot.send_poll(**kwargs)
    # Unknown message type: preserve caption/text when possible, never invent content.
    return await bot.send_message(chat_id,payload.get("text") or payload.get("caption") or "",reply_markup=kb)


async def send_album(bot,chat_id,album,buttons=None):
    media=[]
    for item in album:
        typ=item.get("type"); cap=item.get("caption") or None; cents=entity_objs(item.get("caption_entities")) or None
        if typ=="photo": media.append(InputMediaPhoto(item["file_id"],caption=cap,caption_entities=cents))
        elif typ=="video": media.append(InputMediaVideo(item["file_id"],caption=cap,caption_entities=cents))
        elif typ=="document": media.append(InputMediaDocument(item["file_id"],caption=cap,caption_entities=cents))
        elif typ=="audio": media.append(InputMediaAudio(item["file_id"],caption=cap,caption_entities=cents))
        elif typ=="animation": media.append(InputMediaAnimation(item["file_id"],caption=cap,caption_entities=cents))
    result=await bot.send_media_group(chat_id,media=media)
    if buttons:
        await bot.send_message(chat_id,"‎",reply_markup=build_inline_keyboard(buttons))
    return result


def bcast_create(child_id, kind, total, source_chat=0, source_mid=0, payload=None, buttons=None):
    bid=uuid.uuid4().hex[:16]
    with db() as con:
        con.execute("INSERT INTO child_bot_broadcasts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(bid,child_id,source_chat,source_mid,kind,total,0,0,0,"running",now_iso(),"",json.dumps(payload or {},separators=(",",":")),json.dumps(buttons or [],separators=(",",":"))))
    return bid


async def child_broadcast(manager,child_id,payload,buttons,bid=None,status_msg=None):
    if child_id not in manager.apps:
        await manager.start_child_bot(child_id)
    app=manager.apps.get(child_id)
    if not app: raise RuntimeError("Child bot is not running")
    recipient_list=child_users(child_id)
    kind=payload_kind(payload)
    if bid is None:
        bid=bcast_create(child_id,kind,len(recipient_list),payload=payload,buttons=buttons)
    sent=failed=0; cancelled=False
    task=asyncio.current_task()
    try:
        for i,uid in enumerate(recipient_list,1):
            if task and task.cancelled(): cancelled=True; break
            try:
                if kind=="album":
                    result=await send_album(app.bot,uid,payload.get("album",[]),buttons)
                    mids=[getattr(m,"message_id",0) for m in result]
                else:
                    m=await send_payload(app.bot,uid,payload,buttons); mids=[getattr(m,"message_id",0)]
                with db() as con:
                    for mid in mids: con.execute("INSERT OR IGNORE INTO child_bot_broadcast_messages VALUES(?,?,?)",(bid,uid,mid))
                sent+=1
            except RetryAfter as e:
                await asyncio.sleep(float(e.retry_after)+0.5)
                try:
                    if kind=="album":
                        result=await send_album(app.bot,uid,payload.get("album",[]),buttons)
                        mids=[getattr(m,"message_id",0) for m in result]
                    else:
                        m=await send_payload(app.bot,uid,payload,buttons); mids=[getattr(m,"message_id",0)]
                    with db() as con:
                        for mid in mids: con.execute("INSERT OR IGNORE INTO child_bot_broadcast_messages VALUES(?,?,?)",(bid,uid,mid))
                    sent+=1
                except Exception as e2: failed+=1; logger.warning("Child broadcast retry %s/%s: %s",child_id,uid,sanitize_error(e2))
            except Forbidden:
                failed+=1; child_set_status(child_id,uid,"inactive")
            except TelegramError as e:
                failed+=1; logger.warning("Child broadcast %s/%s: %s",child_id,uid,sanitize_error(e))
            if status_msg and (i%25==0 or i==len(recipient_list)):
                try:
                    await status_msg.edit_text(f"📣 <b>Broadcasting via @{esc((child_get(child_id) or [0,0,''])[2] or 'child')}</b>\n\nProgress: <b>{i}/{len(recipient_list)}</b>\n✅ Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>",reply_markup=InlineKeyboardMarkup([[ib("⏹ Cancel Broadcast",f"cb:cancelbc:{bid}",style="danger")]]),parse_mode=ParseMode.HTML)
                except TelegramError: pass
            if i%25==0: touch_child(child_id)
            await asyncio.sleep(0.08)
    except asyncio.CancelledError:
        cancelled=True
        raise
    finally:
        status="cancelled" if cancelled else "completed"
        with db() as con: con.execute("UPDATE child_bot_broadcasts SET sent=?,failed=?,cancelled=?,status=?,completed_at=? WHERE broadcast_id=?",(sent,failed,int(cancelled),status,now_iso(),bid))
        if status_msg:
            try:
                await status_msg.edit_text(f"{'⏹' if cancelled else '✅'} <b>BROADCAST {status.upper()}</b>\n\nTotal: <b>{len(recipient_list)}</b>\n✅ Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>",reply_markup=InlineKeyboardMarkup([[ib("🔁 Retry Failed",f"cb:retryb:{child_id}:{bid}",style="success")],[ib("🔙 Back",f"cb:select:{child_id}")]]),parse_mode=ParseMode.HTML)
            except TelegramError: pass
    return {"broadcast_id":bid,"total":len(recipient_list),"sent":sent,"failed":failed,"cancelled":cancelled,"status":status}

# =========================================================
# Master dashboard / child panel
# =========================================================

def dash():
    now=datetime.now(); today=now.date().isoformat(); week=(now-timedelta(days=now.weekday())).date().isoformat()
    nt=scalar("SELECT COUNT(*) FROM users WHERE substr(joined_at,1,10)=?",(today,),0); nw=scalar("SELECT COUNT(*) FROM users WHERE substr(joined_at,1,10)>=?",(week,),0)
    up=int(time.monotonic()-STARTED_AT); sz=Path(DB_PATH).stat().st_size if Path(DB_PATH).exists() else 0
    total_child=scalar("SELECT COUNT(*) FROM child_bots WHERE status!='REMOVED'"); running=scalar("SELECT COUNT(*) FROM child_bots WHERE status='RUNNING'"); stopped=scalar("SELECT COUNT(*) FROM child_bots WHERE status='STOPPED'"); errors=scalar("SELECT COUNT(*) FROM child_bots WHERE status IN ('ERROR','TOKEN_INVALID')"); pending=scalar("SELECT COUNT(*) FROM clone_requests WHERE status='pending'"); cusers=scalar("SELECT COUNT(*) FROM child_bot_users"); cbcasts=scalar("SELECT COUNT(*) FROM child_bot_broadcasts")
    return ("╔═━━━✦ 🤖 <b>MASTER ADMIN PANEL</b> ✦━━━═╗\n\n"
            f"👥 Master Users: <b>{scalar('SELECT COUNT(*) FROM users')}</b> | Today <b>{nt}</b> | Week <b>{nw}</b>\n"
            f"📢 Master Channels: <b>{len(channels(True))}</b>\n📣 Master Broadcasts: <b>{scalar('SELECT COUNT(*) FROM broadcasts')}</b>\n\n"
            f"🤖 Child Bots: <b>{total_child}</b> | 🟢 <b>{running}</b> | 🔴 <b>{stopped}</b> | ⚠️ <b>{errors}</b>\n"
            f"📥 Pending Requests: <b>{pending}</b>\n👥 Total Child Users: <b>{cusers}</b>\n📣 Child Broadcasts: <b>{cbcasts}</b>\n\n"
            f"⏱ Uptime: <b>{up//86400}d {(up%86400)//3600}h {(up%3600)//60}m</b>\n💾 DB Size: <b>{sz/1024:.1f} KB</b>\n"
            f"🧩 Version: <code>{BOT_VERSION}</code> | DB <code>{DB_VERSION}</code>")


def admin_kb():
    return InlineKeyboardMarkup([
        [ib("📊 Dashboard","a_dash",style="primary",emoji_id=EMOJI["📊"]),ib("📢 Channels","a_chs",style="primary",emoji_id=EMOJI["📣"])],
        [ib("📝 Messages","a_msgs",style="primary",emoji_id=EMOJI["📝"]),ib("🎨 Buttons","a_buttons",style="success",emoji_id=EMOJI["🌟"])],
        [ib("📣 Broadcast","a_bcast",style="success",emoji_id=EMOJI["📣"]),ib("👥 Members","a_members",style="primary",emoji_id=EMOJI["👑"])],
        [ib("💾 Backup Center","a_backup_menu",style="primary",emoji_id=EMOJI["⚙️"]),ib("♻️ Restore","a_restore",style="danger",emoji_id=EMOJI["✅"])],
        [ib("🩺 Database Health","a_dbhealth",style="primary",emoji_id=EMOJI["📊"]),ib("❤️ Bot Health","a_health",style="success",emoji_id=EMOJI["🌟"])],
        [ib("⚙️ Settings","a_settings",style="primary",emoji_id=EMOJI["⚙️"]),ib("🧪 Premium Button Test","a_premium_test",style="success",emoji_id=EMOJI["🌟"])],
        [ib("🤖 Child Bots","a_child",style="primary",emoji_id=EMOJI["🤖"] if "🤖" in EMOJI else EMOJI["🌟"]),ib("📥 Clone Requests","a_child_requests",style="success",emoji_id=EMOJI["📥"] if "📥" in EMOJI else EMOJI["📌"])],
        [ib("📊 Global Statistics","a_child_stats",style="primary",emoji_id=EMOJI["📊"]),ib("📜 Error Log","a_errors",style="primary",emoji_id=EMOJI["📝"])],
        [ib("❌ Close","a_close",style="danger",emoji_id=EMOJI["❌"])],
    ])


async def admin_cmd(update,ctx):
    if not is_owner(update.effective_user.id): return await update.message.reply_text("❌ Not authorized!")
    await update.message.reply_text(dash(),reply_markup=admin_kb(),parse_mode=ParseMode.HTML)

# Legacy admin helpers compressed but preserved.
def ch_kb():
    rows=[]
    for r in channels(True):
        cid,name,en=r[0],r[2],r[6]
        rows += [[ib("✏️ "+name[:16],f"a_editc_{cid}",style="primary",emoji_id=EMOJI["📝"]),ib("⬅️",f"a_left_{cid}",style="primary"),ib("➡️",f"a_right_{cid}",style="primary")],[ib("🟢 Enable" if not en else "🔴 Disable",f"a_togglec_{cid}",style="success" if not en else "danger"),ib("🧪 Test",f"a_testc_{cid}",style="success"),ib("🗑 Delete",f"a_delc_{cid}",style="danger")]]
    rows += [[ib("➕ Add Channel","a_addch",style="success",emoji_id=EMOJI["➕"])],[ib("🔙 Back","a_back",style="primary",emoji_id=EMOJI["📌"])]]
    return InlineKeyboardMarkup(rows)


def msg_kb():
    return InlineKeyboardMarkup([[ib("📢 Top Message","a_top")],[ib("👋 Welcome Message","a_welcome")],[ib("🖼 Welcome Photo","a_welcome_photo")],[ib("🎉 Post-Join Message","a_postjoin")],[ib("✉️ "+BTN1,"a_btn1")],[ib("✉️ "+BTN2,"a_btn2")],[ib("✉️ "+BTN3,"a_btn3")],[ib("🔙 Back","a_back")]])


def btn_kb():
    rows=[]
    for k,n in (("btn1",BTN1),("btn2",BTN2),("btn3",BTN3)):
        rows += [[ib("🎨 "+n[:16],f"a_style_{k}",style=bstyle(k)),ib("Style: "+bstyle(k),f"a_style_{k}",style=bstyle(k))],[ib("🧩 Emoji "+("ON" if bemoji(k) else "OFF"),f"a_emoji_{k}",style="success" if bemoji(k) else "danger"),ib("🆔 Set Emoji ID",f"a_setemoji_{k}"),ib("👁 Preview",f"a_preview_{k}")],[ib("↩️ Reset",f"a_reset_{k}",style="danger")]]
    rows += [[ib("🧪 Premium Button Test","a_premium_test",style="success")],[ib("🔙 Back","a_back")]]
    return InlineKeyboardMarkup(rows)


def settings_kb():
    def t(k,n):
        on=gset(k,"0")=="1"; return ib(("🟢 " if on else "🔴 ")+n+": "+("ON" if on else "OFF"),"a_toggle_"+k,style="success" if on else "danger")
    return InlineKeyboardMarkup([[t("maintenance_mode","Maintenance Mode")],[t("force_join_enabled","Force Join")],[t("welcome_photo_enabled","Welcome Photo")],[t("broadcast_enabled","Broadcast")],[t("auto_backup_enabled","Auto Backup")],[t("debug_logging","Debug Logging")],[ib("🕒 Auto Backup: "+gset("auto_backup_frequency","daily"),"a_autobackup")],[ib("🔙 Back","a_back")]])


def health():
    up=int(time.monotonic()-STARTED_AT)
    return f"❤️ <b>BOT HEALTH</b>\n\n🟢 Online\nUptime: <b>{up//86400}d {(up%86400)//3600}h {(up%3600)//60}m</b>\nPython: <code>{sys.version.split()[0]}</code>\nPTB: <code>{telegram.__version__}</code>\nDB: <code>{esc(DB_PATH)}</code>\nLast Error: <code>{esc(LAST_ERROR or 'None')}</code>"


def dbhealth():
    try:
        with db() as con:
            ok=con.execute("PRAGMA integrity_check").fetchone()[0]; wal=con.execute("PRAGMA journal_mode").fetchone()[0]
            vals=[("Users","users"),("Channels","channels"),("Settings","settings"),("Join Requests","join_requests"),("Broadcast Records","broadcasts"),("Child Bots","child_bots"),("Child Users","child_bot_users"),("Child Broadcasts","child_bot_broadcasts")]
            s=f"🩺 <b>DATABASE HEALTH</b>\n\nStatus: {'🟢 HEALTHY' if ok=='ok' else '🔴 CORRUPT'}\nWAL: <b>{wal}</b>\nSize: <b>{Path(DB_PATH).stat().st_size/1024:.1f} KB</b>\n"
            return s+"\n".join(f"{n}: <b>{con.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]}</b>" for n,t in vals)
    except Exception as e: return "🔴 Database health check failed: <code>"+esc(sanitize_error(e))+"</code>"

# =========================================================
# Backup / restore
# =========================================================

def safe_zip(z):
    for n in z.namelist():
        p=Path(n)
        if p.is_absolute() or ".." in p.parts: raise ValueError("Unsafe backup archive: path traversal detected.")


def validate_backup(path):
    with zipfile.ZipFile(path) as z:
        safe_zip(z)
        if z.testzip() is not None or not {"bot2.db","manifest.txt"}.issubset(set(z.namelist())): raise ValueError("Invalid backup archive.")
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"bot2.db"
            with z.open("bot2.db") as s, p.open("wb") as out: shutil.copyfileobj(s,out)
            with sqlite3.connect(p) as con:
                ok=con.execute("PRAGMA integrity_check").fetchone()[0]
                tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            req={"users","channels","settings","join_requests","broadcast_msgs","broadcasts","error_logs","child_bots","clone_requests","child_bot_settings","child_bot_admins"}
            if ok!="ok" or not req.issubset(tables): raise ValueError("Database integrity/required tables validation failed.")


def create_backup():
    ts=datetime.now().strftime("%Y%m%d_%H%M%S"); zip_path=BACKUP_DIR/f"bot_backup_{ts}.zip"; tmp=BACKUP_DIR/f".{ts}.db"; man=BACKUP_DIR/f".{ts}.txt"
    try:
        src=sqlite3.connect(DB_PATH); dst=sqlite3.connect(tmp)
        with dst: src.backup(dst)
        src.close(); dst.close(); validate_backup_from_db(tmp)
        man.write_text(f"Telegram Bot Backup\nBackup Date: {now_iso()}\nDatabase Version: {DB_VERSION}\nBot Version: {BOT_VERSION}\nSecrets: raw bot tokens are NOT included; child tokens, if present, are encrypted.\nEncryption key is NOT included.\n",encoding="utf-8")
        with zipfile.ZipFile(zip_path,"w",zipfile.ZIP_DEFLATED) as z: z.write(tmp,"bot2.db"); z.write(man,"manifest.txt")
        sset("last_backup",now_iso()); return zip_path
    finally: tmp.unlink(missing_ok=True); man.unlink(missing_ok=True)


def validate_backup_from_db(p):
    with sqlite3.connect(p) as con:
        ok=con.execute("PRAGMA integrity_check").fetchone()[0]; tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    req={"users","channels","settings","join_requests","broadcast_msgs","broadcasts","error_logs","child_bots","child_bot_admins","clone_requests","child_bot_users","child_bot_channels","child_bot_settings","child_bot_buttons","child_bot_broadcasts","child_bot_broadcast_messages","child_bot_logs"}
    if ok!="ok" or not req.issubset(tables): raise ValueError("Database integrity/required tables validation failed.")


def restore_backup(path):
    validate_backup(path); safety=create_backup()
    with zipfile.ZipFile(path) as z,tempfile.TemporaryDirectory() as td:
        p=Path(td)/"bot2.db"
        with z.open("bot2.db") as s,p.open("wb") as out: shutil.copyfileobj(s,out)
        validate_backup_from_db(p)
        src=sqlite3.connect(p); dst=sqlite3.connect(DB_PATH)
        with dst: src.backup(dst)
        src.close(); dst.close()
    sset("last_restore",now_iso()); return safety


def backup_list(): return sorted(BACKUP_DIR.glob("*.zip"),key=lambda p:p.stat().st_mtime,reverse=True)


def cleanup_backups():
    try:n=max(1,int(gset("auto_backup_keep","7")))
    except ValueError:n=7
    for p in backup_list()[n:]: p.unlink(missing_ok=True)

# =========================================================
# Premium test
# =========================================================

async def premium_test(bot,chat):
    eid=EMOJI["🌟"]; cb="premium_button_test"
    try:
        await bot.send_message(chat,"🧪 <b>Premium Button Test</b>\n\nCustom emoji button test.",reply_markup=InlineKeyboardMarkup([[ib("Premium Button Test",cb,style="success",emoji_id=eid)]]),parse_mode=ParseMode.HTML)
        return True
    except TelegramError:
        try:
            await bot.send_message(chat,"Custom emoji button is unavailable.\n\nNormal button fallback is active.",reply_markup=InlineKeyboardMarkup([[ib("Premium Button Test",cb,style="success")]]))
        except TelegramError: pass
        return False

# =========================================================
# Legacy admin conversation handlers
# =========================================================

async def input_text(update,ctx,key,label,clear=False):
    if not update.message or not update.message.text:return False
    value=update.message.text.strip(); value="" if clear and value.lower()=="clear" else (update.message.text_html.strip() if hasattr(update.message,"text_html") else value); sset(key,value)
    await update.message.reply_text("✅ "+label+" updated.",reply_markup=back_kb("a_msgs")); return True

async def s_top(u,c): return ConversationHandler.END if await input_text(u,c,"top","Top Message",True) else S_TOP
async def s_welcome(u,c): return ConversationHandler.END if await input_text(u,c,"welcome","Welcome Message") else S_WELCOME
async def s_postjoin(u,c): return ConversationHandler.END if await input_text(u,c,"postjoin","Post-Join Message") else S_POSTJOIN
async def s_btn1(u,c): return ConversationHandler.END if await input_text(u,c,"btn1_msg","Button 1 Reply") else S_BTN1
async def s_btn2(u,c): return ConversationHandler.END if await input_text(u,c,"btn2_msg","Button 2 Reply") else S_BTN2
async def s_btn3(u,c): return ConversationHandler.END if await input_text(u,c,"btn3_msg","Button 3 Reply") else S_BTN3

async def s_photo(u,c):
    if u.message.text and u.message.text.strip().lower()=="clear": sset("welcome_photo",""); await u.message.reply_text("✅ Welcome photo cleared.",reply_markup=back_kb("a_msgs")); return ConversationHandler.END
    if not u.message.photo: await u.message.reply_text("❌ Send a photo or 'clear'."); return S_WELCOME_PHOTO
    sset("welcome_photo",u.message.photo[-1].file_id); await u.message.reply_text("✅ Welcome photo updated.",reply_markup=back_kb("a_msgs")); return ConversationHandler.END

async def s_ch_id(u,c): c.user_data["ch_id"]=(u.message.text or "").strip(); await u.message.reply_text("✏️ Send Channel Name:\n\n[🔙 Back] [❌ Cancel]"); return S_CH_NAME
async def s_ch_name(u,c): c.user_data["ch_name"]=(u.message.text or "").strip(); await u.message.reply_text("🔗 Send Channel Invite Link:\n\n[🔙 Back] [❌ Cancel]"); return S_CH_LINK
async def s_ch_link(u,c):
    link=(u.message.text or "").strip()
    if not link.startswith(("https://t.me/","http://t.me/")): await u.message.reply_text("❌ Invalid Telegram link."); return S_CH_LINK
    try:add_channel(c.user_data["ch_id"],c.user_data["ch_name"],link)
    except sqlite3.IntegrityError: await u.message.reply_text("❌ Channel already exists."); return ConversationHandler.END
    c.user_data.clear(); await u.message.reply_text("✅ Channel added.",reply_markup=back_kb("a_chs")); return ConversationHandler.END

async def s_editname(u,c):
    cid=c.user_data.get("edit_channel")
    if not cid:return ConversationHandler.END
    update_channel(cid,name=(u.message.text or "").strip()); await u.message.reply_text("🔗 Send new invite link or <code>skip</code>.",parse_mode=ParseMode.HTML); return S_EDITLINK

async def s_editlink(u,c):
    cid=c.user_data.get("edit_channel"); v=(u.message.text or "").strip()
    if v.lower()!="skip":
        if not v.startswith(("https://t.me/","http://t.me/")): await u.message.reply_text("❌ Invalid Telegram link."); return S_EDITLINK
        update_channel(cid,link=v)
    c.user_data.clear(); await u.message.reply_text("✅ Channel updated.",reply_markup=back_kb("a_chs")); return ConversationHandler.END

async def s_emoji(u,c):
    k=c.user_data.get("emoji_key")
    if not k:return ConversationHandler.END
    v=(u.message.text or "").strip()
    if v.lower()=="clear": sset("button_emoji_"+k,""); sset("button_emoji_enabled_"+k,"0"); await u.message.reply_text("🧩 Custom emoji disabled.",reply_markup=back_kb("a_buttons")); c.user_data.clear(); return ConversationHandler.END
    if not v.isdigit(): await u.message.reply_text("❌ Custom emoji ID must be numeric."); return S_EMOJI
    sset("button_emoji_"+k,v); sset("button_emoji_enabled_"+k,"1"); await u.message.reply_text("✅ Custom emoji ID saved.",reply_markup=back_kb("a_buttons")); c.user_data.clear(); return ConversationHandler.END

async def s_restore(u,c):
    if not is_owner(u.effective_user.id):return ConversationHandler.END
    d=u.message.document
    if not d or not (d.file_name or "").lower().endswith(".zip"): await u.message.reply_text("❌ Send a backup .zip file."); return S_RESTORE
    p=None
    try:
        f=await c.bot.get_file(d.file_id)
        with tempfile.NamedTemporaryFile(suffix=".zip",delete=False) as t:p=t.name
        await f.download_to_drive(p); restore_backup(p)
        await u.message.reply_text("✅ <b>Restore completed.</b>\n\n🔐 Safety backup created.\n🔄 Restart the service to apply restored state.",parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.exception("Restore"); await u.message.reply_text("❌ Restore failed:\n<code>"+esc(sanitize_error(e))+"</code>",parse_mode=ParseMode.HTML)
    finally:
        if p:
            try: os.remove(p)
            except OSError: pass
    return ConversationHandler.END

async def s_search(u,c):
    rows=search_users(u.message.text or "")
    if not rows: await u.message.reply_text("❌ No users found.",reply_markup=back_kb("a_members")); return ConversationHandler.END
    kb=[[ib("👁 View",f"a_viewu_{r[0]}",style="primary"),ib("🚫 Block",f"a_blocku_{r[0]}",style="danger")] for r in rows]
    await u.message.reply_text("\n".join(f"👤 <b>{esc(r[1])}</b> · <code>{r[0]}</code> · @{esc(r[2]) if r[2] else '—'} · {esc(r[4])}" for r in rows),reply_markup=InlineKeyboardMarkup(kb),parse_mode=ParseMode.HTML); return ConversationHandler.END

async def s_usermsg(u,c):
    uid=c.user_data.get("msg_uid")
    try: await c.bot.copy_message(uid,u.message.chat_id,u.message.message_id); await u.message.reply_text("✅ Message sent.",reply_markup=back_kb("a_members"))
    except TelegramError as e: await u.message.reply_text("❌ Send failed: "+esc(sanitize_error(e)),reply_markup=back_kb("a_members"))
    c.user_data.clear(); return ConversationHandler.END

LEGACY_BROADCAST_TASK=None
LEGACY_BROADCAST_LOCK=asyncio.Lock()

def legacy_bcast_create(chat,mid,kind,total):
    bid=uuid.uuid4().hex[:16]
    with db() as con:
        con.execute("INSERT INTO broadcasts VALUES(?,?,?,?,?,?,?,?,?,?,?)",(bid,now_iso(),chat,mid,kind,total,0,0,0,"running",""))
    return bid

def legacy_bcast_update(bid,sent,failed,cancelled,status,err=""):
    with db() as con:
        con.execute("UPDATE broadcasts SET sent=?,failed=?,cancelled=?,status=?,last_error=? WHERE bcast_id=?",(sent,failed,int(cancelled),status,sanitize_error(err),bid))

async def legacy_run_broadcast(bot,msg,bid,recipients,status_msg):
    sent=failed=0; cancelled=False
    try:
        for i,uid in enumerate(recipients,1):
            if asyncio.current_task().cancelled(): cancelled=True; break
            try:
                m=await bot.copy_message(chat_id=uid,from_chat_id=msg.chat_id,message_id=msg.message_id)
                with db() as con: con.execute("INSERT INTO broadcast_msgs VALUES(?,?,?)",(bid,uid,m.message_id))
                sent+=1
            except RetryAfter as e:
                await asyncio.sleep(float(e.retry_after)+0.5)
                try:
                    m=await bot.copy_message(chat_id=uid,from_chat_id=msg.chat_id,message_id=msg.message_id)
                    with db() as con: con.execute("INSERT INTO broadcast_msgs VALUES(?,?,?)",(bid,uid,m.message_id))
                    sent+=1
                except Exception as retry_err:
                    failed+=1; logger.warning("Legacy broadcast retry failed for %s: %s",uid,sanitize_error(retry_err))
            except Forbidden:
                failed+=1; set_status(uid,"inactive")
            except TelegramError as e:
                failed+=1; logger.warning("Legacy broadcast failed for %s: %s",uid,sanitize_error(e))
            if i%20==0 or i==len(recipients):
                try:
                    await status_msg.edit_text(f"📣 <b>Broadcasting...</b>\n\nProgress: <b>{i}/{len(recipients)}</b>\n✅ Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>",reply_markup=InlineKeyboardMarkup([[ib("⏹ Cancel Broadcast",f"a_cancelbc_{bid}",style="danger",emoji_id=EMOJI["❌"])]]),parse_mode=ParseMode.HTML)
                except TelegramError: pass
            await asyncio.sleep(0.08)
    except asyncio.CancelledError:
        cancelled=True
        raise
    finally:
        legacy_bcast_update(bid,sent,failed,cancelled,"cancelled" if cancelled else "completed")
        if not cancelled:
            try:
                await status_msg.edit_text(f"✅ <b>Broadcast Completed</b>\n\nTotal: <b>{len(recipients)}</b>\n✅ Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>",reply_markup=InlineKeyboardMarkup([[ib("🔁 Retry Failed",f"a_retrybc_{bid}",style="success"),ib("🗑 Delete Record",f"a_delbc_{bid}",style="danger")],[ib("🔙 Back","a_bcast")]]),parse_mode=ParseMode.HTML)
            except TelegramError: pass

async def start_bcast(update,ctx):
    global LEGACY_BROADCAST_TASK
    if not is_owner(update.effective_user.id):return ConversationHandler.END
    if gset("broadcast_enabled","1")!="1": await update.message.reply_text("🚫 Broadcast is disabled."); return ConversationHandler.END
    if not (update.message.text or update.message.photo or update.message.video or update.message.document or update.message.audio or update.message.voice or update.message.animation or update.message.sticker or update.message.video_note or update.message.location or update.message.contact or update.message.venue or update.message.poll):
        await update.message.reply_text("❌ Unsupported message type."); return ConversationHandler.END
    if LEGACY_BROADCAST_LOCK.locked():
        await update.message.reply_text("⚠️ Another broadcast is already running."); return ConversationHandler.END
    recipients=users()
    kind=payload_kind(snapshot_message(update.message))
    bid=legacy_bcast_create(update.message.chat_id,update.message.message_id,kind,len(recipients))
    status=await update.message.reply_text(f"⏳ Starting broadcast to <b>{len(recipients)}</b> recipients...",parse_mode=ParseMode.HTML)
    async def runner():
        async with LEGACY_BROADCAST_LOCK:
            await legacy_run_broadcast(ctx.bot,update.message,bid,recipients,status)
    LEGACY_BROADCAST_TASK=asyncio.create_task(runner())
    return ConversationHandler.END

# =========================================================
# Master admin callback (legacy + child system)
# =========================================================

async def admin_cb(update,ctx,callback_data=None):
    global PREMIUM_EMOJI_ENABLED, LEGACY_BROADCAST_TASK
    q=update.callback_query
    if not is_owner(q.from_user.id): await q.answer("❌ Not authorized!",show_alert=True); return ConversationHandler.END
    d=callback_data if callback_data is not None else (q.data or "")
    # owner_clone_callback() owns its callback acknowledgement; do not answer twice.
    if not (d.startswith("clone:") or d.startswith("cr:approve:") or d.startswith("cr:reject:")):
        await q.answer()
    try:
        if d in ("a_back","a_dash"):
            await q.edit_message_text(dash(),reply_markup=admin_kb(),parse_mode=ParseMode.HTML); return ConversationHandler.END
        if d=="a_close": await q.edit_message_text("❌ Admin panel closed."); return ConversationHandler.END
        if d=="a_chs": await q.edit_message_text("📢 <b>CHANNEL MANAGEMENT</b>\n\n"+"\n".join(f"{i}. {'🟢' if r[6] else '🔴'} <b>{esc(r[2])}</b>\nID: <code>{esc(r[1])}</code>\nLink: {esc(r[3])}" for i,r in enumerate(channels(True),1)) or "No channels configured.",reply_markup=ch_kb(),parse_mode=ParseMode.HTML); return ConversationHandler.END
        if d=="a_addch": await q.edit_message_text("📢 <b>Add Channel</b>\n\nSend Channel ID.",parse_mode=ParseMode.HTML); return S_CH_ID
        if d.startswith("a_delc_"):
            cid=int(d.split("_")[-1]); ctx.user_data["confirm"]=('delc',cid); await q.edit_message_text("⚠️ <b>Are you sure?</b>\n\nDelete this channel?",reply_markup=InlineKeyboardMarkup([[ib("✅ Confirm",f"a_confirm_{cid}",style="danger"),ib("❌ Cancel","a_chs")]]),parse_mode=ParseMode.HTML); return ConversationHandler.END
        if d.startswith("a_confirm_"):
            cid=int(d.split("_")[-1]); a,t=ctx.user_data.pop("confirm",("",None));
            if a=="delc" and t==cid: delete_channel(cid)
            return await admin_cb(type("Obj",(),{"callback_query":type("Q",(),{"data":"a_chs","from_user":q.from_user,"edit_message_text":q.edit_message_text,"answer":q.answer})()})(),ctx)
        if d.startswith("a_left_"): move_channel(int(d.split("_")[-1]),"left"); return await admin_cb(update,ctx,callback_data="a_chs")
        if d.startswith("a_right_"): move_channel(int(d.split("_")[-1]),"right"); return await admin_cb(update,ctx,callback_data="a_chs")
        if d.startswith("a_togglec_"): toggle_channel(int(d.split("_")[-1])); return await admin_cb(update,ctx,callback_data="a_chs")
        if d.startswith("a_testc_"):
            r=channel(int(d.split("_")[-1])); ok,adm,title,status=await channel_status(ctx.bot,r[1]); await q.edit_message_text(f"🧪 <b>Channel Status</b>\n\nName: <b>{esc(r[2])}</b>\nAccessible: {'✅ YES' if ok else '❌ NO'}\nBot Admin: {'✅ YES' if adm else '❌ NO'}\nStatus: <code>{esc(status)}</code>",reply_markup=back_kb("a_chs"),parse_mode=ParseMode.HTML); return ConversationHandler.END
        if d.startswith("a_editc_"): ctx.user_data["edit_channel"]=int(d.split("_")[-1]); await q.edit_message_text("✏️ Send new channel name:"); return S_EDITNAME
        if d=="a_msgs": await q.edit_message_text("📝 <b>MESSAGE MANAGEMENT</b>\n\nHTML formatting is preserved.",reply_markup=msg_kb(),parse_mode=ParseMode.HTML); return ConversationHandler.END
        mm={"a_top":("top","Top Message",S_TOP),"a_welcome":("welcome","Welcome Message",S_WELCOME),"a_postjoin":("postjoin","Post-Join Message",S_POSTJOIN),"a_btn1":("btn1_msg",BTN1,S_BTN1),"a_btn2":("btn2_msg",BTN2,S_BTN2),"a_btn3":("btn3_msg",BTN3,S_BTN3)}
        if d in mm:
            k,n,s=mm[d]; await q.edit_message_text(f"✏️ <b>{esc(n)}</b>\n\nCurrent:\n<pre>{esc(gset(k)[:1500])}</pre>\n\nSend new text.\n\nUse /cancel to abort.",parse_mode=ParseMode.HTML); return s
        if d=="a_welcome_photo": await q.edit_message_text("🖼 <b>Welcome Photo</b>\n\nSend a photo or <code>clear</code>.",parse_mode=ParseMode.HTML); return S_WELCOME_PHOTO
        if d=="a_buttons": await q.edit_message_text("🎨 <b>BUTTON MANAGEMENT</b>",reply_markup=btn_kb(),parse_mode=ParseMode.HTML); return ConversationHandler.END
        if d.startswith("a_style_"):
            k=d[len("a_style_"):]; n={"primary":"success","success":"danger","danger":"primary"}[bstyle(k)]; sset("button_style_"+k,n); await q.edit_message_text("🎨 Button style updated.",reply_markup=btn_kb()); return ConversationHandler.END
        if d.startswith("a_emoji_"):
            k=d[len("a_emoji_"):]; sset("button_emoji_enabled_"+k,"0" if bemoji(k) else "1"); await q.edit_message_text("🧩 Button emoji setting updated.",reply_markup=btn_kb()); return ConversationHandler.END
        if d.startswith("a_reset_"):
            k=d[len("a_reset_"):]; defs={"btn1":("primary",EMOJI["🎯"]),"btn2":("success",EMOJI["📊"]),"btn3":("primary",EMOJI["🤝"])}; st,e=defs[k]; sset("button_style_"+k,st); sset("button_emoji_"+k,e); sset("button_emoji_enabled_"+k,"1"); await q.edit_message_text("↩️ Button reset.",reply_markup=btn_kb()); return ConversationHandler.END
        if d.startswith("a_preview_"):
            k=d[len("a_preview_"):]; n={"btn1":BTN1,"btn2":BTN2,"btn3":BTN3}[k]; await q.message.reply_text("🎨 Preview",reply_markup=InlineKeyboardMarkup([[ib(n,"preview_"+k,style=bstyle(k),emoji_id=bemoji(k))]])); return ConversationHandler.END
        if d.startswith("a_setemoji_"): ctx.user_data["emoji_key"]=d[len("a_setemoji_"):]; await q.edit_message_text("🆔 Send numeric Telegram custom emoji ID.\nSend <code>clear</code> to disable.",parse_mode=ParseMode.HTML); return S_EMOJI
        if d=="a_premium_test": await premium_test(ctx.bot,q.message.chat_id); return ConversationHandler.END
        if d=="a_members": await q.edit_message_text(f"👥 <b>MEMBERS</b>\n\nTotal Users: <b>{scalar('SELECT COUNT(*) FROM users')}</b>",reply_markup=InlineKeyboardMarkup([[ib("🔎 Search User","a_search")],[ib("📤 Export Users","a_export")],[ib("🔙 Back","a_back")]]),parse_mode=ParseMode.HTML); return ConversationHandler.END
        if d=="a_search": await q.edit_message_text("🔎 Send User ID, username, or name:"); return S_SEARCH
        if d=="a_export":
            p=Path(tempfile.gettempdir())/f"users_{int(time.time())}.csv"
            with db() as con, p.open("w",newline="",encoding="utf-8") as f:
                w=csv.writer(f); w.writerow(["user_id","first_name","username","join_date","status"]); w.writerows(con.execute("SELECT user_id,first_name,username,joined_at,status FROM users ORDER BY joined_at"))
            try:
                with p.open("rb") as f: await q.message.reply_document(f,filename=p.name)
            finally: p.unlink(missing_ok=True)
            return ConversationHandler.END
        if d=="a_backup_menu":
            names="\n".join("• <code>"+esc(p.name)+"</code>" for p in backup_list()[:10]) or "No backups."; await q.edit_message_text("💾 <b>BACKUP CENTER</b>\n\n"+names,reply_markup=InlineKeyboardMarkup([[ib("💾 Create Backup","a_backup",style="success")],[ib("📤 Download Latest","a_download")],[ib("🗑 Delete Old Backups","a_cleanup",style="danger")],[ib("🔙 Back","a_back")]]),parse_mode=ParseMode.HTML); return ConversationHandler.END
        if d=="a_backup":
            try:
                p=create_backup();
                with p.open("rb") as f: await q.message.reply_document(f,filename=p.name,caption="💾 Backup Ready")
            except Exception: await q.answer("Backup failed.",show_alert=True)
            return ConversationHandler.END
        if d=="a_download":
            ps=backup_list();
            if not ps:return await q.answer("No backups.",show_alert=True)
            with ps[0].open("rb") as f: await q.message.reply_document(f,filename=ps[0].name)
            return ConversationHandler.END
        if d=="a_cleanup": await q.edit_message_text("⚠️ Delete old backups and keep only latest?",reply_markup=confirm_cancel_keyboard("a_cleanup_confirm","a_backup_menu")); return ConversationHandler.END
        if d=="a_cleanup_confirm":
            for p in backup_list()[1:]: p.unlink(missing_ok=True)
            await q.edit_message_text("🗑 Old backups deleted.",reply_markup=back_kb("a_backup_menu")); return ConversationHandler.END
        if d=="a_restore": await q.edit_message_text("⚠️ <b>Safe Restore</b>\n\nSend the backup <code>.zip</code> file.\nA safety backup is created first.",reply_markup=confirm_cancel_keyboard("a_restore_confirm","a_back"),parse_mode=ParseMode.HTML); return ConversationHandler.END
        if d=="a_restore_confirm": await q.edit_message_text("♻️ <b>Safe Restore</b>\n\nSend the backup <code>.zip</code> file.",parse_mode=ParseMode.HTML); return S_RESTORE
        if d=="a_dbhealth": await q.edit_message_text(dbhealth(),reply_markup=back_kb(),parse_mode=ParseMode.HTML); return ConversationHandler.END
        if d=="a_health": await q.edit_message_text(health(),reply_markup=back_kb(),parse_mode=ParseMode.HTML); return ConversationHandler.END
        if d=="a_settings": await q.edit_message_text("⚙️ <b>GLOBAL SETTINGS</b>\n\nAuto backup: "+gset("auto_backup_frequency","daily")+" | Keep: "+gset("auto_backup_keep","7"),reply_markup=settings_kb(),parse_mode=ParseMode.HTML); return ConversationHandler.END
        if d.startswith("a_toggle_"):
            k=d[len("a_toggle_"):]; sset(k,"0" if gset(k,"0")=="1" else "1"); await q.edit_message_text("⚙️ Setting updated.",reply_markup=settings_kb()); return ConversationHandler.END
        if d=="a_autobackup": sset("auto_backup_frequency","weekly" if gset("auto_backup_frequency","daily")=="daily" else "daily"); await q.edit_message_text("⚙️ Auto backup schedule updated.",reply_markup=settings_kb()); return ConversationHandler.END
        if d=="a_errors":
            with db() as con: rows=con.execute("SELECT created_at,level,message FROM error_logs ORDER BY id DESC LIMIT 20").fetchall()
            text="📜 <b>RECENT ERRORS</b>\n\n"+"\n".join(f"<code>{esc(r[0])}</code> · <b>{esc(r[1])}</b> · {esc(r[2])}" for r in rows) if rows else "📜 <b>RECENT ERRORS</b>\n\nNo errors."
            await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup([[ib("🧹 Clear Logs","a_clearlogs_confirm",style="danger")],[ib("🔙 Back","a_back")]]),parse_mode=ParseMode.HTML); return ConversationHandler.END
        if d=="a_clearlogs_confirm": await q.edit_message_text("⚠️ Are you sure?",reply_markup=confirm_cancel_keyboard("a_clearlogs","a_errors")); return ConversationHandler.END
        if d=="a_clearlogs":
            with db() as con: con.execute("DELETE FROM error_logs")
            await q.edit_message_text("🧹 Error log cleared.",reply_markup=back_kb()); return ConversationHandler.END

        if d.startswith("a_cancelbc_"):
            global LEGACY_BROADCAST_TASK
            if LEGACY_BROADCAST_TASK and not LEGACY_BROADCAST_TASK.done():
                LEGACY_BROADCAST_TASK.cancel()
                try: await LEGACY_BROADCAST_TASK
                except asyncio.CancelledError: pass
            bid=d[len("a_cancelbc_"):]; legacy_bcast_update(bid,0,0,True,"cancelled")
            await q.edit_message_text("⏹ Broadcast cancelled.",reply_markup=back_kb("a_bcast")); return ConversationHandler.END
        if d.startswith("child:usersearch:"):
            cid=int(d.split(":")[-1]); ctx.user_data["child_search_id"]=cid; await q.edit_message_text("🔎 Send user ID, username, or name.",reply_markup=back_kb(f"cb:users:{cid}")); return S_CHILD_SEARCH
        if d.startswith("child:export:"):
            cid=int(d.split(":")[-1]); p=Path(tempfile.gettempdir())/f"child_users_{cid}_{int(time.time())}.csv"
            with db() as con,p.open("w",newline="",encoding="utf-8") as f:
                w=csv.writer(f); w.writerow(["user_id","first_name","username","joined_at","status","last_seen"]); w.writerows(con.execute("SELECT user_id,first_name,username,joined_at,status,last_seen FROM child_bot_users WHERE child_bot_id=? ORDER BY joined_at",(cid,)))
            try:
                with p.open("rb") as f: await q.message.reply_document(f,filename=p.name)
            finally: p.unlink(missing_ok=True)
            return ConversationHandler.END
        if d.startswith("child:view:"):
            cid,uid=map(int,d.split(":")[-2:]); r=None
            with db() as con:r=con.execute("SELECT user_id,first_name,username,joined_at,status,last_seen FROM child_bot_users WHERE child_bot_id=? AND user_id=?",(cid,uid)).fetchone()
            if not r:return await q.answer("User not found",show_alert=True)
            await q.edit_message_text(f"👤 <b>CHILD USER</b>\n\nID: <code>{r[0]}</code>\nName: <b>{esc(r[1])}</b>\nUsername: <b>@{esc(r[2]) if r[2] else '—'}</b>\nJoined: <code>{esc(r[3])}</code>\nStatus: <b>{esc(r[4])}</b>\nLast seen: <code>{esc(r[5] or '—')}</code>",reply_markup=InlineKeyboardMarkup([[ib("🚫 Block",f"child:block:{cid}:{uid}",style="danger"),ib("✅ Unblock",f"child:unblock:{cid}:{uid}",style="success")],[ib("💬 Message",f"child:msg:{cid}:{uid}")],[ib("🔙 Back",f"cb:users:{cid}")]]),parse_mode=ParseMode.HTML); return ConversationHandler.END
        if d.startswith("child:block:"):
            cid,uid=map(int,d.split(":")[-2:]); child_set_status(cid,uid,"blocked"); return await q.edit_message_text("🚫 Child user blocked.",reply_markup=back_kb(f"cb:users:{cid}"))
        if d.startswith("child:unblock:"):
            cid,uid=map(int,d.split(":")[-2:]); child_set_status(cid,uid,"active"); return await q.edit_message_text("✅ Child user unblocked.",reply_markup=back_kb(f"cb:users:{cid}"))
        if d.startswith("child:msg:"):
            cid,uid=map(int,d.split(":")[-2:]); ctx.user_data["child_msg_target"]=(cid,uid); await q.edit_message_text("💬 Send the message to deliver to this child user.",reply_markup=back_kb(f"cb:users:{cid}")); return S_CHILD_USERMSG
        if d.startswith("a_delbc_"):
            bid=d[len("a_delbc_"):]
            with db() as con: rows=con.execute("SELECT user_id,message_id FROM broadcast_msgs WHERE bcast_id=?",(bid,)).fetchall()
            removed=0
            for uid,mid in rows:
                try: await ctx.bot.delete_message(uid,mid); removed+=1
                except TelegramError: pass
            with db() as con: con.execute("DELETE FROM broadcast_msgs WHERE bcast_id=?",(bid,)); con.execute("DELETE FROM broadcasts WHERE bcast_id=?",(bid,))
            await q.edit_message_text(f"🗑 Broadcast record deleted. Removed from <b>{removed}</b> chats.",reply_markup=back_kb("a_bcast"),parse_mode=ParseMode.HTML); return ConversationHandler.END
        if d.startswith("a_retrybc_"):
            bid=d[len("a_retrybc_"):]
            with db() as con:
                row=con.execute("SELECT source_chat_id,source_message_id FROM broadcasts WHERE bcast_id=?",(bid,)).fetchone()
                sent_ids={r[0] for r in con.execute("SELECT user_id FROM broadcast_msgs WHERE bcast_id=?",(bid,)).fetchall()}
            if not row:return await q.answer("Broadcast not found.",show_alert=True)
            failed_ids=[uid for uid in users() if uid not in sent_ids]
            if not failed_ids:return await q.answer("No failed recipients found.",show_alert=True)
            if LEGACY_BROADCAST_LOCK.locked():return await q.answer("Another broadcast is already running.",show_alert=True)
            source=type("Source",(),{"chat_id":row[0],"message_id":row[1]})()
            retry_id=legacy_bcast_create(row[0],row[1],"retry",len(failed_ids)); status_msg=await q.message.reply_text(f"🔁 Retrying <b>{len(failed_ids)}</b> failed recipients...",parse_mode=ParseMode.HTML)
            async def retry_runner():
                async with LEGACY_BROADCAST_LOCK: await legacy_run_broadcast(ctx.bot,source,retry_id,failed_ids,status_msg)
            LEGACY_BROADCAST_TASK=asyncio.create_task(retry_runner()); return ConversationHandler.END
        # Child system screens.
        if d=="a_child": return await child_master_menu(q)
        if d=="a_child_requests": return await child_requests_menu(q)
        if d=="a_child_stats": return await child_stats_screen(q)
        if d=="a_child_health":
            await q.edit_message_text("🩺 <b>CHILD BOT HEALTH</b>\n\n"+"\n".join(f"{('🟢' if r[6]=='RUNNING' else '🔴' if r[6]=='STOPPED' else '⚠️')} @{esc(r[2]) or r[1]} — <b>{esc(r[6])}</b> — heartbeat <code>{esc(r[9] or '—')}</code>" for r in MANAGER.get_all_child_bots()) or "No child bots.",reply_markup=back_kb("a_child"),parse_mode=ParseMode.HTML); return ConversationHandler.END
        if d=="a_child_settings":
            await q.edit_message_text("⚙️ <b>CHILD BOT SETTINGS</b>\n\nSelect a child bot to manage its per-bot configuration.",reply_markup=InlineKeyboardMarkup([[ib("🤖 Select Child Bot","cb:list:all",style="primary")],[ib("🔙 Back","a_child")]]),parse_mode=ParseMode.HTML); return ConversationHandler.END
        if d.startswith("cr:view:"): return await request_view_callback(q, d.split(":",2)[2])
        if d.startswith("cr:approve:"): return await owner_clone_callback(update,ctx,callback_data=f"clone:approve:{d.split(":",2)[2]}")
        if d.startswith("cr:reject:"): return await owner_clone_callback(update,ctx,callback_data=f"clone:reject:{d.split(":",2)[2]}")
        if d.startswith("cb:list:"): return await child_manage_list(q)
        if d.startswith("cb:select:"): return await child_select_screen(q,int(d.split(":")[-1]))
        if d.startswith("cb:start:"): await MANAGER.start_child_bot(int(d.split(":")[-1])); return await child_select_screen(q,int(d.split(":")[-1]))
        if d.startswith("cb:stop:"): await MANAGER.stop_child_bot(int(d.split(":")[-1])); return await child_select_screen(q,int(d.split(":")[-1]))
        if d.startswith("cb:restart:"): await MANAGER.restart_child_bot(int(d.split(":")[-1])); return await child_select_screen(q,int(d.split(":")[-1]))
        if d.startswith("cb:remove:"):
            cid=int(d.split(":")[-1]); await q.edit_message_text("⚠️ <b>REMOVE CHILD BOT</b>\n\nThis stops its runtime and marks it REMOVED. Data is retained in the database.\n\nConfirm?",reply_markup=confirm_cancel_keyboard(f"cb:removeconfirm:{cid}",f"cb:select:{cid}"),parse_mode=ParseMode.HTML); return ConversationHandler.END
        if d.startswith("cb:removeconfirm:"): await MANAGER.remove_child_bot(int(d.split(":")[-1])); return await child_manage_list(q)
        if d.startswith("cb:health:"): return await child_health_screen(q,int(d.split(":")[-1]))
        if d.startswith("cb:logs:"):
            cid=int(d.split(":")[-1])
            with db() as con:
                log_rows=con.execute(
                    "SELECT created_at,level,message FROM child_bot_logs WHERE child_bot_id=? ORDER BY id DESC LIMIT 20",
                    (cid,),
                ).fetchall()
            text = "📜 <b>CHILD BOT LOGS</b>\n\n" + (
                "\n".join(
                    f"<code>{esc(r[0])}</code> · <b>{esc(r[1])}</b> · {esc(r[2])}"
                    for r in log_rows
                ) if log_rows else "No logs."
            )
            await q.edit_message_text(text,reply_markup=back_kb(f"cb:health:{cid}"),parse_mode=ParseMode.HTML)
            return ConversationHandler.END
        if d.startswith("cb:users:"): return await child_users_screen(q,int(d.split(":")[-1]))
        if d.startswith("cb:channels:"): return await child_channels_screen(q,int(d.split(":")[-1]))
        if d.startswith("child:chadd:"):
            cid=int(d.split(":")[-1])
            ctx.user_data["child_channel_child_id"]=cid
            await q.edit_message_text(
                "📢 <b>Add Child Channel</b>\n\nSend Channel ID.",
                reply_markup=cancel_kb("bcast:cancel"),
                parse_mode=ParseMode.HTML,
            )
            return S_CHILD_CHANNEL_ID
        if d.startswith("child:chtoggle:"):
            cid,rowid=map(int,d.split(":")[-2:]);
            with db() as con: con.execute("UPDATE child_bot_channels SET enabled=1-enabled WHERE child_bot_id=? AND id=?",(cid,rowid))
            return await child_channels_screen(q,cid)
        if d.startswith("child:chedit:"):
            cid,rowid=map(int,d.split(":")[-2:]); ctx.user_data["child_channel_edit"]=(cid,rowid); await q.edit_message_text("✏️ Send new channel name.",reply_markup=back_kb(f"cb:channels:{cid}")); return S_CHILD_CHANNEL_NAME
        if d.startswith("child:chmove:"):
            parts=d.split(":"); cid=int(parts[-3]); rowid=int(parts[-2]); direction=parts[-1]
            rows=child_rows(cid); ids=[r[0] for r in rows]
            if rowid in ids:
                i=ids.index(rowid); j=i-1 if direction=="up" else i+1
                if 0<=j<len(ids):
                    with db() as con:
                        con.execute("UPDATE child_bot_channels SET order_num=? WHERE id=?",(rows[j][5],rows[i][0])); con.execute("UPDATE child_bot_channels SET order_num=? WHERE id=?",(rows[i][5],rows[j][0]))
            return await child_channels_screen(q,cid)
        if d.startswith("child:chdelete:"):
            cid,rowid=map(int,d.split(":")[-2:]);
            with db() as con: con.execute("DELETE FROM child_bot_channels WHERE child_bot_id=? AND id=?",(cid,rowid))
            return await child_channels_screen(q,cid)
        if d.startswith("cb:buttons:"): return await child_buttons_screen(q,int(d.split(":")[-1]))
        if d.startswith("child:btnreset:"):
            cid=int(d.split(":")[-1]); init_child_defaults(cid); return await child_buttons_screen(q,cid)
        if d.startswith("child:btnedit:"):
            cid=int(d.split(":")[-2]); key=d.split(":")[-1]; ctx.user_data["child_button_edit"]=(cid,key); await q.edit_message_text("✏️ Send: text | callback_data | url | style | emoji_id | enabled(1/0) | row | position\n\nExample: Join | join | https://t.me/example | primary | 522... | 1 | 0 | 0",reply_markup=back_kb(f"cb:buttons:{cid}")); return S_CHILD_MSG
        if d.startswith("child:btnpreview:"):
            cid,key=d.split(":")[-2],d.split(":")[-1]
            with db() as con: rr=con.execute("SELECT text,callback_data,url,style,emoji_id FROM child_bot_buttons WHERE child_bot_id=? AND button_key=?",(int(cid),key)).fetchone()
            if not rr:return await q.answer("Button not found.",show_alert=True)
            await q.message.reply_text("🎨 <b>Button Preview</b>",reply_markup=InlineKeyboardMarkup([[ib(rr[0],callback_data=rr[1] or "preview",url=rr[2] or None,style=rr[3],emoji_id=rr[4] or None)]]),parse_mode=ParseMode.HTML); return ConversationHandler.END
        if d.startswith("child:btntoggle:"):
            cid,key=d.split(":")[-2],d.split(":")[-1];
            with db() as con: con.execute("UPDATE child_bot_buttons SET enabled=1-enabled WHERE child_bot_id=? AND button_key=?",(int(cid),key))
            return await child_buttons_screen(q,int(cid))
        if d.startswith("cb:admins:"): return await child_admin_screen(q,int(d.split(":")[-1]))
        if d.startswith("cb:broadcast:"): ctx.user_data["selected_child_id"]=int(d.split(":")[-1]); ctx.user_data["broadcast_buttons"]=[]; await q.edit_message_text("📣 <b>CHILD BOT BROADCAST</b>\n\nSend the content message now.\nSupported: text, photo, video, document, audio, voice, animation, sticker, video note, location, contact, venue; albums where practical.\n\n[❌ Cancel]",parse_mode=ParseMode.HTML); return S_MASTER_BCAST_CONTENT
        if d.startswith("cb:cancelbc:"):
            bid=d.split(":")[-1]
            ok=await MANAGER.cancel_broadcast(bid)
            if not ok:
                return await q.answer("Broadcast is already finished or not found.",show_alert=True)
            await q.edit_message_text("⏹ Broadcast cancellation requested.",reply_markup=back_kb("a_child")); return ConversationHandler.END
        if d.startswith("cb:retryb:"):
            parts=d.split(":"); cid=int(parts[-2]); bid=parts[-1]
            data=MANAGER.failed_recipients(bid)
            if not data:return await q.answer("Broadcast record not found.",show_alert=True)
            child_id,payload,buttons,failed_ids=data
            if not failed_ids:return await q.answer("No failed recipients remain.",show_alert=True)
            retry_payload=dict(payload); retry_payload["_retry_recipient_ids"]=failed_ids
            status=await q.message.reply_text(f"⏳ Retrying <b>{len(failed_ids)}</b> failed recipients...",parse_mode=ParseMode.HTML)
            # Temporary restriction to failed IDs is carried through the payload and consumed by task runner.
            retry_bid=uuid.uuid4().hex[:16]
            with db() as con:
                con.execute("INSERT INTO child_bot_broadcasts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(retry_bid,child_id,0,0,payload_kind(payload)+"_retry",len(failed_ids),0,0,0,"running",now_iso(),"",json.dumps(payload,separators=(",",":")),json.dumps(buttons,separators=(",",":"))))
            async def retry_task():
                app=MANAGER.apps.get(child_id)
                if not app: await MANAGER.start_child_bot(child_id); app=MANAGER.apps.get(child_id)
                if not app: raise RuntimeError("Child bot is not running")
                sent=failed=0
                try:
                    for i,uid in enumerate(failed_ids,1):
                        try:
                            m=await send_payload(app.bot,uid,payload,buttons); mid=getattr(m,"message_id",0)
                            with db() as con: con.execute("INSERT OR IGNORE INTO child_bot_broadcast_messages VALUES(?,?,?)",(retry_bid,uid,mid))
                            sent+=1
                        except RetryAfter as e:
                            await asyncio.sleep(float(e.retry_after)+0.5)
                            try:
                                m=await send_payload(app.bot,uid,payload,buttons); mid=getattr(m,"message_id",0)
                                with db() as con: con.execute("INSERT OR IGNORE INTO child_bot_broadcast_messages VALUES(?,?,?)",(retry_bid,uid,mid))
                                sent+=1
                            except Exception: failed+=1
                        except Forbidden: failed+=1; child_set_status(child_id,uid,"inactive")
                        except TelegramError as e: failed+=1; logger.warning("Retry broadcast %s/%s: %s",child_id,uid,sanitize_error(e))
                        if i%10==0 or i==len(failed_ids):
                            try: await status.edit_text(f"🔁 <b>Retrying...</b>\n\nProgress: <b>{i}/{len(failed_ids)}</b>\n✅ Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>",parse_mode=ParseMode.HTML)
                            except TelegramError: pass
                        await asyncio.sleep(0.08)
                except asyncio.CancelledError:
                    with db() as con: con.execute("UPDATE child_bot_broadcasts SET status='cancelled',cancelled=1,completed_at=? WHERE broadcast_id=?",(now_iso(),retry_bid))
                    raise
                status_text="completed"
                with db() as con: con.execute("UPDATE child_bot_broadcasts SET sent=?,failed=?,status=?,completed_at=? WHERE broadcast_id=?",(sent,failed,status_text,now_iso(),retry_bid))
                try: await status.edit_text(f"✅ <b>RETRY COMPLETED</b>\n\nTotal: <b>{len(failed_ids)}</b>\n✅ Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>",reply_markup=back_kb(f"cb:select:{child_id}"),parse_mode=ParseMode.HTML)
                except TelegramError: pass
            task=asyncio.create_task(retry_task()); MANAGER.broadcast_tasks[retry_bid]=task; task.add_done_callback(lambda _: MANAGER.broadcast_tasks.pop(retry_bid,None))
            return ConversationHandler.END
        if d.startswith("cb:adminchange:"): ctx.user_data["child_admin_target"]=int(d.split(":")[-1]); await q.edit_message_text("👑 Send Admin ID.",reply_markup=back_kb("a_child")); return S_CHILD_ADMIN_ID
        if d=="cb:global": ctx.user_data["global_child_selection"]=[]; return await global_child_select_screen(q,ctx)
        if d.startswith("cb:globtoggle:"):
            cid=int(d.split(":")[-1]); selected=set(ctx.user_data.get("global_child_selection",[])); selected.remove(cid) if cid in selected else selected.add(cid); ctx.user_data["global_child_selection"]=list(selected); return await global_child_select_screen(q,ctx)
        if d=="cb:globaldone":
            if not ctx.user_data.get("global_child_selection"): return await q.answer("Select at least one child bot.",show_alert=True)
            await q.edit_message_text("📣 <b>GLOBAL CHILD BROADCAST</b>\n\nSend the content message now.",parse_mode=ParseMode.HTML); return S_MASTER_GLOBAL_CONTENT
        if d.startswith("bcast:buttons:"): return await master_bcast_buttons_menu(q,ctx)
        if d=="bcast:add": await q.edit_message_text("🔘 <b>BUTTON BUILDER</b>\n\nSend Button Name.",parse_mode=ParseMode.HTML); return S_MASTER_BCAST_BUTTON_NAME
        if d.startswith("bcast:preview"): return await master_bcast_preview(q,ctx)
        if d=="bcast:done": return await master_bcast_confirm(q,ctx)
        if d=="bcast:cancel": ctx.user_data.clear(); await q.edit_message_text("❌ Broadcast cancelled."); return ConversationHandler.END
        if d=="bcast:edit": await q.edit_message_text("✏️ Resend the content message to replace the current broadcast.",parse_mode=ParseMode.HTML); return S_MASTER_BCAST_CONTENT
        if d.startswith("cadmin:add:"):
            ctx.user_data["admin_child_id"]=int(d.split(":")[-1]); await q.edit_message_text("➕ Send admin Telegram user ID.",reply_markup=back_kb("a_child")); return S_CHILD_ADMIN_ID
        if d.startswith("cadmin:remove:"):
            cid=int(d.split(":")[-2]); uid=int(d.split(":")[-1]); remove_child_admin(cid,uid); return await child_admin_screen(q,cid)
        if d.startswith("cadmin:list:"): return await child_admin_screen(q,int(d.split(":")[-1]))
        if d.startswith("cadmin:owner:"): return await child_select_screen(q,int(d.split(":")[-1]))
        # Request button callbacks generated directly from clone flow.
        if d.startswith("clone:"): return await owner_clone_callback(update,ctx)
        return ConversationHandler.END
    except Exception as e:
        logger.exception("Admin callback failed"); await q.answer("Something went wrong.",show_alert=True); log_error("ERROR",f"Admin callback: {e}"); return ConversationHandler.END

# =========================================================
# Child master UI helpers
# =========================================================

async def child_master_menu(q):
    await q.edit_message_text("🤖 <b>CHILD BOTS</b>",reply_markup=InlineKeyboardMarkup([[ib("🤖 All Child Bots","cb:list:all",style="primary")],[ib("📥 Pending Requests","a_child_requests",style="success")],[ib("📊 Child Bot Statistics","a_child_stats",style="primary")],[ib("📣 Global Child Broadcast","cb:global",style="success")],[ib("🩺 Child Bot Health","a_child_health",style="primary")],[ib("⚙️ Child Bot Settings","a_child_settings",style="primary")],[ib("🔙 Back","a_back")]]),parse_mode=ParseMode.HTML)
    return ConversationHandler.END

async def child_requests_menu(q):
    with db() as con: rows=con.execute("SELECT request_id,requester_name,requester_username,requester_user_id,created_at,status FROM clone_requests WHERE status='pending' ORDER BY id DESC LIMIT 25").fetchall()
    buttons=[]
    text="📥 <b>PENDING REQUESTS</b>\n\n"
    for r in rows:
        text+=f"👤 <b>{esc(r[1])}</b> · @{esc(r[2]) if r[2] else '—'}\nID: <code>{r[3]}</code> · Req: <code>{esc(r[0])}</code>\nDate: <code>{esc(r[4])}</code>\n\n"
        buttons.append([ib("👁 View",f"cr:view:{r[0]}"),ib("✅ Approve",f"cr:approve:{r[0]}",style="success"),ib("❌ Reject",f"cr:reject:{r[0]}",style="danger")])
    buttons.append([ib("🔙 Back","a_child")])
    await q.edit_message_text(text+"No pending requests." if not rows else text,reply_markup=InlineKeyboardMarkup(buttons),parse_mode=ParseMode.HTML)
    return ConversationHandler.END

async def request_view_callback(q,rid):
    r=get_request(rid)
    if not r:return await q.answer("Request not found",show_alert=True)
    text = ("🤖 <b>REQUEST</b>\n\n"
            f"Name: <b>{esc(r[3])}</b>\n"
            f"Username: @{esc(r[4]) if r[4] else '—'}\n"
            f"User ID: <code>{r[2]}</code>\n"
            f"Request ID: <code>{esc(r[1])}</code>\n"
            f"Status: <b>{esc(r[5])}</b>\n"
            f"Created: <code>{esc(r[8])}</code>")
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([
        [ib("✅ Approve", f"clone:approve:{rid}", style="success"), ib("❌ Reject", f"clone:reject:{rid}", style="danger")],
        [ib("🔙 Back", "a_child_requests")]
    ]), parse_mode=ParseMode.HTML)
    return ConversationHandler.END

async def child_manage_list(q):
    rows=MANAGER.get_all_child_bots(); buttons=[]; text="🤖 <b>CHILD BOTS</b>\n\n"
    for r in rows:
        cid,status=r[0],r[6]; icon={"RUNNING":"🟢","STOPPED":"🔴","ERROR":"⚠️","TOKEN_INVALID":"🔐","CONFIGURING":"🟡","DISABLED":"⚫","REMOVED":"🗑"}.get(status,"⚪")
        text+=f"{icon} <b>@{esc(r[2]) or '—'}</b> ID:<code>{r[1]}</code> Owner:<code>{r[5]}</code> Status:<b>{esc(status)}</b>\n"
        buttons.append([ib("⚙️ Manage",f"cb:select:{cid}"),ib("🩺 Health",f"cb:health:{cid}")])
    buttons.append([ib("🔙 Back","a_child")]); await q.edit_message_text(text+"No child bots." if not rows else text,reply_markup=InlineKeyboardMarkup(buttons),parse_mode=ParseMode.HTML); return ConversationHandler.END

async def child_select_screen(q,cid):
    r=child_get(cid)
    if not r:return await q.answer("Child bot not found",show_alert=True)
    users_n=scalar("SELECT COUNT(*) FROM child_bot_users WHERE child_bot_id=?",(cid,)); bcasts=scalar("SELECT COUNT(*) FROM child_bot_broadcasts WHERE child_bot_id=?",(cid,)); chans=scalar("SELECT COUNT(*) FROM child_bot_channels WHERE child_bot_id=?",(cid,)); admins=child_admins(cid)
    icon={"RUNNING":"🟢","STOPPED":"🔴","ERROR":"⚠️","TOKEN_INVALID":"🔐","CONFIGURING":"🟡"}.get(r[7],"⚪")
    action_start=f"cb:stop:{cid}" if r[7]=="RUNNING" else f"cb:start:{cid}"
    action_label="🔴 Stop" if r[7]=="RUNNING" else "🟢 Start"
    await q.edit_message_text(f"🤖 <b>@{esc(r[2]) or '—'}</b>\n\nBot ID: <code>{r[1]}</code>\nOwner: <code>{r[6]}</code>\nStatus: {icon} <b>{esc(r[7])}</b>\nUsers: <b>{users_n}</b>\nChannels: <b>{chans}</b>\nBroadcasts: <b>{bcasts}</b>\nAdmins: <b>{len(admins)}</b>\nLast heartbeat: <code>{esc(r[13] or '—')}</code>\nLast error: <code>{esc(r[12] or 'None')}</code>",reply_markup=InlineKeyboardMarkup([[ib("📣 Broadcast",f"cb:broadcast:{cid}",style="success"),ib(action_label,action_label and action_start,style="danger" if r[7]=="RUNNING" else "success")],[ib("🔄 Restart",f"cb:restart:{cid}")],[ib("👥 Users",f"cb:users:{cid}"),ib("📢 Channels",f"cb:channels:{cid}")],[ib("🎨 Buttons",f"cb:buttons:{cid}"),ib("👑 Admins",f"cb:admins:{cid}")],[ib("🩺 Health",f"cb:health:{cid}"),ib("🗑 Remove",f"cb:remove:{cid}",style="danger")],[ib("🔙 Back","cb:list:all")]]),parse_mode=ParseMode.HTML); return ConversationHandler.END

async def child_health_screen(q,cid):
    r=child_get(cid)
    if not r:return await q.answer("Not found",show_alert=True)
    await q.edit_message_text(f"🩺 <b>CHILD BOT HEALTH</b>\n\nBot: <b>@{esc(r[2]) or '—'}</b>\nStatus: <b>{esc(r[7])}</b>\nLast heartbeat: <code>{esc(r[13] or '—')}</code>\nUsers: <b>{scalar('SELECT COUNT(*) FROM child_bot_users WHERE child_bot_id=?',(cid,))}</b>\nChannels: <b>{scalar('SELECT COUNT(*) FROM child_bot_channels WHERE child_bot_id=?',(cid,))}</b>\nBroadcasts: <b>{scalar('SELECT COUNT(*) FROM child_bot_broadcasts WHERE child_bot_id=?',(cid,))}</b>\nLast error: <code>{esc(r[12] or 'None')}</code>",reply_markup=InlineKeyboardMarkup([[ib("🔄 Restart",f"cb:restart:{cid}"),ib("⏹ Stop",f"cb:stop:{cid}"),ib("▶️ Start",f"cb:start:{cid}")],[ib("📜 Logs",f"cb:logs:{cid}" )],[ib("🔙 Back",f"cb:select:{cid}")]]),parse_mode=ParseMode.HTML); return ConversationHandler.END

async def child_users_screen(q,cid):
    total=scalar("SELECT COUNT(*) FROM child_bot_users WHERE child_bot_id=?",(cid,)); active=scalar("SELECT COUNT(*) FROM child_bot_users WHERE child_bot_id=? AND status='active'",(cid,)); blocked=scalar("SELECT COUNT(*) FROM child_bot_users WHERE child_bot_id=? AND status='blocked'",(cid,)); today=datetime.now().date().isoformat(); week=(datetime.now()-timedelta(days=datetime.now().weekday())).date().isoformat(); today_n=scalar("SELECT COUNT(*) FROM child_bot_users WHERE child_bot_id=? AND substr(joined_at,1,10)=?",(cid,today)); week_n=scalar("SELECT COUNT(*) FROM child_bot_users WHERE child_bot_id=? AND substr(joined_at,1,10)>=?",(cid,week))
    await q.edit_message_text(f"👥 <b>USERS</b>\n\nTotal: <b>{total}</b>\nActive: <b>{active}</b>\nBlocked: <b>{blocked}</b>\nInactive: <b>{total-active-blocked}</b>\nNew today: <b>{today_n}</b>\nNew this week: <b>{week_n}</b>",reply_markup=InlineKeyboardMarkup([[ib("🔎 Search","child:usersearch:"+str(cid))],[ib("📤 Export","child:export:"+str(cid))],[ib("🔙 Back",f"cb:select:{cid}")]]),parse_mode=ParseMode.HTML); return ConversationHandler.END

async def child_channels_screen(q,cid):
    rows=child_rows(cid); text="📢 <b>CHANNELS</b>\n\n"; buttons=[]
    for r in rows:
        text+=f"{r[0]}. {'🟢' if r[6] else '🔴'} <b>{esc(r[2])}</b> · <code>{esc(r[1])}</code>\n   {esc(r[3])}\n"
        buttons.append([ib(("🔴 Disable" if r[6] else "🟢 Enable"),f"child:chtoggle:{cid}:{r[0]}"),ib("🗑 Delete",f"child:chdelete:{cid}:{r[0]}",style="danger")])
    buttons += [[ib("➕ Add Channel",f"child:chadd:{cid}",style="success")],[ib("🔙 Back",f"cb:select:{cid}")]]
    await q.edit_message_text(text if rows else "📢 <b>CHANNELS</b>\n\nNo channels.",reply_markup=InlineKeyboardMarkup(buttons),parse_mode=ParseMode.HTML); return ConversationHandler.END

async def child_buttons_screen(q,cid):
    rows=child_button_rows(cid); text="🎨 <b>BUTTONS</b>\n\n"+"\n".join(f"• <b>{esc(r[1])}</b> | text={esc(r[1]) if False else esc(r[1])} | style={esc(r[4])} | {'🟢 ON' if r[6] else '🔴 OFF'} | emoji={'ON' if r[5] else 'OFF'}" for r in rows)
    buttons=[]
    for r in rows:
        buttons.append([ib("✏️ Edit "+r[1],f"child:btnedit:{cid}:{r[0]}"),ib("👁 Preview",f"child:btnpreview:{cid}:{r[0]}"),ib(("🔴 Disable" if r[6] else "🟢 Enable"),f"child:btntoggle:{cid}:{r[0]}")])
    buttons += [[ib("↩️ Reset Defaults",f"child:btnreset:{cid}",style="danger")],[ib("🔙 Back",f"cb:select:{cid}")]]
    await q.edit_message_text(text or "No buttons.",reply_markup=InlineKeyboardMarkup(buttons),parse_mode=ParseMode.HTML); return ConversationHandler.END

async def child_admin_screen(q,cid):
    rows=child_admins(cid); text="👑 <b>ADMIN MANAGEMENT</b>\n\n"+"\n".join(f"{r[1]} — <b>{esc(r[2])}</b>" for r in rows) or "No admins."
    buttons=[[ib("➕ Add Admin",f"cadmin:add:{cid}",style="success")]]
    for r in rows:
        if r[2]!="CHILD_OWNER": buttons.append([ib("➖ Remove "+str(r[1]),f"cadmin:remove:{cid}:{r[1]}",style="danger")])
    buttons.append([ib("🔙 Back",f"cb:select:{cid}")]); await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup(buttons),parse_mode=ParseMode.HTML); return ConversationHandler.END

async def child_stats_screen(q):
    await q.edit_message_text("📊 <b>GLOBAL CHILD STATISTICS</b>\n\nChild Bots: <b>"+str(scalar("SELECT COUNT(*) FROM child_bots WHERE status!='REMOVED'"))+"</b>\nRunning: <b>"+str(scalar("SELECT COUNT(*) FROM child_bots WHERE status='RUNNING'"))+"</b>\nStopped: <b>"+str(scalar("SELECT COUNT(*) FROM child_bots WHERE status='STOPPED'"))+"</b>\nErrors: <b>"+str(scalar("SELECT COUNT(*) FROM child_bots WHERE status IN ('ERROR','TOKEN_INVALID')"))+"</b>\nPending Requests: <b>"+str(scalar("SELECT COUNT(*) FROM clone_requests WHERE status='pending'"))+"</b>\nChild Users: <b>"+str(scalar("SELECT COUNT(*) FROM child_bot_users"))+"</b>\nChild Broadcasts: <b>"+str(scalar("SELECT COUNT(*) FROM child_bot_broadcasts"))+"</b>",reply_markup=back_kb("a_child"),parse_mode=ParseMode.HTML); return ConversationHandler.END

async def global_child_select_screen(q,ctx):
    selected=set(ctx.user_data.get("global_child_selection",[]))
    rows=MANAGER.get_all_child_bots(); buttons=[[ib(("✅ " if r[0] in selected else "☑️ ")+"@"+(r[2] or str(r[1])),f"cb:globtoggle:{r[0]}")] for r in rows]
    buttons.append([ib("🚀 Done","cb:globaldone",style="success"),ib("🔙 Back","a_child")]); await q.edit_message_text("📣 <b>GLOBAL CHILD BROADCAST</b>\n\nSelect child bots.",reply_markup=InlineKeyboardMarkup(buttons),parse_mode=ParseMode.HTML); return ConversationHandler.END

# =========================================================
# Master broadcast composer states
# =========================================================

async def master_bcast_buttons_menu(q,ctx):
    buttons=ctx.user_data.get("broadcast_buttons",[])
    rows=[]
    for row_num,row in enumerate(buttons,1):
        for idx,b in enumerate(row,1):
            rows.append(f"{row_num}.{idx} <b>{esc(b.get('text','Button'))}</b> — {esc(b.get('url',''))}")
    text="🔘 <b>BROADCAST BUTTONS</b>\n\n"+("\n".join(rows) if rows else "No buttons added.")
    await q.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [ib("➕ Add Button","bcast:add",style="success"),ib("👁 Preview","bcast:preview")],
            [ib("✅ Done","bcast:done",style="success"),ib("❌ Cancel","bcast:cancel",style="danger")],
        ]),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def master_bcast_content(update,ctx):
    if not is_owner(update.effective_user.id): return ConversationHandler.END
    cid=ctx.user_data.get("selected_child_id")
    if not cid: return ConversationHandler.END
    if ctx.user_data.get("bcast_album_mgid") and update.message.media_group_id==ctx.user_data.get("bcast_album_mgid"):
        ctx.user_data.setdefault("bcast_album",[]).append(snapshot_message(update.message)); return S_MASTER_BCAST_CONTENT
    if update.message.media_group_id:
        ctx.user_data["bcast_album_mgid"]=update.message.media_group_id; ctx.user_data["bcast_album"]=[snapshot_message(update.message)]
        await asyncio.sleep(1.0)
        payload={"type":"album","album":ctx.user_data.get("bcast_album",[])}
    else: payload=snapshot_message(update.message)
    ctx.user_data["broadcast_payload"]=payload; ctx.user_data.setdefault("broadcast_buttons",[])
    await update.message.reply_text("📣 <b>BROADCAST COMPOSER</b>\n\nContent received. Do you want inline buttons?",reply_markup=InlineKeyboardMarkup([[ib("➕ Add Button","bcast:add",style="success"),ib("➡️ No Buttons","bcast:done",style="primary")],[ib("👁 Preview","bcast:preview"),ib("❌ Cancel","bcast:cancel",style="danger")]]),parse_mode=ParseMode.HTML)
    return ConversationHandler.END

async def master_bcast_button_name(update,ctx):
    name=(update.message.text or "").strip()
    if not name:return S_MASTER_BCAST_BUTTON_NAME
    ctx.user_data["pending_button_name"]=name; await update.message.reply_text("🔗 Send Button URL (HTTPS or t.me link):\n\n[🔙 Back] [❌ Cancel]"); return S_MASTER_BCAST_BUTTON_URL

async def master_bcast_button_url(update,ctx):
    url=(update.message.text or "").strip()
    if not button_validate_url(url): await update.message.reply_text("❌ Invalid URL. Only HTTPS URLs are allowed."); return S_MASTER_BCAST_BUTTON_URL
    ctx.user_data["pending_button_url"]=url; await update.message.reply_text("↔️ Send row number (1-5).",reply_markup=back_kb("bcast:preview")); return S_MASTER_BCAST_BUTTON_ROW

async def master_bcast_button_row(update,ctx):
    try: row=int((update.message.text or "").strip())
    except ValueError: row=0
    if row<1 or row>5: await update.message.reply_text("❌ Row must be between 1 and 5."); return S_MASTER_BCAST_BUTTON_ROW
    add_payload_button(ctx,ctx.user_data.get("pending_button_name","Button"),ctx.user_data.get("pending_button_url",""),row)
    ctx.user_data.pop("pending_button_name",None);ctx.user_data.pop("pending_button_url",None)
    await update.message.reply_text("✅ Button added.",reply_markup=InlineKeyboardMarkup([[ib("➕ Add Another","bcast:add",style="success"),ib("👁 Preview","bcast:preview")],[ib("✅ Done","bcast:done",style="success"),ib("❌ Cancel","bcast:cancel",style="danger")]]))
    return ConversationHandler.END

async def master_bcast_preview(q,ctx):
    payload=ctx.user_data.get("broadcast_payload"); cid=ctx.user_data.get("selected_child_id")
    if not payload or not cid:return await q.answer("Nothing to preview",show_alert=True)
    try:
        if payload.get("type")=="album":
            await q.message.reply_text("📣 <b>BROADCAST PREVIEW</b>\n\nAlbum preview is represented below; the same album payload will be sent by the child bot.",reply_markup=build_inline_keyboard(ctx.user_data.get("broadcast_buttons",[])),parse_mode=ParseMode.HTML)
            for item in payload.get("album",[])[:10]:
                await send_payload(ctx.bot,q.message.chat_id,item,[])
        else:
            await send_payload(ctx.bot,q.message.chat_id,payload,ctx.user_data.get("broadcast_buttons",[]))
        await q.message.reply_text("⚠️ <b>CONFIRM BROADCAST</b>\n\nBot: <b>@"+esc(child_get(cid)[2])+"</b>\nRecipients: <b>"+str(scalar("SELECT COUNT(*) FROM child_bot_users WHERE child_bot_id=? AND status!='blocked'",(cid,)))+"</b>\nButtons: <b>"+str(sum(len(r) for r in ctx.user_data.get("broadcast_buttons",[])))+"</b>",reply_markup=InlineKeyboardMarkup([[ib("🚀 Send Broadcast","bcast:send",style="success"),ib("✏️ Edit","bcast:edit" )],[ib("🔘 Edit Buttons","bcast:add"),ib("❌ Cancel","bcast:cancel",style="danger")]]),parse_mode=ParseMode.HTML)
    except TelegramError as e: await q.answer("Preview failed: "+sanitize_error(e),show_alert=True)
    return ConversationHandler.END

async def master_bcast_confirm(q,ctx):
    return await master_bcast_preview(q,ctx)

async def master_bcast_send_cb(update,ctx):
    q=update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("Not authorized",show_alert=True)
    await q.answer("Broadcast queued")
    cid=ctx.user_data.get("selected_child_id"); payload=ctx.user_data.get("broadcast_payload"); buttons=ctx.user_data.get("broadcast_buttons",[])
    if not cid or not payload:return await q.edit_message_text("❌ Broadcast data expired.")
    try:
        status=await q.message.reply_text(f"⏳ Preparing broadcast through @{esc((child_get(cid) or [0,0,''])[2] or 'child')}...",parse_mode=ParseMode.HTML)
        bid=await MANAGER.start_broadcast_task(cid,payload,buttons,status)
        await status.edit_text(f"📣 <b>BROADCAST STARTED</b>\n\nBot: <b>@{esc((child_get(cid) or [0,0,''])[2] or '—')}</b>\nBroadcast ID: <code>{bid}</code>\n\nThe broadcast is running in the background.",reply_markup=InlineKeyboardMarkup([[ib("⏹ Cancel Broadcast",f"cb:cancelbc:{bid}",style="danger")]]),parse_mode=ParseMode.HTML)
    except Exception as e:
        await q.edit_message_text("❌ Broadcast failed safely.\n\n"+esc(sanitize_error(e)),parse_mode=ParseMode.HTML); log_error("ERROR",f"Child broadcast: {sanitize_error(e)}")
    finally: ctx.user_data.clear()
    return ConversationHandler.END

async def global_bcast_content(update,ctx):
    if not is_owner(update.effective_user.id): return ConversationHandler.END
    payload=snapshot_message(update.message); ctx.user_data["global_payload"]=payload
    ids=ctx.user_data.get("global_child_selection",[]); total=sum(scalar("SELECT COUNT(*) FROM child_bot_users WHERE child_bot_id=? AND status!='blocked'",(cid,)) for cid in ids)
    await update.message.reply_text(f"⚠️ <b>GLOBAL BROADCAST</b>\n\nSelected bots: <b>{len(ids)}</b>\nEstimated recipients: <b>{total}</b>",reply_markup=confirm_cancel_keyboard("global:confirm","bcast:cancel"),parse_mode=ParseMode.HTML); return ConversationHandler.END

async def global_send_cb(update,ctx):
    q=update.callback_query; await q.answer()
    ids=ctx.user_data.get("global_child_selection",[]); payload=ctx.user_data.get("global_payload")
    if not ids or not payload:return await q.edit_message_text("❌ Broadcast data expired.")
    results=[]
    for cid in ids:
        try: results.append(await MANAGER.broadcast_to_child_bot(cid,payload,[]))
        except Exception as e: results.append({"status":"error","sent":0,"failed":0,"total":0,"broadcast_id":"","error":sanitize_error(e)})
    total=sum(r.get("total",0) for r in results); sent=sum(r.get("sent",0) for r in results); failed=sum(r.get("failed",0) for r in results)
    await q.edit_message_text(f"📊 <b>GLOBAL BROADCAST RESULT</b>\n\nBots: <b>{len(results)}</b>\nTotal: <b>{total}</b>\n✅ Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>",parse_mode=ParseMode.HTML); ctx.user_data.clear(); return ConversationHandler.END

# =========================================================
# Child admin callback / conversations
# =========================================================

async def child_admin_cmd(update,ctx):
    cid=ctx.application.bot_data.get("child_id"); uid=update.effective_user.id
    if not cid or not child_is_authorized(cid,uid): return await update.message.reply_text("❌ Not authorized for this child bot.")
    await update.message.reply_text(f"⚙️ <b>CHILD ADMIN PANEL</b>\n\nBot: <b>@{esc((child_get(cid) or [0,0,'','',''])[2] if child_get(cid) else '—')}</b>",reply_markup=InlineKeyboardMarkup([[ib("📊 Dashboard","ca:dash")],[ib("📢 Channels","ca:channels")],[ib("📝 Messages","ca:messages")],[ib("🎨 Buttons","ca:buttons")],[ib("📣 Broadcast","ca:broadcast",style="success")],[ib("👥 Users","ca:users")],[ib("❤️ Health","ca:health")],[ib("⚙️ Settings","ca:settings")],[ib("❌ Close","ca:close",style="danger")]]),parse_mode=ParseMode.HTML)


async def child_admin_callback(update,ctx,callback_data=None):
    q=update.callback_query; cid=ctx.application.bot_data.get("child_id"); uid=q.from_user.id
    if not cid or not child_is_authorized(cid,uid): return await q.answer("❌ Not authorized.",show_alert=True)
    await q.answer(); d=callback_data if callback_data is not None else (q.data or "")
    if d=="ca:close": return await q.edit_message_text("❌ Closed.")
    if d=="ca:dash":
        await q.edit_message_text(f"📊 <b>CHILD DASHBOARD</b>\n\nUsers: <b>{scalar('SELECT COUNT(*) FROM child_bot_users WHERE child_bot_id=?',(cid,))}</b>\nChannels: <b>{scalar('SELECT COUNT(*) FROM child_bot_channels WHERE child_bot_id=?',(cid,))}</b>\nBroadcasts: <b>{scalar('SELECT COUNT(*) FROM child_bot_broadcasts WHERE child_bot_id=?',(cid,))}</b>",reply_markup=back_kb("ca:dash"),parse_mode=ParseMode.HTML); return
    if d=="ca:channels": return await child_channels_screen(q,cid)
    if d=="ca:buttons": return await child_buttons_screen(q,cid)
    if d=="ca:users": return await child_users_screen(q,cid)
    if d=="ca:health": return await child_health_screen(q,cid)
    if d=="ca:settings":
        on_fj=child_setting(cid,"force_join_enabled","1")=="1"; on_maint=child_setting(cid,"maintenance_mode","0")=="1"; on_bcast=child_setting(cid,"broadcast_enabled","1")=="1"
        await q.edit_message_text("⚙️ <b>CHILD SETTINGS</b>",reply_markup=InlineKeyboardMarkup([[ib(("🟢 " if on_fj else "🔴 ")+"Force Join",f"ca:toggle:{cid}:force_join_enabled")],[ib(("🟢 " if on_maint else "🔴 ")+"Maintenance",f"ca:toggle:{cid}:maintenance_mode")],[ib(("🟢 " if on_bcast else "🔴 ")+"Broadcast",f"ca:toggle:{cid}:broadcast_enabled")],[ib("🔙 Back","ca:dash")]]),parse_mode=ParseMode.HTML); return
    if d=="ca:messages":
        await q.edit_message_text("📝 <b>MESSAGE SETTINGS</b>",reply_markup=InlineKeyboardMarkup([[ib("👋 Welcome","ca:set:welcome")],[ib("🎉 Post-Join","ca:set:postjoin")],[ib("📢 Top","ca:set:top")],[ib("🔙 Back","ca:dash")]]),parse_mode=ParseMode.HTML); return
    if d.startswith("ca:set:"):
        key=d.split(":",2)[2]
        if key not in ("welcome","postjoin","top"): return await q.answer("Invalid setting",show_alert=True)
        ctx.user_data["child_setting_edit"]=key
        await q.edit_message_text(f"✏️ Send new <b>{esc(key)}</b> message.\n\nUse /cancel to abort.",reply_markup=cancel_kb("ca:cancel"),parse_mode=ParseMode.HTML); return
    if d=="ca:broadcast":
        ctx.user_data["child_broadcast_mode"]=True; await q.edit_message_text("📣 Send broadcast content.\n\nText, photo, video, document, audio, voice, animation, sticker, video note supported.",reply_markup=cancel_kb("ca:cancel")); return
    if d.startswith("ca:toggle:"):
        _,_,target,key=d.split(":",3); set_child_setting(int(target),key,"0" if child_setting(int(target),key,"0")=="1" else "1"); return await child_admin_callback(update,ctx,callback_data="ca:settings")
    if d=="ca:cancel": ctx.user_data.clear(); await q.edit_message_text("❌ Cancelled."); return

async def child_message_handler(update,ctx):
    cid=ctx.application.bot_data.get("child_id")
    if not cid or not update.effective_user: return
    # Admin-scoped actions first.
    if ctx.user_data.get("child_setting_edit") and child_is_authorized(cid,update.effective_user.id) and update.message:
        key=ctx.user_data.pop("child_setting_edit")
        if not update.message.text_html and not update.message.text:
            await update.message.reply_text("❌ Please send text."); return
        set_child_setting(cid,key,update.message.text_html if getattr(update.message,"text_html",None) else update.message.text)
        await update.message.reply_text("✅ Child message updated.",reply_markup=back_kb("ca:messages")); return
    if ctx.user_data.get("child_broadcast_mode") and child_is_authorized(cid,update.effective_user.id):
        payload=snapshot_message(update.message)
        try:
            result=await child_broadcast(MANAGER,cid,payload,[])
            await update.message.reply_text(f"📊 Broadcast completed.\nTotal: {result['total']}\n✅ Sent: {result['sent']}\n❌ Failed: {result['failed']}")
        except Exception as e: await update.message.reply_text("❌ Broadcast failed safely.")
        ctx.user_data.clear(); return
    # Ignore ordinary admin chatter; normal user registration happens on /start.

async def child_error_handler(update,ctx):
    cid=ctx.application.bot_data.get("child_id") if ctx.application else None
    if ctx.error:
        text=sanitize_error(ctx.error)
        if cid:
            with db() as con: con.execute("INSERT INTO child_bot_logs(child_bot_id,created_at,level,message) VALUES(?,?,?,?)",(cid,now_iso(),"ERROR",text))
            update_child_status(cid,"ERROR",text)
        logger.error("Child %s error: %s",cid,text)

# =========================================================
# Child admin input states
# =========================================================

async def child_admin_id_input(update,ctx):
    if not is_owner(update.effective_user.id): return ConversationHandler.END
    try: uid=int((update.message.text or "").strip())
    except ValueError: await update.message.reply_text("❌ Invalid numeric ID."); return S_CHILD_ADMIN_ID
    cid=ctx.user_data.get("child_admin_target") or ctx.user_data.get("admin_child_id")
    if not cid:return ConversationHandler.END
    upsert_child_admin(cid,uid,"CHILD_ADMIN"); ctx.user_data.pop("child_admin_target",None); ctx.user_data.pop("admin_child_id",None)
    await update.message.reply_text("✅ Child admin added.",reply_markup=back_kb(f"cb:admins:{cid}")); return ConversationHandler.END


async def child_channel_id_input(update,ctx):
    cid=ctx.user_data.get("child_channel_child_id"); val=(update.message.text or "").strip()
    if not cid:return ConversationHandler.END
    ctx.user_data["child_channel_id"]=val; await update.message.reply_text("✏️ Send Channel Name.",reply_markup=cancel_kb("bcast:cancel")); return S_CHILD_CHANNEL_NAME

async def child_channel_name_input(update,ctx):
    val=(update.message.text or "").strip()
    if not val:return S_CHILD_CHANNEL_NAME
    edit=ctx.user_data.get("child_channel_edit")
    if edit:
        ctx.user_data["child_channel_name"]=val; await update.message.reply_text("🔗 Send new Telegram invite link or <code>skip</code>.",parse_mode=ParseMode.HTML,reply_markup=cancel_kb("bcast:cancel")); return S_CHILD_CHANNEL_LINK
    ctx.user_data["child_channel_name"]=val; await update.message.reply_text("🔗 Send Telegram invite link (https://t.me/...).",reply_markup=cancel_kb("bcast:cancel")); return S_CHILD_CHANNEL_LINK

async def child_channel_link_input(update,ctx):
    link=(update.message.text or "").strip(); edit=ctx.user_data.get("child_channel_edit"); cid=ctx.user_data.get("child_channel_child_id") or (edit[0] if edit else None)
    if not cid:return ConversationHandler.END
    if edit:
        if link.lower()!="skip" and not link.startswith(("https://t.me/","http://t.me/")): await update.message.reply_text("❌ Invalid Telegram link."); return S_CHILD_CHANNEL_LINK
        with db() as con:
            con.execute("UPDATE child_bot_channels SET channel_name=? WHERE child_bot_id=? AND id=?",(ctx.user_data.get("child_channel_name"),cid,edit[1]))
            if link.lower()!="skip": con.execute("UPDATE child_bot_channels SET channel_link=? WHERE child_bot_id=? AND id=?",(link,cid,edit[1]))
        await update.message.reply_text("✅ Channel updated.",reply_markup=back_kb(f"cb:channels:{cid}"))
        for k in ("child_channel_edit","child_channel_name","child_channel_child_id","child_channel_id"): ctx.user_data.pop(k,None)
        return ConversationHandler.END
    if not link.startswith(("https://t.me/","http://t.me/")): await update.message.reply_text("❌ Invalid Telegram link."); return S_CHILD_CHANNEL_LINK
    try:
        with db() as con:
            n=con.execute("SELECT COALESCE(MAX(order_num),0)+1 FROM child_bot_channels WHERE child_bot_id=?",(cid,)).fetchone()[0]
            con.execute("INSERT INTO child_bot_channels(child_bot_id,channel_id,channel_name,channel_link,order_num,enabled) VALUES(?,?,?,?,?,1)",(cid,ctx.user_data.get("child_channel_id"),ctx.user_data.get("child_channel_name"),link,n))
        await update.message.reply_text("✅ Channel added.",reply_markup=back_kb(f"cb:channels:{cid}"))
    except sqlite3.IntegrityError: await update.message.reply_text("❌ Channel already exists.",reply_markup=back_kb(f"cb:channels:{cid}"))
    finally:
        for k in ("child_channel_child_id","child_channel_id","child_channel_name"): ctx.user_data.pop(k,None)
    return ConversationHandler.END

async def child_button_edit_input(update,ctx):
    cid,key=ctx.user_data.get("child_button_edit",(None,None)); raw=(update.message.text or "").strip()
    if not cid or not key:return ConversationHandler.END
    try:
        # text | callback_data | url | style | emoji_id | enabled | row | position
        parts=[x.strip() for x in raw.split("|",7)]
        if len(parts)!=8: raise ValueError("Send exactly 8 fields separated by |.")
        text,cb,url,style,emoji,enabled,row,pos=parts
        if not text: raise ValueError("Text is required")
        if cb and len(cb.encode("utf-8"))>64: raise ValueError("callback_data is over 64 bytes")
        if url and not button_validate_url(url): raise ValueError("URL must be HTTPS/t.me")
        if style not in ("primary","success","danger"): raise ValueError("style must be primary, success or danger")
        with db() as con: con.execute("UPDATE child_bot_buttons SET text=?,callback_data=?,url=?,style=?,emoji_id=?,enabled=?,row_num=?,position=? WHERE child_bot_id=? AND button_key=?",(text,cb,url,style,emoji,int(enabled),int(row),int(pos),cid,key))
        await update.message.reply_text("✅ Child button updated.",reply_markup=back_kb(f"cb:buttons:{cid}"))
        ctx.user_data.pop("child_button_edit",None); return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text("❌ Invalid button data: "+sanitize_error(e)); return S_CHILD_MSG

async def child_search_input(update,ctx):
    cid=ctx.user_data.get("child_search_id");
    if not cid:return ConversationHandler.END
    rows=child_search_users(cid,update.message.text or "")
    if not rows:
        await update.message.reply_text("❌ No users found.",reply_markup=back_kb(f"cb:users:{cid}")); return ConversationHandler.END
    buttons=[[ib("👁 View",f"child:view:{cid}:{r[0]}"),ib("🚫 Block",f"child:block:{cid}:{r[0]}",style="danger")] for r in rows]
    await update.message.reply_text("\n".join(f"👤 <b>{esc(r[1])}</b> · <code>{r[0]}</code> · @{esc(r[2]) if r[2] else '—'} · {esc(r[4])}" for r in rows),reply_markup=InlineKeyboardMarkup(buttons),parse_mode=ParseMode.HTML)
    ctx.user_data.pop("child_search_id",None); return ConversationHandler.END

async def child_user_message_input(update,ctx):
    target=ctx.user_data.get("child_msg_target")
    if not target:return ConversationHandler.END
    cid,uid=target; payload=snapshot_message(update.message)
    try:
        if cid not in MANAGER.apps: await MANAGER.start_child_bot(cid)
        app=MANAGER.apps.get(cid)
        if not app: raise RuntimeError("Child bot is not running")
        await send_payload(app.bot,uid,payload,[])
        await update.message.reply_text("✅ Message sent.",reply_markup=back_kb(f"cb:users:{cid}"))
    except Exception as e:
        await update.message.reply_text("❌ Send failed safely: "+sanitize_error(e),reply_markup=back_kb(f"cb:users:{cid}"))
    ctx.user_data.pop("child_msg_target",None); return ConversationHandler.END

# =========================================================
# Auto backup / health loop
# =========================================================

async def auto_backup_loop():
    while True:
        try:
            await asyncio.sleep(3600)
            if gset("auto_backup_enabled","0")=="1":
                last=gset("last_backup",""); hours=168 if gset("auto_backup_frequency","daily")=="weekly" else 24
                if not last or (datetime.now()-datetime.fromisoformat(last)).total_seconds()>=hours*3600:
                    p=create_backup(); cleanup_backups(); p.unlink(missing_ok=True)
        except asyncio.CancelledError: return
        except Exception as e: logger.exception("Auto backup: %s",sanitize_error(e))


async def child_health_loop():
    while True:
        try:
            await asyncio.sleep(90)
            await MANAGER.health_check()
            # Restart only transient ERROR children; never loop on TOKEN_INVALID.
            for row in MANAGER.get_all_child_bots():
                cid,status=row[0],row[6]
                if status=="ERROR" and cid not in MANAGER.apps:
                    await MANAGER.start_child_bot(cid)
        except asyncio.CancelledError: return
        except Exception as e: logger.exception("Child health loop: %s",sanitize_error(e))

HEALTH_TASK=None

async def post_init(app):
    global AUTO_BACKUP_TASK, HEALTH_TASK, MASTER_BOT_REF
    MASTER_BOT_REF = app.bot
    init_db()
    AUTO_BACKUP_TASK=asyncio.create_task(auto_backup_loop())
    HEALTH_TASK=asyncio.create_task(child_health_loop())
    # Autoload enabled child bots, excluding removed/disabled/token-invalid.
    for row in MANAGER.get_all_child_bots():
        cid,status=row[0],row[6]
        if status not in ("DISABLED","REMOVED","TOKEN_INVALID"):
            try: await MANAGER.start_child_bot(cid)
            except Exception as e: logger.exception("Child autostart %s: %s",cid,sanitize_error(e))


async def post_shutdown(app):
    global AUTO_BACKUP_TASK, HEALTH_TASK, LEGACY_BROADCAST_TASK
    for task in (AUTO_BACKUP_TASK,HEALTH_TASK,LEGACY_BROADCAST_TASK):
        if task and not task.done():
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
    await MANAGER.shutdown_all()

# =========================================================
# Master ConversationHandler glue
# =========================================================

async def cancel(u,c):
    c.user_data.clear(); await u.message.reply_text("❌ Cancelled."); return ConversationHandler.END


def build_master_conversation():
    tf=filters.TEXT & ~filters.COMMAND
    media=filters.ALL & ~filters.COMMAND
    return ConversationHandler(
        entry_points=[CommandHandler("admin",admin_cmd),CommandHandler("create",create_start),CallbackQueryHandler(admin_cb,pattern=r"^(a_|cb:|cr:|clone:|bcast:|cadmin:|child:)")],
        states={
            S_CH_ID:[MessageHandler(tf,s_ch_id)],S_CH_NAME:[MessageHandler(tf,s_ch_name)],S_CH_LINK:[MessageHandler(tf,s_ch_link)],
            S_WELCOME:[MessageHandler(tf,s_welcome)],S_WELCOME_PHOTO:[MessageHandler((filters.PHOTO|filters.TEXT)&~filters.COMMAND,s_photo)],S_POSTJOIN:[MessageHandler(tf,s_postjoin)],S_TOP:[MessageHandler(tf,s_top)],S_BTN1:[MessageHandler(tf,s_btn1)],S_BTN2:[MessageHandler(tf,s_btn2)],S_BTN3:[MessageHandler(tf,s_btn3)],
            S_BCAST:[MessageHandler(media,start_bcast)],S_RESTORE:[MessageHandler(filters.Document.ALL&~filters.COMMAND,s_restore)],S_SEARCH:[MessageHandler(tf,s_search)],S_USERMSG:[MessageHandler(media,s_usermsg)],S_EDITNAME:[MessageHandler(tf,s_editname)],S_EDITLINK:[MessageHandler(tf,s_editlink)],S_EMOJI:[MessageHandler(tf,s_emoji)],
            S_CREATE_CONFIRM:[CallbackQueryHandler(create_confirm_cb,pattern=r"^create_(confirm|cancel)$")],
            S_REJECT_REASON:[MessageHandler(tf,reject_reason_message),CallbackQueryHandler(reject_cancel,pattern=r"^reject_cancel$")],
            S_CREATE_TOKEN:[MessageHandler(tf,requester_token_message),CallbackQueryHandler(create_cancel_cb,pattern=r"^create_cancel$")],
            S_CREATE_ADMIN_ID:[MessageHandler(tf,requester_admin_id_message),CallbackQueryHandler(requester_create_callback,pattern=r"^create:"),CallbackQueryHandler(create_cancel_cb,pattern=r"^create_cancel$")],
            S_MASTER_BCAST_CONTENT:[MessageHandler(media,master_bcast_content)],S_MASTER_BCAST_BUTTON_NAME:[MessageHandler(tf,master_bcast_button_name)],S_MASTER_BCAST_BUTTON_URL:[MessageHandler(tf,master_bcast_button_url)],S_MASTER_BCAST_BUTTON_ROW:[MessageHandler(tf,master_bcast_button_row)],S_MASTER_GLOBAL_CONTENT:[MessageHandler(media,global_bcast_content)],S_CHILD_ADMIN_ID:[MessageHandler(tf,child_admin_id_input)],S_CHILD_CHANNEL_ID:[MessageHandler(tf,child_channel_id_input)],S_CHILD_CHANNEL_NAME:[MessageHandler(tf,child_channel_name_input)],S_CHILD_CHANNEL_LINK:[MessageHandler(tf,child_channel_link_input)],S_CHILD_MSG:[MessageHandler(tf,child_button_edit_input)],S_CHILD_SEARCH:[MessageHandler(tf,child_search_input)],S_CHILD_USERMSG:[MessageHandler(media,child_user_message_input)],
        },
        fallbacks=[CommandHandler("cancel",cancel)],per_chat=False,per_user=True,allow_reentry=True,
    )

# =========================================================
# Extra standalone master callbacks
# =========================================================

async def master_extra_callback(update,ctx):
    # Handles callbacks not safe to nest in the master conversation state machine.
    d=update.callback_query.data or ""
    if not is_owner(update.callback_query.from_user.id): return await update.callback_query.answer("❌ Not authorized.",show_alert=True)
    if d=="a_child": return await child_master_menu(update.callback_query)
    if d=="a_child_requests": return await child_requests_menu(update.callback_query)
    if d=="a_child_stats": return await child_stats_screen(update.callback_query)
    if d=="create_cancel": return await create_cancel_cb(update,ctx)
    if d.startswith("clone:"): return await owner_clone_callback(update,ctx)
    if d=="bcast:send": return await master_bcast_send_cb(update,ctx)
    if d=="global:confirm": return await global_send_cb(update,ctx)
    return ConversationHandler.END

# =========================================================
# Main
# =========================================================

class _RenderHealthHandler(BaseHTTPRequestHandler):
    def _ok(self):
        body=b"ok"
        self.send_response(200)
        self.send_header("Content-Type","text/plain; charset=utf-8")
        self.send_header("Content-Length",str(len(body)))
        self.send_header("Connection","close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        self._ok()

    def do_HEAD(self):
        self._ok()

    def log_message(self,format,*args):
        return


def _start_render_http_server():
    # Render Free supports Web Services, not free Background Workers. The
    # Telegram bot itself remains long-polling; this tiny stdlib endpoint only
    # satisfies Render's required HTTP port. It is enabled only when PORT is set.
    port_raw=os.getenv("PORT","").strip()
    if not port_raw:
        return None
    try:
        port=int(port_raw)
    except ValueError as e:
        raise RuntimeError("PORT must be a valid integer when running on Render.") from e
    server=ThreadingHTTPServer(("0.0.0.0",port),_RenderHealthHandler)
    thread=threading.Thread(target=server.serve_forever,name="render-health",daemon=True)
    thread.start()
    logger.info("Render health server listening on 0.0.0.0:%s",port)
    return server


def main():
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN is missing. Set BOT_TOKEN environment variable.")
    if not OWNER_ID: raise RuntimeError("OWNER_ID/ADMIN_ID is missing or invalid. Set OWNER_ID environment variable.")
    if not SECRET_ENCRYPTION_KEY: raise RuntimeError("SECRET_ENCRYPTION_KEY is missing. Set a URL-safe base64 32-byte key in Render environment variables.")
    get_crypto()
    init_db()
    app=(Application.builder().token(BOT_TOKEN).concurrent_updates(False).post_init(post_init).post_shutdown(post_shutdown).build())
    conv=build_master_conversation()
    app.add_handler(CommandHandler("start",start),group=0)
    app.add_handler(conv,group=1)
    # Create token/admin message paths outside ConversationHandler when no legacy state owns them.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, requester_token_message),group=2)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, requester_admin_id_message),group=3)
    app.add_handler(CallbackQueryHandler(master_extra_callback,pattern=r"^(clone:|bcast:send|global:confirm|create_cancel|a_child|a_child_requests|a_child_stats)$"),group=4)
    app.add_handler(CallbackQueryHandler(cb_check,pattern=r"^check_joined$"),group=5)
    app.add_handler(CallbackQueryHandler(cb_btn,pattern=r"^btn[123]$"),group=5)
    app.add_handler(CallbackQueryHandler(cb_back,pattern=r"^back_main$"),group=5)
    app.add_handler(ChatJoinRequestHandler(join_request),group=5)
    app.add_error_handler(errors)
    logger.info("Bot started: v%s / PTB %s",BOT_VERSION,telegram.__version__)
    render_server=_start_render_http_server()
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES,drop_pending_updates=True)
    finally:
        if render_server is not None:
            try:
                render_server.shutdown()
            finally:
                render_server.server_close()


async def errors(update,ctx):
    global PREMIUM_EMOJI_ENABLED
    if not ctx.error:return
    text=sanitize_error(ctx.error)
    logger.error("Unhandled exception: %s",text)
    low=text.lower()
    if "icon_custom_emoji_id" in low or "custom emoji" in low or "not enough rights" in low:
        PREMIUM_EMOJI_ENABLED=False
        log_error("ERROR","Premium custom emoji disabled after Telegram rejection.")


if __name__=="__main__": main()
