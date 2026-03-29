"""
╔══════════════════════════════════════════════════════════════╗
║              Gemini AI Bot  —  by your order                ║
║  Aiogram 3.x + SQLite + Google Gemini API (multi-key)       ║
╚══════════════════════════════════════════════════════════════╝

Установка зависимостей:
    pip install -U aiogram google-genai aiosqlite python-dotenv

Запуск:
    python gemini_bot.py
"""

import asyncio
import html
import logging
import os
import random
import re
import sys
import time
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, BufferedInputFile
from aiogram.client.default import DefaultBotProperties

try:
    from google import genai
    from google.genai import types as gtypes
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False

# ─── Загрузка конфига ───────────────────────────────────────────────────────
if not os.path.exists("config.py"):
    print("❌ Файл config.py не найден! Создай его рядом с ботом.")
    sys.exit(1)

from config import (
    BOT_TOKEN,
    OWNER_ID,
    DEFAULT_MODEL,
    GEMINI_TIMEOUT,
    MAX_HISTORY_PAIRS,
    MAX_TG_LEN,
    DB_PATH,
)

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не указан в config.py!")

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

# ─── Менеджер API-ключей ─────────────────────────────────────────────────────

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
        return "❌ Библиотека google-genai не установлена. Выполните: pip install -U google-genai"

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
    text = re.sub(r"```(\w+)?\n?([\s\S]+?)```", lambda m: f"<pre><code>{html.escape(m.group(2).strip())}</code></pre>", text)
    text = re.sub(r"`([^`]+)`", lambda m: f"<code>{html.escape(m.group(1))}</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
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
            "/settokens key1,key2,key3 — установить API ключи\n"
            "/setglobalprompt &lt;текст&gt; — глобальный промт\n"
            "/statdb — статистика юзеров\n"
            "/addusers — добавить юзеров из .txt файла\n"
            "/broadcast &lt;текст&gt; — рассылка всем\n"
            "/clearall — очистить всю память\n"
            "/setmodel &lt;модель&gt; — сменить модель Gemini\n"
            "/keystat — статус ключей"
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
    prompt = args[1].strip()
    await set_setting("global_prompt", prompt)
    await msg.answer(f"✅ Глобальный промт установлен ({len(prompt)} символов).", parse_mode=ParseMode.HTML)

# ── /statdb (владелец) ───────────────────────────────────────────────────────

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
        f"<b>📊 Статистика БД:</b>\n"
        f"👥 Всего юзеров: <b>{all_users}</b>\n"
        f"✅ Активных: <b>{total}</b>\n"
        f"💬 Записей в истории: <b>{hist_count}</b>\n"
        f"🔑 API ключей: <b>{keys_count}</b>\n"
        f"🤖 Модель: <code>{model}</code>",
        parse_mode=ParseMode.HTML,
    )

# ── /addusers (владелец, только ЛС) ─────────────────────────────────────────

_awaiting_users_file: set[int] = set()

@router.message(Command("addusers"))
async def cmd_addusers(msg: Message):
    if not is_owner(msg):
        return
    if not is_pm(msg):
        return await msg.answer("Эту команду можно использовать только в ЛС.")
    _awaiting_users_file.add(msg.from_user.id)
    await msg.answer(
        "📎 Пришли .txt файл со списком пользователей.\n\n"
        "<b>Формат файла</b> (каждый ID на новой строке, или через запятую):\n"
        "<pre>123456789\n987654321\n111222333</pre>\n"
        "или\n"
        "<pre>123456789, 987654321, 111222333</pre>\n\n"
        "Бот проверит и добавит их в БД.",
        parse_mode=ParseMode.HTML,
    )

@router.message(F.document, F.chat.type == ChatType.PRIVATE)
async def handle_document(msg: Message, bot: Bot):
    if not msg.from_user:
        return
    uid = msg.from_user.id
    if uid in _awaiting_users_file and is_owner(msg):
        _awaiting_users_file.discard(uid)
        if not msg.document.file_name.endswith(".txt"):
            return await msg.answer("❌ Нужен именно .txt файл.")
        try:
            file = await bot.get_file(msg.document.file_id)
            content_bytes = await bot.download_file(file.file_path)
            text = content_bytes.read().decode("utf-8", errors="ignore")
        except Exception as e:
            return await msg.answer(f"❌ Ошибка чтения файла: {e}")

        raw_ids = re.findall(r"\d{5,15}", text)
        user_ids = list({int(i) for i in raw_ids})

        if not user_ids:
            return await msg.answer("❌ Не найдено ни одного ID в файле.")

        status_msg = await msg.answer(f"⏳ Обрабатываю {len(user_ids)} ID...")
        added, skipped = await add_user_ids_bulk(user_ids)
        await status_msg.edit_text(
            f"✅ Готово!\n"
            f"➕ Добавлено новых: <b>{added}</b>\n"
            f"🔄 Уже были в БД (помечены активными): <b>{skipped}</b>\n"
            f"📊 Всего обработано: <b>{len(user_ids)}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

# ── /broadcast (владелец) ────────────────────────────────────────────────────

@router.message(Command("broadcast"))
async def cmd_broadcast(msg: Message, bot: Bot):
    if not is_owner(msg):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        return await msg.answer("Использование: /broadcast &lt;текст сообщения&gt;", parse_mode=ParseMode.HTML)
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
        f"✅ Рассылка завершена.\n"
        f"📨 Отправлено: <b>{sent}</b>\n"
        f"❌ Не доставлено: <b>{failed}</b>",
        parse_mode=ParseMode.HTML,
    )

