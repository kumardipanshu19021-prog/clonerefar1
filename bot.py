import asyncio, csv, html, logging, os, sqlite3, tempfile, time, uuid, zipfile, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
from telegram.error import TelegramError, RetryAfter, Forbidden
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler, ChatJoinRequestHandler

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
try: ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip())
except ValueError: ADMIN_ID = 0
try: OWNER_ID = int(os.getenv("OWNER_ID", str(ADMIN_ID)).strip())
except ValueError: OWNER_ID = ADMIN_ID
DB_PATH = os.getenv("DB_PATH", "bot2.db").strip() or "bot2.db"
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "backups")); BACKUP_DIR.mkdir(parents=True, exist_ok=True)
BOT_VERSION, DB_VERSION = "4.0.0", 4
STARTED_AT = time.monotonic()
LAST_ERROR = ""
ERROR_LOG_MAX = 200
BROADCAST_TASK = None
BROADCAST_LOCK = asyncio.Lock()
AUTO_BACKUP_TASK = None
PREMIUM_EMOJI_ENABLED = True
HTTP_SERVER = None
HTTP_SERVER_THREAD = None

BTN1, BTN2, BTN3 = "🎯 Claim Agent", "📊 Statistics", "🤝 Refer & Earn"
EMOJI = {"🎯": "5228855127892327218", "📊": "6093382540784046658", "🤝": "6086990448331592466", "📣": "6095891759462617671", "💬": "6095865895169560113", "📝": "6010292709066019210", "🖼️": "5341285075210224047", "➕": "6093406373557571574", "❌": "6010471186432005118", "⚙️": "6010355840790303830", "✅": "6246537187614005254", "🌟": "5783170625090622777", "📌": "6089019283508040459", "🔔": "6093852083788715042", "👑": "6247039939305808563", "💰": "5785325680765965100"}

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("force_join_bot")

# Conversation states
(S_CH_ID,S_CH_NAME,S_CH_LINK,S_WELCOME,S_WELCOME_PHOTO,S_POSTJOIN,S_TOP,S_BTN1,S_BTN2,S_BTN3,
 S_BCAST,S_RESTORE,S_SEARCH,S_USERMSG,S_EDITNAME,S_EDITLINK,S_EMOJI)=range(17)

@contextmanager
def db():
    c=sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    try:
        c.execute("PRAGMA busy_timeout=30000"); c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA synchronous=NORMAL")
        yield c; c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()

def esc(x): return html.escape(str(x or ""))
def scalar(sql,args=(),default=0):
    try:
        with db() as c:
            r=c.execute(sql,args).fetchone()
            return r[0] if r else default
    except Exception as e:
        logger.error("DB scalar: %s",e); return default

def gset(k,d=""):
    try:
        with db() as c:
            r=c.execute("SELECT value FROM settings WHERE key=?",(k,)).fetchone()
            return r[0] if r else d
    except Exception as e:
        logger.error("gset %s: %s",k,e); return d

def sset(k,v):
    with db() as c: c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",(k,str(v)))

def log_error(level,msg):
    global LAST_ERROR
    LAST_ERROR=str(msg)[:2000]
    try:
        with db() as c:
            c.execute("INSERT INTO error_logs(created_at,level,message) VALUES(?,?,?)",(datetime.now().isoformat(),level,str(msg)[:2000]))
            c.execute("DELETE FROM error_logs WHERE id NOT IN (SELECT id FROM error_logs ORDER BY id DESC LIMIT ?)",(ERROR_LOG_MAX,))
    except Exception: pass

class DBHandler(logging.Handler):
    def emit(self,record):
        if record.levelno>=logging.ERROR: log_error(record.levelname,record.getMessage())
