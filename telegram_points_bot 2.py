#!/usr/bin/env python3
"""
Telegram 2-player points/table bot
-----------------------------------
Install:
    pip install pyTelegramBotAPI gTTS

Environment:
    BOT_TOKEN=123456:ABC...
    OWNER_ID=123456789
    UPDATES_CHAT_ID=-1001234567890   # optional; can also be set in code/db

Important:
- This bot uses NON-CASH points only.
- Data is stored in bot_data.sqlite3.
- /cadd and /cminus are intentionally NOT implemented.
- /fulldata_web is intentionally NOT implemented.
- /set_group_rule_text, /set_custom_list_header_text,
  /set_custom_table_header_text, /set_mini_cmd_answer_mode,
  /set_restrict_low_balance_table, /clear_table_lock and /setlog
  are intentionally NOT implemented.
"""

import os
import re
import html
import time
import sqlite3
import logging
import threading
import traceback
from datetime import datetime, date
from functools import wraps

import telebot
from telebot import types
from gtts import gTTS


# ============================================================
# Configuration
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
UPDATES_CHAT_ID = int(os.getenv("UPDATES_CHAT_ID", "0") or 0)

DB_FILE = os.getenv("BOT_DB", "bot_data.sqlite3")
DEFAULT_COMMISSION = 5.0
ESTIMATE_DELETE_SECONDS = 5
IST_NAME = "Asia/Kolkata"

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN environment variable is required.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("points_bot")

DB_LOCK = threading.RLock()


# ============================================================
# Database
# ============================================================

