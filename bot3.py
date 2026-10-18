import logging
import asyncio

logging.basicConfig(level=logging.INFO)

# -*- coding: utf-8 -*-
"""
⭐ STARS CASINO BOT
Python 3.10+ · aiogram 3.x · aiosqlite

Установка:
    pip install -U aiogram aiosqlite

Запуск:
    export BOT_TOKEN="123456:ABC..."     (или впишите токен в BOT_TOKEN ниже)
    python bot4.py

ВАЖНО:
  • токен НЕ храните в коде: задайте переменную BOT_TOKEN (или впишите его ниже);
  • бот должен быть АДМИНИСТРАТОРОМ в каналах WITHDRAW_CHANNEL и SUPPORT_CHANNEL
    (с правом публиковать сообщения);
  • админы должны хотя бы раз нажать /start у бота (иначе он не сможет им писать);
  • для каналов-спонсоров бот тоже должен быть админом (иначе нельзя проверить подписку).
"""

import asyncio
import html
import logging
import math
import os
import random
import re
import sqlite3
import time
from contextlib import suppress
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import DiceEmoji, ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandObject, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

# ════════════════════════════════════════════════════════════════════════
#  ⚙️  НАСТРОЙКИ
# ════════════════════════════════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "8830820622:AAEA0bSNxGtgx5fhGbBRqpjFQAYS1E230Gc")

ADMINS = {8201986821, 8765850538}

WITHDRAW_CHANNEL = "@casinojo1"       # сюда падают заявки на вывод
SUPPORT_CHANNEL = "@podderzhkat45"    # сюда падают обращения в поддержку

DB_PATH = "casino.db"

REF_REWARD = 2            # ⭐ за каждого приглашённого друга
MIN_WITHDRAW = 50         # минимальный вывод
MIN_DEPOSIT = 1
MAX_DEPOSIT = 10_000
MIN_BET = 1

WIN_MULT = 1.5            # угадал  -> ставка x1.5
LOSE_MULT = 0.5           # не угадал -> ставка x0.5

QUICK_DEPOSITS = (10, 25, 50, 100, 250, 500)
QUICK_BETS = (1, 5, 10, 25, 50, 100)
SUPPORT_COOLDOWN = 15     # секунд между обращениями в поддержку

# Спонсоры
ADMINS_SKIP_GATE = False  # False -> админы тоже видят окно подписки (удобно тестировать)
SUB_CACHE_TTL = 120       # сколько секунд помнить, что человек подписан

# ════════════════════════════════════════════════════════════════════════
log = logging.getLogger("casino")
RNG = random.SystemRandom()

BOT_INFO = {"id": 0, "username": ""}
STATE = {"maint": False}                     # режим тех. работ
PENDING_REPLY: dict[int, dict] = {}          # админ -> кому отвечает в поддержке
PLAYING: set[int] = set()                    # кто сейчас в игре
SUB_CACHE: dict[int, float] = {}             # кэш проверки подписки
SUPPORT_LAST: dict[int, float] = {}          # антиспам поддержки
BG_TASKS: set = set()
SPONSOR_WARN: dict[int, float] = {}          # антиспам предупреждений о спонсорах
REF_CLEAN: set[int] = set()                  # у этих пользователей нет неоплаченного реферала

LINE = "━━━━━━━━━━━━━━━━"
REF_RE = re.compile(r"^/start(?:@\w+)?\s+ref_(\d+)")


# ════════════════════════════════════════════════════════════════════════
#  🧰  ХЕЛПЕРЫ
# ════════════════════════════════════════════════════════════════════════
def fmt(x) -> str:
    """132.0 -> '132', 12.5 -> '12.5', 1000 -> '1 000'"""
    x = round(float(x), 2)
    return f"{x:,.2f}".rstrip("0").rstrip(".").replace(",", " ")


def num(x) -> str:
    """Для callback_data: 10.0 -> '10', 12.5 -> '12.5'"""
    x = round(float(x), 2)
    return str(int(x)) if x == int(x) else str(x)


