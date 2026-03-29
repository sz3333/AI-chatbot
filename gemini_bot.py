"""
╔══════════════════════════════════════════════════════════════╗
║              Gemini AI Bot  —  tag: LidF1x                  ║
║  Aiogram 3.x + SQLite + Google Gemini API (multi-key)       ║
╚══════════════════════════════════════════════════════════════╝

Зависимости:
    pip install aiogram google-genai aiosqlite

Запуск:
    python3 gemini_bot.py

Команды владельца:
  /settokens key1,key2      — Gemini API ключи
  /setglobalprompt <текст>  — глобальный промт
  /statdb                   — статистика БД
  /addusers                 — добавить юзеров из .txt
  /broadcast <текст>        — рассылка всем
  /clearall                 — очистить всю историю
  /setmodel <модель>        — сменить модель
  /keystat                  — статус ключей
  /ban <id> [сек]           — забанить
  /unban <id>               — разбанить
  /banlist                  — список банов

Команды для всех (только ЛС):
  /setprompt <текст>        — личный промт
  /clearprompt              — сбросить промт
  /clearhistory             — очистить историю
  /showhistory              — показать историю
  /help                     — справка
"""

import asyncio
import logging
import os
import random
import re
import time
import html
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, BufferedInputFile
from aiogram.enums import ParseMode, ChatType
from aiogram.client.default import DefaultBotProperties

try:
    from google import genai
    from google.genai import types as gtypes
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False

# ─── Конфигурация ────────────────────────────────────────────────────────────

try:
    import config as _cfg
except ModuleNotFoundError:
    raise SystemExit("❌ Файл config.py не найден! Скопируй config.example.py → config.py")

BOT_TOKEN         = getattr(_cfg, "BOT_TOKEN", "")
OWNER_ID          = getattr(_cfg, "OWNER_ID", 0)
DB_PATH           = getattr(_cfg, "DB_PATH", "gemini_bot.db")
DEFAULT_MODEL     = getattr(_cfg, "DEFAULT_MODEL", "gemini-2.0-flash-lite")
GEMINI_TIMEOUT    = getattr(_cfg, "GEMINI_TIMEOUT", 120)
MAX_HISTORY_PAIRS = getattr(_cfg, "MAX_HISTORY_PAIRS", 15)
MAX_TG_LEN        = getattr(_cfg, "MAX_TG_LEN", 4000)
SPAM_WINDOW_SEC   = getattr(_cfg, "SPAM_WINDOW_SEC", 10)
SPAM_MAX_MESSAGES = getattr(_cfg, "SPAM_MAX_MESSAGES", 5)
SPAM_BAN_AFTER    = getattr(_cfg, "SPAM_BAN_AFTER", 10)
SPAM_BAN_DURATION = getattr(_cfg, "SPAM_BAN_DURATION", 300)

if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN не задан в config.py!")
if not OWNER_ID:
    raise SystemExit("❌ OWNER_ID не задан в config.py!")

