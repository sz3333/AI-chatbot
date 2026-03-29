"""
╔══════════════════════════════════════════════════════════════╗
║              Gemini AI Bot  —  by your order                ║
║  Aiogram 3.x + SQLite + Google Gemini API (multi-key)       ║
╚══════════════════════════════════════════════════════════════╝

Установка зависимостей:
    pip install aiogram google-genai aiosqlite python-dotenv

Запуск:
    BOT_TOKEN=<токен> python gemini_bot.py

Или создай .env:
    BOT_TOKEN=ваш_токен_бота

Владелец (OWNER_ID = 93823977) — особые права:
  /settokens key1,key2,key3   — установить Gemini API ключи
  /setglobalprompt <текст>    — глобальный промт для всех
  /statdb                     — статистика пользователей в БД
  /addusers                   — добавить пользователей из .txt файла
  /broadcast <текст>          — рассылка всем активным юзерам
  /clearall                   — очистить всю память бота
  /setmodel <модель>          — сменить модель Gemini
  /keystat                    — статистика ключей API
  /ban <id> [сек]            — забанить юзера вручную
  /unban <id>                — разбанить юзера
  /banlist                   — список активных банов

Команды для всех (только в ЛС):
  /setprompt <текст>          — личный промт (только для себя)
  /clearprompt                — сбросить личный промт
  /clearhistory               — очистить свою историю диалога
  /showhistory                — показать последние 10 сообщений истории
  /help                       — справка
"""

import asyncio
import json
import logging
import os
import random
import re
import time
import html
from datetime import datetime
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    ContentType,
    FSInputFile,
    BufferedInputFile,
)
from aiogram.enums import ParseMode, ChatType
from aiogram.client.default import DefaultBotProperties

try:
    from google import genai
    from google.genai import types as gtypes
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False

# ─── Конфигурация (из config.py) ────────────────────────────────────────────

try:
    import config as _cfg
except ModuleNotFoundError:
    raise SystemExit(
        "❌ Файл config.py не найден!\n"
        "Скопируй config.example.py → config.py и заполни его."
    )

BOT_TOKEN         = getattr(_cfg, "BOT_TOKEN", "")
OWNER_ID          = getattr(_cfg, "OWNER_ID", 0)
DB_PATH           = getattr(_cfg, "DB_PATH", "gemini_bot.db")
DEFAULT_MODEL     = getattr(_cfg, "DEFAULT_MODEL", "gemini-2.5-flash")
GEMINI_TIMEOUT    = getattr(_cfg, "GEMINI_TIMEOUT", 120)
MAX_HISTORY_PAIRS = getattr(_cfg, "MAX_HISTORY_PAIRS", 30)
MAX_TG_LEN        = getattr(_cfg, "MAX_TG_LEN", 4000)

if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN не задан в config.py!")
if not OWNER_ID:
    raise SystemExit("❌ OWNER_ID не задан в config.py!")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("gemini_bot")

# ─── Антиспам конфиг ─────────────────────────────────────────────────────────

SPAM_WINDOW_SEC   = getattr(_cfg, "SPAM_WINDOW_SEC",   10)
SPAM_MAX_MESSAGES = getattr(_cfg, "SPAM_MAX_MESSAGES",  5)
SPAM_BAN_AFTER    = getattr(_cfg, "SPAM_BAN_AFTER",    10)
SPAM_BAN_DURATION = getattr(_cfg, "SPAM_BAN_DURATION", 300)

# ─── База данных ─────────────────────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                last_prompt TEXT DEFAULT '',
                last_seen   INTEGER DEFAULT 0,
                active      INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                chat_id     INTEGER,
                role        TEXT,
                content     TEXT,
                ts          INTEGER
            );
        """)
        await db.commit()

async def get_setting(key: str, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else default

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
        await db.commit()

async def upsert_user(user_id: int, username: str = "", first_name: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users(user_id, username, first_name, last_seen, active)
            VALUES(?,?,?,?,1)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_seen=excluded.last_seen,
                active=1
        """, (user_id, username or "", first_name or "", int(time.time())))
        await db.commit()

