import asyncio
import json
import os
import re
from pathlib import Path

import requests
from flask import Flask
from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes,
)
from telegram import ReplyKeyboardMarkup, KeyboardButton

# --- ENV ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = str(os.environ["CHAT_ID"])  # твой chat_id (строкой)
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "600"))  # 10 минут
PORT = int(os.environ.get("PORT", "10000"))

STATE_FILE = Path("tracks.json")

# принимаем либо ссылку, либо просто трек-номер
TRACK_RE = re.compile(r"(?:[?&]track=)?([\d\-]{6,})", re.IGNORECASE)

# --- tiny web server (Render wants an open port for Web Service) ---
app = Flask(__name__)


@app.get("/")
def home():
    return "ok", 200


# --- storage helpers ---
def load_tracks() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_tracks(data: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# --- telegram send helper (plain HTTP) ---
def tg_send(text: str) -> None:
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )


# --- menu keyboard ---
MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("➕ Добавить трек"), KeyboardButton("📦 Отслеживаемые")],
        [KeyboardButton("➖ Удалить трек"), KeyboardButton("ℹ️ Помощь")],
    ],
    resize_keyboard=True,
)


HELP_TEXT = (
    "Кидай ссылку вида:\n"
    "https://tracking.ozon.ru/?track=94044975-0220-1\n"
    "или просто трек-номер: 94044975-0220-1\n\n"
    "Кнопки:\n"
    "• 📦 Отслеживаемые — список треков и статусов\n"
    "• ➖ Удалить трек — удаление по номеру\n\n"
    f"Опрос статусов раз в {POLL_SECONDS//60} мин.\n"
)


# --- OZON parsing ---
STATUS_CANDIDATES = [
    # из твоих скринов + базовые
    "создан",
    "передается в доставку",
    "в пути",
    "заказ принят перевозчиком",
    "заказ везут на таможню в стране отправления",
    "заказ привезли на таможню для экспортного таможенного оформления",
    "заказ везут на таможню в стране назначения",
    "заказ привезли в страну назначения",
    "заказ передан на импортное таможенное оформление",
    "заказ проходит импортное таможенное оформление",
    "заказ выпущен импортной таможней",
    "заказ отправили на сортировочный терминал",
    "заказ покинул сортировочный терминал",
    "заказ ожидает отправки в город получателя",
    "заказ везут в город получателя",
    "заказ везут",
    "заказ передали в курьерскую доставку",
    "готово к выдаче",
    "на пункте выдачи",
    "доставлено",
    "получено",
    "ожидает",
    "прибыло",
    "передано",
    "отправлено",
]


async def ozon_get_status(track: str) -> str:
    url = f"https://tracking.ozon.ru/?track={track}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,  # можно True, но добавим маскировку
            args=["--disable-blink-features=AutomationControlled"]
        )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )

        page = await context.new_page()

        await page.goto(url, wait_until="networkidle", timeout=60000)

        # Ждём появления текста статусов
        await page.wait_for_timeout(5000)

        body = await page.inner_text("body")

        await browser.close()

    text = body.lower()

    # реальные статусы Ozon
    statuses = [
        "создан",
        "передается в доставку",
        "в пути",
        "заказ принят перевозчиком",
        "на таможне",
        "выпущен импортной таможней",
        "прибыл",
        "в городе получателя",
        "передан курьеру",
        "доставлен",
        "готов к выдаче",
    ]

    for s in statuses:
        if s in text:
            return s

    # для отладки
    print("DEBUG BODY:", text[:500], flush=True)

    return "unknown"


# --- bot commands / handlers ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return
    await update.message.reply_text("🤖 Бот запущен. Жми кнопки или кидай трек/ссылку.", reply_markup=MENU_KB)
    await update.message.reply_text(HELP_TEXT, reply_markup=MENU_KB)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return
    await update.message.reply_text(HELP_TEXT, reply_markup=MENU_KB)


async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return
    if not context.args:
        await update.message.reply_text("Использование: /debug 94044975-0220-1", reply_markup=MENU_KB)
        return

    track = context.args[0].strip()
    status = await ozon_get_status(track)
    await update.message.reply_text(
        f"debug status = {status}\n(подробности смотри в Render Logs: OZON DEBUG ...)",
        reply_markup=MENU_KB,
    )