def db():
    conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with DB_LOCK, db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            custom_emoji_id TEXT DEFAULT '',
            custom_emoji_text TEXT DEFAULT '✔️',
            default_commission REAL DEFAULT 5,
            chat_balance_commission INTEGER DEFAULT 1,
            chat_balance_list_visibility INTEGER DEFAULT 1,
            full_info_button_visibility INTEGER DEFAULT 0,
            show_table_creation_event INTEGER DEFAULT 1,
            cancel_button_visibility INTEGER DEFAULT 1,
            estimate_table_balance INTEGER DEFAULT 0,
            show_in_match_users_list INTEGER DEFAULT 1,
            list_privacy_mode INTEGER DEFAULT 0,
            balance_privacy_mode INTEGER DEFAULT 0,
            button_confirm_mode INTEGER DEFAULT 1,
            admin_set_result_button INTEGER DEFAULT 1,
            self_set_result_button INTEGER DEFAULT 0,
            hide_zero_balance INTEGER DEFAULT 0,
            private_log_mode INTEGER DEFAULT 1,
            autodel_last_list_document INTEGER DEFAULT 1,
            custom_admin_mode INTEGER DEFAULT 0,
            response_qr_filter_mode INTEGER DEFAULT 0,
            updates_chat_id INTEGER DEFAULT 0,
            private_logs INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            name TEXT DEFAULT '',
            custom_name TEXT DEFAULT '',
            balance REAL DEFAULT 0,
            side_balance REAL DEFAULT 0,
            commission_pct REAL,
            notifications INTEGER DEFAULT 1,
            removed INTEGER DEFAULT 0,
            PRIMARY KEY(chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS admins (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY(chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS table_masters (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY(chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS qr_filters (
            chat_id INTEGER NOT NULL,
            filter_text TEXT NOT NULL,
            PRIMARY KEY(chat_id, filter_text)
        );

        CREATE TABLE IF NOT EXISTS tables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_id INTEGER,
            creator_id INTEGER DEFAULT 0,
            player1_id INTEGER NOT NULL,
            player2_id INTEGER NOT NULL,
            player1_stake REAL DEFAULT 0,
            player2_stake REAL DEFAULT 0,
            total_stake REAL DEFAULT 0,
            winner_id INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            source_text TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chat_commissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            table_id INTEGER,
            user_id INTEGER,
            amount REAL DEFAULT 0,
            reason TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS match_updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            table_id INTEGER,
            creator_id INTEGER,
            winner_id INTEGER,
            loser_id INTEGER,
            commission REAL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS list_documents (
            chat_id INTEGER PRIMARY KEY,
            message_id INTEGER DEFAULT 0
        );
        """)
        ensure_chat(0, con=con)


def now():
    # Server timezone independent UTC-ish timestamp is enough for DB ordering.
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def ensure_chat(chat_id, con=None):
    own = con is None
    con = con or db()
    try:
        con.execute(
            "INSERT OR IGNORE INTO chats(chat_id, default_commission) VALUES(?, ?)",
            (chat_id, DEFAULT_COMMISSION),
        )
        con.commit()
    finally:
        if own:
            con.close()


def get_chat(chat_id):
    with DB_LOCK, db() as con:
        ensure_chat(chat_id, con)
        return con.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)).fetchone()


def setting(chat_id, key):
    row = get_chat(chat_id)
    return row[key]


def set_setting(chat_id, key, value):
    allowed = {
        "custom_emoji_id", "custom_emoji_text", "default_commission",
        "chat_balance_commission", "chat_balance_list_visibility",
        "full_info_button_visibility", "show_table_creation_event",
        "cancel_button_visibility", "estimate_table_balance",
        "show_in_match_users_list", "list_privacy_mode",
        "balance_privacy_mode", "button_confirm_mode",
        "admin_set_result_button", "self_set_result_button",
        "hide_zero_balance", "private_log_mode",
        "autodel_last_list_document", "custom_admin_mode",
        "response_qr_filter_mode", "updates_chat_id", "private_logs"
    }
    if key not in allowed:
        raise ValueError("Invalid setting")
    with DB_LOCK, db() as con:
        ensure_chat(chat_id, con)
        con.execute(f"UPDATE chats SET {key}=? WHERE chat_id=?", (value, chat_id))
        con.commit()


def upsert_user(chat_id, user):
    if not user:
        return
    name = " ".join(x for x in [user.first_name, user.last_name] if x).strip()
    username = user.username or ""
    with DB_LOCK, db() as con:
        ensure_chat(chat_id, con)
        con.execute("""
            INSERT INTO users(chat_id,user_id,username,name)
            VALUES(?,?,?,?)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET
                username=excluded.username,
                name=excluded.name,
                removed=0
        """, (chat_id, user.id, username, name))
        con.commit()


def get_user(chat_id, user_id):
    with DB_LOCK, db() as con:
        return con.execute(
            "SELECT * FROM users WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()


def display_name(row):
    if not row:
        return "Unknown"
    if row["custom_name"]:
        return row["custom_name"]
    if row["name"]:
        return row["name"]
    if row["username"]:
        return "@" + row["username"]
    return str(row["user_id"])


def fmt_num(value):
    value = float(value or 0)
    if value.is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def signed(value):
    value = float(value or 0)
    return f"+{fmt_num(value)}" if value >= 0 else f"-{fmt_num(abs(value))}"


# ============================================================
# Permissions
# ============================================================

def is_owner(user_id):
    return user_id == OWNER_ID and OWNER_ID != 0


def is_chat_admin(chat_id, user_id):
    if is_owner(user_id):
        return True
    with DB_LOCK, db() as con:
        row = con.execute(
            "SELECT 1 FROM admins WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
    if row:
        return True
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def is_custom_admin(chat_id, user_id):
    if is_owner(user_id):
        return True
    if is_chat_admin(chat_id, user_id):
        return True
    if not setting(chat_id, "custom_admin_mode"):
        return False
    with DB_LOCK, db() as con:
        return bool(con.execute(
            "SELECT 1 FROM admins WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone())


def is_table_master(chat_id, user_id):
    if is_owner(user_id) or is_chat_admin(chat_id, user_id):
        return True
    with DB_LOCK, db() as con:
        return bool(con.execute(
            "SELECT 1 FROM table_masters WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone())


def admin_only(fn):
    @wraps(fn)
    def wrapper(message, *args, **kwargs):
        if not is_custom_admin(message.chat.id, message.from_user.id):
            safe_reply(message, "❌ Admin permission required.")
            return
        upsert_user(message.chat.id, message.from_user)
        return fn(message, *args, **kwargs)
    return wrapper


def owner_only(fn):
    @wraps(fn)
    def wrapper(message, *args, **kwargs):
        if not is_owner(message.from_user.id):
            safe_reply(message, "❌ Owner only command.")
            return
        return fn(message, *args, **kwargs)
    return wrapper


def result_permission(fn):
    @wraps(fn)
    def wrapper(message, *args, **kwargs):
        if not is_table_master(message.chat.id, message.from_user.id):
            safe_reply(message, "❌ Only admins/table masters can set results.")
            return
        return fn(message, *args, **kwargs)
    return wrapper


# ============================================================
# Safe Telegram helpers
# ============================================================

def safe_send(chat_id, text, **kwargs):
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception as exc:
        log.exception("send_message failed: %s", exc)
        return None


def safe_reply(message, text, **kwargs):
    return safe_send(message.chat.id, text, reply_to_message_id=message.message_id, **kwargs)


def safe_delete(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def safe_edit(chat_id, message_id, text, **kwargs):
    try:
        return bot.edit_message_text(text, chat_id, message_id, **kwargs)
    except Exception as exc:
        log.warning("edit failed: %s", exc)
        return None


def schedule_delete(chat_id, message_id, seconds):
    def worker():
        time.sleep(seconds)
        safe_delete(chat_id, message_id)
    threading.Thread(target=worker, daemon=True).start()


def announce_update(chat_id, text):
    row = get_chat(chat_id)
    target = int(row["updates_chat_id"] or UPDATES_CHAT_ID or 0)
    if target:
        safe_send(target, text)


# ============================================================
# Balance / commission
# ============================================================

def commission_pct(chat_id, user_id):
    row = get_user(chat_id, user_id)
    if row and row["commission_pct"] is not None:
        return float(row["commission_pct"])
    return float(setting(chat_id, "default_commission"))


def change_balance(chat_id, user_id, amount):
    with DB_LOCK, db() as con:
        con.execute(
            "UPDATE users SET balance=balance+?, removed=0 WHERE chat_id=? AND user_id=?",
            (amount, chat_id, user_id),
        )
        con.commit()


def change_side_balance(chat_id, user_id, amount):
    with DB_LOCK, db() as con:
        con.execute(
            "UPDATE users SET side_balance=side_balance+?, removed=0 WHERE chat_id=? AND user_id=?",
            (amount, chat_id, user_id),
        )
        con.commit()


def set_balance_value(chat_id, user_id, value):
    with DB_LOCK, db() as con:
        con.execute(
            "UPDATE users SET balance=?, removed=0 WHERE chat_id=? AND user_id=?",
            (value, chat_id, user_id),
        )
        con.commit()


def set_side_value(chat_id, user_id, value):
    with DB_LOCK, db() as con:
        con.execute(
            "UPDATE users SET side_balance=?, removed=0 WHERE chat_id=? AND user_id=?",
            (value, chat_id, user_id),
        )
        con.commit()


def add_chat_commission(chat_id, table_id, user_id, amount, reason):
    if amount == 0:
        return
    with DB_LOCK, db() as con:
        con.execute("""
            INSERT INTO chat_commissions(chat_id,table_id,user_id,amount,reason,created_at)
            VALUES(?,?,?,?,?,?)
        """, (chat_id, table_id, user_id, amount, reason, now()))
        con.commit()


def totals(chat_id):
    with DB_LOCK, db() as con:
        rows = con.execute(
            "SELECT balance,side_balance FROM users WHERE chat_id=? AND removed=0",
            (chat_id,)
        ).fetchall()
    main = sum(float(r["balance"]) for r in rows)
    side = sum(float(r["side_balance"]) for r in rows)
    return main, side


# ============================================================
# User resolution
# ============================================================

def parse_user_id(text):
    m = re.search(r"(?<!\d)(\d{5,20})(?!\d)", text or "")
    return int(m.group(1)) if m else None


def resolve_user(chat_id, token):
    token = token.strip().strip(",")
    uid = parse_user_id(token)
    if uid:
        return uid

    username = token.lstrip("@").lower()
    if not username:
        return None

    with DB_LOCK, db() as con:
        row = con.execute(
            "SELECT user_id FROM users WHERE chat_id=? AND lower(username)=? AND removed=0",
            (chat_id, username)
        ).fetchone()
    return int(row["user_id"]) if row else None


def ensure_mentioned_user(message):
    upsert_user(message.chat.id, message.from_user)
    # Register users from message entities where Telegram supplies user objects.
    if message.entities:
        for ent in message.entities:
            if ent.type == "text_mention" and ent.user:
                upsert_user(message.chat.id, ent.user)


# ============================================================
# Table parsing / rendering
# ============================================================

STAKE_RE = re.compile(r"(?<!\w)(\d+(?:\.\d+)?)\s*([kKmMbB])?\b")


def parse_amount(raw_num, suffix):
    value = float(raw_num)
    if suffix:
        mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
        value *= mult[suffix.lower()]
    return value


def custom_emoji_present(message):
    wanted = str(setting(message.chat.id, "custom_emoji_id") or "")
    if not wanted:
        # Default can be detected as normal text only.
        return "✔" in (message.text or "")

    for ent in message.entities or []:
        if ent.type == "custom_emoji" and getattr(ent, "custom_emoji_id", "") == wanted:
            return True
    return False


def extract_table_players(message):
    text = message.text or ""
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    candidates = []

    # First try text_mention entities: they carry the real Telegram user ID.
    for ent in message.entities or []:
        if ent.type == "text_mention" and ent.user:
            if ent.user.id not in candidates:
                candidates.append(ent.user.id)

    # Then resolve @username tokens from the bot's known user cache.
    for token in re.findall(r"@[A-Za-z0-9_]{3,32}", text):
        uid = resolve_user(message.chat.id, token)
        if uid and uid not in candidates:
            candidates.append(uid)

    # Finally, accept numeric IDs if the table text contains them.
    for token in re.findall(r"\b\d{5,20}\b", text):
        uid = resolve_user(message.chat.id, token)
        if uid and uid not in candidates:
            candidates.append(uid)

    return candidates[:2], lines


def extract_stakes(lines):
    stakes = []
    for line in lines:
        for m in STAKE_RE.finditer(line):
            value = parse_amount(m.group(1), m.group(2))
            if value > 0:
                stakes.append(value)
    return stakes


def table_markup(chat_id, table_id):
    row = get_chat(chat_id)
    kb = types.InlineKeyboardMarkup(row_width=2)

    if row["admin_set_result_button"]:
        kb.add(
            types.InlineKeyboardButton("WIN 1", callback_data=f"win:{table_id}:1"),
            types.InlineKeyboardButton("WIN 2", callback_data=f"win:{table_id}:2"),
        )

    if row["self_set_result_button"]:
        kb.add(types.InlineKeyboardButton("I WON", callback_data=f"selfwin:{table_id}"))

    if row["cancel_button_visibility"]:
        kb.add(types.InlineKeyboardButton("CANCEL ❌", callback_data=f"cancel:{table_id}"))

    return kb


def emoji_html(chat_id):
    row = get_chat(chat_id)
    eid = row["custom_emoji_id"]
    text = html.escape(row["custom_emoji_text"] or "✔️")
    if eid:
        return f'<tg-emoji emoji-id="{html.escape(str(eid))}">{text}</tg-emoji>'
    return text


def render_table(table):
    chat_id = table["chat_id"]
    p1 = get_user(chat_id, table["player1_id"])
    p2 = get_user(chat_id, table["player2_id"])
    n1 = html.escape(display_name(p1))
    n2 = html.escape(display_name(p2))
    e = emoji_html(chat_id)

    marker1 = f" {e}" if table["winner_id"] == table["player1_id"] else ""
    marker2 = f" {e}" if table["winner_id"] == table["player2_id"] else ""

    stake = table["total_stake"]
    if not stake:
        stake = max(table["player1_stake"], table["player2_stake"])

    lines = [
        f"@{p1['username']}" if p1 and p1["username"] else n1,
        "",
        f"@{p2['username']}" if p2 and p2["username"] else n2,
        "",
        f"<b>{fmt_num(stake)}{e}</b>",
        "Game Details",
    ]

    if table["winner_id"] == table["player1_id"]:
        lines[0] += marker1
    elif table["winner_id"] == table["player2_id"]:
        lines[2] += marker2

    if setting(chat_id, "show_in_match_users_list"):
        # Kept compact; useful when privacy mode is disabled.
        pass

    return "\n".join(lines)


def create_table_from_message(message):
    if message.chat.type not in ("group", "supergroup"):
        return None

    if not custom_emoji_present(message):
        return None

    players, lines = extract_table_players(message)
    if len(players) != 2:
        return None

    stakes = extract_stakes(lines)
    if not stakes:
        return None

    stake = stakes[0]
    p1, p2 = players
    upsert_user_by_id(message.chat.id, p1)
    upsert_user_by_id(message.chat.id, p2)

    # Optional estimate message.
    if setting(message.chat.id, "estimate_table_balance"):
        send_estimate(message.chat.id, p1, p2, stake)

    with DB_LOCK, db() as con:
        cur = con.execute("""
            INSERT INTO tables(
                chat_id,message_id,creator_id,player1_id,player2_id,
                player1_stake,player2_stake,total_stake,status,source_text,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            message.chat.id, message.message_id, message.from_user.id,
            p1, p2, stake, stake, stake, "active",
            message.text or "", now(), now()
        ))
        table_id = cur.lastrowid
        con.commit()

    safe_delete(message.chat.id, message.message_id)
    table = get_table(table_id)

    sent = safe_send(
        message.chat.id,
        render_table(table),
        reply_markup=table_markup(message.chat.id, table_id),
    )

    if sent:
        with DB_LOCK, db() as con:
            con.execute(
                "UPDATE tables SET message_id=?, updated_at=? WHERE id=?",
                (sent.message_id, now(), table_id)
            )
            con.commit()

    if setting(message.chat.id, "show_table_creation_event"):
        announce_update(
            message.chat.id,
            f"🟢 <b>Table Created</b>\n"
            f"Table ID: <code>{table_id}</code>\n"
            f"Stake: {fmt_num(stake)}\n"
            f"Creator: {html.escape(message.from_user.full_name)}"
        )

    return table_id


def upsert_user_by_id(chat_id, user_id):
    row = get_user(chat_id, user_id)
    if row:
        return
    # Telegram lookup may fail; create placeholder and later enrich.
    with DB_LOCK, db() as con:
        con.execute("""
            INSERT OR IGNORE INTO users(chat_id,user_id,name)
            VALUES(?,?,?)
        """, (chat_id, user_id, str(user_id)))
        con.commit()


def get_table(table_id):
    with DB_LOCK, db() as con:
        return con.execute("SELECT * FROM tables WHERE id=?", (table_id,)).fetchone()


def active_table_for_user(chat_id, user_id):
    with DB_LOCK, db() as con:
        return con.execute("""
            SELECT * FROM tables
            WHERE chat_id=? AND status='active'
              AND (player1_id=? OR player2_id=?)
            ORDER BY id DESC LIMIT 1
        """, (chat_id, user_id, user_id)).fetchone()


def send_estimate(chat_id, p1, p2, stake):
    u1 = get_user(chat_id, p1)
    u2 = get_user(chat_id, p2)
    b1 = float(u1["balance"] if u1 else 0)
    b2 = float(u2["balance"] if u2 else 0)

    text = (
        "📊 <b>Estimated Balance</b>\n\n"
        f"{html.escape(display_name(u1))}: <b>{fmt_num(b1 - stake)}</b>\n"
        f"{html.escape(display_name(u2))}: <b>{fmt_num(b2 - stake)}</b>\n\n"
        "This is only an estimate."
    )
    msg = safe_send(chat_id, text)
    if msg:
        schedule_delete(chat_id, msg.message_id, ESTIMATE_DELETE_SECONDS)


# ============================================================
# Table settlement
# ============================================================

def settle_table(table_id, winner_id, actor_id, reason="button"):
    table = get_table(table_id)
    if not table:
        return False, "Table not found."
    if table["status"] != "active":
        return False, f"Table is already {table['status']}."

    chat_id = table["chat_id"]
    p1 = table["player1_id"]
    p2 = table["player2_id"]

    if winner_id not in (p1, p2):
        return False, "Winner is not a player in this table."

    loser_id = p2 if winner_id == p1 else p1
    stake = float(table["total_stake"] or table["player1_stake"] or 0)

    # The points accounting is non-cash:
    # winner gains stake, loser loses stake.
    # Commission is recorded separately as Chat Commission.
    pct = commission_pct(chat_id, loser_id)
    commission = round(stake * pct / 100.0, 2)

    with DB_LOCK, db() as con:
        con.execute(
            "UPDATE users SET balance=balance+? WHERE chat_id=? AND user_id=?",
            (stake, chat_id, winner_id)
        )
        con.execute(
            "UPDATE users SET balance=balance-? WHERE chat_id=? AND user_id=?",
            (stake, chat_id, loser_id)
        )
        con.execute("""
            UPDATE tables SET winner_id=?, status='completed', updated_at=?
            WHERE id=?
        """, (winner_id, now(), table_id))

        con.execute("""
            INSERT INTO match_updates(
                chat_id,table_id,creator_id,winner_id,loser_id,commission,created_at
            ) VALUES(?,?,?,?,?,?,?)
        """, (
            chat_id, table_id, table["creator_id"], winner_id,
            loser_id, commission, now()
        ))

        if commission:
            con.execute("""
                INSERT INTO chat_commissions(
                    chat_id,table_id,user_id,amount,reason,created_at
                ) VALUES(?,?,?,?,?,?)
            """, (
                chat_id, table_id, loser_id, commission,
                f"Table settlement ({pct:g}%)", now()
            ))

        con.commit()

    updated = get_table(table_id)
    if updated["message_id"]:
        safe_edit(
            chat_id,
            updated["message_id"],
            render_table(updated),
            reply_markup=table_markup(chat_id, table_id),
        )

    winner = get_user(chat_id, winner_id)
    loser = get_user(chat_id, loser_id)

    update_text = (
        f"🏆 <b>Table #{table_id} Result</b>\n"
        f"Winner: {html.escape(display_name(winner))}\n"
        f"Loser: {html.escape(display_name(loser))}\n"
        f"Stake: {fmt_num(stake)}\n"
        f"Commission: {fmt_num(commission)} ({pct:g}%)"
    )
    announce_update(chat_id, update_text)
    send_private_log(chat_id, winner_id, update_text)
    send_private_log(chat_id, loser_id, update_text)

    send_table_creator_stats(chat_id)
    return True, "Winner set."


def cancel_table(table_id, actor_id):
    table = get_table(table_id)
    if not table:
        return False, "Table not found."
    if table["status"] != "active":
        return False, f"Table is already {table['status']}."

    with DB_LOCK, db() as con:
        con.execute(
            "UPDATE tables SET status='cancelled',updated_at=? WHERE id=?",
            (now(), table_id)
        )
        con.commit()

    safe_edit(
        table["chat_id"],
        table["message_id"],
        render_table(get_table(table_id)) + "\n\n❌ <b>CANCELLED</b>",
        reply_markup=table_markup(table["chat_id"], table_id),
    )
    announce_update(table["chat_id"], f"❌ Table #{table_id} cancelled.")
    return True, "Table cancelled."


def revive_table(table_id):
    table = get_table(table_id)
    if not table:
        return False, "Table not found."
    if table["status"] != "cancelled":
        return False, "Only cancelled tables can be revived."

    with DB_LOCK, db() as con:
        con.execute(
            "UPDATE tables SET status='active',updated_at=? WHERE id=?",
            (now(), table_id)
        )
        con.commit()

    table = get_table(table_id)
    safe_edit(
        table["chat_id"],
        table["message_id"],
        render_table(table),
        reply_markup=table_markup(table["chat_id"], table_id),
    )
    announce_update(table["chat_id"], f"♻️ Table #{table_id} revived.")
    return True, "Table revived."


# ============================================================
# Notifications / list
# ============================================================

def send_private_log(chat_id, user_id, text):
    row = get_user(chat_id, user_id)
    if not row:
        return
    if not row["notifications"]:
        return
    if not setting(chat_id, "private_log_mode"):
        return
    safe_send(user_id, text)


def list_text(chat_id):
    row = get_chat(chat_id)
    with DB_LOCK, db() as con:
        users = con.execute("""
            SELECT * FROM users
            WHERE chat_id=? AND removed=0
            ORDER BY name COLLATE NOCASE, user_id
        """, (chat_id,)).fetchall()

    lines = ["📋 <b>Balance Sheet</b>", ""]
    if not users:
        lines.append("No users recorded.")
    else:
        for u in users:
            bal = float(u["balance"])
            side = float(u["side_balance"])
            if row["hide_zero_balance"] and bal == 0:
                continue

            if row["list_privacy_mode"]:
                name = f"User {u['user_id']}"
                balance = "••••"
            else:
                name = html.escape(display_name(u))
                balance = fmt_num(bal)

            side_part = f" ({signed(side)})"
            lines.append(f"• {name} — <b>{balance}</b>{side_part}")

    main, side = totals(chat_id)
    lines += [
        "",
        f"Total Main Balance: <b>{fmt_num(main)}</b>",
        f"Total Side Balance: <b>{fmt_num(side)}</b>",
    ]

    if row["chat_balance_list_visibility"] and row["chat_balance_commission"]:
        with DB_LOCK, db() as con:
            commission = con.execute(
                "SELECT COALESCE(SUM(amount),0) x FROM chat_commissions WHERE chat_id=?",
                (chat_id,)
            ).fetchone()["x"]
        lines.append(f"ChatBalance / Commission: <b>{fmt_num(commission)}</b>")

    return "\n".join(lines)


def list_markup(chat_id):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🔔 Notification", callback_data="notify"),
        types.InlineKeyboardButton("💰 Check Your Balance", callback_data="mybalance"),
    )
    if setting(chat_id, "full_info_button_visibility"):
        kb.add(types.InlineKeyboardButton("📄 Full Details", callback_data="fulldetails"))
    return kb


def send_balance_list(chat_id):
    msg = safe_send(chat_id, list_text(chat_id), reply_markup=list_markup(chat_id))
    if msg:
        with DB_LOCK, db() as con:
            old = con.execute(
                "SELECT message_id FROM list_documents WHERE chat_id=?",
                (chat_id,)
            ).fetchone()
            if old and old["message_id"] and setting(chat_id, "autodel_last_list_document"):
                safe_delete(chat_id, old["message_id"])
            con.execute("""
                INSERT INTO list_documents(chat_id,message_id) VALUES(?,?)
                ON CONFLICT(chat_id) DO UPDATE SET message_id=excluded.message_id
            """, (chat_id, msg.message_id))
            con.commit()
    return msg


def private_balance(chat_id, user_id):
    row = get_user(chat_id, user_id)
    if not row:
        return "No balance record found."
    if setting(chat_id, "balance_privacy_mode"):
        return "🔒 Your balance is hidden by admin."
    return (
        f"💰 <b>Your Balance</b>\n"
        f"Main: <b>{fmt_num(row['balance'])}</b>\n"
        f"Side: <b>{signed(row['side_balance'])}</b>"
    )


def send_table_creator_stats(chat_id):
    # Today is based on UTC date for DB consistency; the bot's intended display
    # is IST. For deployments where exact IST midnight is critical, set the
    # server timezone to Asia/Kolkata.
    today = date.today().isoformat()
    with DB_LOCK, db() as con:
        rows = con.execute("""
            SELECT creator_id, COUNT(*) cnt,
                   COALESCE(SUM(total_stake),0) stake
            FROM tables
            WHERE chat_id=? AND substr(created_at,1,10)=?
            GROUP BY creator_id
            ORDER BY cnt DESC
        """, (chat_id, today)).fetchall()

    lines = [f"📊 <b>Table Creator Stats — {today}</b>", ""]
    if not rows:
        lines.append("No tables today.")
    else:
        for i, r in enumerate(rows, 1):
            u = get_user(chat_id, r["creator_id"])
            name = display_name(u) if u else str(r["creator_id"])
            lines.append(
                f"{i}. {html.escape(name)} — "
                f"Tables: <b>{r['cnt']}</b> | Stake: <b>{fmt_num(r['stake'])}</b>"
            )

    announce_update(chat_id, "\n".join(lines))


# ============================================================
# Command helpers
# ============================================================

def args(message):
    text = message.text or ""
    return text.split()[1:]


def require_two_args(message):
    a = args(message)
    if len(a) < 2:
        safe_reply(message, "Usage: /command @user amount")
        return None
    uid = resolve_user(message.chat.id, a[0])
    if not uid:
        safe_reply(message, "❌ User not found. The player must first interact with the bot.")
        return None
    try:
        value = float(a[1].replace(",", ""))
    except ValueError:
        safe_reply(message, "❌ Invalid amount.")
        return None
    return uid, value


def parse_bool_arg(message):
    a = args(message)
    if not a or a[0].lower() not in ("on", "off"):
        safe_reply(message, "Usage: /command on|off")
        return None
    return a[0].lower() == "on"


def command_setting(message, key, label):
    value = parse_bool_arg(message)
    if value is None:
        return
    set_setting(message.chat.id, key, int(value))
    safe_reply(message, f"✅ {label}: {'ON' if value else 'OFF'}")


# ============================================================
# Commands
# ============================================================

@bot.message_handler(commands=["start"])
def cmd_start(message):
    upsert_user(message.chat.id, message.from_user)
    safe_reply(
        message,
        "🤖 <b>Bot started.</b>\n"
        "Use /help to see available commands."
    )


@bot.message_handler(commands=["id"])
def cmd_id(message):
    safe_reply(message, f"🆔 Your User ID: <code>{message.from_user.id}</code>")


@bot.message_handler(commands=["balance"])
def cmd_balance(message):
    upsert_user(message.chat.id, message.from_user)
    safe_reply(message, private_balance(message.chat.id, message.from_user.id))


@bot.message_handler(commands=["add"])
@admin_only
def cmd_add(message):
    result = require_two_args(message)
    if not result:
        return
    uid, amount = result
    if amount < 0:
        safe_reply(message, "❌ Amount must be positive.")
        return
    change_balance(message.chat.id, uid, amount)
    safe_reply(message, f"✅ Added {fmt_num(amount)} points.")


@bot.message_handler(commands=["minus"])
@admin_only
def cmd_minus(message):
    result = require_two_args(message)
    if not result:
        return
    uid, amount = result
    if amount < 0:
        safe_reply(message, "❌ Amount must be positive.")
        return
    change_balance(message.chat.id, uid, -amount)
    safe_reply(message, f"✅ Deducted {fmt_num(amount)} points.")


@bot.message_handler(commands=["set_balance"])
@admin_only
def cmd_set_balance(message):
    result = require_two_args(message)
    if not result:
        return
    uid, value = result
    set_balance_value(message.chat.id, uid, value)
    safe_reply(message, f"✅ Main balance set to {fmt_num(value)}.")


@bot.message_handler(commands=["set_side_balance"])
@admin_only
def cmd_set_side_balance(message):
    result = require_two_args(message)
    if not result:
        return
    uid, value = result
    set_side_value(message.chat.id, uid, value)
    safe_reply(message, f"✅ Side balance set to {signed(value)}.")


@bot.message_handler(commands=["remove"])
@admin_only
def cmd_remove(message):
    a = args(message)
    if not a:
        safe_reply(message, "Usage: /remove @user or /remove USER_ID")
        return
    uid = resolve_user(message.chat.id, a[0])
    if not uid:
        safe_reply(message, "❌ User not found.")
        return
    with DB_LOCK, db() as con:
        con.execute(
            "UPDATE users SET removed=1 WHERE chat_id=? AND user_id=?",
            (message.chat.id, uid)
        )
        con.commit()
    safe_reply(message, "✅ User removed from active balance records.")


@bot.message_handler(commands=["total_balance"])
@admin_only
def cmd_total_balance(message):
    main, side = totals(message.chat.id)
    positive = 0.0
    negative = 0.0
    with DB_LOCK, db() as con:
        rows = con.execute(
            "SELECT balance FROM users WHERE chat_id=? AND removed=0",
            (message.chat.id,)
        ).fetchall()
    for r in rows:
        if r["balance"] >= 0:
            positive += r["balance"]
        else:
            negative += r["balance"]

    text = (
        "📊 <b>Total Balance</b>\n"
        f"Total Negative Balance: <b>{fmt_num(negative)}</b>\n"
        f"Total Positive Balance: <b>+{fmt_num(positive)}</b>\n"
        f"Total Balance: <b>{fmt_num(main)}</b>\n"
        f"Total Side Balance: <b>{signed(side)}</b>"
    )
    safe_reply(message, text)
    send_balance_list(message.chat.id)


@bot.message_handler(commands=["chat_balance_info"])
@admin_only
def cmd_chat_balance_info(message):
    a = args(message)
    target_date = a[0] if a else date.today().isoformat()
    with DB_LOCK, db() as con:
        row = con.execute("""
            SELECT COALESCE(SUM(amount),0) total,
                   COUNT(*) cnt
            FROM chat_commissions
            WHERE chat_id=? AND substr(created_at,1,10)=?
        """, (message.chat.id, target_date)).fetchone()

        details = con.execute("""
            SELECT user_id, COALESCE(SUM(amount),0) total
            FROM chat_commissions
            WHERE chat_id=? AND substr(created_at,1,10)=?
            GROUP BY user_id
            ORDER BY total DESC
        """, (message.chat.id, target_date)).fetchall()

    lines = [
        f"💬 <b>Chat Commission — {html.escape(target_date)}</b>",
        f"Total: <b>{fmt_num(row['total'])}</b>",
        f"Entries: <b>{row['cnt']}</b>",
    ]
    for d in details:
        u = get_user(message.chat.id, d["user_id"])
        lines.append(f"• {html.escape(display_name(u))}: {fmt_num(d['total'])}")
    safe_reply(message, "\n".join(lines))


@bot.message_handler(commands=["win"])
@result_permission
def cmd_win(message):
    a = args(message)
    if not a:
        safe_reply(message, "Usage: /win @user")
        return
    uid = resolve_user(message.chat.id, a[0])
    if not uid:
        safe_reply(message, "❌ User not found.")
        return
    table = active_table_for_user(message.chat.id, uid)
    if not table:
        safe_reply(message, "❌ No active table found for that player.")
        return
    ok, msg = settle_table(table["id"], uid, message.from_user.id, "manual")
    safe_reply(message, ("✅ " if ok else "❌ ") + msg)


@bot.message_handler(commands=["cancel"])
@result_permission
def cmd_cancel(message):
    a = args(message)
    table_id = int(a[0]) if a and a[0].isdigit() else None
    table = get_table(table_id) if table_id else None
    if not table:
        table = active_table_for_user(message.chat.id, message.from_user.id)
    if not table:
        safe_reply(message, "Usage: /cancel TABLE_ID")
        return
    ok, msg = cancel_table(table["id"], message.from_user.id)
    safe_reply(message, ("✅ " if ok else "❌ ") + msg)


@bot.message_handler(commands=["revive_table"])
@admin_only
def cmd_revive(message):
    a = args(message)
    if not a or not a[0].isdigit():
        safe_reply(message, "Usage: /revive_table TABLE_ID")
        return
    ok, msg = revive_table(int(a[0]))
    safe_reply(message, ("✅ " if ok else "❌ ") + msg)


@bot.message_handler(commands=["list"])
@admin_only
def cmd_list(message):
    send_balance_list(message.chat.id)


@bot.message_handler(commands=["fulldata"])
@admin_only
def cmd_fulldata(message):
    chat_id = message.chat.id
    row = get_chat(chat_id)
    main, side = totals(chat_id)

    with DB_LOCK, db() as con:
        users = con.execute(
            "SELECT * FROM users WHERE chat_id=? AND removed=0 ORDER BY user_id",
            (chat_id,)
        ).fetchall()
        matches = con.execute(
            "SELECT COUNT(*) c FROM tables WHERE chat_id=? AND status='completed'",
            (chat_id,)
        ).fetchone()["c"]
        commission = con.execute(
            "SELECT COALESCE(SUM(amount),0) c FROM chat_commissions WHERE chat_id=?",
            (chat_id,)
        ).fetchone()["c"]

    lines = [
        "📄 <b>Chat History Report</b>",
        f"• Chat ID: <code>{chat_id}</code>",
        f"• Logging Chat ID: <code>{int(row['updates_chat_id'] or UPDATES_CHAT_ID or 0)}</code>",
        f"• Total Main Balance: {fmt_num(main)}",
        f"• Positive Main Balance: +{fmt_num(max(main, 0))}",
        f"• Negative Main Balance: {fmt_num(min(main, 0))}",
        f"• Total Side Balance: {fmt_num(side)}",
        f"• Chat Commission Rate: {fmt_num(row['default_commission'])}%",
        f"• Total Commission: {fmt_num(commission)}",
        f"• Total Matches: {matches}",
        f"• Custom Emoji: {html.escape(row['custom_emoji_text'] or '✔️')}",
        "",
        "<b>Users Info</b>",
        "S.No | User ID | Name | Custom Name | Balance | Side Balance | Commission %",
    ]

    for i, u in enumerate(users, 1):
        lines.append(
            f"{i} | {u['user_id']} | {html.escape(u['name'])} | "
            f"{html.escape(u['custom_name'])} | {fmt_num(u['balance'])} | "
            f"{signed(u['side_balance'])} | {commission_pct(chat_id,u['user_id']):g}%"
        )

    text = "\n".join(lines)
    if len(text) <= 3900:
        safe_reply(message, text)
    else:
        # Telegram message limit; send chunks.
        for i in range(0, len(text), 3800):
            safe_send(chat_id, text[i:i+3800])


@bot.message_handler(commands=["list_users"])
@admin_only
def cmd_list_users(message):
    with DB_LOCK, db() as con:
        rows = con.execute(
            "SELECT * FROM users WHERE chat_id=? AND removed=0 ORDER BY name",
            (message.chat.id,)
        ).fetchall()
    lines = ["👥 <b>Users</b>", ""]
    for i, u in enumerate(rows, 1):
        lines.append(f"{i}. {html.escape(display_name(u))} — <code>{u['user_id']}</code>")
    safe_reply(message, "\n".join(lines))


@bot.message_handler(commands=["set_custom_name"])
@admin_only
def cmd_set_custom_name(message):
    a = args(message)
    if len(a) < 2:
        safe_reply(message, "Usage: /set_custom_name @user Custom Name")
        return
    uid = resolve_user(message.chat.id, a[0])
    if not uid:
        safe_reply(message, "❌ User not found.")
        return
    custom = " ".join(a[1:]).strip()
    with DB_LOCK, db() as con:
        con.execute(
            "UPDATE users SET custom_name=? WHERE chat_id=? AND user_id=?",
            (custom, message.chat.id, uid)
        )
        con.commit()
    safe_reply(message, "✅ Custom name saved.")


@bot.message_handler(commands=["remove_custom_name"])
@admin_only
def cmd_remove_custom_name(message):
    a = args(message)
    if not a:
        safe_reply(message, "Usage: /remove_custom_name @user")
        return
    uid = resolve_user(message.chat.id, a[0])
    if not uid:
        safe_reply(message, "❌ User not found.")
        return
    with DB_LOCK, db() as con:
        con.execute(
            "UPDATE users SET custom_name='' WHERE chat_id=? AND user_id=?",
            (message.chat.id, uid)
        )
        con.commit()
    safe_reply(message, "✅ Custom name removed.")


@bot.message_handler(commands=["set_user_commission"])
@admin_only
def cmd_set_user_commission(message):
    a = args(message)
    if len(a) != 2:
        safe_reply(message, "Usage: /set_user_commission @user PERCENT")
        return
    uid = resolve_user(message.chat.id, a[0])
    if not uid:
        safe_reply(message, "❌ User not found.")
        return
    try:
        pct = float(a[1])
        if pct < 0 or pct > 100:
            raise ValueError
    except ValueError:
        safe_reply(message, "❌ Commission must be between 0 and 100.")
        return
    with DB_LOCK, db() as con:
        con.execute(
            "UPDATE users SET commission_pct=? WHERE chat_id=? AND user_id=?",
            (pct, message.chat.id, uid)
        )
        con.commit()
    safe_reply(message, f"✅ User commission set to {pct:g}%.")


@bot.message_handler(commands=["set_chat_commission"])
@admin_only
def cmd_set_chat_commission(message):
    a = args(message)
    if len(a) != 1:
        safe_reply(message, "Usage: /set_chat_commission PERCENT")
        return
    try:
        pct = float(a[0])
        if pct < 0 or pct > 100:
            raise ValueError
    except ValueError:
        safe_reply(message, "❌ Commission must be between 0 and 100.")
        return
    set_setting(message.chat.id, "default_commission", pct)
    safe_reply(message, f"✅ Default chat commission: {pct:g}%")


@bot.message_handler(commands=["set_private_log_mode"])
@admin_only
def cmd_private_log(message):
    command_setting(message, "private_log_mode", "Private logs")


@bot.message_handler(commands=["set_hide_zero_balance"])
@admin_only
def cmd_hide_zero(message):
    command_setting(message, "hide_zero_balance", "Hide zero balance")


@bot.message_handler(commands=["set_button_confirm_mode"])
@admin_only
def cmd_confirm(message):
    command_setting(message, "button_confirm_mode", "Table confirmation")


@bot.message_handler(commands=["set_admin_set_result_button"])
@admin_only
def cmd_admin_result(message):
    command_setting(message, "admin_set_result_button", "Admin result buttons")


@bot.message_handler(commands=["set_self_set_result_button"])
@admin_only
def cmd_self_result(message):
    command_setting(message, "self_set_result_button", "Self WIN/WON detection")


@bot.message_handler(commands=["set_chat_balance_commission"])
@admin_only
def cmd_chat_balance_commission(message):
    command_setting(message, "chat_balance_commission", "Chat commission display")


@bot.message_handler(commands=["set_chat_balance_list_visibility"])
@admin_only
def cmd_chat_balance_visibility(message):
    command_setting(message, "chat_balance_list_visibility", "ChatBalance visibility")


@bot.message_handler(commands=["set_full_info_button_visibility"])
@admin_only
def cmd_full_info(message):
    command_setting(message, "full_info_button_visibility", "Full Details button")


@bot.message_handler(commands=["set_show_table_creation_event"])
@admin_only
def cmd_creation_event(message):
    command_setting(message, "show_table_creation_event", "Table creation log")


@bot.message_handler(commands=["set_cancel_button_visibility"])
@admin_only
def cmd_cancel_visibility(message):
    command_setting(message, "cancel_button_visibility", "Cancel button")


@bot.message_handler(commands=["set_estimate_table_balance"])
@admin_only
def cmd_estimate_table(message):
    command_setting(message, "estimate_table_balance", "Estimated table balance")


@bot.message_handler(commands=["set_show_in_match_users_list"])
@admin_only
def cmd_match_users(message):
    command_setting(message, "show_in_match_users_list", "Match users list")


@bot.message_handler(commands=["set_list_privacy_mode"])
@admin_only
def cmd_list_privacy(message):
    command_setting(message, "list_privacy_mode", "List privacy")


@bot.message_handler(commands=["set_balance_privacy_mode"])
@admin_only
def cmd_balance_privacy(message):
    command_setting(message, "balance_privacy_mode", "Balance privacy")


@bot.message_handler(commands=["set_custom_emoji"])
@admin_only
def cmd_custom_emoji(message):
    # Best way: reply to a message containing the desired custom emoji.
    target = message.reply_to_message
    if target:
        for ent in target.entities or []:
            if ent.type == "custom_emoji":
                eid = getattr(ent, "custom_emoji_id", "")
                raw = target.text or target.caption or "✔️"
                set_setting(message.chat.id, "custom_emoji_id", eid)
                set_setting(message.chat.id, "custom_emoji_text", raw[:20])
                safe_reply(message, "✅ Custom emoji updated from the replied message.")
                return

    # Allow plain text fallback.
    a = args(message)
    if a:
        set_setting(message.chat.id, "custom_emoji_id", "")
        set_setting(message.chat.id, "custom_emoji_text", " ".join(a)[:20])
        safe_reply(message, "✅ Custom emoji marker text updated.")
        return

    safe_reply(
        message,
        "Usage: reply to a message containing the desired custom emoji with "
        "<code>/set_custom_emoji</code>."
    )


@bot.message_handler(commands=["autodel_last_list_document"])
@admin_only
def cmd_autodel(message):
    command_setting(message, "autodel_last_list_document", "Auto-delete previous list")


@bot.message_handler(commands=["set_custom_admin_mode"])
@owner_only
def cmd_custom_admin_mode(message):
    value = parse_bool_arg(message)
    if value is None:
        return
    set_setting(message.chat.id, "custom_admin_mode", int(value))
    safe_reply(message, f"✅ Custom admin mode: {'ON' if value else 'OFF'}")


@bot.message_handler(commands=["add_admin"])
@owner_only
def cmd_add_admin(message):
    a = args(message)
    if not a:
        safe_reply(message, "Usage: /add_admin USER_ID")
        return
    uid = parse_user_id(a[0])
    if not uid:
        safe_reply(message, "❌ Invalid user ID.")
        return
    with DB_LOCK, db() as con:
        con.execute(
            "INSERT OR IGNORE INTO admins(chat_id,user_id) VALUES(?,?)",
            (message.chat.id, uid)
        )
        con.commit()
    safe_reply(message, "✅ Admin added.")


@bot.message_handler(commands=["rmv_admin"])
@owner_only
def cmd_rmv_admin(message):
    a = args(message)
    if not a:
        safe_reply(message, "Usage: /rmv_admin USER_ID")
        return
    uid = parse_user_id(a[0])
    if not uid:
        safe_reply(message, "❌ Invalid user ID.")
        return
    with DB_LOCK, db() as con:
        con.execute(
            "DELETE FROM admins WHERE chat_id=? AND user_id=?",
            (message.chat.id, uid)
        )
        con.commit()
    safe_reply(message, "✅ Admin removed.")


@bot.message_handler(commands=["list_admins"])
@owner_only
def cmd_list_admins(message):
    with DB_LOCK, db() as con:
        rows = con.execute(
            "SELECT user_id FROM admins WHERE chat_id=? ORDER BY user_id",
            (message.chat.id,)
        ).fetchall()
    text = "👑 <b>Custom Admins</b>\n" + "\n".join(
        f"• <code>{r['user_id']}</code>" for r in rows
    )
    safe_reply(message, text)


@bot.message_handler(commands=["add_table_master"])
@owner_only
def cmd_add_master(message):
    a = args(message)
    if not a:
        safe_reply(message, "Usage: /add_table_master USER_ID")
        return
    uid = parse_user_id(a[0])
    if not uid:
        safe_reply(message, "❌ Invalid user ID.")
        return
    with DB_LOCK, db() as con:
        con.execute(
            "INSERT OR IGNORE INTO table_masters(chat_id,user_id) VALUES(?,?)",
            (message.chat.id, uid)
        )
        con.commit()
    safe_reply(message, "✅ Table master added.")


@bot.message_handler(commands=["rmv_table_master"])
@owner_only
def cmd_rmv_master(message):
    a = args(message)
    if not a:
        safe_reply(message, "Usage: /rmv_table_master USER_ID")
        return
    uid = parse_user_id(a[0])
    if not uid:
        safe_reply(message, "❌ Invalid user ID.")
        return
    with DB_LOCK, db() as con:
        con.execute(
            "DELETE FROM table_masters WHERE chat_id=? AND user_id=?",
            (message.chat.id, uid)
        )
        con.commit()
    safe_reply(message, "✅ Table master removed.")


@bot.message_handler(commands=["list_table_masters"])
@owner_only
def cmd_list_masters(message):
    with DB_LOCK, db() as con:
        rows = con.execute(
            "SELECT user_id FROM table_masters WHERE chat_id=? ORDER BY user_id",
            (message.chat.id,)
        ).fetchall()
    text = "🏆 <b>Table Masters</b>\n" + "\n".join(
        f"• <code>{r['user_id']}</code>" for r in rows
    )
    safe_reply(message, text)


@bot.message_handler(commands=["set_response_qr_filter_mode"])
@owner_only
def cmd_qr_mode(message):
    value = parse_bool_arg(message)
    if value is None:
        return
    set_setting(message.chat.id, "response_qr_filter_mode", int(value))
    safe_reply(message, f"✅ QR filter mode: {'ON' if value else 'OFF'}")


@bot.message_handler(commands=["add_qr_code_filter"])
@owner_only
def cmd_add_qr(message):
    a = args(message)
    if not a:
        safe_reply(message, "Usage: /add_qr_code_filter FILTER_TEXT")
        return
    value = " ".join(a).strip()
    with DB_LOCK, db() as con:
        con.execute(
            "INSERT OR IGNORE INTO qr_filters(chat_id,filter_text) VALUES(?,?)",
            (message.chat.id, value)
        )
        con.commit()
    safe_reply(message, "✅ QR filter added.")


@bot.message_handler(commands=["rmv_qr_code_filter"])
@owner_only
def cmd_rmv_qr(message):
    a = args(message)
    if not a:
        safe_reply(message, "Usage: /rmv_qr_code_filter FILTER_TEXT")
        return
    value = " ".join(a).strip()
    with DB_LOCK, db() as con:
        con.execute(
            "DELETE FROM qr_filters WHERE chat_id=? AND filter_text=?",
            (message.chat.id, value)
        )
        con.commit()
    safe_reply(message, "✅ QR filter removed.")


@bot.message_handler(commands=["list_qr_code_filters"])
@owner_only
def cmd_list_qr(message):
    with DB_LOCK, db() as con:
        rows = con.execute(
            "SELECT filter_text FROM qr_filters WHERE chat_id=? ORDER BY filter_text",
            (message.chat.id,)
        ).fetchall()
    text = "🔎 <b>QR Filters</b>\n" + "\n".join(
        f"• {html.escape(r['filter_text'])}" for r in rows
    )
    safe_reply(message, text)


@bot.message_handler(commands=["get_table_creator_stats"])
@admin_only
def cmd_creator_stats(message):
    send_table_creator_stats(message.chat.id)


@bot.message_handler(commands=["calculate"])
def cmd_calculate(message):
    expression = (message.text or "").split(maxsplit=1)
    if len(expression) < 2:
        safe_reply(message, "Usage: /calculate 1000*5/100")
        return

    expr = expression[1].strip()
    # Safe arithmetic parser: no eval, no variables, no functions.
    if len(expr) > 100 or not re.fullmatch(r"[0-9+\-*/().%\s]+", expr):
        safe_reply(message, "❌ Only basic arithmetic is allowed.")
        return

    try:
        # Convert % to /100 for simple percentage expressions.
        expr = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", expr)
        result = eval(expr, {"__builtins__": {}}, {})
        if not isinstance(result, (int, float)) or result != result:
            raise ValueError
        safe_reply(message, f"🧮 Result: <b>{fmt_num(result)}</b>")
    except Exception:
        safe_reply(message, "❌ Invalid calculation.")


@bot.message_handler(commands=["send_notification"])
@admin_only
def cmd_send_notification(message):
    target = message.reply_to_message
    if not target:
        safe_reply(message, "Reply to the message you want to send.")
        return

    a = args(message)
    mode_stats = "--stats" in a
    mode_ok = "--ok" in a

    if mode_stats:
        with DB_LOCK, db() as con:
            count = con.execute(
                "SELECT COUNT(*) c FROM users WHERE chat_id=? AND removed=0",
                (message.chat.id,)
            ).fetchone()["c"]
        safe_reply(message, f"📊 Notification recipients: <b>{count}</b>")
        return

    with DB_LOCK, db() as con:
        users = con.execute(
            "SELECT user_id FROM users WHERE chat_id=? AND removed=0 AND notifications=1",
            (message.chat.id,)
        ).fetchall()

    if not mode_ok:
        safe_reply(message, "Use <code>--ok</code> to confirm broadcast.")
        return

    sent = 0
    for u in users:
        try:
            bot.copy_message(u["user_id"], message.chat.id, target.message_id)
            sent += 1
        except Exception:
            continue

    safe_reply(message, f"✅ Broadcast finished. Sent: <b>{sent}</b>")


@bot.message_handler(commands=["help"])
def cmd_help(message):
    safe_reply(
        message,
        """🤖 <b>Available Commands</b>

🟢 <b>Basic</b>
/start
/id
/balance

💰 <b>Balance</b>
/add
/minus
/set_balance
/set_side_balance
/remove
/total_balance
/chat_balance_info

🏆 <b>Game / Table</b>
/win
/cancel
/revive_table
/list
/fulldata

👥 <b>User</b>
/list_users
/set_custom_name
/remove_custom_name
/set_user_commission

⚙️ <b>Settings</b>
/set_private_log_mode
/set_hide_zero_balance
/set_button_confirm_mode
/set_custom_emoji
/set_admin_set_result_button
/set_self_set_result_button
/set_chat_balance_commission
/set_chat_balance_list_visibility
/set_full_info_button_visibility
/set_show_table_creation_event
/set_cancel_button_visibility
/set_estimate_table_balance
/set_show_in_match_users_list
/set_list_privacy_mode
/set_balance_privacy_mode
/set_estimate_balance_dlt_timer
/set_chat_commission
/get_table_creator_stats

👑 <b>Owner</b>
/set_custom_admin_mode
/add_admin
/rmv_admin
/list_admins
/add_table_master
/rmv_table_master
/list_table_masters

🔎 <b>QR Filters</b>
/set_response_qr_filter_mode
/add_qr_code_filter
/rmv_qr_code_filter
/list_qr_code_filters

🧮 <b>Utilities</b>
/calculate
/help
/send_notification

/cadd and /cminus are intentionally disabled.
""",
    )


@bot.message_handler(commands=["set_estimate_balance_dlt_timer"])
@admin_only
def cmd_estimate_timer(message):
    safe_reply(message, "ℹ️ Estimate balance delete timer is fixed at 5 seconds.")


# ============================================================
# Notification callbacks
# ============================================================

@bot.callback_query_handler(func=lambda call: call.data == "notify")
def cb_notify(call):
    chat_id = call.message.chat.id
    uid = call.from_user.id
    upsert_user(chat_id, call.from_user)
    row = get_user(chat_id, uid)
    new_value = 0 if row["notifications"] else 1

    with DB_LOCK, db() as con:
        con.execute(
            "UPDATE users SET notifications=? WHERE chat_id=? AND user_id=?",
            (new_value, chat_id, uid)
        )
        con.commit()

    try:
        bot.answer_callback_query(
            call.id,
            f"Private notifications {'ON' if new_value else 'OFF'}",
            show_alert=True
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data == "mybalance")
def cb_mybalance(call):
    upsert_user(call.message.chat.id, call.from_user)
    text = private_balance(call.message.chat.id, call.from_user.id)
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    safe_send(call.from_user.id, text)


@bot.callback_query_handler(func=lambda call: call.data == "fulldetails")
def cb_fulldetails(call):
    chat_id = call.message.chat.id
    if not setting(chat_id, "full_info_button_visibility"):
        return
    row = get_user(chat_id, call.from_user.id)
    if not row:
        safe_send(call.from_user.id, "No record found.")
        return
    safe_send(
        call.from_user.id,
        f"📄 <b>Full Details</b>\n"
        f"Name: {html.escape(display_name(row))}\n"
        f"Main: {fmt_num(row['balance'])}\n"
        f"Side: {signed(row['side_balance'])}\n"
        f"Commission: {commission_pct(chat_id,row['user_id']):g}%"
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("win:"))
def cb_win(call):
    try:
        _, tid, which = call.data.split(":")
        tid = int(tid)
        which = int(which)
    except Exception:
        return

    table = get_table(tid)
    if not table:
        bot.answer_callback_query(call.id, "Table not found.", show_alert=True)
        return

    if not is_table_master(table["chat_id"], call.from_user.id):
        bot.answer_callback_query(call.id, "Admin/table master only.", show_alert=True)
        return

    winner_id = table["player1_id"] if which == 1 else table["player2_id"]
    ok, msg = settle_table(tid, winner_id, call.from_user.id, "button")
    bot.answer_callback_query(call.id, msg, show_alert=not ok)


@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel:"))
def cb_cancel(call):
    try:
        tid = int(call.data.split(":")[1])
    except Exception:
        return
    table = get_table(tid)
    if not table:
        bot.answer_callback_query(call.id, "Table not found.", show_alert=True)
        return
    if not is_table_master(table["chat_id"], call.from_user.id):
        bot.answer_callback_query(call.id, "Admin/table master only.", show_alert=True)
        return
    ok, msg = cancel_table(tid, call.from_user.id)
    bot.answer_callback_query(call.id, msg, show_alert=not ok)


@bot.callback_query_handler(func=lambda call: call.data.startswith("selfwin:"))
def cb_selfwin(call):
    try:
        tid = int(call.data.split(":")[1])
    except Exception:
        return
    table = get_table(tid)
    if not table:
        bot.answer_callback_query(call.id, "Table not found.", show_alert=True)
        return
    if not setting(table["chat_id"], "self_set_result_button"):
        bot.answer_callback_query(call.id, "Self result is disabled.", show_alert=True)
        return
    if call.from_user.id not in (table["player1_id"], table["player2_id"]):
        bot.answer_callback_query(call.id, "You are not a player in this table.", show_alert=True)
        return
    ok, msg = settle_table(tid, call.from_user.id, call.from_user.id, "self-button")
    bot.answer_callback_query(call.id, msg, show_alert=not ok)


# ============================================================
# Player "WIN/WON" text detection
# ============================================================

@bot.message_handler(
    func=lambda m: (
        m.chat.type in ("group", "supergroup")
        and bool(re.fullmatch(r"\s*(win|won|i\s+won)\s*[.!]?\s*", m.text or "", re.I))
    )
)
def player_win_text(message):
    if not setting(message.chat.id, "self_set_result_button"):
        return

    table = active_table_for_user(message.chat.id, message.from_user.id)
    if not table:
        return

    ok, _ = settle_table(
        table["id"],
        message.from_user.id,
        message.from_user.id,
        "self-text"
    )
    if ok:
        safe_delete(message.chat.id, message.message_id)


# ============================================================
# Automatic table detection
# ============================================================

@bot.message_handler(
    content_types=["text"],
    func=lambda m: m.chat.type in ("group", "supergroup")
)
def general_text_handler(message):
    try:
        ensure_mentioned_user(message)

        # Ignore commands.
        if (message.text or "").startswith("/"):
            return

        # A table is recognized only when the configured custom emoji is present.
        if custom_emoji_present(message):
            # By default, only admins/table masters may create automatic tables.
            if not is_table_master(message.chat.id, message.from_user.id):
                return
            create_table_from_message(message)
    except Exception:
        log.exception("general_text_handler failed")


# ============================================================
# Global error handler
# ============================================================

def polling_loop():
    while True:
        try:
            log.info("Bot polling started.")
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=False,
                allowed_updates=None,
            )
        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            break
        except Exception as exc:
            log.error("Polling crashed: %s", exc)
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    init_db()
    log.info("Database ready: %s", DB_FILE)
    log.info("Non-cash points bot starting...")
    polling_loop()