async def get_user_prompt(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT last_prompt FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else ""

async def set_user_prompt(user_id: int, prompt: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_prompt=? WHERE user_id=?", (prompt, user_id))
        await db.commit()

async def count_active_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE active=1") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

async def all_active_user_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE active=1") as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]

async def add_user_ids_bulk(ids: list[int]) -> tuple[int, int]:
    """Добавляет список ID. Возвращает (добавлено, пропущено)."""
    added = skipped = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for uid in ids:
            async with db.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)) as cur:
                exists = await cur.fetchone()
            if exists:
                # уже есть — просто помечаем активным
                await db.execute("UPDATE users SET active=1 WHERE user_id=?", (uid,))
                skipped += 1
            else:
                await db.execute(
                    "INSERT OR IGNORE INTO users(user_id, last_seen, active) VALUES(?,?,1)",
                    (uid, int(time.time()))
                )
                added += 1
        await db.commit()
    return added, skipped

# ─── История диалогов ────────────────────────────────────────────────────────

async def get_history(user_id: int, chat_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT role, content FROM history WHERE user_id=? AND chat_id=? ORDER BY id DESC LIMIT ?",
            (user_id, chat_id, MAX_HISTORY_PAIRS * 2)
        ) as cur:
            rows = await cur.fetchall()
    rows.reverse()
    return [{"role": r[0], "content": r[1]} for r in rows]

async def add_history(user_id: int, chat_id: int, role: str, content: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO history(user_id, chat_id, role, content, ts) VALUES(?,?,?,?,?)",
            (user_id, chat_id, role, content, int(time.time()))
        )
        # Обрезаем лишнее
        await db.execute("""
            DELETE FROM history WHERE id IN (
                SELECT id FROM history
                WHERE user_id=? AND chat_id=?
                ORDER BY id DESC
                LIMIT -1 OFFSET ?
            )
        """, (user_id, chat_id, MAX_HISTORY_PAIRS * 2))
        await db.commit()