async def list_tracks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracks = load_tracks()
    if not tracks:
        await update.message.reply_text("Пока нет отслеживаемых треков.", reply_markup=MENU_KB)
        return

    lines = ["📦 Отслеживаемые треки:"]
    for t, info in tracks.items():
        st = info.get("status") or "unknown"
        lines.append(f"• {t} — {st}")
    await update.message.reply_text("\n".join(lines), reply_markup=MENU_KB)


async def delete_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_delete"] = True
    await update.message.reply_text("Введи трек-номер, который удалить:", reply_markup=MENU_KB)


async def add_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_add"] = True
    await update.message.reply_text("Пришли ссылку/трек вида:\nhttps://tracking.ozon.ru/?track=940... или 940...", reply_markup=MENU_KB)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return

    text = (update.message.text or "").strip()

    # кнопки меню
    if text == "📦 Отслеживаемые":
        await list_tracks(update, context)
        return

    if text == "ℹ️ Помощь":
        await update.message.reply_text(HELP_TEXT, reply_markup=MENU_KB)
        return

    if text == "➕ Добавить трек":
        await add_flow(update, context)
        return

    if text == "➖ Удалить трек":
        await delete_flow(update, context)
        return

    # режим удаления
    if context.user_data.get("awaiting_delete"):
        context.user_data["awaiting_delete"] = False
        m = TRACK_RE.search(text)
        if not m:
            await update.message.reply_text("Не похоже на трек-номер. Попробуй ещё раз.", reply_markup=MENU_KB)
            return
        track = m.group(1)

        tracks = load_tracks()
        if track in tracks:
            tracks.pop(track, None)
            save_tracks(tracks)
            await update.message.reply_text(f"🗑️ Удалил трек: {track}", reply_markup=MENU_KB)
        else:
            await update.message.reply_text(f"Такого трека нет в списке: {track}", reply_markup=MENU_KB)
        return

    # режим добавления (или просто сообщение с треком)
    if context.user_data.get("awaiting_add"):
        context.user_data["awaiting_add"] = False

    m = TRACK_RE.search(text)
    if not m:
        # тихо игнорим, чтобы не бесить
        return

    track = m.group(1)
    tracks = load_tracks()

    if track in tracks:
        await update.message.reply_text(f"Уже отслеживается: {track}", reply_markup=MENU_KB)
        return

    tracks[track] = {"status": None}
    save_tracks(tracks)
    await update.message.reply_text(f"✅ Добавил трек: {track}", reply_markup=MENU_KB)


# --- watcher loop ---
async def watcher_loop():
    # первичная инициализация (без спама)
    tracks = load_tracks()
    changed = False
    for track, info in tracks.items():
        if info.get("status") is None:
            try:
                info["status"] = await ozon_get_status(track)
                changed = True
            except Exception as e:
                print("OZON INIT ERROR:", repr(e), flush=True)
    if changed:
        save_tracks(tracks)

    # сообщение "бот запущен" отправляем один раз при старте процесса
    tg_send("🤖 Бот запущен. Жми /start или кидай треки.")

    while True:
        tracks = load_tracks()
        updated = False

        for track, info in list(tracks.items()):
            old = info.get("status")
            try:
                new = await ozon_get_status(track)

                if new != "unknown" and old is not None and new != old:
                    tg_send(f"📦 {track}: {old} → {new}")
                    info["status"] = new
                    updated = True
                elif old is None and new != "unknown":
                    info["status"] = new
                    updated = True

            except Exception as e:
                print("OZON LOOP ERROR:", track, repr(e), flush=True)
                continue

        if updated:
            save_tracks(tracks)

        await asyncio.sleep(POLL_SECONDS)


def run_bot() -> None:
    """
    Важно: НЕ asyncio.run().
    python-telegram-bot сам управляет event loop внутри run_polling().
    """
    tg_app = ApplicationBuilder().token(BOT_TOKEN).build()

    tg_app.add_handler(CommandHandler("start", start_cmd))
    tg_app.add_handler(CommandHandler("help", help_cmd))
    tg_app.add_handler(CommandHandler("debug", debug_cmd))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async def post_init(app_):
        # ✅ запуск watcher ТОЛЬКО после старта приложения
        app_.create_task(watcher_loop())

    tg_app.post_init = post_init
    tg_app.run_polling()


if __name__ == "__main__":
    run_bot()
