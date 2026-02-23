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
    MessageHandler,
    ContextTypes,
    filters,
)

# --- ENV ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = str(os.environ["CHAT_ID"])  # разрешённый чат (твой)
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "600"))  # 10 минут
PORT = int(os.environ.get("PORT", "10000"))

STATE_FILE = Path("tracks.json")
TRACK_RE = re.compile(r"[?&]track=([\d\-]+)")

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


# --- telegram helpers ---
def tg_send(text: str) -> None:
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )

def main_keyboard() -> ReplyKeyboardMarkup:
    # Редкая, но полезная фича: iOS иногда “съедает” эмодзи — поэтому тексты простые
    buttons = [
        ["➕ Добавить трек", "📦 Отслеживаемые"],
        ["➖ Удалить трек", "ℹ️ Помощь"],
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


# --- Ozon scraping ---
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


# --- bot handlers ---
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return
    await update.message.reply_text(
        "Привет. Я слежу за треками Ozon.\n"
        "Нажми «Добавить трек» или просто пришли ссылку вида:\n"
        "https://tracking.ozon.ru/?track=94044975-0220-1",
        reply_markup=main_keyboard(),
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return
    await update.message.reply_text(
        "Команды и кнопки:\n"
        "• «Добавить трек» — пришли ссылку tracking.ozon.ru/?track=...\n"
        "• «Отслеживаемые» — покажу список\n"
        "• «Удалить трек» — пришли номер трека (например 94044975-0220-1)\n\n"
        "Я проверяю статусы раз в POLL_SECONDS (по умолчанию 10 минут).",
        reply_markup=main_keyboard(),
    )

# режимы (простенький стейт)
MODE_ADD = "add"
MODE_DEL = "del"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """СНАЧАЛА пытаемся распознать трек-ссылку/трек, и только если не подходит — меню."""
    if str(update.effective_chat.id) != CHAT_ID:
        return

    text = (update.message.text or "").strip()

    # 1) Если мы в режиме удаления — ждём трек номер
    if context.user_data.get("mode") == MODE_DEL:
        # принимаем либо чистый трек, либо ссылку
        m = TRACK_RE.search(text)
        track = m.group(1) if m else text
        tracks = load_tracks()

        if track in tracks:
            tracks.pop(track, None)
            save_tracks(tracks)
            await update.message.reply_text(f"🗑 Удалил трек: {track}", reply_markup=main_keyboard())
        else:
            await update.message.reply_text("Не нашёл такой трек в отслеживании.", reply_markup=main_keyboard())

        context.user_data.pop("mode", None)
        return

    # 2) Если текст содержит ссылку — добавляем трек
    m = TRACK_RE.search(text)
    if m:
        track = m.group(1)
        tracks = load_tracks()

        if track in tracks:
            await update.message.reply_text(f"Уже отслеживается: {track}", reply_markup=main_keyboard())
            return

        tracks[track] = {"status": None}
        save_tracks(tracks)
        await update.message.reply_text(f"✅ Добавил трек: {track}", reply_markup=main_keyboard())
        return

    # 3) Иначе — отдаём в меню-обработчик
    await handle_menu(update, context)


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return

    text = (update.message.text or "").strip()
    t = text.lower()

    # “+ Добавить трек” / “➕ Добавить трек” / “добавить”
    if "добав" in t:
        context.user_data["mode"] = MODE_ADD  # чисто для семантики, но можно и не хранить
        await update.message.reply_text(
            "Пришли ссылку вида:\nhttps://tracking.ozon.ru/?track=94044975-0220-1",
            reply_markup=main_keyboard(),
        )
        return

    if "отслеж" in t or "заказы" in t or "трек" in t and "спис" in t:
        tracks = load_tracks()
        if not tracks:
            await update.message.reply_text("Пока пусто. Добавь трек.", reply_markup=main_keyboard())
            return

        lines = ["📦 Отслеживаемые треки:"]
        for trk, info in tracks.items():
            st = info.get("status")
            lines.append(f"• {trk} — {st if st else 'неизвестно'}")
        await update.message.reply_text("\n".join(lines), reply_markup=main_keyboard())
        return

    if "удал" in t or "убрат" in t:
        context.user_data["mode"] = MODE_DEL
        await update.message.reply_text(
            "Окей. Пришли номер трека (например: 94044975-0220-1) или ссылку tracking.ozon.ru/?track=...",
            reply_markup=main_keyboard(),
        )
        return

    if "помощ" in t or "help" in t:
        await cmd_help(update, context)
        return

    # Если вообще неизвестно что прислали
    await update.message.reply_text(
        "Я тебя слышу, но не понимаю. Жми кнопки или пришли ссылку tracking.ozon.ru/?track=...",
        reply_markup=main_keyboard(),
    )


# --- watcher loop ---
async def watcher_loop():
    """
    Следит за статусами треков и шлёт уведомления при изменении.
    """
    # 1) Первичная инициализация статусов без спама
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

    # 2) ОДНО стартовое сообщение за запуск процесса
    tg_send("🤖 Бот запущен. Жми «Добавить трек» или кидай ссылки tracking.ozon.ru/?track=...")

    # 3) Основной цикл
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
    Важно: НЕ asyncio.run() — run_polling сам управляет event loop.
    """
    tg_app = ApplicationBuilder().token(BOT_TOKEN).build()

    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("help", cmd_help))

    # Один “универсальный” обработчик: он сам решит — это ссылка или меню
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def post_init(app_):
        app_.create_task(watcher_loop())

    tg_app.post_init = post_init
    tg_app.run_polling()


if __name__ == "__main__":
    run_bot()