async def clear_history(user_id: int, chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM history WHERE user_id=? AND chat_id=?", (user_id, chat_id))
        await db.commit()

async def clear_all_history():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM history")
        await db.commit()

# ─── Антиспам / Rate limiter ─────────────────────────────────────────────────

class RateLimiter:
    """
    Sliding-window rate limiter + автобан.
    Хранится в RAM (сбрасывается при рестарте бота).
    """
    def __init__(self):
        # user_id -> list[timestamp]
        self._history: dict[int, list[float]] = {}
        # user_id -> ban_until (timestamp)
        self._banned: dict[int, float] = {}
        # user_id -> предупреждён уже в этом окне?
        self._warned: dict[int, bool] = {}

    def _clean(self, uid: int, now: float):
        """Убираем старые отметки из окна."""
        self._history.setdefault(uid, [])
        self._history[uid] = [t for t in self._history[uid] if now - t < SPAM_WINDOW_SEC]

    def check(self, uid: int) -> tuple[str, int]:
        """
        Возвращает (статус, ban_left_sec).
        статус: 'ok' | 'warn' | 'ban' | 'banned'
        """
        now = time.time()

        # Уже забанен?
        ban_until = self._banned.get(uid, 0)
        if ban_until > now:
            return "banned", int(ban_until - now)

        self._clean(uid, now)
        self._history[uid].append(now)
        count = len(self._history[uid])

        if count >= SPAM_BAN_AFTER:
            self._banned[uid] = now + SPAM_BAN_DURATION
            self._history[uid] = []
            self._warned[uid] = False
            log.warning(f"[ANTISPAM] Забанен uid={uid} на {SPAM_BAN_DURATION}с")
            return "ban", SPAM_BAN_DURATION

        if count >= SPAM_MAX_MESSAGES:
            if not self._warned.get(uid):
                self._warned[uid] = True
                return "warn", 0
            return "spam", 0   # молча игнорируем

        self._warned[uid] = False
        return "ok", 0

    def unban(self, uid: int):
        self._banned.pop(uid, None)
        self._history.pop(uid, None)
        self._warned.pop(uid, None)

    def is_banned(self, uid: int) -> tuple[bool, int]:
        ban_until = self._banned.get(uid, 0)
        now = time.time()
        if ban_until > now:
            return True, int(ban_until - now)
        return False, 0

    def ban_list(self) -> list[tuple[int, int]]:
        now = time.time()
        return [(uid, int(until - now)) for uid, until in self._banned.items() if until > now]

    def manual_ban(self, uid: int, duration: int = SPAM_BAN_DURATION):
        self._banned[uid] = time.time() + duration
        self._history.pop(uid, None)

rate_limiter = RateLimiter()

# ─── Менеджер API-ключей ─────────────────────────────────────────────────────

class KeyManager:
    def __init__(self):
        self._keys: list[str] = []
        self._idx  = 0
        self._failures: dict[str, int] = {}   # key -> fail_count

    async def load(self):
        raw = await get_setting("gemini_keys", "")
        self._keys = [k.strip() for k in raw.split(",") if k.strip()]
        log.info(f"Загружено ключей: {len(self._keys)}")

    async def save(self, keys: list[str]):
        self._keys = keys
        self._failures = {}
        await set_setting("gemini_keys", ",".join(keys))

    def get_keys(self) -> list[str]:
        return list(self._keys)

    def next_key(self) -> Optional[str]:
        """Круговое переключение с учётом сбоев."""
        if not self._keys:
            return None
        # Сортируем: меньше сбоев — раньше
        sorted_keys = sorted(self._keys, key=lambda k: (self._failures.get(k, 0), random.random()))
        return sorted_keys[0]

    def mark_fail(self, key: str):
        self._failures[key] = self._failures.get(key, 0) + 1

    def mark_ok(self, key: str):
        self._failures[key] = 0

    def stat(self) -> str:
        if not self._keys:
            return "Ключей нет."
        lines = []
        for k in self._keys:
            fails = self._failures.get(k, 0)
            status = "✅" if fails == 0 else f"⚠️ ({fails} ошибок)"
            lines.append(f"  <code>...{k[-6:]}</code> {status}")
        return "\n".join(lines)

key_manager = KeyManager()

# ─── Gemini API ──────────────────────────────────────────────────────────────

def _build_contents(history: list[dict], new_text: str) -> list:
    contents = []
    for item in history:
        role = "model" if item["role"] == "model" else "user"
        contents.append(gtypes.Content(role=role, parts=[gtypes.Part(text=item["content"])]))
    contents.append(gtypes.Content(role="user", parts=[gtypes.Part(text=new_text)]))
    return contents

async def call_gemini(
    user_text: str,
    history: list[dict],
    system_prompt: str = "",
    model_name: str = DEFAULT_MODEL,
) -> str:
    if not GOOGLE_AVAILABLE:
        return "❌ Библиотека google-genai не установлена. Выполните: pip install google-genai"

    keys = key_manager.get_keys()
    if not keys:
        return "❌ API ключи не настроены. Владелец должен выполнить /settokens"

    contents = _build_contents(history, user_text)

    safety = [
        gtypes.SafetySetting(category=c, threshold="BLOCK_NONE")
        for c in [
            "HARM_CATEGORY_HARASSMENT",
            "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "HARM_CATEGORY_DANGEROUS_CONTENT",
        ]
    ]
    cfg = gtypes.GenerateContentConfig(
        system_instruction=system_prompt.strip() or None,
        safety_settings=safety,
        temperature=1.0,
    )

    # Попытки по всем ключам
    sorted_keys = sorted(keys, key=lambda k: (key_manager._failures.get(k, 0), random.random()))
    last_err = "Неизвестная ошибка"
    for key in sorted_keys:
        try:
            client = genai.Client(api_key=key)
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=cfg,
                ),
                timeout=GEMINI_TIMEOUT,
            )
            text = response.text
            if text:
                key_manager.mark_ok(key)
                return text.strip()
            key_manager.mark_fail(key)
        except asyncio.TimeoutError:
            last_err = f"Таймаут ({GEMINI_TIMEOUT}с)"
            key_manager.mark_fail(key)
        except Exception as e:
            last_err = str(e)
            key_manager.mark_fail(key)
            log.warning(f"Ключ ...{key[-6:]} — ошибка: {e}")

    return f"❌ Все ключи исчерпаны. Последняя ошибка: <code>{html.escape(last_err)}</code>"

# ─── Вспомогательные функции ─────────────────────────────────────────────────