logger.addHandler(DBHandler())

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY,first_name TEXT DEFAULT '',username TEXT DEFAULT '',joined_at TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active');
        CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id TEXT UNIQUE NOT NULL,channel_name TEXT NOT NULL,channel_link TEXT NOT NULL,position INTEGER DEFAULT 0,order_num INTEGER DEFAULT 0,enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS join_requests(user_id INTEGER NOT NULL,channel_id TEXT NOT NULL,requested_at TEXT,status TEXT DEFAULT 'active',PRIMARY KEY(user_id,channel_id));
        CREATE TABLE IF NOT EXISTS broadcast_msgs(bcast_id TEXT NOT NULL,user_id INTEGER NOT NULL,message_id INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS broadcasts(bcast_id TEXT PRIMARY KEY,created_at TEXT,source_chat_id INTEGER,source_message_id INTEGER,kind TEXT,total INTEGER DEFAULT 0,sent INTEGER DEFAULT 0,failed INTEGER DEFAULT 0,cancelled INTEGER DEFAULT 0,status TEXT DEFAULT 'running',last_error TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS error_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT,level TEXT,message TEXT);
        CREATE TABLE IF NOT EXISTS referrals(referral_id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER UNIQUE NOT NULL,referrer_id INTEGER NOT NULL,created_at TEXT NOT NULL);
        """)
        # Migrate original schema safely.
        cols={r[1] for r in c.execute("PRAGMA table_info(users)")}
        if "username" not in cols: c.execute("ALTER TABLE users ADD COLUMN username TEXT DEFAULT ''")
        if "status" not in cols: c.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        cols={r[1] for r in c.execute("PRAGMA table_info(channels)")}
        if "enabled" not in cols: c.execute("ALTER TABLE channels ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
        cols={r[1] for r in c.execute("PRAGMA table_info(join_requests)")}
        if "requested_at" not in cols: c.execute("ALTER TABLE join_requests ADD COLUMN requested_at TEXT")
        if "status" not in cols: c.execute("ALTER TABLE join_requests ADD COLUMN status TEXT DEFAULT 'active'")
        c.execute("UPDATE join_requests SET requested_at=COALESCE(requested_at,?)",(datetime.now().isoformat(),))
        defaults={
          "welcome":"👋 <b>Welcome!</b>\n\n🛑 Join all required channels below.\n\n💣 Then click <b>✅ Joined</b>",
          "welcome_photo":"","postjoin":"🏛️ <b>Welcome!</b>\n\n📋 <b>Rules</b>\n• One agent per user\n• Permanent assignment","top":"",
          "btn1_msg":"🎯 Agent claim coming soon!","btn2_msg":"📊 Statistics coming soon!","btn3_msg":"🤝 Refer & Earn coming soon!",
          "maintenance_mode":"0","force_join_enabled":"1","welcome_photo_enabled":"1","broadcast_enabled":"1","auto_backup_enabled":"0",
          "auto_backup_frequency":"daily","auto_backup_keep":"7","debug_logging":"0","last_backup":"","last_restore":"",
          "button_style_btn1":"primary","button_style_btn2":"success","button_style_btn3":"primary",
          "button_emoji_btn1":EMOJI["🎯"],"button_emoji_btn2":EMOJI["📊"],"button_emoji_btn3":EMOJI["🤝"],
          "button_emoji_enabled_btn1":"1","button_emoji_enabled_btn2":"1","button_emoji_enabled_btn3":"1",
        }
        for k,v in defaults.items(): c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,str(v)))

def control_ids():
    ids=set()
    if ADMIN_ID: ids.add(ADMIN_ID)
    if OWNER_ID: ids.add(OWNER_ID)
    return ids

def is_admin(uid): return bool(uid in control_ids())
def is_owner(uid): return bool(OWNER_ID and uid==OWNER_ID)

def add_user(u):
    with db() as c:
        old=c.execute("SELECT 1 FROM users WHERE user_id=?",(u.id,)).fetchone()
        if old:
            c.execute("UPDATE users SET first_name=?,username=? WHERE user_id=?",(u.first_name or "",u.username or "",u.id)); return False
        c.execute("INSERT INTO users VALUES(?,?,?,?,?)",(u.id,u.first_name or "",u.username or "",datetime.now().isoformat(),"active")); return True

def user_status(uid): return scalar("SELECT status FROM users WHERE user_id=?",(uid,),"active")
def set_status(uid,status):
    with db() as c:c.execute("UPDATE users SET status=? WHERE user_id=?",(status,uid))
def delete_user(uid):
    with db() as c:
        c.execute("DELETE FROM users WHERE user_id=?",(uid,)); c.execute("DELETE FROM join_requests WHERE user_id=?",(uid,))

def users(include_blocked=False):
    with db() as c:
        q="SELECT user_id FROM users" if include_blocked else "SELECT user_id FROM users WHERE status!='blocked'"
        return [r[0] for r in c.execute(q)]

def search_users(q):
    q=str(q).strip()
    with db() as c:
        if q.isdigit(): return c.execute("SELECT user_id,first_name,username,joined_at,status FROM users WHERE user_id=?",(int(q),)).fetchall()
        x=f"%{q}%"; return c.execute("SELECT user_id,first_name,username,joined_at,status FROM users WHERE first_name LIKE ? OR username LIKE ? LIMIT 25",(x,x)).fetchall()

def channels(all_rows=True):
    with db() as c:
        q="SELECT id,channel_id,channel_name,channel_link,position,order_num,enabled FROM channels ORDER BY order_num,id"
        if not all_rows:q=q.replace(" ORDER BY"," WHERE enabled=1 ORDER BY")
        return c.execute(q).fetchall()
def channel(cid):
    with db() as c:return c.execute("SELECT id,channel_id,channel_name,channel_link,position,order_num,enabled FROM channels WHERE id=?",(cid,)).fetchone()
def add_channel(cid,name,link):
    with db() as c:
        n=c.execute("SELECT COALESCE(MAX(order_num),0) FROM channels").fetchone()[0]+1
        c.execute("INSERT INTO channels(channel_id,channel_name,channel_link,order_num,enabled) VALUES(?,?,?, ?,1)",(str(cid),name,link,n))
def update_channel(cid,name=None,link=None):
    with db() as c:
        if name is not None:c.execute("UPDATE channels SET channel_name=? WHERE id=?",(name,cid))
        if link is not None:c.execute("UPDATE channels SET channel_link=? WHERE id=?",(link,cid))
def delete_channel(cid):
    with db() as c:c.execute("DELETE FROM channels WHERE id=?",(cid,))
def toggle_channel(cid):
    with db() as c:c.execute("UPDATE channels SET enabled=1-enabled WHERE id=?",(cid,))
def move_channel(cid,direction):
    rows=channels(True); ids=[r[0] for r in rows]
    if cid not in ids:return
    i=ids.index(cid); j=i-1 if direction=="left" else i+1
    if j<0 or j>=len(ids):return
    with db() as c:
        c.execute("UPDATE channels SET order_num=? WHERE id=?",(rows[j][5],rows[i][0]))
        c.execute("UPDATE channels SET order_num=? WHERE id=?",(rows[i][5],rows[j][0]))

def record_req(uid,cid):
    with db() as c:c.execute("INSERT OR REPLACE INTO join_requests VALUES(?,?,?,?)",(uid,str(cid),datetime.now().isoformat(),"active"))
def mark_req(uid,cid):
    with db() as c:c.execute("UPDATE join_requests SET status='joined' WHERE user_id=? AND channel_id=?",(uid,str(cid)))
def has_req(uid,cid):
    return bool(scalar("SELECT 1 FROM join_requests WHERE user_id=? AND channel_id=? AND status IN ('active','joined')",(uid,str(cid)),0))

async def check_joined(bot,uid):
    if gset("force_join_enabled","1")!="1":return True,set()
    rows=channels(False)
    if not rows:return True,set()
    joined=set()
    for r in rows:
        cid=r[1]
        try:
            m=await bot.get_chat_member(cid,uid)
            if m.status in ("member","administrator","creator","restricted"):
                joined.add(cid);mark_req(uid,cid);continue
        except TelegramError as e:logger.warning("Join check %s/%s: %s",cid,uid,e)
        if has_req(uid,cid):joined.add(cid)
    return len(joined)==len(rows),joined

async def channel_status(bot,cid):
    try:
        me=await bot.get_me(); ch=await bot.get_chat(cid); m=await bot.get_chat_member(cid,me.id)
        return True,m.status in ("administrator","creator"),ch.title or str(cid),m.status
    except TelegramError as e:return False,False,str(e),"error"

def ib(text,callback_data=None,url=None,style=None,emoji_id=None):
    kw={"text":text}
    if not PREMIUM_EMOJI_ENABLED: emoji_id=None
    if callback_data is not None:kw["callback_data"]=callback_data
    if url is not None:kw["url"]=url
    if style in ("primary","success","danger"):kw["style"]=style
    if emoji_id:kw["icon_custom_emoji_id"]=str(emoji_id)
    try:return InlineKeyboardButton(**kw)
    except (TypeError,ValueError):
        kw.pop("style",None);kw.pop("icon_custom_emoji_id",None);return InlineKeyboardButton(**kw)

def bstyle(k):
    x=gset("button_style_"+k,"primary");return x if x in ("primary","success","danger") else "primary"
def bemoji(k):
    return gset("button_emoji_"+k,"") if gset("button_emoji_enabled_"+k,"1")=="1" else None

def referral_count(uid):
    return scalar("SELECT COUNT(*) FROM referrals WHERE referrer_id=?",(uid,),0)

def referral_link(bot_username, uid):
    return f"https://t.me/{bot_username}?start={uid}"

async def referral_text(bot, uid):
    me=await bot.get_me()
    count=referral_count(uid)
    target=max(1,int(gset("referral_target","1") or "1"))
    reward=gset("referral_reward","1")
    needed=max(0,target-count)
    link=referral_link(me.username or "",uid)
    return (
        "🤩<b>ᴍʏ ʀᴇꜰᴇʀʀᴀʟ ʟɪɴᴋ</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💎 <b>ᴘᴇʀ ʀᴇꜰᴇʀʀ :</b> {esc(reward)}\n\n"
        f"👥 <b>ʀᴇꜰᴇʀʀᴀʟs :</b> {count}\n\n"
        f"🎯 <b>ᴛᴀʀɢᴇᴛ :</b> {target}\n\n"
        f"⏳ <b>ꜱᴛɪʟʟ ɴᴇᴇᴅᴇᴅ :</b> {needed}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔑 <b>ɪɴᴠɪᴛᴀᴛɪᴏɴ ᴄᴏᴅᴇ :</b> <code>{uid}</code>\n\n"
        f"<code>{esc(link)}</code>\n\n"
        "📨<b>ꜱʜᴀʀᴇ ᴡɪᴛʜ ꜰʀɪᴇɴᴅs.</b>"
    )

def referral_kb(link):
    share_url=("https://t.me/share/url?url="+quote(link,safe=""))+"&text="+quote("Join this bot using my referral link!",safe="")
    return InlineKeyboardMarkup([[ib("📤 Share Referral Link",url=share_url,style="success",emoji_id=EMOJI["📣"])],[ib("🔙 Back","back_main",style="primary",emoji_id=EMOJI["📌"])]] )

async def show_referral(update,ctx):
    uid=update.effective_user.id
    try:
        me=await ctx.bot.get_me()
        text=await referral_text(ctx.bot,uid)
        kb=referral_kb(referral_link(me.username or "",uid))
        if update.callback_query:
            q=update.callback_query
            await q.edit_message_text(text,reply_markup=kb,parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(text,reply_markup=kb,parse_mode=ParseMode.HTML)
    except TelegramError as e:
        logger.error("Referral UI failed: %s",e)

def process_referral(new_user_id, args):
    if not args: return False
    code=str(args[0]).strip()
    if not code.isdigit(): return False
    referrer_id=int(code)
    if referrer_id==new_user_id: return False
    if not scalar("SELECT 1 FROM users WHERE user_id=?",(referrer_id,),0): return False
    try:
        with db() as c:
            c.execute("INSERT INTO referrals(user_id,referrer_id,created_at) VALUES(?,?,?)",(new_user_id,referrer_id,datetime.now().isoformat()))
        return True
    except sqlite3.IntegrityError:
        return False

def main_kb():
    return InlineKeyboardMarkup([[ib(BTN1,"btn1",style=bstyle("btn1"),emoji_id=bemoji("btn1")),ib(BTN2,"btn2",style=bstyle("btn2"),emoji_id=bemoji("btn2"))],[ib(BTN3,"btn3",style=bstyle("btn3"),emoji_id=bemoji("btn3"))]])
def back_kb(cb="a_back"):return InlineKeyboardMarkup([[ib("🔙 Back",cb,style="primary",emoji_id=EMOJI["📌"])]])
def join_kb(rows,joined):
    a=[];b=[]
    for r in rows:
        if r[6] and r[1] not in joined:
            x=ib("📢 "+r[2],url=r[3],style="primary",emoji_id=EMOJI["📣"])
            (a if r[4]==0 else b).append(x)
    out=[]
    for i in range(max(len(a),len(b))):
        row=[]; 
        if i<len(a):row.append(a[i])
        if i<len(b):row.append(b[i])
        out.append(row)
    out.append([ib("✅ Joined","check_joined",style="success",emoji_id=EMOJI["✅"])])
    return InlineKeyboardMarkup(out)

async def send_welcome(bot,chat,text,kb):
    try:
        p=gset("welcome_photo")
        if p and gset("welcome_photo_enabled","1")=="1":
            await bot.send_photo(chat,p,caption=text,reply_markup=kb,parse_mode=ParseMode.HTML)
        else:await bot.send_message(chat,text=text,reply_markup=kb,parse_mode=ParseMode.HTML)
    except TelegramError as e:logger.error("Welcome send failed: %s",e)

async def start(update,ctx):
    u=update.effective_user
    if not u:return
    is_new=add_user(u)
    if is_new and ctx.args:
        process_referral(u.id,ctx.args)
    if user_status(u.id)=="blocked":return await update.message.reply_text("🚫 You are blocked from using this bot.")
    if gset("maintenance_mode","0")=="1" and not is_admin(u.id):return await update.message.reply_text("🛠 Bot is under maintenance.")
    rows=channels(False);ok,joined=await check_joined(ctx.bot,u.id)
    if not rows or ok:return await update.message.reply_text(gset("postjoin"),reply_markup=main_kb(),parse_mode=ParseMode.HTML)
    top=gset("top"); text=f"{top}\n\n{gset('welcome')}" if top else gset("welcome")
    await send_welcome(ctx.bot,u.id,text,join_kb(rows,joined))

async def cb_check(update,ctx):
    q=update.callback_query;uid=q.from_user.id
    if user_status(uid)=="blocked":return await q.answer("🚫 You are blocked.",show_alert=True)
    await q.answer(); rows=channels(False);ok,joined=await check_joined(ctx.bot,uid)
    if ok:
        try:await q.edit_message_text(gset("postjoin"),reply_markup=main_kb(),parse_mode=ParseMode.HTML)
        except TelegramError:await ctx.bot.send_message(q.message.chat_id,gset("postjoin"),reply_markup=main_kb(),parse_mode=ParseMode.HTML)
    else:
        await q.answer("🚫 Join every required channel, then press ✅ Joined again.",show_alert=True)
        try:await q.message.delete()
        except TelegramError:pass
        top=gset("top");text=f"{top}\n\n{gset('welcome')}" if top else gset("welcome")
        await send_welcome(ctx.bot,q.message.chat_id,text,join_kb(rows,joined))

async def cb_btn(update,ctx):
    q=update.callback_query
    if user_status(q.from_user.id)=="blocked":return await q.answer("🚫 You are blocked.",show_alert=True)
    await q.answer()
    if q.data=="btn3":
        return await show_referral(update,ctx)
    k={"btn1":"btn1_msg","btn2":"btn2_msg"}.get(q.data)
    if not k:return
    try:await q.edit_message_text(gset(k),reply_markup=back_kb("back_main"),parse_mode=ParseMode.HTML)
    except TelegramError:await ctx.bot.send_message(q.message.chat_id,gset(k),reply_markup=back_kb("back_main"),parse_mode=ParseMode.HTML)
async def cb_ref_share(update,ctx):
    q=update.callback_query
    await q.answer()
    try:
        me=await ctx.bot.get_me()
        link=referral_link(me.username or "",q.from_user.id)
        await q.message.reply_text(f"📨 <b>Share this referral link:</b>\n\n<code>{esc(link)}</code>",parse_mode=ParseMode.HTML)
    except TelegramError as e:
        logger.warning("Referral share failed: %s",e)

async def cb_back(update,ctx):
    q=update.callback_query;await q.answer()
    try:await q.edit_message_text(gset("postjoin"),reply_markup=main_kb(),parse_mode=ParseMode.HTML)
    except TelegramError:await ctx.bot.send_message(q.message.chat_id,gset("postjoin"),reply_markup=main_kb(),parse_mode=ParseMode.HTML)
async def join_request(update,ctx):
    r=update.chat_join_request
    if r:record_req(r.from_user.id,r.chat.id)

def dash():
    now=datetime.now();today=now.date().isoformat();week=(now-timedelta(days=now.weekday())).date().isoformat()
    nt=scalar("SELECT COUNT(*) FROM users WHERE substr(joined_at,1,10)=?",(today,),0);nw=scalar("SELECT COUNT(*) FROM users WHERE substr(joined_at,1,10)>=?",(week,),0)
    up=int(time.monotonic()-STARTED_AT);sz=Path(DB_PATH).stat().st_size if Path(DB_PATH).exists() else 0
    return ("╔═━━━✦ 🤖 <b>ADVANCED ADMIN DASHBOARD</b> ✦━━━═╗\n\n"
            f"👥 Total Users: <b>{scalar('SELECT COUNT(*) FROM users')}</b>\n🆕 New Today: <b>{nt}</b>\n📅 New This Week: <b>{nw}</b>\n"
            f"📢 Total Channels: <b>{len(channels(True))}</b>\n📨 Active Join Requests: <b>{scalar('SELECT COUNT(*) FROM join_requests WHERE status=\'active\'')}</b>\n"
            f"📣 Broadcasts: <b>{scalar('SELECT COUNT(*) FROM broadcasts')}</b> | Sent <b>{scalar('SELECT COALESCE(SUM(sent),0) FROM broadcasts')}</b> | Failed <b>{scalar('SELECT COALESCE(SUM(failed),0) FROM broadcasts')}</b>\n"
            f"⏱ Uptime: <b>{up//86400}d {(up%86400)//3600}h {(up%3600)//60}m</b>\n💾 DB Size: <b>{sz/1024:.1f} KB</b>\n"
            f"🗄 Last Backup: <b>{esc(gset('last_backup','—'))}</b>\n♻️ Last Restore: <b>{esc(gset('last_restore','—'))}</b>\n🟢 Bot Status: <b>ONLINE</b>\n"
            f"🧩 Version: <code>{BOT_VERSION}</code>\n╚═━━━━━━━━━━━━━━━━━━━━━━━━═╝")

def admin_kb():
    return InlineKeyboardMarkup([
      [ib("📊 Dashboard","a_dash",style="primary",emoji_id=EMOJI["📊"]),ib("📢 Channels","a_chs",style="primary",emoji_id=EMOJI["📣"])],
      [ib("📝 Messages","a_msgs",style="primary",emoji_id=EMOJI["📝"]),ib("🎨 Buttons","a_buttons",style="success",emoji_id=EMOJI["🌟"])],
      [ib("📣 Broadcast","a_bcast",style="success",emoji_id=EMOJI["📣"]),ib("👥 Members","a_members",style="primary",emoji_id=EMOJI["👑"])],
      [ib("💾 Backup Center","a_backup_menu",style="primary",emoji_id=EMOJI["⚙️"]),ib("♻️ Restore","a_restore",style="danger",emoji_id=EMOJI["✅"])],
      [ib("🩺 Database Health","a_dbhealth",style="primary",emoji_id=EMOJI["📊"]),ib("❤️ Bot Health","a_health",style="success",emoji_id=EMOJI["🌟"])],
      [ib("⚙️ Settings","a_settings",style="primary",emoji_id=EMOJI["⚙️"]),ib("🧪 Premium Button Test","a_premium_test",style="success",emoji_id=EMOJI["🌟"])],
      [ib("📜 Error Log","a_errors",style="primary",emoji_id=EMOJI["📝"]),ib("❌ Close","a_close",style="danger",emoji_id=EMOJI["❌"])]
    ])

async def admin_cmd(update,ctx):
    if not is_admin(update.effective_user.id):return await update.message.reply_text("❌ Not authorized!")
    await update.message.reply_text(dash(),reply_markup=admin_kb(),parse_mode=ParseMode.HTML)

async def dkboss_cmd(update,ctx):
    if not is_owner(update.effective_user.id):return await update.message.reply_text("❌ DK BOSS access denied!")
    await update.message.reply_text(
        "👑 <b>DK BOSS OWNER PANEL</b>\n\n" + dash(),
        reply_markup=admin_kb(), parse_mode=ParseMode.HTML
    )

def ch_kb():
    rows=[]
    for r in channels(True):
        cid,name,en=r[0],r[2],r[6]
        rows += [[ib("✏️ "+name[:16],f"a_editc_{cid}",style="primary",emoji_id=EMOJI["📝"]),ib("⬅️",f"a_left_{cid}",style="primary",emoji_id=EMOJI["📌"]),ib("➡️",f"a_right_{cid}",style="primary",emoji_id=EMOJI["📌"])],
                 [ib("🟢 Enable" if not en else "🔴 Disable",f"a_togglec_{cid}",style="success" if not en else "danger",emoji_id=EMOJI["✅"] if not en else EMOJI["❌"]),ib("🧪 Test",f"a_testc_{cid}",style="success",emoji_id=EMOJI["🌟"]),ib("🗑 Delete",f"a_delc_{cid}",style="danger",emoji_id=EMOJI["❌"])]]
    rows += [[ib("➕ Add Channel","a_addch",style="success",emoji_id=EMOJI["➕"])],[ib("🔙 Back","a_back",style="primary",emoji_id=EMOJI["📌"])]]
    return InlineKeyboardMarkup(rows)

async def show_channels(q):
    txt="📢 <b>CHANNEL MANAGEMENT</b>\n\n"
    for i,r in enumerate(channels(True),1):txt+=f"{i}. {'🟢' if r[6] else '🔴'} <b>{esc(r[2])}</b>\n   ID: <code>{esc(r[1])}</code>\n   Link: {esc(r[3])}\n"
    await q.edit_message_text(txt if len(channels(True)) else txt+"No channels configured.",reply_markup=ch_kb(),parse_mode=ParseMode.HTML)

def msg_kb():
    return InlineKeyboardMarkup([[ib("📢 Top Message","a_top",style="primary",emoji_id=EMOJI["📣"])],[ib("👋 Welcome Message","a_welcome",style="primary",emoji_id=EMOJI["💬"])],[ib("🖼 Welcome Photo","a_welcome_photo",style="success",emoji_id=EMOJI["🖼️"])],[ib("🎉 Post-Join Message","a_postjoin",style="success",emoji_id=EMOJI["🌟"])],[ib("✉️ "+BTN1,"a_btn1",style="primary",emoji_id=EMOJI["🎯"])],[ib("✉️ "+BTN2,"a_btn2",style="success",emoji_id=EMOJI["📊"])],[ib("✉️ "+BTN3,"a_btn3",style="primary",emoji_id=EMOJI["🤝"])],[ib("🔙 Back","a_back",style="primary",emoji_id=EMOJI["📌"])]])

def btn_kb():
    rows=[]
    for k,n in (("btn1",BTN1),("btn2",BTN2),("btn3",BTN3)):
        rows += [[ib("🎨 "+n[:16],f"a_style_{k}",style=bstyle(k),emoji_id=bemoji(k)),ib("Style: "+bstyle(k),f"a_style_{k}",style=bstyle(k))],
                 [ib("🧩 Emoji "+("ON" if bemoji(k) else "OFF"),f"a_emoji_{k}",style="success" if bemoji(k) else "danger",emoji_id=EMOJI["🌟"]),
                  ib("🆔 Set Emoji ID",f"a_setemoji_{k}",style="primary",emoji_id=EMOJI["📌"]),
                  ib("👁 Preview",f"a_preview_{k}",style="primary",emoji_id=EMOJI["📌"])],
                 [ib("↩️ Reset",f"a_reset_{k}",style="danger",emoji_id=EMOJI["❌"])]]
    rows += [[ib("🧪 Premium Button Test","a_premium_test",style="success",emoji_id=EMOJI["🌟"])],[ib("🔙 Back","a_back",style="primary",emoji_id=EMOJI["📌"])]]
    return InlineKeyboardMarkup(rows)

def settings_kb():
    def t(k,n):
        on=gset(k,"0")=="1";return ib(("🟢 " if on else "🔴 ")+n+": "+("ON" if on else "OFF"),"a_toggle_"+k,style="success" if on else "danger",emoji_id=EMOJI["✅"] if on else EMOJI["❌"])
    return InlineKeyboardMarkup([[t("maintenance_mode","Maintenance Mode")],[t("force_join_enabled","Force Join")],[t("welcome_photo_enabled","Welcome Photo")],[t("broadcast_enabled","Broadcast")],[t("auto_backup_enabled","Auto Backup")],[t("debug_logging","Debug Logging")],[ib("🕒 Auto Backup: "+gset("auto_backup_frequency","daily"),"a_autobackup",style="primary",emoji_id=EMOJI["🔔"])],[ib("🔙 Back","a_back",style="primary",emoji_id=EMOJI["📌"])]] )

def health():
    up=int(time.monotonic()-STARTED_AT)
    return ("❤️ <b>BOT HEALTH</b>\n\n🟢 Online\n"
            f"Uptime: <b>{up//86400}d {(up%86400)//3600}h {(up%3600)//60}m</b>\n"
            f"Python: <code>{sys.version.split()[0]}</code>\nPTB: <code>{telegram.__version__}</code>\n"
            f"DB: <code>{esc(DB_PATH)}</code>\nLast Error: <code>{esc(LAST_ERROR or 'None')}</code>\n"
            f"Last Backup: <code>{esc(gset('last_backup','None'))}</code>\nLast Restore: <code>{esc(gset('last_restore','None'))}</code>")

def dbhealth():
    try:
        with db() as c:
            ok=c.execute("PRAGMA integrity_check").fetchone()[0];wal=c.execute("PRAGMA journal_mode").fetchone()[0]
            vals=[("Users","users"),("Channels","channels"),("Settings","settings"),("Join Requests","join_requests"),("Broadcast Records","broadcasts")]
            s=f"🩺 <b>DATABASE HEALTH</b>\n\nStatus: {'🟢 HEALTHY' if ok=='ok' else '🔴 CORRUPT'}\nWAL: <b>{wal}</b>\nSize: <b>{Path(DB_PATH).stat().st_size/1024:.1f} KB</b>\n"
            return s+"\n".join(f"{n}: <b>{c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]}</b>" for n,t in vals)
    except Exception as e:return "🔴 Database health check failed:\n<code>"+esc(e)+"</code>"

def safe_zip(z):
    for n in z.namelist():
        p=Path(n)
        if p.is_absolute() or ".." in p.parts:raise ValueError("Unsafe backup archive: path traversal detected.")

def validate_backup(path):
    with zipfile.ZipFile(path) as z:
        safe_zip(z)
        if z.testzip() is not None or not {"bot2.db","manifest.txt"}.issubset(set(z.namelist())):raise ValueError("Invalid backup archive.")
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"bot2.db"
            with z.open("bot2.db") as s:
                with p.open("wb") as p2: shutil.copyfileobj(s,p2)
            c=sqlite3.connect(p);ok=c.execute("PRAGMA integrity_check").fetchone()[0];tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")};c.close()
            req={"users","channels","settings","join_requests","broadcast_msgs","broadcasts","error_logs","referrals"}
            if ok!="ok" or not req.issubset(tables):raise ValueError("Database integrity/required table validation failed.")

def create_backup():
    ts=datetime.now().strftime("%Y%m%d_%H%M%S");zip_path=BACKUP_DIR/f"bot_backup_{ts}.zip";tmp=BACKUP_DIR/f".{ts}.db";man=BACKUP_DIR/f".{ts}.txt"
    try:
        src=sqlite3.connect(DB_PATH);dst=sqlite3.connect(tmp)
        with dst:src.backup(dst)
        src.close();dst.close()
        validate_backup_from_db(tmp)
        man.write_text(f"Telegram Bot Backup\nBackup Date: {datetime.now().isoformat()}\nDatabase Version: {DB_VERSION}\nBot Version: {BOT_VERSION}\nDatabase Name: {Path(DB_PATH).name}\nSecrets: BOT_TOKEN is NOT included.\n",encoding="utf-8")
        with zipfile.ZipFile(zip_path,"w",zipfile.ZIP_DEFLATED) as z:z.write(tmp,"bot2.db");z.write(man,"manifest.txt")
        sset("last_backup",datetime.now().isoformat());return zip_path
    finally:tmp.unlink(missing_ok=True);man.unlink(missing_ok=True)

def validate_backup_from_db(p):
    c=sqlite3.connect(p);ok=c.execute("PRAGMA integrity_check").fetchone()[0];tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")};c.close()
    req={"users","channels","settings","join_requests","broadcast_msgs","broadcasts","error_logs","referrals"}
    if ok!="ok" or not req.issubset(tables):raise ValueError("Database integrity check failed.")

def restore_backup(path):
    validate_backup(path);safety=create_backup()
    with zipfile.ZipFile(path) as z,tempfile.TemporaryDirectory() as td:
        p=Path(td)/"bot2.db"
        with z.open("bot2.db") as s:
                with p.open("wb") as p2: shutil.copyfileobj(s,p2)
        validate_backup_from_db(p)
        src=sqlite3.connect(p);dst=sqlite3.connect(DB_PATH)
        with dst:src.backup(dst)
        src.close();dst.close()
    sset("last_restore",datetime.now().isoformat());return safety

async def premium_test(bot,chat):
    eid=EMOJI["🌟"];cb="premium_button_test"
    try:
        kb=InlineKeyboardMarkup([[ib("Premium Button Test",cb,style="success",emoji_id=eid)]])
        await bot.send_message(chat,"🧪 <b>Premium Button Test</b>\n\nStyle: <code>success</code>\nicon_custom_emoji_id: <code>"+eid+"</code>\ncallback_data: <code>"+cb+"</code>",reply_markup=kb,parse_mode=ParseMode.HTML)
        return True
    except TelegramError:
        try:
            await bot.send_message(chat,"Custom emoji button is unavailable for this bot/account.\n\nNormal button fallback is active.",reply_markup=InlineKeyboardMarkup([[ib("Premium Button Test",cb,style="success")]]))
        except TelegramError:logger.exception("Premium fallback failed")
        return False

def backup_list():return sorted(BACKUP_DIR.glob("*.zip"),key=lambda p:p.stat().st_mtime,reverse=True)
def cleanup_backups():
    try:n=max(1,int(gset("auto_backup_keep","7")))
    except ValueError:n=7
    for p in backup_list()[n:]:p.unlink(missing_ok=True)

def bcast_create(chat,mid,kind,total):
    bid=uuid.uuid4().hex[:16]
    with db() as c:c.execute("INSERT INTO broadcasts VALUES(?,?,?,?,?,?,?,?,?,?,?)",(bid,datetime.now().isoformat(),chat,mid,kind,total,0,0,0,"running",""))
    return bid
def bcast_update(bid,sent,failed,cancelled,status,err=""):
    with db() as c:c.execute("UPDATE broadcasts SET sent=?,failed=?,cancelled=?,status=?,last_error=? WHERE bcast_id=?",(sent,failed,int(cancelled),status,str(err)[:2000],bid))
def bcast_save(bid,uid,mid):
    with db() as c:c.execute("INSERT INTO broadcast_msgs VALUES(?,?,?)",(bid,uid,mid))
def bcast_rows(bid):
    with db() as c:return c.execute("SELECT user_id,message_id FROM broadcast_msgs WHERE bcast_id=?",(bid,)).fetchall()

async def run_broadcast(bot,admin_chat,msg,bid,recipient_list,status_msg):
    sent=failed=0;cancelled=False
    for i,uid in enumerate(recipient_list,1):
        if asyncio.current_task().cancelled():cancelled=True;break
        try:
            m=await bot.copy_message(uid,msg.chat_id,msg.message_id);bcast_save(bid,uid,m.message_id);sent+=1
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after)+.5)
            try:m=await bot.copy_message(uid,msg.chat_id,msg.message_id);bcast_save(bid,uid,m.message_id);sent+=1
            except Exception:failed+=1
        except Forbidden:
            failed+=1;set_status(uid,"inactive")
        except TelegramError as e:failed+=1;logger.warning("Broadcast %s failed for %s: %s",bid,uid,e)
        if i%10==0 or i==len(recipient_list):
            try:await status_msg.edit_text(f"📣 <b>Broadcasting...</b>\n\n📤 Progress: <b>{i}/{len(recipient_list)}</b>\n✅ Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>",reply_markup=InlineKeyboardMarkup([[ib("⏹ Cancel Broadcast",f"a_cancelbc_{bid}",style="danger",emoji_id=EMOJI["❌"])] ]),parse_mode=ParseMode.HTML)
            except TelegramError:pass
        await asyncio.sleep(.08)
    status="cancelled" if cancelled else "completed";bcast_update(bid,sent,failed,cancelled,status)
    kb=InlineKeyboardMarkup([[ib("🔁 Retry Failed",f"a_retrybc_{bid}",style="success",emoji_id=EMOJI["🌟"])],[ib("🗑 Delete Broadcast",f"a_delbc_{bid}",style="danger",emoji_id=EMOJI["❌"])],[ib("🔙 Back","a_bcast",style="primary",emoji_id=EMOJI["📌"])]])
    try:await status_msg.edit_text(f"{'⏹' if cancelled else '✅'} <b>Broadcast {status.title()}</b>\n\n📤 Total: <b>{len(recipient_list)}</b>\n✅ Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>",reply_markup=kb,parse_mode=ParseMode.HTML)
    except TelegramError:pass

async def start_bcast(update,ctx):
    global BROADCAST_TASK
    if not is_admin(update.effective_user.id):return ConversationHandler.END
    if gset("broadcast_enabled","1")!="1":await update.message.reply_text("🚫 Broadcast is disabled.");return ConversationHandler.END
    if BROADCAST_LOCK.locked():await update.message.reply_text("⚠️ Another broadcast is already running.");return ConversationHandler.END
    if not (update.message.text or update.message.photo or update.message.video):await update.message.reply_text("❌ Supported: Text, Photo, Video.");return S_BCAST
    us=users();bid=bcast_create(update.message.chat_id,update.message.message_id,"photo" if update.message.photo else "video" if update.message.video else "text",len(us))
    sm=await update.message.reply_text(f"⏳ Starting broadcast to <b>{len(us)}</b> recipients...",parse_mode=ParseMode.HTML)
    async def runner():
        async with BROADCAST_LOCK:await run_broadcast(ctx.bot,update.effective_chat.id,update.message,bid,us,sm)
    BROADCAST_TASK=asyncio.create_task(runner());return ConversationHandler.END

async def cancel_bcast(q,bid):
    global BROADCAST_TASK
    if BROADCAST_TASK and not BROADCAST_TASK.done():
        BROADCAST_TASK.cancel()
        try:await BROADCAST_TASK
        except asyncio.CancelledError:pass
    bcast_update(bid,0,0,True,"cancelled")

async def input_text(update,ctx,key,label,clear=False):
    if not update.message.text:return False
    v=update.message.text.strip();v="" if clear and v.lower()=="clear" else update.message.text_html.strip();sset(key,v)
    await update.message.reply_text("✅ "+label+" updated.",reply_markup=back_kb("a_msgs"));return True

async def s_top(u,c):return ConversationHandler.END if await input_text(u,c,"top","Top Message",True) else S_TOP
async def s_welcome(u,c):return ConversationHandler.END if await input_text(u,c,"welcome","Welcome Message") else S_WELCOME
async def s_postjoin(u,c):return ConversationHandler.END if await input_text(u,c,"postjoin","Post-Join Message") else S_POSTJOIN
async def s_btn1(u,c):return ConversationHandler.END if await input_text(u,c,"btn1_msg","Button 1 Reply") else S_BTN1
async def s_btn2(u,c):return ConversationHandler.END if await input_text(u,c,"btn2_msg","Button 2 Reply") else S_BTN2
async def s_btn3(u,c):return ConversationHandler.END if await input_text(u,c,"btn3_msg","Button 3 Reply") else S_BTN3

async def s_photo(u,c):
    if u.message.text and u.message.text.strip().lower()=="clear":sset("welcome_photo","");await u.message.reply_text("✅ Welcome photo cleared.",reply_markup=back_kb("a_msgs"));return ConversationHandler.END
    if not u.message.photo:await u.message.reply_text("❌ Send a photo or 'clear'.");return S_WELCOME_PHOTO
    sset("welcome_photo",u.message.photo[-1].file_id);await u.message.reply_text("✅ Welcome photo updated.",reply_markup=back_kb("a_msgs"));return ConversationHandler.END

async def s_ch_id(u,c):c.user_data["ch_id"]=(u.message.text or "").strip();await u.message.reply_text("✏️ Send Channel Name:");return S_CH_NAME
async def s_ch_name(u,c):c.user_data["ch_name"]=(u.message.text or "").strip();await u.message.reply_text("🔗 Send Channel Invite Link:");return S_CH_LINK
async def s_ch_link(u,c):
    link=(u.message.text or "").strip()
    if not link.startswith(("https://t.me/","http://t.me/")):await u.message.reply_text("❌ Invalid Telegram link.");return S_CH_LINK
    try:add_channel(c.user_data["ch_id"],c.user_data["ch_name"],link)
    except sqlite3.IntegrityError:await u.message.reply_text("❌ Channel already exists.");return ConversationHandler.END
    c.user_data.clear();await u.message.reply_text("✅ Channel added.",reply_markup=back_kb("a_chs"));return ConversationHandler.END

async def s_editname(u,c):
    cid=c.user_data.get("edit_channel")
    if not cid:return ConversationHandler.END
    update_channel(cid,name=(u.message.text or "").strip());await u.message.reply_text("🔗 Send new invite link or <code>skip</code>.",parse_mode=ParseMode.HTML);return S_EDITLINK
async def s_editlink(u,c):
    cid=c.user_data.get("edit_channel");v=(u.message.text or "").strip()
    if v.lower()!="skip":
        if not v.startswith(("https://t.me/","http://t.me/")):await u.message.reply_text("❌ Invalid Telegram link.");return S_EDITLINK
        update_channel(cid,link=v)
    c.user_data.clear();await u.message.reply_text("✅ Channel updated.",reply_markup=back_kb("a_chs"));return ConversationHandler.END

async def s_emoji(u,c):
    k=c.user_data.get("emoji_key")
    if not k:return ConversationHandler.END
    v=(u.message.text or "").strip()
    if v.lower()=="clear":
        sset("button_emoji_"+k,"");sset("button_emoji_enabled_"+k,"0")
        await u.message.reply_text("🧩 Custom emoji disabled.",reply_markup=back_kb("a_buttons"))
        c.user_data.clear();return ConversationHandler.END
    if not v.isdigit():
        await u.message.reply_text("❌ Custom emoji ID must be numeric.")
        return S_EMOJI
    sset("button_emoji_"+k,v);sset("button_emoji_enabled_"+k,"1")
    await u.message.reply_text("✅ Custom emoji ID saved.",reply_markup=back_kb("a_buttons"))
    c.user_data.clear();return ConversationHandler.END

async def s_restore(u,c):
    if not is_admin(u.effective_user.id):return ConversationHandler.END
    d=u.message.document
    if not d or not (d.file_name or "").lower().endswith(".zip"):await u.message.reply_text("❌ Send a backup .zip file.");return S_RESTORE
    p=None
    try:
        f=await c.bot.get_file(d.file_id)
        with tempfile.NamedTemporaryFile(suffix=".zip",delete=False) as t:p=t.name
        await f.download_to_drive(p);safety=restore_backup(p)
        await u.message.reply_text("✅ <b>Restore completed.</b>\n\n🔐 Safety backup created.\n🔄 Restart bot to apply restored state.",parse_mode=ParseMode.HTML)
    except Exception as e:logger.exception("Restore");await u.message.reply_text("❌ Restore failed:\n<code>"+esc(e)+"</code>",parse_mode=ParseMode.HTML)
    finally:
        if p:
            try:os.remove(p)
            except OSError:pass
    return ConversationHandler.END

async def s_search(u,c):
    rows=search_users(u.message.text or "")
    if not rows:await u.message.reply_text("❌ No users found.",reply_markup=back_kb("a_members"));return ConversationHandler.END
    kb=[]
    for r in rows:kb.append([ib("👁 View",f"a_viewu_{r[0]}",style="primary",emoji_id=EMOJI["📊"]),ib("🚫 Block",f"a_blocku_{r[0]}",style="danger",emoji_id=EMOJI["❌"])])
    await u.message.reply_text("\n".join(f"👤 <b>{esc(r[1])}</b> · <code>{r[0]}</code> · @{esc(r[2]) if r[2] else '—'} · {esc(r[4])}" for r in rows),reply_markup=InlineKeyboardMarkup(kb),parse_mode=ParseMode.HTML);return ConversationHandler.END

async def s_usermsg(u,c):
    uid=c.user_data.get("msg_uid")
    try:await c.bot.copy_message(uid,u.message.chat_id,u.message.message_id);await u.message.reply_text("✅ Message sent.",reply_markup=back_kb("a_members"))
    except TelegramError as e:await u.message.reply_text("❌ Send failed: "+esc(e),reply_markup=back_kb("a_members"))
    c.user_data.clear();return ConversationHandler.END

async def admin_cb(update,ctx):
    q=update.callback_query
    if not is_admin(q.from_user.id):await q.answer("❌ Not authorized!",show_alert=True);return ConversationHandler.END
    await q.answer();d=q.data or ""
    # User-management callbacks are handled here because the admin ConversationHandler
    # owns all ^a_ callbacks.
    if d.startswith("a_viewu_"):
        uid=int(d.split("_")[-1])
        with db() as c:r=c.execute("SELECT user_id,first_name,username,joined_at,status FROM users WHERE user_id=?",(uid,)).fetchone()
        if not r:return await q.answer("User not found.",show_alert=True)
        await q.edit_message_text(
            f"👤 <b>USER</b>\n\nID: <code>{r[0]}</code>\nFirst Name: <b>{esc(r[1])}</b>\n"
            f"Username: <b>@{esc(r[2]) if r[2] else '—'}</b>\nJoin Date: <code>{esc(r[3])}</code>\nStatus: <b>{esc(r[4])}</b>",
            reply_markup=InlineKeyboardMarkup([
                [ib("💬 Send Message",f"a_msgu_{uid}",style="primary",emoji_id=EMOJI["💬"])],
                [ib("🚫 Block",f"a_blocku_{uid}",style="danger",emoji_id=EMOJI["❌"]),
                 ib("✅ Unblock",f"a_unblocku_{uid}",style="success",emoji_id=EMOJI["✅"])],
                [ib("🗑 Delete",f"a_deleteu_{uid}",style="danger",emoji_id=EMOJI["❌"])],
                [ib("🔙 Back","a_members",style="primary",emoji_id=EMOJI["📌"])]
            ]),parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    if d.startswith("a_blocku_"):
        uid=int(d.split("_")[-1])
        await q.edit_message_text("⚠️ <b>Are you sure?</b>\n\nBlock this user?",
            reply_markup=InlineKeyboardMarkup([[ib("✅ Confirm",f"a_confirmblock_{uid}",style="danger",emoji_id=EMOJI["❌"]),
                                                 ib("❌ Cancel",f"a_viewu_{uid}",style="primary",emoji_id=EMOJI["📌"])]]),
            parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    if d.startswith("a_confirmblock_"):
        set_status(int(d.split("_")[-1]),"blocked")
        await q.edit_message_text("🚫 User blocked.",reply_markup=back_kb("a_members"))
        return ConversationHandler.END
    if d.startswith("a_unblocku_"):
        set_status(int(d.split("_")[-1]),"active")
        await q.edit_message_text("✅ User unblocked.",reply_markup=back_kb("a_members"))
        return ConversationHandler.END
    if d.startswith("a_deleteu_"):
        uid=int(d.split("_")[-1])
        await q.edit_message_text("⚠️ <b>Are you sure?</b>\n\nDelete this user permanently?",
            reply_markup=InlineKeyboardMarkup([[ib("✅ Confirm",f"a_confirmdeleteu_{uid}",style="danger",emoji_id=EMOJI["❌"]),
                                                 ib("❌ Cancel",f"a_viewu_{uid}",style="primary",emoji_id=EMOJI["📌"])]]),
            parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    if d.startswith("a_confirmdeleteu_"):
        delete_user(int(d.split("_")[-1]))
        await q.edit_message_text("🗑 User deleted.",reply_markup=back_kb("a_members"))
        return ConversationHandler.END
    if d.startswith("a_msgu_"):
        ctx.user_data["msg_uid"]=int(d.split("_")[-1])
        await q.edit_message_text("💬 Send the message to deliver to this user.")
        return S_USERMSG

    if d in ("a_back","a_dash"):await q.edit_message_text(dash(),reply_markup=admin_kb(),parse_mode=ParseMode.HTML);return ConversationHandler.END
    if d=="a_close":await q.edit_message_text("❌ Admin panel closed.");return ConversationHandler.END
    if d=="a_chs":await show_channels(q);return ConversationHandler.END
    if d=="a_addch":await q.edit_message_text("📢 <b>Add Channel</b>\n\nSend Channel ID.",parse_mode=ParseMode.HTML);return S_CH_ID
    if d.startswith("a_delc_"):
        cid=int(d.split("_")[-1]);ctx.user_data["confirm"]=("delc",cid)
        await q.edit_message_text("⚠️ <b>Are you sure?</b>\n\nDelete this channel?",reply_markup=InlineKeyboardMarkup([[ib("✅ Confirm",f"a_confirm_{cid}",style="danger",emoji_id=EMOJI["❌"]),ib("❌ Cancel","a_chs",style="primary",emoji_id=EMOJI["📌"])] ]),parse_mode=ParseMode.HTML);return ConversationHandler.END
    if d.startswith("a_confirm_"):
        cid=int(d.split("_")[-1]);a,t=ctx.user_data.pop("confirm",("",None))
        if a=="delc" and t==cid:delete_channel(cid)
        await show_channels(q);return ConversationHandler.END
    if d.startswith("a_left_"):move_channel(int(d.split("_")[-1]),"left");await show_channels(q);return ConversationHandler.END
    if d.startswith("a_right_"):move_channel(int(d.split("_")[-1]),"right");await show_channels(q);return ConversationHandler.END
    if d.startswith("a_togglec_"):toggle_channel(int(d.split("_")[-1]));await show_channels(q);return ConversationHandler.END
    if d.startswith("a_testc_"):
        r=channel(int(d.split("_")[-1]));ok,adm,title,status=await channel_status(ctx.bot,r[1])
        await q.edit_message_text(f"🧪 <b>Channel Status</b>\n\nName: <b>{esc(r[2])}</b>\nAccessible: {'✅ YES' if ok else '❌ NO'}\nBot Admin: {'✅ YES' if adm else '❌ NO'}\nStatus: <code>{esc(status)}</code>",reply_markup=back_kb("a_chs"),parse_mode=ParseMode.HTML);return ConversationHandler.END
    if d.startswith("a_editc_"):
        ctx.user_data["edit_channel"]=int(d.split("_")[-1]);await q.edit_message_text("✏️ Send new channel name:");return S_EDITNAME
    if d=="a_msgs":await q.edit_message_text("📝 <b>MESSAGE MANAGEMENT</b>\n\nHTML formatting is preserved.",reply_markup=msg_kb(),parse_mode=ParseMode.HTML);return ConversationHandler.END
    mm={"a_top":("top","Top Message",S_TOP),"a_welcome":("welcome","Welcome Message",S_WELCOME),"a_postjoin":("postjoin","Post-Join Message",S_POSTJOIN),"a_btn1":("btn1_msg",BTN1,S_BTN1),"a_btn2":("btn2_msg",BTN2,S_BTN2),"a_btn3":("btn3_msg",BTN3,S_BTN3)}
    if d in mm:
        k,n,s=mm[d];await q.edit_message_text(f"✏️ <b>{esc(n)}</b>\n\nCurrent:\n<pre>{esc(gset(k)[:1500])}</pre>\n\nSend new text.",parse_mode=ParseMode.HTML);return s
    if d=="a_welcome_photo":await q.edit_message_text("🖼 <b>Welcome Photo</b>\n\nSend a photo or <code>clear</code>.",parse_mode=ParseMode.HTML);return S_WELCOME_PHOTO
    if d=="a_buttons":await q.edit_message_text("🎨 <b>BUTTON MANAGEMENT</b>\n\nprimary=blue, success=green, danger=red.\nCustom emoji uses icon_custom_emoji_id.",reply_markup=btn_kb(),parse_mode=ParseMode.HTML);return ConversationHandler.END
    if d.startswith("a_style_"):
        k=d[len("a_style_"):];n={"primary":"success","success":"danger","danger":"primary"}[bstyle(k)];sset("button_style_"+k,n);await q.edit_message_text("🎨 Button style updated.",reply_markup=btn_kb());return ConversationHandler.END
    if d.startswith("a_emoji_"):
        k=d[len("a_emoji_"):];sset("button_emoji_enabled_"+k,"0" if bemoji(k) else "1");await q.edit_message_text("🧩 Button emoji setting updated.",reply_markup=btn_kb());return ConversationHandler.END
    if d.startswith("a_reset_"):
        k=d[len("a_reset_"):];defs={"btn1":("primary",EMOJI["🎯"]),"btn2":("success",EMOJI["📊"]),"btn3":("primary",EMOJI["🤝"])};st,e=defs[k];sset("button_style_"+k,st);sset("button_emoji_"+k,e);sset("button_emoji_enabled_"+k,"1");await q.edit_message_text("↩️ Button reset.",reply_markup=btn_kb());return ConversationHandler.END
    if d.startswith("a_preview_"):
        k=d[len("a_preview_"):];n={"btn1":BTN1,"btn2":BTN2,"btn3":BTN3}[k];await q.message.reply_text("🎨 Preview",reply_markup=InlineKeyboardMarkup([[ib(n,"preview_"+k,style=bstyle(k),emoji_id=bemoji(k))]]));return ConversationHandler.END
    if d.startswith("a_setemoji_"):
        k=d[len("a_setemoji_"):]
        ctx.user_data["emoji_key"]=k
        await q.edit_message_text(
            f"🆔 <b>Custom Emoji ID — {esc(k)}</b>\n\n"
            "Send the numeric Telegram custom emoji ID.\n"
            "Send <code>clear</code> to disable the ID.",
            parse_mode=ParseMode.HTML)
        return S_EMOJI
    if d=="a_premium_test":
        ok=await premium_test(ctx.bot,q.message.chat_id)
        if not ok:await q.answer("Custom emoji unavailable; fallback sent.",show_alert=True)
        return ConversationHandler.END
    if d=="a_members":
        await q.edit_message_text(f"👥 <b>MEMBERS</b>\n\nTotal Users: <b>{scalar('SELECT COUNT(*) FROM users')}</b>",reply_markup=InlineKeyboardMarkup([[ib("🔎 Search User","a_search",style="primary",emoji_id=EMOJI["📊"])],[ib("📤 Export Users","a_export",style="success",emoji_id=EMOJI["📌"])],[ib("🔙 Back","a_back",style="primary",emoji_id=EMOJI["📌"])] ]),parse_mode=ParseMode.HTML);return ConversationHandler.END
    if d=="a_search":await q.edit_message_text("🔎 Send User ID, username, or name:");return S_SEARCH
    if d=="a_export":
        p=Path(tempfile.gettempdir())/f"users_{int(time.time())}.csv"
        with db() as c:
            with p.open("w",newline="",encoding="utf-8") as p2:
                w=csv.writer(p2);w.writerow(["user_id","first_name","username","join_date","status"]);w.writerows(c.execute("SELECT user_id,first_name,username,joined_at,status FROM users ORDER BY joined_at"))
        try:
            with p.open("rb") as f:await q.message.reply_document(f,filename=p.name)
        finally:p.unlink(missing_ok=True)
        return ConversationHandler.END
    if d=="a_bcast":
        rows=scalar("SELECT COUNT(*) FROM broadcasts");await q.edit_message_text(f"📣 <b>BROADCAST CENTER</b>\n\nHistory records: <b>{rows}</b>",reply_markup=InlineKeyboardMarkup([[ib("📣 New Broadcast","a_newbcast",style="success",emoji_id=EMOJI["📣"])],[ib("🔙 Back","a_back",style="primary",emoji_id=EMOJI["📌"])] ]),parse_mode=ParseMode.HTML);return ConversationHandler.END
    if d=="a_newbcast":await q.edit_message_text("📣 <b>Send Text, Photo+Caption, or Video+Caption.</b>",parse_mode=ParseMode.HTML);return S_BCAST
    if d.startswith("a_cancelbc_"):await cancel_bcast(q,d[len("a_cancelbc_"):]);await q.edit_message_text("⏹ Broadcast cancelled.",reply_markup=back_kb("a_bcast"));return ConversationHandler.END
    if d.startswith("a_delbc_"):
        bid=d[len("a_delbc_"):];removed=0
        for uid,mid in bcast_rows(bid):
            try:await ctx.bot.delete_message(uid,mid);removed+=1
            except TelegramError:pass
        with db() as c:c.execute("DELETE FROM broadcast_msgs WHERE bcast_id=?",(bid,));c.execute("DELETE FROM broadcasts WHERE bcast_id=?",(bid,))
        await q.edit_message_text(f"🗑 Broadcast deleted.\nRemoved from <b>{removed}</b> chats.",reply_markup=back_kb("a_bcast"),parse_mode=ParseMode.HTML);return ConversationHandler.END
    if d.startswith("a_retrybc_"):
        global BROADCAST_TASK
        bid=d[len("a_retrybc_"):]
        if BROADCAST_LOCK.locked():
            await q.answer("Another broadcast is already running.",show_alert=True)
            return ConversationHandler.END
        with db() as c:
            row=c.execute("SELECT source_chat_id,source_message_id FROM broadcasts WHERE bcast_id=?",(bid,)).fetchone()
            sent_ids={r[0] for r in c.execute("SELECT user_id FROM broadcast_msgs WHERE bcast_id=?",(bid,)).fetchall()}
        if not row:
            await q.answer("Broadcast not found.",show_alert=True);return ConversationHandler.END
        failed_ids=[uid for uid in users() if uid not in sent_ids]
        if not failed_ids:
            await q.answer("No failed recipients found.",show_alert=True);return ConversationHandler.END
        source=SimpleNamespace(chat_id=row[0],message_id=row[1])
        retry_id=bcast_create(row[0],row[1],"retry",len(failed_ids))
        status_msg=await q.message.reply_text(f"🔁 Retrying <b>{len(failed_ids)}</b> failed recipients...",parse_mode=ParseMode.HTML)
        async def retry_runner():
            async with BROADCAST_LOCK:
                await run_broadcast(ctx.bot,q.message.chat_id,source,retry_id,failed_ids,status_msg)
        BROADCAST_TASK=asyncio.create_task(retry_runner())
        return ConversationHandler.END
    if d=="a_backup_menu":
        names="\n".join("• <code>"+esc(p.name)+"</code>" for p in backup_list()[:10]) or "No backups."
        await q.edit_message_text("💾 <b>BACKUP CENTER</b>\n\n"+names,reply_markup=InlineKeyboardMarkup([[ib("💾 Create Backup","a_backup",style="success",emoji_id=EMOJI["⚙️"])],[ib("📤 Download Latest","a_download",style="primary",emoji_id=EMOJI["📌"])],[ib("🗑 Delete Old Backups","a_cleanup",style="danger",emoji_id=EMOJI["❌"])],[ib("🔙 Back","a_back",style="primary",emoji_id=EMOJI["📌"])] ]),parse_mode=ParseMode.HTML);return ConversationHandler.END
    if d=="a_backup":
        try:
            p=create_backup()
            with p.open("rb") as f:await q.message.reply_document(f,filename=p.name,caption="💾 Backup Ready")
        except Exception as e:logger.exception("Backup");await q.answer("Backup failed.",show_alert=True)
        return ConversationHandler.END
    if d=="a_download":
        ps=backup_list()
        if not ps:return await q.answer("No backups.",show_alert=True)
        with ps[0].open("rb") as f:await q.message.reply_document(f,filename=ps[0].name)
        return ConversationHandler.END
    if d=="a_cleanup":
        await q.edit_message_text(
            "⚠️ <b>Are you sure?</b>\n\nDelete all old backups and keep only the latest one?",
            reply_markup=InlineKeyboardMarkup([[
                ib("✅ Confirm","a_cleanup_confirm",style="danger",emoji_id=EMOJI["❌"]),
                ib("❌ Cancel","a_backup_menu",style="primary",emoji_id=EMOJI["📌"])
            ]]), parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    if d=="a_cleanup_confirm":
        for p in backup_list()[1:]:p.unlink(missing_ok=True)
        await q.edit_message_text("🗑 Old backups deleted.",reply_markup=back_kb("a_backup_menu"));return ConversationHandler.END
    if d=="a_restore":
        await q.edit_message_text(
            "⚠️ <b>Are you sure?</b>\n\nRestore will replace the live database after validation. "
            "A safety backup will be created first.",
            reply_markup=InlineKeyboardMarkup([[
                ib("✅ Confirm Restore","a_restore_confirm",style="danger",emoji_id=EMOJI["❌"]),
                ib("❌ Cancel","a_back",style="primary",emoji_id=EMOJI["📌"])
            ]]), parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    if d=="a_restore_confirm":
        await q.edit_message_text(
            "♻️ <b>Safe Restore</b>\n\nSend the backup <code>.zip</code> file.\n"
            "ZIP, path traversal, database integrity and required tables are validated.\n"
            "A safety backup is created first.\n\nAfter restore: <b>Restart bot to apply restored state.</b>",
            parse_mode=ParseMode.HTML)
        return S_RESTORE
    if d=="a_dbhealth":await q.edit_message_text(dbhealth(),reply_markup=back_kb(),parse_mode=ParseMode.HTML);return ConversationHandler.END
    if d=="a_health":await q.edit_message_text(health(),reply_markup=back_kb(),parse_mode=ParseMode.HTML);return ConversationHandler.END
    if d=="a_settings":await q.edit_message_text("⚙️ <b>GLOBAL SETTINGS</b>\n\nAuto backup: "+gset("auto_backup_frequency","daily")+" | Keep: "+gset("auto_backup_keep","7"),reply_markup=settings_kb(),parse_mode=ParseMode.HTML);return ConversationHandler.END
    if d.startswith("a_toggle_"):
        k=d[len("a_toggle_"):];sset(k,"0" if gset(k,"0")=="1" else "1");await q.edit_message_text("⚙️ Setting updated.",reply_markup=settings_kb());return ConversationHandler.END
    if d=="a_autobackup":sset("auto_backup_frequency","weekly" if gset("auto_backup_frequency","daily")=="daily" else "daily");await q.edit_message_text("⚙️ Auto backup schedule updated.",reply_markup=settings_kb());return ConversationHandler.END
    if d=="a_errors":
        with db() as c:rows=c.execute("SELECT created_at,level,message FROM error_logs ORDER BY id DESC LIMIT 20").fetchall()
        text="📜 <b>RECENT ERRORS</b>\n\n"+"\n".join(f"<code>{esc(r[0])}</code> · <b>{esc(r[1])}</b> · {esc(r[2])}" for r in rows) if rows else "📜 <b>RECENT ERRORS</b>\n\nNo errors."
        await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup([[ib("🧹 Clear Logs","a_clearlogs_confirm",style="danger",emoji_id=EMOJI["❌"])],[ib("🔙 Back","a_back",style="primary",emoji_id=EMOJI["📌"])] ]),parse_mode=ParseMode.HTML);return ConversationHandler.END
    if d=="a_clearlogs_confirm":await q.edit_message_text("⚠️ <b>Are you sure?</b>",reply_markup=InlineKeyboardMarkup([[ib("✅ Confirm","a_clearlogs",style="danger",emoji_id=EMOJI["❌"]),ib("❌ Cancel","a_errors",style="primary",emoji_id=EMOJI["📌"])] ]),parse_mode=ParseMode.HTML);return ConversationHandler.END
    if d=="a_clearlogs":
        with db() as c:c.execute("DELETE FROM error_logs")
        await q.edit_message_text("🧹 Error log cleared.",reply_markup=back_kb());return ConversationHandler.END
    return ConversationHandler.END

async def extra_cb(update,ctx):
    q=update.callback_query
    if not is_admin(q.from_user.id):return await q.answer("❌ Not authorized!",show_alert=True)
    d=q.data
    if d.startswith("a_viewu_"):
        uid=int(d.split("_")[-1])
        with db() as c:r=c.execute("SELECT user_id,first_name,username,joined_at,status FROM users WHERE user_id=?",(uid,)).fetchone()
        if not r:return await q.answer("User not found.",show_alert=True)
        await q.edit_message_text(f"👤 <b>USER</b>\n\nID: <code>{r[0]}</code>\nFirst Name: <b>{esc(r[1])}</b>\nUsername: <b>@{esc(r[2]) if r[2] else '—'}</b>\nJoin Date: <code>{esc(r[3])}</code>\nStatus: <b>{esc(r[4])}</b>",reply_markup=InlineKeyboardMarkup([[ib("💬 Send Message",f"a_msgu_{uid}",style="primary",emoji_id=EMOJI["💬"])],[ib("🚫 Block",f"a_blocku_{uid}",style="danger",emoji_id=EMOJI["❌"]),ib("✅ Unblock",f"a_unblocku_{uid}",style="success",emoji_id=EMOJI["✅"])],[ib("🗑 Delete",f"a_deleteu_{uid}",style="danger",emoji_id=EMOJI["❌"])],[ib("🔙 Back","a_members",style="primary",emoji_id=EMOJI["📌"])] ]),parse_mode=ParseMode.HTML)
    elif d.startswith("a_blocku_"):
        uid=int(d.split("_")[-1]);await q.edit_message_text("⚠️ <b>Are you sure?</b>\n\nBlock this user?",reply_markup=InlineKeyboardMarkup([[ib("✅ Confirm",f"a_confirmblock_{uid}",style="danger",emoji_id=EMOJI["❌"]),ib("❌ Cancel",f"a_viewu_{uid}",style="primary",emoji_id=EMOJI["📌"])] ]),parse_mode=ParseMode.HTML)
    elif d.startswith("a_confirmblock_"):set_status(int(d.split("_")[-1]),"blocked");await q.edit_message_text("🚫 User blocked.",reply_markup=back_kb("a_members"))
    elif d.startswith("a_unblocku_"):set_status(int(d.split("_")[-1]),"active");await q.edit_message_text("✅ User unblocked.",reply_markup=back_kb("a_members"))
    elif d.startswith("a_deleteu_"):
        uid=int(d.split("_")[-1]);await q.edit_message_text("⚠️ <b>Are you sure?</b>\n\nDelete this user permanently?",reply_markup=InlineKeyboardMarkup([[ib("✅ Confirm",f"a_confirmdeleteu_{uid}",style="danger",emoji_id=EMOJI["❌"]),ib("❌ Cancel",f"a_viewu_{uid}",style="primary",emoji_id=EMOJI["📌"])] ]),parse_mode=ParseMode.HTML)
    elif d.startswith("a_confirmdeleteu_"):delete_user(int(d.split("_")[-1]));await q.edit_message_text("🗑 User deleted.",reply_markup=back_kb("a_members"))
    elif d.startswith("a_msgu_"):ctx.user_data["msg_uid"]=int(d.split("_")[-1]);await q.edit_message_text("💬 Send the message to deliver.");return S_USERMSG
    return None

async def auto_backup_loop():
    while True:
        try:
            await asyncio.sleep(3600)
            if gset("auto_backup_enabled","0")=="1":
                last=gset("last_backup","")
                hours=168 if gset("auto_backup_frequency","daily")=="weekly" else 24
                if not last or (datetime.now()-datetime.fromisoformat(last)).total_seconds()>=hours*3600:
                    p=create_backup();cleanup_backups();p.unlink(missing_ok=True)
        except asyncio.CancelledError:return
        except Exception as e:logger.exception("Auto backup: %s",e)

async def post_init(app):
    global AUTO_BACKUP_TASK
    init_db();AUTO_BACKUP_TASK=asyncio.create_task(auto_backup_loop())
async def post_shutdown(app):
    global AUTO_BACKUP_TASK,BROADCAST_TASK
    for t in (AUTO_BACKUP_TASK,BROADCAST_TASK):
        if t and not t.done():
            t.cancel()
            try:await t
            except asyncio.CancelledError:pass

async def cancel(u,c):c.user_data.clear();await u.message.reply_text("❌ Cancelled.");return ConversationHandler.END
async def errors(update,ctx):
    global PREMIUM_EMOJI_ENABLED
    if ctx.error:
        logger.exception("Unhandled exception",exc_info=ctx.error)
        # If Telegram rejects icon_custom_emoji_id because of account eligibility,
        # disable premium icons for subsequent keyboards instead of allowing a crash loop.
        msg=str(ctx.error).lower()
        if "icon_custom_emoji_id" in msg or "custom emoji" in msg or "not enough rights" in msg:
            PREMIUM_EMOJI_ENABLED=False
            log_error("ERROR","Premium custom emoji disabled after Telegram rejection: "+str(ctx.error))

class RenderHealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            body=("OK\n" if self.path=="/" else "{\"status\":\"ok\"}\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json" if self.path!="/" else "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers();self.wfile.write(body);return
        self.send_response(404);self.end_headers()
    def log_message(self,format,*args):
        return

def start_render_health_server():
    global HTTP_SERVER, HTTP_SERVER_THREAD
    port=int(os.getenv("PORT","10000"))
    HTTP_SERVER=ThreadingHTTPServer(("0.0.0.0",port),RenderHealthHandler)
    HTTP_SERVER.daemon_threads=True
    HTTP_SERVER_THREAD=threading.Thread(target=HTTP_SERVER.serve_forever,name="render-health",daemon=True)
    HTTP_SERVER_THREAD.start()
    logger.info("Render health server listening on 0.0.0.0:%s",port)

def stop_render_health_server():
    global HTTP_SERVER
    if HTTP_SERVER is not None:
        try: HTTP_SERVER.shutdown();HTTP_SERVER.server_close()
        except Exception: pass
        HTTP_SERVER=None

def main():
    if not BOT_TOKEN:raise RuntimeError("BOT_TOKEN is missing. Set BOT_TOKEN environment variable.")
    if not ADMIN_ID:raise RuntimeError("ADMIN_ID is missing or invalid. Set ADMIN_ID environment variable.")
    init_db()
    app=(Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build())
    tf=filters.TEXT & ~filters.COMMAND
    bf=(filters.TEXT|filters.PHOTO|filters.VIDEO)&~filters.COMMAND
    rf=filters.Document.ALL&~filters.COMMAND
    conv=ConversationHandler(
      entry_points=[CommandHandler("admin",admin_cmd),CommandHandler("dkboss",dkboss_cmd),CallbackQueryHandler(admin_cb,pattern=r"^a_")],
      states={
        S_CH_ID:[MessageHandler(tf,s_ch_id)],S_CH_NAME:[MessageHandler(tf,s_ch_name)],S_CH_LINK:[MessageHandler(tf,s_ch_link)],
        S_WELCOME:[MessageHandler(tf,s_welcome)],S_WELCOME_PHOTO:[MessageHandler((filters.PHOTO|filters.TEXT)&~filters.COMMAND,s_photo)],
        S_POSTJOIN:[MessageHandler(tf,s_postjoin)],S_TOP:[MessageHandler(tf,s_top)],S_BTN1:[MessageHandler(tf,s_btn1)],S_BTN2:[MessageHandler(tf,s_btn2)],S_BTN3:[MessageHandler(tf,s_btn3)],
        S_BCAST:[MessageHandler(bf,start_bcast)],S_RESTORE:[MessageHandler(rf,s_restore)],S_SEARCH:[MessageHandler(tf,s_search)],S_USERMSG:[MessageHandler(bf,s_usermsg)],
        S_EDITNAME:[MessageHandler(tf,s_editname)],S_EDITLINK:[MessageHandler(tf,s_editlink)],S_EMOJI:[MessageHandler(tf,s_emoji)]
      },fallbacks=[CommandHandler("cancel",cancel)],per_chat=False,per_user=True,allow_reentry=True)
    app.add_handler(CommandHandler("start",start));app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(cb_check,pattern=r"^check_joined$"));app.add_handler(CallbackQueryHandler(cb_btn,pattern=r"^btn[123]$"));app.add_handler(CallbackQueryHandler(cb_back,pattern=r"^back_main$"))
    app.add_handler(ChatJoinRequestHandler(join_request));app.add_error_handler(errors)
    start_render_health_server()
    logger.info("Bot started: v%s / PTB %s / ADMIN_ID=%s / OWNER_ID=%s",BOT_VERSION,telegram.__version__,ADMIN_ID,OWNER_ID)
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES,drop_pending_updates=True)
    finally:
        stop_render_health_server()

if __name__=="__main__":main()