# ─── Логгер ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("gemini_bot")

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
                username    TEXT DEFAULT '',
                first_name  TEXT DEFAULT '',
                last_prompt TEXT DEFAULT '',
                last_seen   INTEGER DEFAULT 0,
                active      INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS history (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER,
                chat_id  INTEGER,
                role     TEXT,
                content  TEXT,
                ts       INTEGER
            );
        """)
        await db.commit()

async def get_setting(key: str, default=None) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else default

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
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

async def all_active_user_ids() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE active=1") as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]

async def add_user_ids_bulk(ids: list[int]) -> tuple[int, int]:
    added = skipped = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for uid in ids:
            async with db.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)) as cur:
                exists = await cur.fetchone()
            if exists:
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

# ─── История ─────────────────────────────────────────────────────────────────

async def get_history(user_id: int, chat_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT role, content FROM history "
            "WHERE user_id=? AND chat_id=? ORDER BY id DESC LIMIT ?",
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

# ─── Антиспам ────────────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self):
        self._history: dict[int, list[float]] = {}
        self._banned:  dict[int, float]       = {}
        self._warned:  dict[int, bool]        = {}

    def _clean(self, uid: int, now: float):
        self._history.setdefault(uid, [])
        self._history[uid] = [t for t in self._history[uid] if now - t < SPAM_WINDOW_SEC]

    def check(self, uid: int) -> tuple[str, int]:
        now = time.time()
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
            log.warning(f"[ANTISPAM] Автобан uid={uid} на {SPAM_BAN_DURATION}с")
            return "ban", SPAM_BAN_DURATION
        if count >= SPAM_MAX_MESSAGES:
            if not self._warned.get(uid):
                self._warned[uid] = True
                return "warn", 0
            return "spam", 0
        self._warned[uid] = False
        return "ok", 0

    def unban(self, uid: int):
        self._banned.pop(uid, None)
        self._history.pop(uid, None)
        self._warned.pop(uid, None)

    def manual_ban(self, uid: int, duration: int = SPAM_BAN_DURATION):
        self._banned[uid] = time.time() + duration
        self._history.pop(uid, None)

    def ban_list(self) -> list[tuple[int, int]]:
        now = time.time()
        return [(uid, int(until - now)) for uid, until in self._banned.items() if until > now]

rate_limiter = RateLimiter()

# ─── Менеджер ключей ─────────────────────────────────────────────────────────

class KeyManager:
    def __init__(self):
        self._keys: list[str] = []
        self._failures: dict[str, int] = {}

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
            status = "✅" if fails == 0 else f"⚠️ {fails} ошибок"
            lines.append(f"  <code>...{k[-6:]}</code> — {status}")
        return "\n".join(lines)

key_manager = KeyManager()

# ─── Gemini API ───────────────────────────────────────────────────────────────

async def call_gemini(
    user_text: str,
    history: list[dict],
    system_prompt: str = "",
    model_name: str = DEFAULT_MODEL,
) -> str:
    if not GOOGLE_AVAILABLE:
        return "❌ google-genai не установлен. Выполни: pip install google-genai"

    keys = key_manager.get_keys()
    if not keys:
        return "❌ API ключи не настроены. Владелец должен выполнить /settokens"

    contents = []
    for item in history:
        role = "model" if item["role"] == "model" else "user"
        contents.append(gtypes.Content(role=role, parts=[gtypes.Part(text=item["content"])]))
    contents.append(gtypes.Content(role="user", parts=[gtypes.Part(text=user_text)]))

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
            last_err = "Пустой ответ от модели"
        except asyncio.TimeoutError:
            last_err = f"Таймаут ({GEMINI_TIMEOUT}с)"
            key_manager.mark_fail(key)
        except Exception as e:
            last_err = str(e)
            key_manager.mark_fail(key)
            log.warning(f"Ключ ...{key[-6:]} ошибка: {e}")

    return f"❌ Все ключи исчерпаны. Последняя ошибка: <code>{html.escape(last_err)}</code>"

# ─── Утилиты ─────────────────────────────────────────────────────────────────

def md_to_html(text: str) -> str:
    text = re.sub(
        r"```(\w+)?\n?([\s\S]+?)```",
        lambda m: f"<pre><code>{html.escape(m.group(2).strip())}</code></pre>",
        text
    )
    text = re.sub(r"`([^`]+)`", lambda m: f"<code>{html.escape(m.group(1))}</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__",     r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*",     r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~",     r"<s>\1</s>", text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^[\s]*[-*+]\s+", "• ", text, flags=re.MULTILINE)
    return text

async def send_reply(message: Message, text: str):
    formatted = md_to_html(text)
    if len(formatted) <= MAX_TG_LEN:
        try:
            await message.reply(formatted, parse_mode=ParseMode.HTML)
            return
        except Exception:
            pass
    doc = BufferedInputFile(text.encode("utf-8"), filename="response.txt")
    await message.reply_document(doc, caption="📄 Ответ слишком длинный, отправлен файлом.")

def is_pm(message: Message) -> bool:
    return message.chat.type == ChatType.PRIVATE

def is_owner(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == OWNER_ID)

# ─── Роутер ──────────────────────────────────────────────────────────────────

router = Router()
_awaiting_users_file: set[int] = set()

# ── /start ───────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(msg: Message):
    if msg.from_user:
        await upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.first_name or "")
    await msg.answer(
        "👋 Привет! Я AI-бот на базе Google Gemini.\n\n"
        "В личке просто пиши — отвечу.\n"
        "В группах — ответь на моё сообщение или упомяни меня.\n\n"
        "/help — список команд"
    )

# ── /help ────────────────────────────────────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(msg: Message):
    owner_block = ""
    if is_owner(msg):
        owner_block = (
            "\n\n<b>🔐 Команды владельца:</b>\n"
            "/settokens key1,key2 — API ключи Gemini\n"
            "/setglobalprompt &lt;текст&gt; — глобальный промт\n"
            "/statdb — статистика\n"
            "/addusers — добавить юзеров из .txt (в ЛС)\n"
            "/broadcast &lt;текст&gt; — рассылка всем\n"
            "/clearall — очистить всю историю\n"
            "/setmodel &lt;модель&gt; — сменить модель\n"
            "/keystat — статус ключей\n"
            "/ban &lt;id&gt; [сек] — забанить\n"
            "/unban &lt;id&gt; — разбанить\n"
            "/banlist — список банов"
        )
    await msg.answer(
        "<b>📚 Команды (только в ЛС):</b>\n"
        "/setprompt &lt;текст&gt; — личный промт\n"
        "/clearprompt — сбросить промт\n"
        "/clearhistory — очистить историю\n"
        "/showhistory — показать историю\n"
        "/help — эта справка"
        + owner_block,
        parse_mode=ParseMode.HTML,
    )

# ── /settokens ───────────────────────────────────────────────────────────────

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

# ── /setglobalprompt ─────────────────────────────────────────────────────────

@router.message(Command("setglobalprompt"))
async def cmd_setglobalprompt(msg: Message):
    if not is_owner(msg):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        cur = await get_setting("global_prompt", "")
        return await msg.answer(
            f"Текущий глобальный промт:\n<pre>{html.escape(cur or '(пусто)')}</pre>\n\n"
            "Установить: /setglobalprompt &lt;текст&gt;",
            parse_mode=ParseMode.HTML,
        )
    prompt = args[1].strip()
    await set_setting("global_prompt", prompt)
    await msg.answer(f"✅ Глобальный промт установлен ({len(prompt)} символов).")

# ── /statdb ──────────────────────────────────────────────────────────────────

@router.message(Command("statdb"))
async def cmd_statdb(msg: Message):
    if not is_owner(msg):
        return
    total = await count_active_users()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM history") as cur:
            hist_count = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            all_users = (await cur.fetchone())[0]
    model = await get_setting("gemini_model", DEFAULT_MODEL)
    keys_count = len(key_manager.get_keys())
    await msg.answer(
        f"<b>📊 Статистика:</b>\n"
        f"👥 Всего юзеров: <b>{all_users}</b>\n"
        f"✅ Активных: <b>{total}</b>\n"
        f"💬 Записей истории: <b>{hist_count}</b>\n"
        f"🔑 API ключей: <b>{keys_count}</b>\n"
        f"🤖 Модель: <code>{model}</code>",
        parse_mode=ParseMode.HTML,
    )

# ── /addusers ────────────────────────────────────────────────────────────────

@router.message(Command("addusers"))
async def cmd_addusers(msg: Message):
    if not is_owner(msg):
        return
    if not is_pm(msg):
        return await msg.answer("Только в ЛС.")
    _awaiting_users_file.add(msg.from_user.id)
    await msg.answer(
        "📎 Пришли .txt файл со списком ID.\n\n"
        "<b>Формат</b> — каждый ID на новой строке:\n"
        "<pre>123456789\n987654321</pre>",
        parse_mode=ParseMode.HTML,
    )

@router.message(F.document, F.chat.type == ChatType.PRIVATE)
async def handle_document(msg: Message, bot: Bot):
    if not msg.from_user:
        return
    uid = msg.from_user.id
    if uid not in _awaiting_users_file or not is_owner(msg):
        return
    _awaiting_users_file.discard(uid)
    if not msg.document.file_name.endswith(".txt"):
        return await msg.answer("❌ Нужен .txt файл.")
    try:
        file = await bot.get_file(msg.document.file_id)
        content_bytes = await bot.download_file(file.file_path)
        text = content_bytes.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return await msg.answer(f"❌ Ошибка чтения: {e}")
    raw_ids = re.findall(r"\d{5,15}", text)
    user_ids = list({int(i) for i in raw_ids})
    if not user_ids:
        return await msg.answer("❌ Не найдено ни одного ID.")
    status_msg = await msg.answer(f"⏳ Обрабатываю {len(user_ids)} ID...")
    added, skipped = await add_user_ids_bulk(user_ids)
    await status_msg.edit_text(
        f"✅ Готово!\n➕ Добавлено: <b>{added}</b>\n🔄 Уже были: <b>{skipped}</b>",
        parse_mode=ParseMode.HTML,
    )

# ── /broadcast ───────────────────────────────────────────────────────────────

@router.message(Command("broadcast"))
async def cmd_broadcast(msg: Message, bot: Bot):
    if not is_owner(msg):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        return await msg.answer("Использование: /broadcast &lt;текст&gt;", parse_mode=ParseMode.HTML)
    text = args[1]
    ids = await all_active_user_ids()
    sent = failed = 0
    status = await msg.answer(f"⏳ Рассылка {len(ids)} пользователям...")
    for uid in ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await status.edit_text(
        f"✅ Рассылка завершена.\n📨 Отправлено: <b>{sent}</b>\n❌ Не доставлено: <b>{failed}</b>",
        parse_mode=ParseMode.HTML,
    )

# ── /clearall ────────────────────────────────────────────────────────────────

@router.message(Command("clearall"))
async def cmd_clearall(msg: Message):
    if not is_owner(msg):
        return
    await clear_all_history()
    await msg.answer("🧹 Вся история очищена.")

# ── /setmodel ────────────────────────────────────────────────────────────────

@router.message(Command("setmodel"))
async def cmd_setmodel(msg: Message):
    if not is_owner(msg):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        cur = await get_setting("gemini_model", DEFAULT_MODEL)
        return await msg.answer(
            f"Текущая модель: <code>{cur}</code>\n"
            "Использование: /setmodel &lt;модель&gt;\n\n"
            "Рекомендуемые:\n"
            "• <code>gemini-2.0-flash-lite</code> — минимум токенов\n"
            "• <code>gemini-2.0-flash</code> — баланс\n"
            "• <code>gemini-2.5-flash</code> — умнее\n"
            "• <code>gemini-2.5-pro</code> — максимум",
            parse_mode=ParseMode.HTML,
        )
    model = args[1].strip()
    await set_setting("gemini_model", model)
    await msg.answer(f"✅ Модель: <code>{model}</code>", parse_mode=ParseMode.HTML)

# ── /keystat ─────────────────────────────────────────────────────────────────

@router.message(Command("keystat"))
async def cmd_keystat(msg: Message):
    if not is_owner(msg):
        return
    stat = key_manager.stat()
    await msg.answer(f"<b>🔑 Статус ключей:</b>\n{stat}", parse_mode=ParseMode.HTML)

# ── /ban /unban /banlist ──────────────────────────────────────────────────────

@router.message(Command("ban"))
async def cmd_ban(msg: Message):
    if not is_owner(msg):
        return
    args = msg.text.split()
    if len(args) < 2 or not args[1].lstrip("-").isdigit():
        return await msg.answer("Использование: /ban &lt;user_id&gt; [секунды]", parse_mode=ParseMode.HTML)
    uid = int(args[1])
    duration = int(args[2]) if len(args) >= 3 and args[2].isdigit() else SPAM_BAN_DURATION
    rate_limiter.manual_ban(uid, duration)
    await msg.answer(f"🔨 <code>{uid}</code> забанен на <b>{duration}</b>с.", parse_mode=ParseMode.HTML)

@router.message(Command("unban"))
async def cmd_unban(msg: Message):
    if not is_owner(msg):
        return
    args = msg.text.split()
    if len(args) < 2 or not args[1].lstrip("-").isdigit():
        return await msg.answer("Использование: /unban &lt;user_id&gt;", parse_mode=ParseMode.HTML)
    rate_limiter.unban(int(args[1]))
    await msg.answer(f"✅ <code>{args[1]}</code> разбанен.", parse_mode=ParseMode.HTML)

@router.message(Command("banlist"))
async def cmd_banlist(msg: Message):
    if not is_owner(msg):
        return
    bans = rate_limiter.ban_list()
    if not bans:
        return await msg.answer("Забаненных нет. 🕊")
    lines = [f"• <code>{uid}</code> — ещё {sec}с" for uid, sec in sorted(bans, key=lambda x: -x[1])]
    await msg.answer(
        f"<b>🔨 Активные баны ({len(bans)}):</b>\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )

# ── /setprompt /clearprompt ───────────────────────────────────────────────────

@router.message(Command("setprompt"))
async def cmd_setprompt(msg: Message):
    if not is_pm(msg):
        return await msg.answer("Только в личных сообщениях.")
    if not msg.from_user:
        return
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        cur = await get_user_prompt(msg.from_user.id)
        return await msg.answer(
            f"Текущий промт:\n<pre>{html.escape(cur or '(пусто)')}</pre>\n\n"
            "Установить: /setprompt &lt;текст&gt;",
            parse_mode=ParseMode.HTML,
        )
    await set_user_prompt(msg.from_user.id, args[1].strip())
    await msg.answer(f"✅ Промт установлен ({len(args[1].strip())} символов).")

@router.message(Command("clearprompt"))
async def cmd_clearprompt(msg: Message):
    if not is_pm(msg) or not msg.from_user:
        return
    await set_user_prompt(msg.from_user.id, "")
    await msg.answer("🗑 Промт сброшен.")

# ── /clearhistory /showhistory ───────────────────────────────────────────────

@router.message(Command("clearhistory"))
async def cmd_clearhistory(msg: Message):
    if not msg.from_user:
        return
    await clear_history(msg.from_user.id, msg.chat.id)
    await msg.answer("🧹 История очищена.")

@router.message(Command("showhistory"))
async def cmd_showhistory(msg: Message):
    if not msg.from_user:
        return
    hist = await get_history(msg.from_user.id, msg.chat.id)
    if not hist:
        return await msg.answer("История пуста.")
    lines = []
    for item in hist[-20:]:
        label = "🤖" if item["role"] == "model" else "👤"
        snippet = html.escape(item["content"][:200])
        lines.append(f"{label} {snippet}")
    await msg.answer("\n\n".join(lines), parse_mode=ParseMode.HTML)

# ── Основной обработчик ───────────────────────────────────────────────────────

@router.message(F.text)
async def handle_text(msg: Message, bot: Bot):
    if not msg.from_user or msg.from_user.is_bot:
        return

    text = msg.text or ""
    chat_type = msg.chat.type

    # В группах — только если упомянули или ответили боту
    if chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
        me = await bot.get_me()
        bot_username = me.username or ""
        is_reply_to_bot = (
            msg.reply_to_message
            and msg.reply_to_message.from_user
            and msg.reply_to_message.from_user.id == me.id
        )
        is_mention = bool(bot_username and f"@{bot_username}" in text)
        if not is_reply_to_bot and not is_mention:
            return
        text = text.replace(f"@{bot_username}", "").strip()

    if not text:
        return

    uid = msg.from_user.id

    # Антиспам (владельца не трогаем)
    if uid != OWNER_ID:
        status, ban_left = rate_limiter.check(uid)
        if status == "banned":
            return
        elif status == "ban":
            await msg.answer(
                f"🚫 Слишком быстро. Бан на <b>{ban_left}</b> секунд.",
                parse_mode=ParseMode.HTML,
            )
            return
        elif status == "warn":
            await msg.answer(
                f"⚠️ Не спамь! Ещё {SPAM_BAN_AFTER - SPAM_MAX_MESSAGES} "
                "быстрых сообщений — получишь бан."
            )
            return
        elif status == "spam":
            return

    # Регистрируем юзера
    await upsert_user(uid, msg.from_user.username or "", msg.from_user.first_name or "")

    # Промты
    global_prompt = await get_setting("global_prompt", "") or ""
    user_prompt = (await get_user_prompt(uid) or "") if is_pm(msg) else ""
    parts = [p for p in [global_prompt, user_prompt] if p.strip()]
    system_prompt = "\n\n".join(parts)

    # История
    history = await get_history(uid, msg.chat.id)

    # Модель
    model_name = await get_setting("gemini_model", DEFAULT_MODEL) or DEFAULT_MODEL

    # Печатает...
    await bot.send_chat_action(msg.chat.id, "typing")

    # Запрос к Gemini
    answer = await call_gemini(
        user_text=text,
        history=history,
        system_prompt=system_prompt,
        model_name=model_name,
    )

    # Сохраняем историю
    if not answer.startswith("❌"):
        await add_history(uid, msg.chat.id, "user", text)
        await add_history(uid, msg.chat.id, "model", answer)

    await send_reply(msg, answer)

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

    log.info("Бот запущен. Владелец ID: %d | Модель: %s", OWNER_ID, DEFAULT_MODEL)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