# ── /clearall (владелец) ─────────────────────────────────────────────────────

@router.message(Command("clearall"))
async def cmd_clearall(msg: Message):
    if not is_owner(msg):
        return
    await clear_all_history()
    await msg.answer("🧹 Вся история диалогов очищена.")

# ── /setmodel (владелец) ─────────────────────────────────────────────────────

@router.message(Command("setmodel"))
async def cmd_setmodel(msg: Message):
    if not is_owner(msg):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        cur = await get_setting("gemini_model", DEFAULT_MODEL)
        return await msg.answer(f"Текущая модель: <code>{cur}</code>\nИспользование: /setmodel &lt;модель&gt;", parse_mode=ParseMode.HTML)
    model = args[1].strip()
    await set_setting("gemini_model", model)
    await msg.answer(f"✅ Модель установлена: <code>{model}</code>", parse_mode=ParseMode.HTML)

# ── /keystat (владелец) ──────────────────────────────────────────────────────

@router.message(Command("keystat"))
async def cmd_keystat(msg: Message):
    if not is_owner(msg):
        return
    stat = key_manager.stat()
    await msg.answer(f"<b>🔑 Статус ключей:</b>\n{stat}", parse_mode=ParseMode.HTML)

# ── /setprompt (все, только ЛС) ─────────────────────────────────────────────

@router.message(Command("setprompt"))
async def cmd_setprompt(msg: Message):
    if not is_pm(msg):
        return await msg.answer("Эта команда работает только в личных сообщениях.")
    if not msg.from_user:
        return
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        cur = await get_user_prompt(msg.from_user.id)
        return await msg.answer(
            f"Текущий твой промт:\n<pre>{html.escape(cur or '(пусто)')}</pre>\n\n"
            "Чтобы установить: /setprompt &lt;текст&gt;",
            parse_mode=ParseMode.HTML,
        )
    prompt = args[1].strip()
    await set_user_prompt(msg.from_user.id, prompt)
    await msg.answer(f"✅ Личный промт установлен ({len(prompt)} символов).")

# ── /clearprompt ────────────────────────────────────────────────────────────

@router.message(Command("clearprompt"))
async def cmd_clearprompt(msg: Message):
    if not is_pm(msg):
        return
    if not msg.from_user:
        return
    await set_user_prompt(msg.from_user.id, "")
    await msg.answer("🗑 Личный промт сброшен.")

# ── /clearhistory ───────────────────────────────────────────────────────────

@router.message(Command("clearhistory"))
async def cmd_clearhistory(msg: Message):
    if not msg.from_user:
        return
    await clear_history(msg.from_user.id, msg.chat.id)
    await msg.answer("🧹 Твоя история диалога очищена.")

# ── /showhistory ────────────────────────────────────────────────────────────

@router.message(Command("showhistory"))
async def cmd_showhistory(msg: Message):
    if not msg.from_user:
        return
    hist = await get_history(msg.from_user.id, msg.chat.id)
    if not hist:
        return await msg.answer("История пуста.")
    lines = []
    for item in hist[-20:]:
        role_label = "🤖 Бот" if item["role"] == "model" else "👤 Ты"
        snippet = html.escape(item["content"][:200])
        lines.append(f"<b>{role_label}:</b> {snippet}")
    await msg.answer("\n\n".join(lines), parse_mode=ParseMode.HTML)

# ── Основной обработчик сообщений ────────────────────────────────────────────

@router.message(F.text)
async def handle_text(msg: Message, bot: Bot):
    if not msg.from_user or msg.from_user.is_bot:
        return

    chat_type = msg.chat.type
    text = msg.text or ""

    # В группах — отвечаем только если упомянули бота или ответили на его сообщение
    if chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
        me = await bot.get_me()
        bot_username = me.username or ""
        is_reply_to_bot = (
            msg.reply_to_message
            and msg.reply_to_message.from_user
            and msg.reply_to_message.from_user.id == me.id
        )
        is_mention = bot_username and (f"@{bot_username}" in text)
        if not is_reply_to_bot and not is_mention:
            return
        text = text.replace(f"@{bot_username}", "").strip()

    if not text:
        return

    await upsert_user(
        msg.from_user.id,
        msg.from_user.username or "",
        msg.from_user.first_name or "",
    )

    global_prompt = await get_setting("global_prompt", "") or ""
    user_prompt = ""
    if is_pm(msg):
        user_prompt = await get_user_prompt(msg.from_user.id) or ""

    parts = [p for p in [global_prompt, user_prompt] if p.strip()]
    system_prompt = "\n\n".join(parts)

    history = await get_history(msg.from_user.id, msg.chat.id)

    model_name = await get_setting("gemini_model", DEFAULT_MODEL) or DEFAULT_MODEL

    await bot.send_chat_action(msg.chat.id, "typing")

    answer = await call_gemini(
        user_text=text,
        history=history,
        system_prompt=system_prompt,
        model_name=model_name,
    )

    if not answer.startswith("❌"):
        await add_history(msg.from_user.id, msg.chat.id, "user", text)
        await add_history(msg.from_user.id, msg.chat.id, "model", answer)

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

    log.info(f"Бот запущен. Владелец ID: {OWNER_ID}")
    await dp.start_polling(bot, allowed_updates=["message"])

if __name__ == "__main__":
    asyncio.run(main())