def md_to_simple_html(text: str) -> str:
    """Базовая конвертация markdown → HTML для Telegram."""
    # Код-блоки
    text = re.sub(r"```(\w+)?\n?([\s\S]+?)```", lambda m: f"<pre><code>{html.escape(m.group(2).strip())}</code></pre>", text)
    # Инлайн-код
    text = re.sub(r"`([^`]+)`", lambda m: f"<code>{html.escape(m.group(1))}</code>", text)
    # Жирный
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    # Курсив
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    # Зачёркнутый
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    # Заголовки → жирный
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    # Списки → буллет
    text = re.sub(r"^[\s]*[-*+]\s+", "• ", text, flags=re.MULTILINE)
    return text

async def send_reply(message: Message, text: str):
    """Отправляет текст или файл, если слишком длинный."""
    formatted = md_to_simple_html(text)
    if len(formatted) <= MAX_TG_LEN:
        try:
            await message.reply(formatted, parse_mode=ParseMode.HTML)
        except Exception:
            await message.reply(text)
    else:
        buf = text.encode("utf-8")
        doc = BufferedInputFile(buf, filename="response.txt")
        await message.reply_document(doc, caption="📄 Ответ слишком длинный, отправлен файлом.")

def is_pm(message: Message) -> bool:
    return message.chat.type == ChatType.PRIVATE

def is_owner(message: Message) -> bool:
    return message.from_user and message.from_user.id == OWNER_ID

# ─── Роутер и хендлеры ───────────────────────────────────────────────────────

router = Router()

# ── /start и /help ──────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(msg: Message):
    if msg.from_user:
        await upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.first_name or "")
    await msg.answer(
        "👋 Привет! Я AI-бот на базе Google Gemini.\n\n"
        "В личных сообщениях просто пиши мне — я отвечу.\n"
        "В группах отвечаю на сообщения, адресованные мне (упоминание или ответ).\n\n"
        "📌 /help — список команд",
        parse_mode=ParseMode.HTML,
    )

@router.message(Command("help"))
async def cmd_help(msg: Message):
    owner_block = ""
    if is_owner(msg):
        owner_block = (
            "\n\n<b>🔐 Команды владельца:</b>\n"
            "/settokens key1,key2 — установить API ключи\n"
            "/setglobalprompt &lt;текст&gt; — глобальный промт\n"
            "/statdb — статистика юзеров\n"
            "/addusers — добавить юзеров из .txt файла\n"
            "/broadcast &lt;текст&gt; — рассылка всем\n"
            "/clearall — очистить всю память\n"
            "/setmodel &lt;модель&gt; — сменить модель Gemini\n"
            "/keystat — статус ключей\n"
            "/ban &lt;id&gt; [сек] — забанить юзера\n"
            "/unban &lt;id&gt; — разбанить\n"
            "/banlist — список активных банов"
        )
    await msg.answer(
        "<b>📚 Команды:</b>\n"
        "/setprompt &lt;текст&gt; — установить личный промт (только ЛС)\n"
        "/clearprompt — сбросить личный промт\n"
        "/clearhistory — очистить историю диалога\n"
        "/showhistory — последние 10 сообщений истории\n"
        "/help — эта справка"
        + owner_block,
        parse_mode=ParseMode.HTML,
    )

# ── /settokens (владелец) ────────────────────────────────────────────────────

@router.message(Command("settokens"))
async def cmd_settokens(msg: Message):
    if not is_owner(msg):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        return await msg.answer("Использование: /settokens ключ1,ключ2,ключ3")
    keys = [k.strip() for k in args[1].split(",") if k.strip()]
    if not keys:
        return await msg.answer("❌ Не распознано ни одного ключа.")
    await key_manager.save(keys)
    await msg.answer(f"✅ Сохранено <b>{len(keys)}</b> ключ(ей).", parse_mode=ParseMode.HTML)

# ── /setglobalprompt (владелец) ──────────────────────────────────────────────

@router.message(Command("setglobalprompt"))
async def cmd_setglobalprompt(msg: Message):
    if not is_owner(msg):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        cur = await get_setting("global_prompt", "")
        return await msg.answer(
            f"Текущий глобальный промт:\n<pre>{html.escape(cur or '(пусто)')}</pre>\n\n"
            "Чтобы установить: /setglobalprompt &lt;текст&gt;",
            parse_mode=ParseMode.HTML,
        )

# ─── Запуск ──────────────────────────────────────────────────────────────────

async def main():
    await init_db()
    await key_manager.load()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    log.info("Бот запущен. Владелец ID: %d", OWNER_ID)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())