def esc(s) -> str:
    return html.escape(str(s) if s is not None else "", quote=False)


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def ts(t) -> str:
    return datetime.fromtimestamp(int(t), timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def parse_amount(s) -> Optional[float]:
    try:
        v = float(str(s).strip().replace(",", ".").replace(" ", ""))
    except ValueError:
        return None
    if not math.isfinite(v):
        return None
    return round(v, 2)


def parse_duration(s: str) -> Optional[int]:
    """'30м' '2ч' '7д' '90m' '12h' '3d' -> секунды (без единицы = часы)."""
    m = re.fullmatch(r"(\d+)\s*([a-zа-я]*)", s.strip().lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    if unit in ("м", "мин", "m", "min"):
        k = 60
    elif unit in ("", "ч", "час", "h", "hr"):
        k = 3600
    elif unit in ("д", "дн", "d", "day"):
        k = 86400
    else:
        return None
    return n * k if n > 0 else None


def parse_link(raw: str):
    """-> (url, chat) ; chat = '@username' или None, если проверить нельзя."""
    raw = raw.strip()
    m = re.match(
        r"^(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,32})/?(?:\?.*)?$", raw
    )
    if m:
        return f"https://t.me/{m.group(1)}", f"@{m.group(1)}"
    m = re.match(r"^@([A-Za-z0-9_]{4,32})$", raw)
    if m:
        return f"https://t.me/{m.group(1)}", f"@{m.group(1)}"
    if re.match(r"^https?://\S+$", raw):
        return raw, None
    return None, None


def norm_chat(chat):
    s = str(chat)
    return int(s) if re.fullmatch(r"-?\d+", s) else s


def is_banned(row) -> bool:
    return bool(row["banned"]) or row["ban_until"] > time.time()


def spawn(coro):
    t = asyncio.create_task(coro)
    BG_TASKS.add(t)
    t.add_done_callback(BG_TASKS.discard)
    return t


def B(text, cb=None, url=None, style=None) -> InlineKeyboardButton:
    """Кнопка. style: 'primary' (синяя) | 'success' (зелёная) | 'danger' (красная)."""
    kw = {"text": text}
    if cb is not None:
        kw["callback_data"] = cb
    if url is not None:
        kw["url"] = url
    if style:
        kw["style"] = style
    return InlineKeyboardButton(**kw)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


async def safe_edit(message: Message, text: str, markup=None):
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        await message.answer(text, reply_markup=markup)


async def is_member(bot: Bot, chat, uid: int) -> Optional[bool]:
    """True/False, либо None, если проверить не удалось."""
    try:
        m = await bot.get_chat_member(norm_chat(chat), uid)
    except Exception as e:
        log.warning("get_chat_member(%s) failed: %s", chat, e)
        return None
    if m.status in ("left", "kicked"):
        return False
    if m.status == "restricted" and not getattr(m, "is_member", True):
        return False
    return True


async def chat_info(bot: Bot, chat):
    """-> (title, bot_is_admin, chat_id) ; title=None если чат не найден."""
    try:
        c = await bot.get_chat(norm_chat(chat))
    except Exception:
        return None, False, None
    admin = False
    with suppress(Exception):
        m = await bot.get_chat_member(c.id, BOT_INFO["id"])
        admin = m.status in ("administrator", "creator")
    return (c.title or c.username or str(chat)), admin, c.id


# ════════════════════════════════════════════════════════════════════════
#  🗄  БАЗА ДАННЫХ
# ════════════════════════════════════════════════════════════════════════
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    balance REAL NOT NULL DEFAULT 0,
    referrer_id INTEGER,
    ref_count INTEGER NOT NULL DEFAULT 0,
    banned INTEGER NOT NULL DEFAULT 0,
    ban_until INTEGER NOT NULL DEFAULT 0,
    total_deposit REAL NOT NULL DEFAULT 0,
    total_withdraw REAL NOT NULL DEFAULT 0,
    games INTEGER NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0,
    ref_paid INTEGER NOT NULL DEFAULT 0,
    joined_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payments(
    charge_id TEXT PRIMARY KEY,
    user_id INTEGER,
    amount INTEGER,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS withdrawals(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    channel_msg_id INTEGER,
    created_at INTEGER,
    handled_by INTEGER
);
CREATE TABLE IF NOT EXISTS promos(
    code TEXT PRIMARY KEY,
    reward REAL,
    max_uses INTEGER,
    used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS promo_uses(
    code TEXT,
    user_id INTEGER,
    PRIMARY KEY(code, user_id)
);
CREATE TABLE IF NOT EXISTS tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    url TEXT,
    chat TEXT,
    reward REAL
);
CREATE TABLE IF NOT EXISTS task_done(
    task_id INTEGER,
    user_id INTEGER,
    PRIMARY KEY(task_id, user_id)
);
CREATE TABLE IF NOT EXISTS sponsors(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    url TEXT,
    chat TEXT
);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection = None  # type: ignore
        self.lock = asyncio.Lock()

    async def init(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        await self.conn.commit()

    async def _migrate(self):
        """Добавляет новые колонки в уже существующую базу."""
        cols = {r["name"] for r in await self.all("PRAGMA table_info(users)")}
        if "ref_paid" not in cols:
            await self.conn.execute(
                "ALTER TABLE users ADD COLUMN ref_paid INTEGER NOT NULL DEFAULT 0"
            )
            # все прежние рефералы уже были оплачены при старте — повторно не платим
            await self.conn.execute("UPDATE users SET ref_paid=1")

    # ── низкоуровневое ──
    async def one(self, sql, args=()):
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchone()

    async def all(self, sql, args=()):
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchall()

    async def run(self, sql, args=()) -> int:
        async with self.lock:
            cur = await self.conn.execute(sql, args)
            await self.conn.commit()
            rc = cur.rowcount
            await cur.close()
            return rc

    # ── пользователи ──
    async def register(self, u, ref_id: Optional[int] = None):
        """-> (is_new, referrer_id | None)"""
        async with self.lock:
            row = await self.one(
                "SELECT username, first_name, blocked FROM users WHERE user_id=?",
                (u.id,),
            )
            if row:
                if (
                    row["username"] != u.username
                    or row["first_name"] != u.first_name
                    or row["blocked"]
                ):
                    await self.conn.execute(
                        "UPDATE users SET username=?, first_name=?, blocked=0 WHERE user_id=?",
                        (u.username, u.first_name, u.id),
                    )
                    await self.conn.commit()
                return False, None

            referrer = None
            if ref_id and ref_id != u.id:
                r = await self.one("SELECT user_id FROM users WHERE user_id=?", (ref_id,))
                if r:
                    referrer = ref_id
            # награда рефереру НЕ выдаётся здесь — только после подписки на спонсоров
            await self.conn.execute(
                "INSERT INTO users(user_id, username, first_name, referrer_id, joined_at) "
                "VALUES(?,?,?,?,?)",
                (u.id, u.username, u.first_name, referrer, int(time.time())),
            )
            await self.conn.commit()
            return True, referrer

    async def credit_referral(self, uid: int) -> Optional[int]:
        """Засчитывает приглашённого (один раз). -> id реферера, если награда выдана сейчас."""
        async with self.lock:
            row = await self.one(
                "SELECT referrer_id, ref_paid FROM users WHERE user_id=?", (uid,)
            )
            if not row or not row["referrer_id"] or row["ref_paid"]:
                return None
            await self.conn.execute("UPDATE users SET ref_paid=1 WHERE user_id=?", (uid,))
            await self.conn.execute(
                "UPDATE users SET balance=ROUND(balance+?,2), ref_count=ref_count+1 "
                "WHERE user_id=?",
                (REF_REWARD, row["referrer_id"]),
            )
            await self.conn.commit()
            return row["referrer_id"]

    async def ref_pending(self, uid: int) -> int:
        """Сколько приглашённых ещё не подписались на спонсоров."""
        r = await self.one(
            "SELECT COUNT(*) AS c FROM users WHERE referrer_id=? AND ref_paid=0", (uid,)
        )
        return r["c"] if r else 0

    async def user(self, uid: int):
        return await self.one("SELECT * FROM users WHERE user_id=?", (uid,))

    async def find_user(self, q: str):
        q = q.strip().replace("https://t.me/", "").replace("t.me/", "").lstrip("@")
        if not q:
            return None
        if q.isdigit():
            return await self.user(int(q))
        return await self.one(
            "SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (q,)
        )

    async def all_user_ids(self):
        rows = await self.all("SELECT user_id FROM users WHERE blocked=0")
        return [r["user_id"] for r in rows]

    async def mark_blocked(self, uid: int):
        await self.run("UPDATE users SET blocked=1 WHERE user_id=?", (uid,))

    async def set_ban(self, uid: int, banned: int, until: int):
        await self.run(
            "UPDATE users SET banned=?, ban_until=? WHERE user_id=?", (banned, until, uid)
        )

    # ── баланс ──
    async def add(self, uid: int, amount: float, game: bool = False):
        sql = "UPDATE users SET balance=ROUND(balance+?,2)"
        if game:
            sql += ", games=games+1"
        await self.run(sql + " WHERE user_id=?", (amount, uid))

    async def take(self, uid: int, amount: float) -> bool:
        return (
            await self.run(
                "UPDATE users SET balance=ROUND(balance-?,2) WHERE user_id=? AND balance>=?",
                (amount, uid, amount),
            )
            > 0
        )

    async def admin_take(self, uid: int, amount: float):
        await self.run(
            "UPDATE users SET balance=MAX(0, ROUND(balance-?,2)) WHERE user_id=?",
            (amount, uid),
        )

    async def settle(self, uid: int, bet: float, payout: float) -> bool:
        """Атомарно: списать ставку и сразу начислить выигрыш."""
        return (
            await self.run(
                "UPDATE users SET balance=ROUND(balance-?+?,2), games=games+1 "
                "WHERE user_id=? AND balance>=?",
                (bet, payout, uid, bet),
            )
            > 0
        )

    # ── пополнения ──
    async def record_payment(self, uid: int, amount: int, charge_id: str) -> bool:
        async with self.lock:
            cur = await self.conn.execute(
                "INSERT OR IGNORE INTO payments(charge_id,user_id,amount,created_at) "
                "VALUES(?,?,?,?)",
                (charge_id, uid, amount, int(time.time())),
            )
            inserted = cur.rowcount > 0
            await cur.close()
            if inserted:
                await self.conn.execute(
                    "UPDATE users SET balance=ROUND(balance+?,2), "
                    "total_deposit=total_deposit+? WHERE user_id=?",
                    (amount, amount, uid),
                )
            await self.conn.commit()
            return inserted

    # ── выводы ──
    async def create_withdrawal(self, uid: int, amount: float) -> Optional[int]:
        async with self.lock:
            cur = await self.conn.execute(
                "UPDATE users SET balance=ROUND(balance-?,2) WHERE user_id=? AND balance>=?",
                (amount, uid, amount),
            )
            ok = cur.rowcount > 0
            await cur.close()
            if not ok:
                await self.conn.commit()
                return None
            cur = await self.conn.execute(
                "INSERT INTO withdrawals(user_id, amount, created_at) VALUES(?,?,?)",
                (uid, amount, int(time.time())),
            )
            wid = cur.lastrowid
            await cur.close()
            await self.conn.commit()
            return wid

    async def withdrawal(self, wid: int):
        return await self.one("SELECT * FROM withdrawals WHERE id=?", (wid,))

    async def resolve_withdrawal(self, wid: int, status: str, admin_id: int):
        """status: paid | rejected. Возвращает заявку, если она была в ожидании."""
        async with self.lock:
            w = await self.one(
                "SELECT * FROM withdrawals WHERE id=? AND status='pending'", (wid,)
            )
            if not w:
                return None
            await self.conn.execute(
                "UPDATE withdrawals SET status=?, handled_by=? WHERE id=?",
                (status, admin_id, wid),
            )
            if status == "paid":
                await self.conn.execute(
                    "UPDATE users SET total_withdraw=total_withdraw+? WHERE user_id=?",
                    (w["amount"], w["user_id"]),
                )
            else:  # возврат средств
                await self.conn.execute(
                    "UPDATE users SET balance=ROUND(balance+?,2) WHERE user_id=?",
                    (w["amount"], w["user_id"]),
                )
            await self.conn.commit()
            return w

    # ── промокоды ──
    async def create_promo(self, code: str, reward: float, max_uses: int) -> bool:
        try:
            await self.run(
                "INSERT INTO promos(code, reward, max_uses) VALUES(?,?,?)",
                (code, reward, max_uses),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    async def promos(self):
        return await self.all("SELECT * FROM promos ORDER BY rowid DESC")

    async def del_promo(self, code: str):
        await self.run("DELETE FROM promos WHERE code=?", (code,))
        await self.run("DELETE FROM promo_uses WHERE code=?", (code,))

    async def use_promo(self, uid: int, code: str):
        async with self.lock:
            p = await self.one("SELECT * FROM promos WHERE code=?", (code,))
            if not p:
                return "none", 0
            if await self.one(
                "SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (code, uid)
            ):
                return "used", 0
            if p["used"] >= p["max_uses"]:
                return "over", 0
            await self.conn.execute(
                "INSERT INTO promo_uses(code, user_id) VALUES(?,?)", (code, uid)
            )
            await self.conn.execute("UPDATE promos SET used=used+1 WHERE code=?", (code,))
            await self.conn.execute(
                "UPDATE users SET balance=ROUND(balance+?,2) WHERE user_id=?",
                (p["reward"], uid),
            )
            await self.conn.commit()
            return "ok", p["reward"]

    # ── задания ──
    async def add_task(self, title, url, chat, reward) -> int:
        async with self.lock:
            cur = await self.conn.execute(
                "INSERT INTO tasks(title, url, chat, reward) VALUES(?,?,?,?)",
                (title, url, chat, reward),
            )
            tid = cur.lastrowid
            await cur.close()
            await self.conn.commit()
            return tid

    async def tasks(self):
        return await self.all("SELECT * FROM tasks ORDER BY id")

    async def tasks_for(self, uid: int):
        return await self.all(
            "SELECT * FROM tasks WHERE id NOT IN "
            "(SELECT task_id FROM task_done WHERE user_id=?) ORDER BY id",
            (uid,),
        )

    async def task(self, tid: int):
        return await self.one("SELECT * FROM tasks WHERE id=?", (tid,))

    async def del_task(self, tid: int):
        await self.run("DELETE FROM tasks WHERE id=?", (tid,))
        await self.run("DELETE FROM task_done WHERE task_id=?", (tid,))

    async def complete_task(self, uid: int, tid: int) -> Optional[float]:
        async with self.lock:
            t = await self.one("SELECT * FROM tasks WHERE id=?", (tid,))
            if not t:
                return None
            cur = await self.conn.execute(
                "INSERT OR IGNORE INTO task_done(task_id, user_id) VALUES(?,?)", (tid, uid)
            )
            inserted = cur.rowcount > 0
            await cur.close()
            if not inserted:
                await self.conn.commit()
                return None
            await self.conn.execute(
                "UPDATE users SET balance=ROUND(balance+?,2) WHERE user_id=?",
                (t["reward"], uid),
            )
            await self.conn.commit()
            return t["reward"]

    # ── спонсоры ──
    async def add_sponsor(self, title, url, chat):
        await self.run(
            "INSERT INTO sponsors(title, url, chat) VALUES(?,?,?)", (title, url, chat)
        )

    async def sponsors(self):
        return await self.all("SELECT * FROM sponsors ORDER BY id")

    async def del_sponsor(self, sid: int):
        await self.run("DELETE FROM sponsors WHERE id=?", (sid,))

    # ── настройки ──
    async def get_setting(self, key: str):
        r = await self.one("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else None

    async def set_setting(self, key: str, value: str):
        await self.run(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ── статистика ──
    async def stats(self) -> dict:
        now = int(time.time())
        u = await self.one(
            "SELECT COUNT(*) AS total, SUM(joined_at>=?) AS d1, SUM(joined_at>=?) AS d7, "
            "SUM(banned=1 OR ban_until>?) AS banned, SUM(blocked) AS blocked, "
            "COALESCE(SUM(balance),0) AS bal, COALESCE(SUM(games),0) AS games, "
            "COALESCE(SUM(ref_count),0) AS refs FROM users",
            (now - 86400, now - 7 * 86400, now),
        )
        dep = await self.one(
            "SELECT COALESCE(SUM(amount),0) AS s, COUNT(*) AS c FROM payments"
        )
        paid = await self.one(
            "SELECT COALESCE(SUM(amount),0) AS s, COUNT(*) AS c FROM withdrawals WHERE status='paid'"
        )
        pend = await self.one(
            "SELECT COALESCE(SUM(amount),0) AS s, COUNT(*) AS c FROM withdrawals WHERE status='pending'"
        )
        return {
            "total": u["total"] or 0,
            "d1": u["d1"] or 0,
            "d7": u["d7"] or 0,
            "banned": u["banned"] or 0,
            "blocked": u["blocked"] or 0,
            "bal": u["bal"],
            "games": u["games"],
            "refs": u["refs"],
            "dep_s": dep["s"],
            "dep_c": dep["c"],
            "paid_s": paid["s"],
            "paid_c": paid["c"],
            "pend_s": pend["s"],
            "pend_c": pend["c"],
        }


db = DB(DB_PATH)


# ════════════════════════════════════════════════════════════════════════
#  🧠  СОСТОЯНИЯ (FSM)
# ════════════════════════════════════════════════════════════════════════
class BetSt(StatesGroup):
    amount = State()


class DepSt(StatesGroup):
    amount = State()


class WdSt(StatesGroup):
    amount = State()


class SupSt(StatesGroup):
    text = State()


class PromoSt(StatesGroup):
    code = State()


class AdmSt(StatesGroup):
    find = State()
    give = State()
    take = State()
    tban = State()
    promo = State()
    task = State()
    sponsor = State()


# ════════════════════════════════════════════════════════════════════════
#  🎨  ТЕКСТЫ И КЛАВИАТУРЫ
# ════════════════════════════════════════════════════════════════════════
GAMES = {
    "color": {"title": "🔴🔵 <b>КРАСНОЕ · СИНЕЕ</b>"},
    "dice": {"title": "🎲 <b>КУБИК</b>"},
}


def menu_text(name, balance, games=0) -> str:
    return (
        f"⭐ <b>STARS CASINO</b>\n{LINE}\n"
        f"👋 Привет, <b>{esc(str(name)[:30])}</b>!\n\n"
        f"💎 Баланс: <b>{fmt(balance)} ⭐</b>\n"
        f"🎮 Сыграно игр: <b>{games}</b>\n{LINE}\n"
        "🎯 Готов к большим победам?"
    )


def main_kb() -> InlineKeyboardMarkup:
    return kb(
        [B("🎁 Получить бесплатно", "free")],
        [B("🎟 Промокод", "promo"), B("⭐ Задания", "tasks")],
        [
            B("💳 Пополнить", "deposit", style="success"),
            B("💸 Вывести", "withdraw", style="success"),
        ],
        [B("🎮 Игры со ставками", "games", style="primary")],
        [B("🛠 Тех поддержка", "support")],
    )


def back_kb(cb="menu", text="⬅️ Назад") -> InlineKeyboardMarkup:
    return kb([B(text, cb)])


async def render_menu(msg: Message, uid: int, edit: bool = False):
    row = await db.user(uid)
    text = menu_text(
        row["first_name"] if row else "друг",
        row["balance"] if row else 0,
        row["games"] if row else 0,
    )
    if edit:
        await safe_edit(msg, text, main_kb())
    else:
        await msg.answer(text, reply_markup=main_kb())


# ════════════════════════════════════════════════════════════════════════
#  🛡  MIDDLEWARE: регистрация · бан · тех. работы · спонсоры · рефералы
# ════════════════════════════════════════════════════════════════════════
async def deny(event, text_html: str):
    with suppress(Exception):
        if isinstance(event, CallbackQuery):
            await event.answer(strip_tags(text_html)[:200], show_alert=True)
        else:
            await event.answer(text_html)


async def warn_unreachable(bot: Bot, s):
    """Бот не может проверить подписку на спонсора → предупреждаем админов (раз в час)."""
    now = time.time()
    if SPONSOR_WARN.get(s["id"], 0) > now:
        return
    SPONSOR_WARN[s["id"]] = now + 3600
    for a in ADMINS:
        with suppress(Exception):
            await bot.send_message(
                a,
                f"⚠️ <b>Спонсор не проверяется</b>\n{LINE}\n"
                f"📢 {esc(s['title'])}\n\n"
                "Бот не может проверить подписку: он не админ канала или его оттуда "
                "удалили. Пока что этот спонсор пропускается.\n\n"
                "<i>Проверить: /panel → 📢 Спонсоры → 📋 Список</i>",
            )


async def check_sponsors(bot: Bot, uid: int):
    """-> (missing, statuses); statuses = [(спонсор, True | False | None)]."""
    if SUB_CACHE.get(uid, 0) > time.time():
        return [], []
    statuses = []
    for s in await db.sponsors():
        st = await is_member(bot, s["chat"], uid)
        if st is None:
            spawn(warn_unreachable(bot, s))
        statuses.append((s, st))
    missing = [s for s, st in statuses if st is False]
    if not missing:
        SUB_CACHE[uid] = time.time() + SUB_CACHE_TTL
    return missing, statuses


def gate_content(statuses):
    """-> (text, rows) окна подписки. Спонсоры, которых нельзя проверить (None), не показываем."""
    shown = [(s, st) for s, st in statuses if st is not None]
    lines = [f"{'✅' if st else '❌'}  {esc(s['title'])}" for s, st in shown]
    text = (
        f"🔒 <b>ДОСТУП ЗАКРЫТ</b>\n{LINE}\n"
        "Чтобы открыть бота, подпишись на каналы наших спонсоров 👇\n\n"
        + "\n".join(lines)
        + f"\n{LINE}\n"
        "После подписки нажми <b>«✅ Проверить подписку»</b>"
    )
    rows = [
        [B(f"📢 Подписаться · {s['title']}"[:60], url=s["url"], style="primary")]
        for s, st in shown
        if st is False
    ]
    return text, rows


async def show_gate(event, statuses):
    text, rows = gate_content(statuses)
    rows.append([B("✅ Проверить подписку", "check_sub", style="success")])
    markup = kb(*rows)
    if isinstance(event, CallbackQuery):
        with suppress(Exception):
            await event.answer()
        if event.message:
            await safe_edit(event.message, text, markup)
    else:
        await event.answer(text, reply_markup=markup)


async def notify_referrer(bot: Bot, referrer: int, u):
    with suppress(Exception):
        await bot.send_message(
            referrer,
            f"🎉 <b>Друг засчитан!</b>\n{LINE}\n"
            f"👤 {esc(str(u.first_name)[:30])} подписался на спонсоров "
            "и стал вашим рефералом\n"
            f"💰 Вам начислено: <b>+{REF_REWARD} ⭐</b>",
            reply_markup=kb([B("🎁 Пригласить ещё", "free", style="success")]),
        )


async def finalize_access(bot: Bot, u):
    """Человек прошёл проверку подписки → засчитываем его реферала (один раз)."""
    if u.id in REF_CLEAN:
        return
    try:
        referrer = await db.credit_referral(u.id)
    except Exception:
        log.exception("Не удалось засчитать реферала для %s", u.id)
        return
    REF_CLEAN.add(u.id)
    if referrer:
        spawn(notify_referrer(bot, referrer, u))


class Guard(BaseMiddleware):
    """Внешний middleware: регистрация, бан, тех. работы."""

    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u is None or u.is_bot:
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            chat = event.message.chat if event.message else None
            text = ""
        else:
            chat = event.chat
            text = event.text or ""

        # каналы / группы (кнопки "Ответить", "Выплачено") — проверяют админа сами
        if chat is not None and chat.type != "private":
            return await handler(event, data)

        m = REF_RE.match(text)
        await db.register(u, int(m.group(1)) if m else None)

        if isinstance(event, Message) and event.successful_payment:
            return await handler(event, data)
        if u.id in ADMINS:
            return await handler(event, data)

        row = await db.user(u.id)
        if row and is_banned(row):
            if row["banned"]:
                await deny(event, "🚫 <b>Вы заблокированы</b>\n\nДоступ к боту ограничен администрацией.")
            else:
                await deny(event, f"⏳ <b>Вы временно заблокированы</b>\n\nДо: {ts(row['ban_until'])}")
            return

        if STATE["maint"]:
            await deny(
                event,
                "🛠 <b>Технические работы</b>\n\nБот скоро вернётся. Спасибо за терпение! 💙",
            )
            return

        return await handler(event, data)


class SponsorGate(BaseMiddleware):
    """Внутренний middleware пользовательского роутера: обязательная подписка на спонсоров.
    Админские хендлеры (/panel, /send, ответы поддержки, выплаты) сюда не попадают."""

    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u is None or u.is_bot:
            return await handler(event, data)

        if isinstance(event, Message):
            if event.chat.type != "private" or event.successful_payment:
                return await handler(event, data)
        elif isinstance(event, CallbackQuery):
            if event.message and event.message.chat.type != "private":
                return await handler(event, data)
            if event.data == "check_sub":  # эту проверку делает сам хендлер
                return await handler(event, data)

        bot: Bot = data["bot"]
        if not (u.id in ADMINS and ADMINS_SKIP_GATE):
            missing, statuses = await check_sponsors(bot, u.id)
            if missing:
                await show_gate(event, statuses)
                return

        await finalize_access(bot, u)  # подписка есть (или спонсоров нет) → реферал засчитан
        return await handler(event, data)


# ════════════════════════════════════════════════════════════════════════
#  🔀  РОУТЕРЫ
# ════════════════════════════════════════════════════════════════════════
class IsAdmin(Filter):
    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        return bool(u and u.id in ADMINS)


class PendingReply(Filter):
    async def __call__(self, message: Message) -> bool:
        return bool(
            message.from_user
            and message.chat.type == "private"
            and message.from_user.id in PENDING_REPLY
            and message.text
            and not message.text.startswith("/")
        )


r_act = Router(name="actions")   # действия из каналов + ответы поддержки
r_admin = Router(name="admin")   # /panel, /send
r_user = Router(name="user")     # всё остальное
r_user.message.middleware(SponsorGate())
r_user.callback_query.middleware(SponsorGate())

r_admin.message.filter(IsAdmin(), F.chat.type == "private")
r_admin.callback_query.filter(IsAdmin())


# ════════════════════════════════════════════════════════════════════════
#  📨  ДЕЙСТВИЯ ИЗ КАНАЛОВ: поддержка и заявки на вывод
# ════════════════════════════════════════════════════════════════════════
def wd_text(w, row, status="pending", admin=None) -> str:
    st = {
        "pending": "⏳ <b>Ожидает выплаты</b>",
        "paid": "✅ <b>Выплачено</b>",
        "rejected": "❌ <b>Отклонено</b> · средства возвращены",
    }[status]
    uname = f"@{esc(row['username'])}" if row["username"] else "—"
    t = (
        f"💸 <b>ЗАЯВКА НА ВЫВОД #{w['id']}</b>\n{LINE}\n"
        f"👤 {esc(row['first_name'])} · {uname}\n"
        f"🆔 <code>{w['user_id']}</code>\n"
        f"💰 Сумма: <b>{fmt(w['amount'])} ⭐</b>\n"
        f"🕒 {ts(w['created_at'])}\n{LINE}\n{st}"
    )
    if admin:
        t += f"\n👮 {esc(admin.full_name)}"
    return t


@r_act.callback_query(F.data.regexp(r"^wd(ok|no):\d+$"))
async def wd_admin(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMINS:
        await call.answer("⛔ Только для администраторов", show_alert=True)
        return
    action, wid_s = call.data.split(":")
    wid = int(wid_s)
    status = "paid" if action == "wdok" else "rejected"
    w = await db.resolve_withdrawal(wid, status, call.from_user.id)
    if not w:
        await call.answer("Эта заявка уже обработана", show_alert=True)
        return
    row = await db.user(w["user_id"])
    await safe_edit(call.message, wd_text(w, row, status, call.from_user), None)
    if status == "paid":
        msg = (
            f"✅ <b>Заявка #{wid} выплачена!</b>\n\n"
            f"💰 Сумма: <b>{fmt(w['amount'])} ⭐</b>\n"
            "Спасибо, что играете с нами 💙"
        )
        await call.answer("✅ Отмечено как выплаченное")
    else:
        msg = (
            f"❌ <b>Заявка #{wid} отклонена</b>\n\n"
            f"💰 {fmt(w['amount'])} ⭐ возвращены на ваш баланс.\n"
            "Если есть вопросы — напишите в поддержку."
        )
        await call.answer("❌ Заявка отклонена, средства возвращены")
    with suppress(Exception):
        await bot.send_message(
            w["user_id"], msg, reply_markup=kb([B("🏠 В меню", "menu")])
        )


@r_act.callback_query(F.data.startswith("sup:"))
async def sup_reply_btn(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMINS:
        await call.answer("⛔ Только для администраторов", show_alert=True)
        return
    uid = int(call.data.split(":")[1])
    PENDING_REPLY[call.from_user.id] = {
        "uid": uid,
        "chat": call.message.chat.id,
        "msg": call.message.message_id,
        "html": call.message.html_text,
    }
    try:
        await bot.send_message(
            call.from_user.id,
            f"✍️ <b>Ответ пользователю</b> <code>{uid}</code>\n{LINE}\n"
            "Отправьте текст ответа одним сообщением — я доставлю его в бота.\n\n"
            "<i>Отмена — /cancel</i>",
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        PENDING_REPLY.pop(call.from_user.id, None)
        await call.answer(
            "Сначала откройте бота и нажмите /start, затем повторите.", show_alert=True
        )
        return
    await call.answer("✍️ Введите ответ в личных сообщениях с ботом")


@r_act.message(PendingReply())
async def sup_reply_send(message: Message, bot: Bot):
    data = PENDING_REPLY.pop(message.from_user.id)
    uid = data["uid"]
    try:
        await bot.send_message(
            uid,
            f"💬 <b>ОТВЕТ ПОДДЕРЖКИ</b>\n{LINE}\n{message.html_text}\n{LINE}\n"
            "<i>Остались вопросы? Напишите нам снова 👇</i>",
            reply_markup=kb(
                [B("✉️ Написать ещё", "support", style="primary")],
                [B("🏠 В меню", "menu")],
            ),
        )
    except TelegramForbiddenError:
        await message.answer("⚠️ Пользователь заблокировал бота — ответ не доставлен.")
        return
    except Exception as e:
        await message.answer(f"⚠️ Не удалось отправить ответ: {esc(e)}")
        return

    await message.answer(f"✅ Ответ доставлен пользователю <code>{uid}</code>")
    preview = esc(message.text[:300])
    new = (
        f"{data['html']}\n\n✅ <b>Отвечено</b> · <i>{esc(message.from_user.full_name)}</i>\n"
        f"<blockquote>{preview}</blockquote>"
    )
    with suppress(Exception):
        await bot.edit_message_text(
            new,
            chat_id=data["chat"],
            message_id=data["msg"],
            reply_markup=kb([B("✉️ Ответить ещё раз", f"sup:{uid}", style="primary")]),
        )


# ════════════════════════════════════════════════════════════════════════
#  👑  АДМИН: /send (рассылка)
# ════════════════════════════════════════════════════════════════════════
async def _deliver(bot: Bot, uid: int, src: Optional[Message], text: Optional[str]) -> str:
    for _ in range(3):
        try:
            if src is not None:
                await bot.copy_message(uid, src.chat.id, src.message_id)
            else:
                await bot.send_message(uid, text)
            return "ok"
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            return "blocked"
        except Exception:
            return "fail"
    return "fail"


async def broadcast(bot: Bot, src, text, ids, status: Message, admin_id: int):
    ok = blocked = fail = 0
    total = len(ids)
    for i, uid in enumerate(ids, 1):
        res = await _deliver(bot, uid, src, text)
        if res == "ok":
            ok += 1
        elif res == "blocked":
            blocked += 1
            await db.mark_blocked(uid)
        else:
            fail += 1
        await asyncio.sleep(0.05)  # ~20 сообщений/сек
        if i % 100 == 0:
            with suppress(Exception):
                await status.edit_text(f"📨 <b>Рассылка…</b> {i}/{total}")
    report = (
        f"📨 <b>РАССЫЛКА ЗАВЕРШЕНА</b>\n{LINE}\n"
        f"✅ Доставлено: <b>{ok}</b>\n"
        f"🚫 Заблокировали бота: <b>{blocked}</b>\n"
        f"⚠️ Ошибок: <b>{fail}</b>\n"
        f"👥 Всего: <b>{total}</b>"
    )
    with suppress(Exception):
        await status.edit_text(report)
    with suppress(Exception):
        await bot.send_message(admin_id, report)


@r_admin.message(Command("send"))
async def cmd_send(message: Message, command: CommandObject, bot: Bot):
    src = message.reply_to_message
    text = None
    if src is None:
        if not command.args:
            await message.answer(
                "📨 <b>РАССЫЛКА</b>\n"
                f"{LINE}\n"
                "Два способа:\n\n"
                "1️⃣ Отправьте боту любое сообщение (текст, фото, видео…), затем "
                "<b>ответьте на него</b> командой <code>/send</code>\n\n"
                "2️⃣ Или просто: <code>/send ваш текст</code>"
            )
            return
        text = re.sub(r"^/send(?:@\w+)?\s*", "", message.html_text, count=1)
    ids = await db.all_user_ids()
    status = await message.answer(f"📨 <b>Рассылка запущена…</b>\n👥 Получателей: {len(ids)}")
    spawn(broadcast(bot, src, text, ids, status, message.from_user.id))


# ════════════════════════════════════════════════════════════════════════
#  👑  АДМИН: /panel
# ════════════════════════════════════════════════════════════════════════
def panel_text() -> str:
    st = "🔴 <b>ВКЛЮЧЕНЫ</b> — бот доступен только админам" if STATE["maint"] else "🟢 выключены"
    return (
        f"👑 <b>АДМИН-ПАНЕЛЬ</b>\n{LINE}\n"
        f"🛠 Тех. работы: {st}\n{LINE}\n"
        "👇 Выберите раздел"
    )


def panel_kb() -> InlineKeyboardMarkup:
    m = STATE["maint"]
    return kb(
        [B("👤 Управление пользователями", "adm:users", style="primary")],
        [B("🎟 Промокоды", "adm:promo"), B("📋 Задания", "adm:tasks")],
        [B("📢 Спонсоры", "adm:sponsors"), B("📊 Статистика", "adm:stats")],
        [
            B(
                "🔴 Тех. работы: ВКЛ" if m else "🟢 Тех. работы: ВЫКЛ",
                "adm:maint",
                style="danger" if m else "success",
            )
        ],
        [B("✖️ Закрыть", "adm:close")],
    )


ADM_BACK = [B("⬅️ В панель", "adm:home")]


@r_admin.message(Command("panel"))
async def cmd_panel(message: Message, state: FSMContext):
    await state.clear()
    PENDING_REPLY.pop(message.from_user.id, None)
    await message.answer(panel_text(), reply_markup=panel_kb())


@r_admin.callback_query(F.data == "adm:home")
async def adm_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call.message, panel_text(), panel_kb())
    await call.answer()


@r_admin.callback_query(F.data == "adm:close")
async def adm_close(call: CallbackQuery, state: FSMContext):
    await state.clear()
    with suppress(Exception):
        await call.message.delete()
    await call.answer()


@r_admin.callback_query(F.data == "adm:maint")
async def adm_maint(call: CallbackQuery):
    STATE["maint"] = not STATE["maint"]
    await db.set_setting("maint", "1" if STATE["maint"] else "0")
    await safe_edit(call.message, panel_text(), panel_kb())
    await call.answer(
        "🛠 Режим тех. работ ВКЛЮЧЁН" if STATE["maint"] else "✅ Режим тех. работ выключен",
        show_alert=True,
    )


# ── 📊 статистика ──
@r_admin.callback_query(F.data == "adm:stats")
async def adm_stats(call: CallbackQuery):
    s = await db.stats()
    text = (
        f"📊 <b>СТАТИСТИКА БОТА</b>\n{LINE}\n"
        f"👥 Пользователей за всё время: <b>{s['total']}</b>\n"
        f"🆕 За 24 часа: <b>{s['d1']}</b>\n"
        f"📅 За 7 дней: <b>{s['d7']}</b>\n"
        f"🚫 В бане: <b>{s['banned']}</b>\n"
        f"💀 Заблокировали бота: <b>{s['blocked']}</b>\n{LINE}\n"
        f"💎 Сумма балансов: <b>{fmt(s['bal'])} ⭐</b>\n"
        f"💳 Пополнений: <b>{s['dep_c']}</b> на <b>{fmt(s['dep_s'])} ⭐</b>\n"
        f"💸 Выплачено: <b>{s['paid_c']}</b> на <b>{fmt(s['paid_s'])} ⭐</b>\n"
        f"⏳ Ждут выплаты: <b>{s['pend_c']}</b> на <b>{fmt(s['pend_s'])} ⭐</b>\n"
        f"🎮 Сыграно игр: <b>{s['games']}</b>\n"
        f"🤝 Приглашено друзей: <b>{s['refs']}</b>"
    )
    await safe_edit(
        call.message, text, kb([B("🔄 Обновить", "adm:stats")], ADM_BACK)
    )
    await call.answer()


# ── 👤 пользователи ──
def status_of(row) -> str:
    if row["banned"]:
        return "🚫 Забанен навсегда"
    if row["ban_until"] > time.time():
        return f"⏳ Бан до {ts(row['ban_until'])}"
    return "✅ Активен"


def profile_text(row) -> str:
    uname = f"@{esc(row['username'])}" if row["username"] else "—"
    return (
        f"👤 <b>ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ</b>\n{LINE}\n"
        f"🆔 ID: <code>{row['user_id']}</code>\n"
        f"👤 Имя: <b>{esc(row['first_name'])}</b>\n"
        f"🔗 Юзернейм: {uname}\n{LINE}\n"
        f"💎 Баланс: <b>{fmt(row['balance'])} ⭐</b>\n"
        f"💳 Пополнил: {fmt(row['total_deposit'])} ⭐\n"
        f"💸 Выведено: {fmt(row['total_withdraw'])} ⭐\n"
        f"👥 Рефералов: {row['ref_count']}\n"
        f"🎮 Игр сыграно: {row['games']}\n{LINE}\n"
        f"📅 Регистрация: {ts(row['joined_at'])}\n"
        f"🚦 Статус: {status_of(row)}"
    )


def profile_kb(row) -> InlineKeyboardMarkup:
    uid = row["user_id"]
    ban_btn = (
        B("✅ Разбанить", f"au:unban:{uid}", style="success")
        if is_banned(row)
        else B("🚫 Забанить", f"au:ban:{uid}", style="danger")
    )
    return kb(
        [
            B("➕ Выдать ⭐", f"au:give:{uid}", style="success"),
            B("➖ Забрать ⭐", f"au:take:{uid}", style="danger"),
        ],
        [ban_btn],
        [B("⏳ Бан на время", f"au:tban:{uid}")],
        [B("🔄 Обновить", f"au:view:{uid}"), B("🔍 Другой юзер", "adm:users")],
        ADM_BACK,
    )


@r_admin.callback_query(F.data == "adm:users")
async def adm_users(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdmSt.find)
    await safe_edit(
        call.message,
        "👤 <b>УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ</b>\n"
        f"{LINE}\n"
        "Отправьте <b>@username</b> или <b>ID</b> пользователя 👇",
        kb(ADM_BACK),
    )
    await call.answer()


@r_admin.message(AdmSt.find, F.text)
async def adm_find(message: Message, state: FSMContext):
    row = await db.find_user(message.text)
    if not row:
        await message.answer(
            "❌ Пользователь не найден в базе.\n"
            "Он должен был хотя бы раз запустить бота. Попробуйте ещё раз:",
            reply_markup=kb(ADM_BACK),
        )
        return
    await state.clear()
    await message.answer(profile_text(row), reply_markup=profile_kb(row))


@r_admin.callback_query(F.data.startswith("au:"))
async def au_action(call: CallbackQuery, state: FSMContext, bot: Bot):
    parts = call.data.split(":")
    action, uid = parts[1], int(parts[2])
    row = await db.user(uid)
    if not row:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    if action in ("ban", "unban", "tb") and uid in ADMINS:
        await call.answer("⛔ Администратора забанить нельзя", show_alert=True)
        return

    if action == "ban":
        await db.set_ban(uid, 1, 0)
        with suppress(Exception):
            await bot.send_message(uid, "🚫 <b>Вы заблокированы администрацией.</b>")
        await call.answer("🚫 Пользователь забанен")
    elif action == "unban":
        await db.set_ban(uid, 0, 0)
        with suppress(Exception):
            await bot.send_message(
                uid, "✅ <b>Вы разблокированы!</b> Добро пожаловать обратно 💙",
                reply_markup=kb([B("🏠 В меню", "menu")]),
            )
        await call.answer("✅ Пользователь разбанен")
    elif action == "tb":
        until = int(time.time()) + int(parts[3])
        await db.set_ban(uid, 0, until)
        with suppress(Exception):
            await bot.send_message(
                uid, f"⏳ <b>Вы временно заблокированы</b>\nДо: {ts(until)}"
            )
        await call.answer("⏳ Временный бан выдан")
    elif action == "tban":
        await state.set_state(AdmSt.tban)
        await state.update_data(uid=uid)
        await safe_edit(
            call.message,
            f"⏳ <b>БАН НА ВРЕМЯ</b> · <code>{uid}</code>\n{LINE}\n"
            "Выберите срок или введите свой:\n"
            "<code>30м</code> · <code>12ч</code> · <code>7д</code> (без буквы = часы)",
            kb(
                [
                    B("1 час", f"au:tb:{uid}:3600"),
                    B("1 день", f"au:tb:{uid}:86400"),
                ],
                [
                    B("7 дней", f"au:tb:{uid}:604800"),
                    B("30 дней", f"au:tb:{uid}:2592000"),
                ],
                [B("⬅️ Назад", f"au:view:{uid}")],
            ),
        )
        await call.answer()
        return
    elif action in ("give", "take"):
        await state.set_state(AdmSt.give if action == "give" else AdmSt.take)
        await state.update_data(uid=uid)
        word = "выдать" if action == "give" else "забрать"
        await safe_edit(
            call.message,
            f"{'➕' if action == 'give' else '➖'} <b>Сколько ⭐ {word}?</b>\n"
            f"{LINE}\nПользователь: <code>{uid}</code>\n"
            f"Текущий баланс: <b>{fmt(row['balance'])} ⭐</b>\n\nВведите число 👇",
            kb([B("⬅️ Назад", f"au:view:{uid}")]),
        )
        await call.answer()
        return
    else:  # view
        await call.answer()

    await state.clear()
    row = await db.user(uid)
    await safe_edit(call.message, profile_text(row), profile_kb(row))


@r_admin.message(AdmSt.give, F.text)
async def adm_give(message: Message, state: FSMContext, bot: Bot):
    amount = parse_amount(message.text)
    if amount is None or amount <= 0:
        await message.answer("❌ Введите положительное число, например: <code>25</code>")
        return
    uid = (await state.get_data())["uid"]
    await db.add(uid, amount)
    await state.clear()
    with suppress(Exception):
        await bot.send_message(
            uid, f"🎁 <b>Администратор начислил вам {fmt(amount)} ⭐</b>",
            reply_markup=kb([B("🏠 В меню", "menu")]),
        )
    row = await db.user(uid)
    await message.answer(
        f"✅ Выдано <b>{fmt(amount)} ⭐</b>\n\n{profile_text(row)}", reply_markup=profile_kb(row)
    )


@r_admin.message(AdmSt.take, F.text)
async def adm_take(message: Message, state: FSMContext):
    amount = parse_amount(message.text)
    if amount is None or amount <= 0:
        await message.answer("❌ Введите положительное число, например: <code>25</code>")
        return
    uid = (await state.get_data())["uid"]
    await db.admin_take(uid, amount)
    await state.clear()
    row = await db.user(uid)
    await message.answer(
        f"✅ Списано до <b>{fmt(amount)} ⭐</b>\n\n{profile_text(row)}",
        reply_markup=profile_kb(row),
    )


@r_admin.message(AdmSt.tban, F.text)
async def adm_tban(message: Message, state: FSMContext, bot: Bot):
    secs = parse_duration(message.text)
    if not secs:
        await message.answer("❌ Не понял срок. Примеры: <code>30м</code>, <code>12ч</code>, <code>7д</code>")
        return
    uid = (await state.get_data())["uid"]
    if uid in ADMINS:
        await message.answer("⛔ Администратора забанить нельзя")
        await state.clear()
        return
    until = int(time.time()) + secs
    await db.set_ban(uid, 0, until)
    await state.clear()
    with suppress(Exception):
        await bot.send_message(uid, f"⏳ <b>Вы временно заблокированы</b>\nДо: {ts(until)}")
    row = await db.user(uid)
    await message.answer(
        f"✅ Бан выдан до {ts(until)}\n\n{profile_text(row)}", reply_markup=profile_kb(row)
    )


# ── 🎟 промокоды (админ) ──
@r_admin.callback_query(F.data == "adm:promo")
async def adm_promo(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(
        call.message,
        f"🎟 <b>ПРОМОКОДЫ</b>\n{LINE}\nСоздавайте коды с наградой и лимитом активаций.",
        kb(
            [B("➕ Создать промокод", "ap:new", style="success")],
            [B("📋 Список / удалить", "ap:list")],
            ADM_BACK,
        ),
    )
    await call.answer()


@r_admin.callback_query(F.data == "ap:new")
async def ap_new(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdmSt.promo)
    await safe_edit(
        call.message,
        f"➕ <b>НОВЫЙ ПРОМОКОД</b>\n{LINE}\n"
        "Отправьте одной строкой:\n"
        "<code>НАЗВАНИЕ  ЗВЁЗДЫ  АКТИВАЦИИ</code>\n\n"
        "Пример: <code>SUMMER 10 100</code>\n"
        "<i>(код SUMMER даст по 10 ⭐ первым 100 пользователям)</i>",
        kb([B("⬅️ Назад", "adm:promo")]),
    )
    await call.answer()


@r_admin.message(AdmSt.promo, F.text)
async def ap_create(message: Message, state: FSMContext):
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("❌ Нужно 3 значения: <code>НАЗВАНИЕ ЗВЁЗДЫ АКТИВАЦИИ</code>")
        return
    code = parts[0].upper()
    reward = parse_amount(parts[1])
    try:
        uses = int(parts[2])
    except ValueError:
        uses = 0
    if not re.fullmatch(r"[A-Z0-9_\-]{3,20}", code):
        await message.answer("❌ Название: 3–20 символов, только латиница, цифры, _ и -")
        return
    if reward is None or reward <= 0 or uses < 1:
        await message.answer("❌ Звёзды и активации должны быть положительными числами")
        return
    if not await db.create_promo(code, reward, uses):
        await message.answer("❌ Такой промокод уже существует. Придумайте другое название:")
        return
    await state.clear()
    await message.answer(
        f"✅ <b>Промокод создан!</b>\n{LINE}\n"
        f"🎟 Код: <code>{code}</code>\n"
        f"⭐ Награда: <b>{fmt(reward)}</b>\n"
        f"🔢 Активаций: <b>{uses}</b>",
        reply_markup=kb([B("➕ Ещё один", "ap:new")], ADM_BACK),
    )


@r_admin.callback_query(F.data == "ap:list")
async def ap_list(call: CallbackQuery):
    items = await db.promos()
    if not items:
        text = f"🎟 <b>ПРОМОКОДЫ</b>\n{LINE}\nПока нет ни одного промокода."
        markup = kb([B("➕ Создать", "ap:new", style="success")], [B("⬅️ Назад", "adm:promo")])
    else:
        lines = [
            f"🎟 <code>{p['code']}</code> · {fmt(p['reward'])} ⭐ · {p['used']}/{p['max_uses']}"
            for p in items[:30]
        ]
        text = f"🎟 <b>ПРОМОКОДЫ</b>\n{LINE}\n" + "\n".join(lines)
        rows = [[B(f"🗑 {p['code']}", f"ap:del:{p['code']}", style="danger")] for p in items[:30]]
        rows.append([B("⬅️ Назад", "adm:promo")])
        markup = kb(*rows)
    await safe_edit(call.message, text, markup)
    await call.answer()


@r_admin.callback_query(F.data.startswith("ap:del:"))
async def ap_del(call: CallbackQuery):
    await db.del_promo(call.data.split(":", 2)[2])
    await call.answer("🗑 Промокод удалён")
    await ap_list(call)


# ── 📋 задания (админ) ──
@r_admin.callback_query(F.data == "adm:tasks")
async def adm_tasks(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(
        call.message,
        f"📋 <b>ЗАДАНИЯ</b>\n{LINE}\nДобавляйте задания: ссылка + награда за выполнение.",
        kb(
            [B("➕ Добавить задание", "at:new", style="success")],
            [B("📋 Список / удалить", "at:list")],
            ADM_BACK,
        ),
    )
    await call.answer()


@r_admin.callback_query(F.data == "at:new")
async def at_new(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdmSt.task)
    await safe_edit(
        call.message,
        f"➕ <b>НОВОЕ ЗАДАНИЕ</b>\n{LINE}\n"
        "Отправьте одной строкой:\n"
        "<code>ССЫЛКА  НАГРАДА</code>\n\n"
        "Пример: <code>https://t.me/mychannel 5</code>\n\n"
        "🔎 Если это <b>публичный канал/группа</b> и бот там админ — подписка будет "
        "проверяться автоматически.\n"
        "🔒 Для приватного канала: <code>-100123456789 https://t.me/+abc 5</code> "
        "(ID чата, ссылка, награда).\n"
        "⚠️ Для остальных ссылок проверки нет — награда выдаётся по нажатию кнопки.",
        kb([B("⬅️ Назад", "adm:tasks")]),
    )
    await call.answer()


@r_admin.message(AdmSt.task, F.text)
async def at_create(message: Message, state: FSMContext, bot: Bot):
    tokens = message.text.split()
    if len(tokens) < 2:
        await message.answer("❌ Формат: <code>ССЫЛКА НАГРАДА</code>")
        return
    reward = parse_amount(tokens[-1])
    if reward is None or reward <= 0:
        await message.answer("❌ Награда должна быть положительным числом")
        return
    rest = tokens[:-1]
    chat, url = None, None
    if len(rest) == 2 and re.fullmatch(r"-?\d+", rest[0]) and re.match(r"https?://", rest[1]):
        chat, url = rest[0], rest[1]
    elif len(rest) == 1:
        url, chat = parse_link(rest[0])
    if not url:
        await message.answer("❌ Не понял ссылку. Она должна начинаться с <code>https://</code> или быть @username")
        return

    verified, title = False, "Выполнить задание"
    if chat:
        ctitle, is_admin, cid = await chat_info(bot, chat)
        if ctitle:
            title = f"Подписаться: {ctitle}"
        if is_admin and cid is not None:
            chat, verified = str(cid), True
        else:
            chat = None
    tid = await db.add_task(title, url, chat, reward)
    await state.clear()
    note = (
        "✅ Автопроверка подписки <b>включена</b>"
        if verified
        else "⚠️ Автопроверки <b>нет</b> (бот не админ в чате или это не чат) — награда по нажатию"
    )
    await message.answer(
        f"✅ <b>Задание #{tid} добавлено!</b>\n{LINE}\n"
        f"📝 {esc(title)}\n🔗 {esc(url)}\n⭐ Награда: <b>{fmt(reward)}</b>\n\n{note}",
        reply_markup=kb([B("➕ Ещё одно", "at:new")], ADM_BACK),
    )


@r_admin.callback_query(F.data == "at:list")
async def at_list(call: CallbackQuery):
    items = await db.tasks()
    if not items:
        text = f"📋 <b>ЗАДАНИЯ</b>\n{LINE}\nПока нет ни одного задания."
        markup = kb([B("➕ Добавить", "at:new", style="success")], [B("⬅️ Назад", "adm:tasks")])
    else:
        lines = [
            f"<b>#{t['id']}</b> · {esc(t['title'])[:40]} · {fmt(t['reward'])} ⭐ · "
            f"{'✅ проверка' if t['chat'] else '⚠️ без проверки'}"
            for t in items[:30]
        ]
        text = f"📋 <b>ЗАДАНИЯ</b>\n{LINE}\n" + "\n".join(lines)
        rows = [[B(f"🗑 Удалить #{t['id']}", f"at:del:{t['id']}", style="danger")] for t in items[:30]]
        rows.append([B("⬅️ Назад", "adm:tasks")])
        markup = kb(*rows)
    await safe_edit(call.message, text, markup)
    await call.answer()


@r_admin.callback_query(F.data.startswith("at:del:"))
async def at_del(call: CallbackQuery):
    await db.del_task(int(call.data.split(":")[2]))
    await call.answer("🗑 Задание удалено")
    await at_list(call)


# ── 📢 спонсоры (админ) ──
@r_admin.callback_query(F.data == "adm:sponsors")
async def adm_sponsors(call: CallbackQuery, state: FSMContext):
    await state.clear()
    n = len(await db.sponsors())
    who = "кроме админов" if ADMINS_SKIP_GATE else "включая админов"
    await safe_edit(
        call.message,
        f"📢 <b>СПОНСОРЫ</b>\n{LINE}\n"
        "Обязательная подписка: пока человек не подпишется на всех спонсоров, "
        "бот ему не откроется, а приглашённый друг не засчитается рефереру.\n\n"
        f"📌 Спонсоров сейчас: <b>{n}</b>\n"
        f"👥 Проверка действует на всех ({who})",
        kb(
            [B("➕ Добавить спонсора", "as:new", style="success")],
            [B("📋 Список / проверка", "as:list")],
            [B("👁 Как это видят пользователи", "as:preview")],
            ADM_BACK,
        ),
    )
    await call.answer()


@r_admin.callback_query(F.data == "as:new")
async def as_new(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdmSt.sponsor)
    await safe_edit(
        call.message,
        f"➕ <b>НОВЫЙ СПОНСОР</b>\n{LINE}\n"
        "1️⃣ Сделайте бота <b>администратором</b> канала/группы.\n"
        "2️⃣ Отправьте ссылку или @username:\n"
        "<code>https://t.me/mychannel</code>\n\n"
        "🔒 Для приватного канала — ID чата и ссылку-приглашение:\n"
        "<code>-100123456789 https://t.me/+abc</code>",
        kb([B("⬅️ Назад", "adm:sponsors")]),
    )
    await call.answer()


@r_admin.message(AdmSt.sponsor, F.text)
async def as_create(message: Message, state: FSMContext, bot: Bot):
    tokens = message.text.split()
    chat, url = None, None
    if len(tokens) == 2 and re.fullmatch(r"-?\d+", tokens[0]) and re.match(r"https?://", tokens[1]):
        chat, url = tokens[0], tokens[1]
    elif len(tokens) == 1:
        url, chat = parse_link(tokens[0])
    if not url or not chat:
        await message.answer(
            "❌ Не понял. Отправьте <code>@username</code>, ссылку <code>https://t.me/name</code> "
            "или <code>-100… https://t.me/+abc</code>"
        )
        return
    title, is_admin, cid = await chat_info(bot, chat)
    if not title:
        await message.answer("❌ Бот не видит этот чат. Добавьте бота в канал/группу и повторите.")
        return
    if not is_admin:
        await message.answer(
            f"⚠️ Бот не администратор в «{esc(title)}» — подписку проверить не получится.\n"
            "Сделайте его админом и отправьте ссылку ещё раз."
        )
        return
    if any(s["chat"] == str(cid) for s in await db.sponsors()):
        await message.answer(f"⚠️ «{esc(title)}» уже есть в списке спонсоров.")
        return
    await db.add_sponsor(title, url, str(cid))
    SUB_CACHE.clear()  # у всех проверка начнётся заново
    await state.clear()
    await message.answer(
        f"✅ <b>Спонсор добавлен!</b>\n{LINE}\n📢 {esc(title)}\n🔗 {esc(url)}\n\n"
        "Теперь окно подписки появится у всех, кто ещё не подписан.",
        reply_markup=kb(
            [B("👁 Предпросмотр", "as:preview")],
            [B("➕ Ещё один", "as:new")],
            ADM_BACK,
        ),
    )


@r_admin.callback_query(F.data == "as:list")
async def as_list(call: CallbackQuery, bot: Bot):
    items = await db.sponsors()
    if not items:
        text = f"📢 <b>СПОНСОРЫ</b>\n{LINE}\nСпонсоров пока нет."
        markup = kb([B("➕ Добавить", "as:new", style="success")], [B("⬅️ Назад", "adm:sponsors")])
    else:
        lines = []
        for s in items:
            title, is_admin, cid = await chat_info(bot, s["chat"])
            if title is None:
                state = "❌ бот не видит этот чат"
            elif not is_admin:
                state = "⚠️ бот не админ — проверка не работает"
            else:
                state = "✅ проверка работает"
                with suppress(Exception):
                    state += f" · 👥 {await bot.get_chat_member_count(cid)}"
            lines.append(f"<b>#{s['id']}</b> · {esc(s['title'])}\n      {state}")
        text = f"📢 <b>СПОНСОРЫ</b>\n{LINE}\n" + "\n".join(lines)
        rows = [[B(f"🗑 Удалить #{s['id']}", f"as:del:{s['id']}", style="danger")] for s in items]
        rows.append([B("🔄 Проверить заново", "as:list"), B("⬅️ Назад", "adm:sponsors")])
        markup = kb(*rows)
    await safe_edit(call.message, text, markup)
    with suppress(Exception):
        await call.answer()


@r_admin.callback_query(F.data.startswith("as:del:"))
async def as_del(call: CallbackQuery, bot: Bot):
    await db.del_sponsor(int(call.data.split(":")[2]))
    SUB_CACHE.clear()
    with suppress(Exception):
        await call.answer("🗑 Спонсор удалён")
    await as_list(call, bot)


@r_admin.callback_query(F.data == "as:preview")
async def as_preview(call: CallbackQuery):
    items = await db.sponsors()
    if not items:
        await call.answer("Спонсоров пока нет — окно подписки не показывается.", show_alert=True)
        return
    text, rows = gate_content([(s, False) for s in items])
    rows.append([B("⬅️ Назад", "adm:sponsors")])
    await safe_edit(call.message, f"👁 <b>ПРЕДПРОСМОТР</b> · так видят пользователи\n\n{text}", kb(*rows))
    await call.answer()


# ════════════════════════════════════════════════════════════════════════
#  🙋  ПОЛЬЗОВАТЕЛЬ: старт, меню, отмена
# ════════════════════════════════════════════════════════════════════════
@r_user.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await render_menu(message, message.from_user.id)


@r_user.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    await render_menu(message, message.from_user.id)


@r_user.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    PENDING_REPLY.pop(message.from_user.id, None)
    await message.answer("✖️ Действие отменено.")
    await render_menu(message, message.from_user.id)


@r_user.message(Command("panel", "send"))
async def no_access(message: Message):
    await message.answer(
        "⛔ <b>Доступ запрещён</b>\n\nЭта команда доступна только администраторам."
    )


@r_user.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await render_menu(call.message, call.from_user.id, edit=True)
    await call.answer()


@r_user.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery, bot: Bot, state: FSMContext):
    SUB_CACHE.pop(call.from_user.id, None)
    missing, statuses = await check_sponsors(bot, call.from_user.id)
    if missing:
        text, rows = gate_content(statuses)
        rows.append([B("✅ Проверить подписку", "check_sub", style="success")])
        if call.message:
            await safe_edit(call.message, text, kb(*rows))
        await call.answer("❌ Ты подписался не на всех спонсоров — проверь список", show_alert=True)
        return
    await state.clear()
    await finalize_access(bot, call.from_user)  # засчитываем реферала
    await call.answer("✅ Спасибо за подписку!")
    await render_menu(call.message, call.from_user.id, edit=True)


# ════════════════════════════════════════════════════════════════════════
#  🎁  БЕСПЛАТНО (рефералы)
# ════════════════════════════════════════════════════════════════════════
@r_user.callback_query(F.data == "free")
async def cb_free(call: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = call.from_user.id
    row = await db.user(uid)
    pending = await db.ref_pending(uid)
    has_sponsors = bool(await db.sponsors())
    link = f"https://t.me/{BOT_INFO['username']}?start=ref_{uid}"
    share = (
        "https://t.me/share/url?url="
        + quote(link)
        + "&text="
        + quote("🎁 Заходи, играй и выводи ⭐ Telegram Stars!")
    )
    rule = (
        "Друг засчитывается, когда запустит бота и <b>подпишется на спонсоров</b>."
        if has_sponsors
        else "Друг засчитывается, когда запустит бота по твоей ссылке."
    )
    text = (
        f"🎁 <b>БЕСПЛАТНЫЕ ЗВЁЗДЫ</b>\n{LINE}\n"
        f"Приглашай друзей и получай <b>+{REF_REWARD} ⭐</b> за каждого!\n"
        f"<i>{rule}</i>\n\n"
        "🔗 <b>Твоя ссылка</b> (нажми, чтобы скопировать):\n"
        f"<code>{link}</code>\n{LINE}\n"
        f"👥 Засчитано друзей: <b>{row['ref_count']}</b>\n"
        + (f"⏳ Ждут подписки: <b>{pending}</b>\n" if pending else "")
        + f"💰 Заработано: <b>{fmt(row['ref_count'] * REF_REWARD)} ⭐</b>"
    )
    await safe_edit(
        call.message,
        text,
        kb([B("📤 Поделиться ссылкой", url=share, style="success")], [B("⬅️ Назад", "menu")]),
    )
    await call.answer()


# ════════════════════════════════════════════════════════════════════════
#  🎟  ПРОМОКОД
# ════════════════════════════════════════════════════════════════════════
async def apply_promo(message: Message, code: str):
    status, reward = await db.use_promo(message.from_user.id, code.strip().upper())
    row = await db.user(message.from_user.id)
    if status == "ok":
        text = (
            f"🎉 <b>Промокод активирован!</b>\n{LINE}\n"
            f"💰 Начислено: <b>+{fmt(reward)} ⭐</b>\n"
            f"💎 Баланс: <b>{fmt(row['balance'])} ⭐</b>"
        )
    elif status == "used":
        text = "⚠️ Вы уже активировали этот промокод."
    elif status == "over":
        text = "😔 Лимит активаций этого промокода исчерпан."
    else:
        text = "❌ Такого промокода не существует."
    await message.answer(text, reply_markup=kb([B("🏠 В меню", "menu")]))


@r_user.callback_query(F.data == "promo")
async def cb_promo(call: CallbackQuery, state: FSMContext):
    await state.set_state(PromoSt.code)
    await safe_edit(
        call.message,
        f"🎟 <b>ПРОМОКОД</b>\n{LINE}\nОтправьте промокод сообщением 👇",
        back_kb(),
    )
    await call.answer()


@r_user.message(PromoSt.code, F.text)
async def promo_input(message: Message, state: FSMContext):
    await state.clear()
    await apply_promo(message, message.text)


@r_user.message(Command("promo"))
async def cmd_promo(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Использование: <code>/promo КОД</code>")
        return
    await apply_promo(message, command.args)


# ════════════════════════════════════════════════════════════════════════
#  ⭐  ЗАДАНИЯ
# ════════════════════════════════════════════════════════════════════════
async def tasks_view(uid: int):
    items = await db.tasks_for(uid)
    if not items:
        return (
            f"⭐ <b>ЗАДАНИЯ</b>\n{LINE}\n"
            "🎉 Все задания выполнены! Загляните позже — скоро появятся новые.",
            back_kb(),
        )
    text = (
        f"⭐ <b>ЗАДАНИЯ</b>\n{LINE}\n"
        "Выполняй задания и получай звёзды 👇"
    )
    rows = [
        [B(f"⭐ +{fmt(t['reward'])} · {t['title']}"[:60], f"task:{t['id']}")]
        for t in items[:15]
    ]
    rows.append([B("⬅️ Назад", "menu")])
    return text, kb(*rows)


@r_user.callback_query(F.data == "tasks")
async def cb_tasks(call: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = await tasks_view(call.from_user.id)
    await safe_edit(call.message, text, markup)
    await call.answer()


@r_user.callback_query(F.data.startswith("task:"))
async def cb_task_open(call: CallbackQuery):
    t = await db.task(int(call.data.split(":")[1]))
    if not t:
        await call.answer("Задание больше недоступно", show_alert=True)
        return
    text = (
        f"📝 <b>{esc(t['title'])}</b>\n{LINE}\n"
        f"💰 Награда: <b>+{fmt(t['reward'])} ⭐</b>\n\n"
        "1️⃣ Нажмите «🔗 Перейти» и выполните задание\n"
        "2️⃣ Вернитесь и нажмите «✅ Проверить»"
    )
    await safe_edit(
        call.message,
        text,
        kb(
            [B("🔗 Перейти", url=t["url"], style="primary")],
            [B("✅ Проверить", f"taskchk:{t['id']}", style="success")],
            [B("⬅️ Назад", "tasks")],
        ),
    )
    await call.answer()


@r_user.callback_query(F.data.startswith("taskchk:"))
async def cb_task_check(call: CallbackQuery, bot: Bot):
    tid = int(call.data.split(":")[1])
    t = await db.task(tid)
    if not t:
        await call.answer("Задание больше недоступно", show_alert=True)
        return
    if t["chat"]:
        res = await is_member(bot, t["chat"], call.from_user.id)
        if res is None:
            await call.answer("⚠️ Проверка временно недоступна. Попробуйте позже.", show_alert=True)
            return
        if res is False:
            await call.answer("❌ Вы ещё не выполнили задание", show_alert=True)
            return
    reward = await db.complete_task(call.from_user.id, tid)
    if reward is None:
        await call.answer("Вы уже получили награду за это задание", show_alert=True)
    else:
        await call.answer(f"✅ Задание выполнено! +{fmt(reward)} ⭐", show_alert=True)
    text, markup = await tasks_view(call.from_user.id)
    await safe_edit(call.message, text, markup)


# ════════════════════════════════════════════════════════════════════════
#  💳  ПОПОЛНЕНИЕ (Telegram Stars)
# ════════════════════════════════════════════════════════════════════════
def deposit_text() -> str:
    return (
        f"💳 <b>ПОПОЛНЕНИЕ БАЛАНСА</b>\n{LINE}\n"
        "Оплата проходит <b>реальными ⭐ Telegram Stars</b>.\n"
        "Курс: <b>1 ⭐ = 1 ⭐</b> на балансе.\n\n"
        f"✏️ Введите количество звёзд сообщением или выберите ниже 👇\n"
        f"<i>От {MIN_DEPOSIT} до {fmt(MAX_DEPOSIT)} ⭐</i>"
    )


def deposit_kb() -> InlineKeyboardMarkup:
    q = [B(f"⭐ {a}", f"dep:{a}") for a in QUICK_DEPOSITS]
    return kb(q[:3], q[3:], [B("⬅️ Назад", "menu")])


async def send_invoice(bot: Bot, chat_id: int, amount: int):
    await bot.send_invoice(
        chat_id=chat_id,
        title=f"Пополнение на {amount} ⭐",
        description="Пополнение игрового баланса. Оплата реальными Telegram Stars ⭐",
        payload=f"deposit_{chat_id}_{amount}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{amount} ⭐", amount=amount)],
    )


@r_user.callback_query(F.data == "deposit")
async def cb_deposit(call: CallbackQuery, state: FSMContext):
    await state.set_state(DepSt.amount)
    await safe_edit(call.message, deposit_text(), deposit_kb())
    await call.answer()


@r_user.callback_query(F.data.startswith("dep:"))
async def cb_deposit_quick(call: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    await call.answer()
    await send_invoice(bot, call.message.chat.id, int(call.data.split(":")[1]))


@r_user.message(DepSt.amount, F.text)
async def deposit_input(message: Message, bot: Bot, state: FSMContext):
    try:
        amount = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите целое число, например: <code>50</code>")
        return
    if amount < MIN_DEPOSIT or amount > MAX_DEPOSIT:
        await message.answer(f"❌ Сумма должна быть от {MIN_DEPOSIT} до {fmt(MAX_DEPOSIT)} ⭐")
        return
    await state.clear()
    await send_invoice(bot, message.chat.id, amount)


@r_user.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    row = await db.user(q.from_user.id)
    if row and is_banned(row) and q.from_user.id not in ADMINS:
        await q.answer(ok=False, error_message="Аккаунт заблокирован")
        return
    await q.answer(ok=True)


@r_user.message(F.successful_payment)
async def on_paid(message: Message):
    p = message.successful_payment
    await db.record_payment(message.from_user.id, p.total_amount, p.telegram_payment_charge_id)
    row = await db.user(message.from_user.id)
    log.info("Deposit %s XTR from %s", p.total_amount, message.from_user.id)
    await message.answer(
        f"✅ <b>ОПЛАТА ПРОШЛА УСПЕШНО!</b>\n{LINE}\n"
        f"➕ Зачислено: <b>{p.total_amount} ⭐</b>\n"
        f"💎 Баланс: <b>{fmt(row['balance'])} ⭐</b>",
        reply_markup=kb(
            [B("🎮 Играть", "games", style="success")], [B("🏠 В меню", "menu")]
        ),
    )


# ════════════════════════════════════════════════════════════════════════
#  💸  ВЫВОД
# ════════════════════════════════════════════════════════════════════════
@r_user.callback_query(F.data == "withdraw")
async def cb_withdraw(call: CallbackQuery, state: FSMContext):
    row = await db.user(call.from_user.id)
    bal = row["balance"]
    if bal < MIN_WITHDRAW:
        await call.answer(
            f"❌ Минимум для вывода — {MIN_WITHDRAW} ⭐\nВаш баланс: {fmt(bal)} ⭐",
            show_alert=True,
        )
        return
    await state.set_state(WdSt.amount)
    await safe_edit(
        call.message,
        f"💸 <b>ВЫВОД СРЕДСТВ</b>\n{LINE}\n"
        f"💎 Баланс: <b>{fmt(bal)} ⭐</b>\n"
        f"📉 Минимум: <b>{MIN_WITHDRAW} ⭐</b>\n\n"
        "✏️ Введите сумму вывода (целое число) 👇",
        kb(
            [B(f"💰 Вывести всё ({int(math.floor(bal))} ⭐)", "wd_all", style="success")],
            [B("⬅️ Назад", "menu")],
        ),
    )
    await call.answer()


async def wd_confirm_screen(msg: Message, uid: int, amount: int, edit: bool):
    row = await db.user(uid)
    text = (
        f"💸 <b>ПОДТВЕРЖДЕНИЕ ВЫВОДА</b>\n{LINE}\n"
        f"💰 Сумма: <b>{amount} ⭐</b>\n"
        f"💎 Останется: <b>{fmt(row['balance'] - amount)} ⭐</b>\n{LINE}\n"
        "Заявка уйдёт на проверку. Сумма спишется с баланса сразу, "
        "а при отказе вернётся обратно."
    )
    markup = kb(
        [B("✅ Подтвердить", f"wd_go:{amount}", style="success")],
        [B("✖️ Отмена", "menu", style="danger")],
    )
    if edit:
        await safe_edit(msg, text, markup)
    else:
        await msg.answer(text, reply_markup=markup)


@r_user.callback_query(F.data == "wd_all")
async def cb_wd_all(call: CallbackQuery, state: FSMContext):
    row = await db.user(call.from_user.id)
    amount = int(math.floor(row["balance"]))
    if amount < MIN_WITHDRAW:
        await call.answer(f"❌ Минимум для вывода — {MIN_WITHDRAW} ⭐", show_alert=True)
        return
    await state.clear()
    await wd_confirm_screen(call.message, call.from_user.id, amount, edit=True)
    await call.answer()


@r_user.message(WdSt.amount, F.text)
async def wd_amount_input(message: Message, state: FSMContext):
    try:
        amount = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите целое число, например: <code>50</code>")
        return
    row = await db.user(message.from_user.id)
    if amount < MIN_WITHDRAW:
        await message.answer(f"❌ Минимальная сумма вывода — {MIN_WITHDRAW} ⭐")
        return
    if amount > row["balance"]:
        await message.answer(f"❌ Недостаточно средств. Баланс: {fmt(row['balance'])} ⭐")
        return
    await state.clear()
    await wd_confirm_screen(message, message.from_user.id, amount, edit=False)


@r_user.callback_query(F.data.startswith("wd_go:"))
async def cb_wd_go(call: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    uid = call.from_user.id
    amount = int(call.data.split(":")[1])
    if amount < MIN_WITHDRAW:
        await call.answer(f"❌ Минимум для вывода — {MIN_WITHDRAW} ⭐", show_alert=True)
        return
    wid = await db.create_withdrawal(uid, amount)
    if wid is None:
        await call.answer("❌ Недостаточно средств", show_alert=True)
        return
    w = await db.withdrawal(wid)
    row = await db.user(uid)
    try:
        sent = await bot.send_message(
            WITHDRAW_CHANNEL,
            wd_text(w, row),
            reply_markup=kb(
                [
                    B("✅ Выплачено", f"wdok:{wid}", style="success"),
                    B("❌ Отклонить", f"wdno:{wid}", style="danger"),
                ]
            ),
        )
        await db.run(
            "UPDATE withdrawals SET channel_msg_id=? WHERE id=?", (sent.message_id, wid)
        )
    except Exception:
        log.exception("Не удалось отправить заявку #%s в канал", wid)
        await db.resolve_withdrawal(wid, "rejected", 0)  # вернуть деньги
        for a in ADMINS:
            with suppress(Exception):
                await bot.send_message(
                    a,
                    f"⚠️ Бот не смог отправить заявку в {WITHDRAW_CHANNEL}. "
                    "Проверьте, что он админ канала с правом публикации.",
                )
        await call.answer("⚠️ Не удалось отправить заявку. Попробуйте позже.", show_alert=True)
        return
    await call.answer()
    await safe_edit(
        call.message,
        f"✅ <b>ЗАЯВКА #{wid} СОЗДАНА</b>\n{LINE}\n"
        f"💰 Сумма: <b>{amount} ⭐</b>\n"
        "⏳ Статус: <b>на рассмотрении</b>\n\n"
        "Мы пришлём уведомление, как только заявка будет обработана 💙",
        kb([B("🏠 В меню", "menu")]),
    )


# ════════════════════════════════════════════════════════════════════════
#  🛠  ПОДДЕРЖКА
# ════════════════════════════════════════════════════════════════════════
@r_user.callback_query(F.data == "support")
async def cb_support(call: CallbackQuery, state: FSMContext):
    await state.set_state(SupSt.text)
    await safe_edit(
        call.message,
        f"🛠 <b>ТЕХ ПОДДЕРЖКА</b>\n{LINE}\n"
        "Опишите ваш вопрос или проблему <b>одним сообщением</b> — "
        "мы ответим прямо здесь, в боте 💬",
        back_kb(),
    )
    await call.answer()


@r_user.message(SupSt.text, F.text)
async def support_msg(message: Message, bot: Bot, state: FSMContext):
    uid = message.from_user.id
    now = time.time()
    if now - SUPPORT_LAST.get(uid, 0) < SUPPORT_COOLDOWN:
        await message.answer("⏳ Не так быстро! Подождите несколько секунд и отправьте снова.")
        return
    row = await db.user(uid)
    uname = f"@{esc(row['username'])}" if row["username"] else "—"
    body = message.text if len(message.text) <= 3000 else message.text[:3000] + "…"
    text = (
        f"📩 <b>НОВОЕ ОБРАЩЕНИЕ</b>\n{LINE}\n"
        f"👤 {esc(row['first_name'])} · {uname}\n"
        f"🆔 <code>{uid}</code>\n"
        f"💎 Баланс: {fmt(row['balance'])} ⭐\n{LINE}\n"
        f"💬 <b>Сообщение:</b>\n<blockquote>{esc(body)}</blockquote>"
    )
    try:
        await bot.send_message(
            SUPPORT_CHANNEL,
            text,
            reply_markup=kb([B("✉️ Ответить", f"sup:{uid}", style="primary")]),
        )
    except Exception:
        log.exception("Не удалось отправить обращение в канал поддержки")
        await message.answer("⚠️ Не удалось отправить сообщение. Попробуйте позже.")
        return
    SUPPORT_LAST[uid] = now
    await state.clear()
    await message.answer(
        f"✅ <b>СООБЩЕНИЕ ОТПРАВЛЕНО</b>\n{LINE}\n"
        "Поддержка уже получила ваше обращение.\n"
        "Ответ придёт сюда, в чат с ботом 💬",
        reply_markup=kb([B("🏠 В меню", "menu")]),
    )


@r_user.message(SupSt.text)
async def support_not_text(message: Message):
    await message.answer("📝 Отправьте, пожалуйста, <b>текстовое</b> сообщение.")


# ════════════════════════════════════════════════════════════════════════
#  🎮  ИГРЫ
# ════════════════════════════════════════════════════════════════════════
DICE_ANIM = 3.5   # сек. ждём, пока докрутится анимация кубика


def games_text(balance) -> str:
    w, l = fmt(WIN_MULT), fmt(LOSE_MULT)
    return (
        f"🎮 <b>ИГРЫ СО СТАВКАМИ</b>\n{LINE}\n"
        f"🔴🔵 <b>Красное · Синее</b>\n"
        f"┗ угадал цвет — ×{w} · не угадал — ×{l}\n\n"
        f"🎲 <b>Кубик</b>\n"
        f"┗ угадал «больше / меньше» — ×{w} · не угадал — ×{l}\n{LINE}\n"
        f"💎 Баланс: <b>{fmt(balance)} ⭐</b>"
    )


def games_kb() -> InlineKeyboardMarkup:
    return kb(
        [B("🔴🔵 Красное · Синее", "game:color", style="danger")],
        [B("🎲 Кубик", "game:dice", style="primary")],
        [B("⬅️ Назад", "menu")],
    )


def bet_text(game: str, bal) -> str:
    return (
        f"{GAMES[game]['title']}\n{LINE}\n"
        f"✅ Угадал — ставка <b>×{fmt(WIN_MULT)}</b>\n"
        f"❌ Не угадал — ставка <b>×{fmt(LOSE_MULT)}</b>\n{LINE}\n"
        f"💎 Баланс: <b>{fmt(bal)} ⭐</b>\n\n"
        f"✏️ Введите ставку сообщением или выберите ниже 👇\n"
        f"<i>Минимум: {fmt(MIN_BET)} ⭐</i>"
    )


def bet_kb(game: str) -> InlineKeyboardMarkup:
    q = [B(f"{b} ⭐", f"bet:{game}:{b}") for b in QUICK_BETS]
    return kb(
        q[:3],
        q[3:],
        [B("💰 Ва-банк (весь баланс)", f"bet:{game}:all", style="danger")],
        [B("⬅️ Назад", "games")],
    )


def bet_error(bet: Optional[float], bal: float) -> Optional[str]:
    if bet is None or bet < MIN_BET:
        return f"❌ Минимальная ставка — {fmt(MIN_BET)} ⭐"
    if bet > bal:
        return f"❌ Недостаточно средств. Баланс: {fmt(bal)} ⭐"
    return None


def pick_kb(game: str, bet: float) -> InlineKeyboardMarkup:
    b = num(bet)
    if game == "color":
        first = [
            B("🔴 Красное", f"pick:color:red:{b}", style="danger"),
            B("🔵 Синее", f"pick:color:blue:{b}", style="primary"),
        ]
    else:
        first = [
            B("⬆️ Больше (4–6)", f"pick:dice:high:{b}", style="success"),
            B("⬇️ Меньше (1–3)", f"pick:dice:low:{b}", style="primary"),
        ]
    return kb(first, [B("✏️ Изменить ставку", f"game:{game}")], [B("⬅️ К играм", "games")])


def again_kb(game: str, bet: float) -> InlineKeyboardMarkup:
    return kb(
        [B(f"🔁 Ещё раз · {fmt(bet)} ⭐", f"bet:{game}:{num(bet)}", style="success")],
        [B("💰 Другая ставка", f"game:{game}"), B("🎮 Игры", "games")],
        [B("🏠 В меню", "menu")],
    )


def result_text(head: str, win: bool, bet: float, payout: float, balance) -> str:
    profit = round(payout - bet, 2)
    if win:
        status = "🎉 <b>ПОБЕДА!</b>"
        pr = f"📈 Профит: <b>+{fmt(profit)} ⭐</b>"
        mult = WIN_MULT
    else:
        status = "😔 <b>Не повезло</b>"
        pr = f"📉 Итог: <b>{fmt(profit)} ⭐</b>"
        mult = LOSE_MULT
    return (
        f"{head}\n{LINE}\n{status}\n\n"
        f"💰 Ставка: {fmt(bet)} ⭐ → ×{fmt(mult)} → <b>{fmt(payout)} ⭐</b>\n{pr}\n{LINE}\n"
        f"💎 Баланс: <b>{fmt(balance)} ⭐</b>"
    )


async def ask_pick(msg: Message, game: str, bet: float, edit: bool):
    text = (
        f"{GAMES[game]['title']}\n{LINE}\n"
        f"💰 Ставка: <b>{fmt(bet)} ⭐</b>\n"
        f"🏆 Победа: ×{fmt(WIN_MULT)} → <b>{fmt(bet * WIN_MULT)} ⭐</b>\n"
        f"💔 Поражение: ×{fmt(LOSE_MULT)} → <b>{fmt(bet * LOSE_MULT)} ⭐</b>\n{LINE}\n"
        + (
            "Выбери цвет 👇"
            if game == "color"
            else "Выпадет <b>больше</b> (4–6) или <b>меньше</b> (1–3)? 👇"
        )
    )
    if edit:
        await safe_edit(msg, text, pick_kb(game, bet))
    else:
        await msg.answer(text, reply_markup=pick_kb(game, bet))


@r_user.callback_query(F.data == "games")
async def cb_games(call: CallbackQuery, state: FSMContext):
    await state.clear()
    row = await db.user(call.from_user.id)
    await safe_edit(call.message, games_text(row["balance"]), games_kb())
    await call.answer()


@r_user.callback_query(F.data.startswith("game:"))
async def cb_game_open(call: CallbackQuery, state: FSMContext):
    game = call.data.split(":")[1]
    if game not in GAMES:  # например, старая кнопка «баскетбол»
        await call.answer("🎲 Игры обновились — открой раздел «Игры» заново", show_alert=True)
        return
    row = await db.user(call.from_user.id)
    await state.set_state(BetSt.amount)
    await state.update_data(game=game)
    await safe_edit(call.message, bet_text(game, row["balance"]), bet_kb(game))
    await call.answer()


@r_user.callback_query(F.data.startswith("bet:"))
async def cb_bet_quick(call: CallbackQuery, state: FSMContext):
    _, game, val = call.data.split(":")
    if game not in GAMES:
        await call.answer("🎲 Игры обновились — открой раздел «Игры» заново", show_alert=True)
        return
    row = await db.user(call.from_user.id)
    bal = row["balance"]
    bet = round(bal, 2) if val == "all" else parse_amount(val)
    err = bet_error(bet, bal)
    if err:
        await call.answer(err, show_alert=True)
        return
    await state.clear()
    await ask_pick(call.message, game, bet, edit=True)
    await call.answer()


@r_user.message(BetSt.amount, F.text)
async def bet_input(message: Message, state: FSMContext):
    game = (await state.get_data()).get("game")
    if game not in GAMES:
        await state.clear()
        await render_menu(message, message.from_user.id)
        return
    row = await db.user(message.from_user.id)
    bet = parse_amount(message.text)
    err = bet_error(bet, row["balance"])
    if err:
        await message.answer(err)
        return
    await state.clear()
    await ask_pick(message, game, bet, edit=False)


async def play_color(call: CallbackQuery, choice: str, bet: float):
    uid = call.from_user.id
    result = RNG.choice(("red", "blue"))
    win = result == choice
    payout = round(bet * (WIN_MULT if win else LOSE_MULT), 2)
    if not await db.settle(uid, bet, payout):
        await call.answer("❌ Недостаточно средств", show_alert=True)
        return
    await call.answer()
    for frame in ("🔴 🔵 🔴 🔵 🔴", "🔵 🔴 🔵 🔴 🔵", "🔴 🔵 🔴 🔵 🔴"):
        await safe_edit(
            call.message,
            f"🎰 <b>Крутим барабан…</b>\n{LINE}\n💰 Ставка: <b>{fmt(bet)} ⭐</b>\n\n{frame}",
        )
        await asyncio.sleep(0.7)
    row = await db.user(uid)
    head = (
        "🔴 Выпало: <b>КРАСНОЕ</b>" if result == "red" else "🔵 Выпало: <b>СИНЕЕ</b>"
    )
    await safe_edit(
        call.message,
        result_text(head, win, bet, payout, row["balance"]),
        again_kb("color", bet),
    )


async def play_dice(call: CallbackQuery, bot: Bot, choice: str, bet: float):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    if not await db.take(uid, bet):
        await call.answer("❌ Недостаточно средств", show_alert=True)
        return
    await call.answer()
    try:
        await safe_edit(
            call.message,
            f"🎲 <b>Бросаем кубик!</b>\n{LINE}\n💰 Ставка: <b>{fmt(bet)} ⭐</b>\n"
            f"Твой выбор: {'⬆️ Больше (4–6)' if choice == 'high' else '⬇️ Меньше (1–3)'}",
        )
        dice = await bot.send_dice(chat_id, emoji=DiceEmoji.DICE)
        value = dice.dice.value  # 1..6 — результат определяет сам Telegram
    except Exception:
        log.exception("Ошибка броска кубика")
        await db.add(uid, bet)  # вернуть ставку
        with suppress(Exception):
            await bot.send_message(chat_id, "⚠️ Ошибка броска. Ставка возвращена на баланс.")
        return

    high = value >= 4
    win = (choice == "high") == high
    payout = round(bet * (WIN_MULT if win else LOSE_MULT), 2)
    await db.add(uid, payout, game=True)  # зачисляем сразу — даже если дальше что-то упадёт

    await asyncio.sleep(DICE_ANIM)  # дождаться анимации
    row = await db.user(uid)
    head = f"🎲 Выпало: <b>{value}</b> — {'больше' if high else 'меньше'}"
    await bot.send_message(
        chat_id,
        result_text(head, win, bet, payout, row["balance"]),
        reply_markup=again_kb("dice", bet),
    )


@r_user.callback_query(F.data.startswith("pick:"))
async def cb_pick(call: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    try:
        _, game, choice, bet_s = call.data.split(":")
    except ValueError:
        await call.answer()
        return
    bet = parse_amount(bet_s)
    valid = {"color": ("red", "blue"), "dice": ("high", "low")}
    if game not in valid or choice not in valid[game] or bet is None or bet < MIN_BET:
        await call.answer("🎲 Игры обновились — открой раздел «Игры» заново", show_alert=True)
        return
    uid = call.from_user.id
    if uid in PLAYING:
        await call.answer("⏳ Подождите, игра ещё идёт")
        return
    PLAYING.add(uid)
    try:
        if game == "color":
            await play_color(call, choice, bet)
        else:
            await play_dice(call, bot, choice, bet)
    finally:
        PLAYING.discard(uid)


# ════════════════════════════════════════════════════════════════════════
#  🧯  ЗАПАСНЫЕ ОБРАБОТЧИКИ
# ════════════════════════════════════════════════════════════════════════
@r_user.callback_query()
async def cb_fallback(call: CallbackQuery):
    await call.answer()


@r_user.message(F.chat.type == "private")
async def msg_fallback(message: Message, state: FSMContext):
    await state.clear()
    await render_menu(message, message.from_user.id)


async def on_error(event: ErrorEvent):
    log.exception("Unhandled error: %s", event.exception)
    return True


# ════════════════════════════════════════════════════════════════════════
#  🚀  ЗАПУСК
# ════════════════════════════════════════════════════════════════════════
async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    if BOT_TOKEN.startswith("PASTE"):
        raise SystemExit("❌ Укажите токен бота: переменная BOT_TOKEN или константа в начале файла")

    await db.init()
    STATE["maint"] = (await db.get_setting("maint")) == "1"

    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML, link_preview_is_disabled=True
        ),
    )
    me = await bot.get_me()
    BOT_INFO.update(id=me.id, username=me.username)
    log.info("Бот запущен: @%s", me.username)

    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(Guard())
    dp.callback_query.outer_middleware(Guard())
    dp.include_routers(r_act, r_admin, r_user)
    dp.errors.register(on_error)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
