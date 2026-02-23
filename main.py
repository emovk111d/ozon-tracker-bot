import asyncio
import json
import os
import re
from pathlib import Path

import requests
from flask import Flask
from playwright.async_api import async_playwright
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --- ENV ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = str(os.environ["CHAT_ID"])
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "600"))  # 10 минут
PORT = int(os.environ.get("PORT", "10000"))

STATE_FILE = Path("tracks.json")
TRACK_RE = re.compile(r"[?&]track=([\d\-]+)")

MENU = ReplyKeyboardMarkup(
    keyboard=[
        ["📦 Отслеживаемые заказы"],
        ["➕ Добавить трек", "➖ Удалить трек"],
        ["ℹ️ Помощь"],
    ],
    resize_keyboard=True,
)

# --- tiny web server (Render wants an open port for Web Service) ---
app = Flask(__name__)


@app.get("/")
def home():
    return "ok", 200


# --- helpers ---
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


def tg_send(text: str) -> None:
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )


async def ozon_get_status(track: str) -> str:
    url = f"https://tracking.ozon.ru/?track={track}&__rr=1"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=60000)
        body_text = await page.inner_text("body")
        await browser.close()

    text = " ".join(body_text.split()).lower()
    candidates = [
        "доставлено",
        "готово к выдаче",
        "на пункте выдачи",
        "в пути",
        "прибыло",
        "передано",
        "получено",
        "ожидает",
        "отправлено",
    ]
    for c in candidates:
        if c in text:
            return c
    return "unknown"


# --- commands ---
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return
    await update.message.reply_text(
        "Я отслеживаю статусы Ozon-треков.\n"
        "Кидай ссылку tracking.ozon.ru/?track=... или жми кнопки ниже.",
        reply_markup=MENU,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return
    await update.message.reply_text(
        "Команды и кнопки:\n"
        "• 📦 Отслеживаемые заказы — список треков и статусов\n"
        "• ➕ Добавить трек — пришли ссылку tracking.ozon.ru/?track=...\n"
        "• ➖ Удалить трек — пришли номер трека (пример: 94044975-0220-1)\n",
        reply_markup=MENU,
    )


# --- menu handler ---
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return

    text = (update.message.text or "").strip()

    # Если это ссылка с track= — не трогаем, обработает handle_message
    if TRACK_RE.search(text):
        return

    if text == "📦 Отслеживаемые заказы":
        tracks = load_tracks()
        if not tracks:
            await update.message.reply_text("Пока нет отслеживаемых заказов.", reply_markup=MENU)
            return

        lines = ["📦 Отслеживаемые заказы:"]
        for tr, info in tracks.items():
            st = info.get("status") or "—"
            lines.append(f"• {tr} — {st}")
        await update.message.reply_text("\n".join(lines), reply_markup=MENU)
        return

    if text == "➕ Добавить трек":
        await update.message.reply_text(
            "Пришли ссылку вида:\nhttps://tracking.ozon.ru/?track=94044975-0220-1",
            reply_markup=MENU,
        )
        return

    if text == "➖ Удалить трек":
        context.user_data["awaiting_delete"] = True
        await update.message.reply_text(
            "Ок. Пришли номер трека, который удалить (пример: 94044975-0220-1).",
            reply_markup=MENU,
        )
        return

    if text == "ℹ️ Помощь":
        await cmd_help(update, context)
        return

    # Режим удаления
    if context.user_data.get("awaiting_delete"):
        context.user_data["awaiting_delete"] = False
        track = re.sub(r"\s+", "", text)

        tracks = load_tracks()
        if track in tracks:
            del tracks[track]
            save_tracks(tracks)
            await update.message.reply_text(f"➖ Удалил: {track}", reply_markup=MENU)
        else:
            await update.message.reply_text(f"Не нашёл в списке: {track}", reply_markup=MENU)
        return

    await update.message.reply_text(
        "Жми кнопки или кидай ссылку tracking.ozon.ru/?track=...",
        reply_markup=MENU,
    )


# --- link handler (adds tracking) ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return

    text = (update.message.text or "").strip()
    m = TRACK_RE.search(text)
    if not m:
        return

    track = m.group(1)
    tracks = load_tracks()

    if track in tracks:
        await update.message.reply_text(f"Уже отслеживается: {track}", reply_markup=MENU)
        return

    tracks[track] = {"status": None}
    save_tracks(tracks)
    await update.message.reply_text(f"✅ Добавил трек: {track}", reply_markup=MENU)


async def watcher_loop():
    # первичная инициализация без спама
    tracks = load_tracks()
    changed = False
    for track, info in tracks.items():
        if info.get("status") is None:
            try:
                info["status"] = await ozon_get_status(track)
                changed = True
            except Exception:
                pass
    if changed:
        save_tracks(tracks)

    # Одно приветствие на старт процесса
    tg_send("🤖 Бот запущен. Кидай ссылки tracking.ozon.ru/?track=...")

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

            except Exception:
                continue

        if updated:
            save_tracks(tracks)

        await asyncio.sleep(POLL_SECONDS)


def run_bot() -> None:
    """
    Запускает Telegram polling.
    python-telegram-bot сам управляет event loop внутри run_polling().
    """
    tg_app = ApplicationBuilder().token(BOT_TOKEN).build()

    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("help", cmd_help))

    # Сначала меню, потом ссылки
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def post_init(app):
        app.create_task(watcher_loop())

    tg_app.post_init = post_init

    tg_app.run_polling()


if __name__ == "__main__":
    run_bot()
