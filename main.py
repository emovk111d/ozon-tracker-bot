import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests
from flask import Flask
from playwright.async_api import async_playwright
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

# =========================
# ENV
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = str(os.environ["CHAT_ID"])  # твой chat_id строкой
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "600"))  # 10 минут
PORT = int(os.environ.get("PORT", "10000"))

STATE_FILE = Path("tracks.json")
META_FILE = Path("meta.json")

# Ловим и ссылки, и просто трек-номер
TRACK_RE = re.compile(r"(?:(?:\?|&)track=)?(\d{6,}-\d{4,}-\d{1,})", re.IGNORECASE)

# =========================
# Flask (Render Web Service требует порт)
# =========================
app = Flask(__name__)

@app.get("/")
def home():
    return "ok", 200


# =========================
# Storage helpers
# =========================
def load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def load_tracks() -> Dict[str, dict]:
    return load_json(STATE_FILE)

def save_tracks(data: Dict[str, dict]) -> None:
    save_json(STATE_FILE, data)

def load_meta() -> dict:
    return load_json(META_FILE)

def save_meta(data: dict) -> None:
    save_json(META_FILE, data)


# =========================
# Telegram send (в watcher)
# =========================
def tg_send(text: str) -> None:
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )


# =========================
# UI (кнопки)
# =========================
MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("➕ Добавить трек"), KeyboardButton("📦 Отслеживаемые")],
        [KeyboardButton("➖ Удалить трек"), KeyboardButton("ℹ️ Помощь")],
    ],
    resize_keyboard=True,
)

HELP_TEXT = (
    "Я слежу за публичным трекингом Ozon.\n\n"
    "• Нажми «➕ Добавить трек» и пришли ссылку вида:\n"
    "  https://tracking.ozon.ru/?track=94044975-0220-1\n"
    "  или просто трек-номер: 94044975-0220-1\n\n"
    "• «📦 Отслеживаемые» — список треков и текущих статусов\n"
    "• «➖ Удалить трек» — удаление по номеру\n\n"
    f"Опрос статусов раз в {POLL_SECONDS//60} мин."
)


# =========================
# Ozon parsing
# =========================
# ВАЖНО: тут должны быть реальные фразы из трекинга
STATUS_CANDIDATES = [
    # из твоего скрина / типовые
    "создан",
    "передается в доставку",
    "передаётся в доставку",
    "в пути",
    "заказ принят перевозчиком",
    "заказ принят перевозчиком",
    "готово к выдаче",
    "на пункте выдачи",
    "в пункте выдачи",
    "прибыло",
    "прибыл",
    "передано",
    "получено",
    "доставлено",
    "ожидает",
    "отправлено",
    "собран",
    "собирает",
]

def normalize_status(text: str) -> str:
    """Нормализуем разные формулировки к коротким статусам."""
    t = text.lower()
    mapping = [
        ("создан", "создан"),
        ("передается в доставку", "передается в доставку"),
        ("передаётся в доставку", "передается в доставку"),
        ("в пути", "в пути"),
        ("заказ принят перевозчиком", "принят перевозчиком"),
        ("готово к выдаче", "готово к выдаче"),
        ("на пункте выдачи", "на пункте выдачи"),
        ("в пункте выдачи", "на пункте выдачи"),
        ("доставлено", "доставлено"),
        ("получено", "получено"),
    ]
    for k, v in mapping:
        if k in t:
            return v
    return text

async def ozon_get_status(track: str) -> str:
    base_url = "https://tracking.ozon.ru"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(locale="ru-RU")
        page = await context.new_page()

        await page.goto(base_url, wait_until="domcontentloaded", timeout=60000)

        inp = page.locator("input").first
        await inp.wait_for(timeout=20000)
        await inp.fill(track)
        await inp.press("Enter")

        await page.wait_for_timeout(2500)  # чуть дольше, чем networkidle
        await page.wait_for_load_state("domcontentloaded")

        current_url = page.url
        title = await page.title()
        body_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        text = " ".join((body_text or "").split()).lower()

        await context.close()
        await browser.close()

    # 👇 это увидишь в Render Logs
    print("OZON DEBUG URL:", current_url)
    print("OZON DEBUG TITLE:", title)
    print("OZON DEBUG TEXT HEAD:", text[:800])

    # временно всегда unknown
    return "unknown"

    statuses = [
        "создан",
        "передается в доставку",
        "передаётся в доставку",
        "передан в доставку",
        "в пути",
        "заказ принят перевозчиком",
        "заказ везут на таможню",
        "заказ везут на таможню в стране отправления",
        "заказ везут на таможню в стране назначения",
        "заказ привезли на таможню",
        "заказ передан на импортное таможенное оформление",
        "заказ проходит импортное таможенное оформление",
        "заказ выпущен импортной таможней",
        "заказ отправили на сортировочный терминал",
        "заказ покинул сортировочный терминал",
        "заказ ожидает отправки в город получателя",
        "заказ везут в город получателя",
        "заказ везут",
        "заказ передали в курьерскую доставку",
        "на пункте выдачи",
        "готово к выдаче",
        "доставлено",
        "получено",
        "успешно доставлен",
    ]

    for s in statuses:
        if s in text:
            return s

    return "unknown"

# =========================
# Bot logic
# =========================
def is_my_chat(update: Update) -> bool:
    return str(update.effective_chat.id) == CHAT_ID

def get_flags(context: ContextTypes.DEFAULT_TYPE) -> dict:
    # user_data работает на одного пользователя в polling.
    return context.user_data.setdefault("flags", {})

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_my_chat(update):
        return
    await update.message.reply_text("Ок, я тут. Жми кнопки. 😼", reply_markup=MENU)
    await update.message.reply_text(HELP_TEXT)

async def show_tracks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_my_chat(update):
        return
    tracks = load_tracks()
    if not tracks:
        await update.message.reply_text("Пока пусто. Добавь трек через «➕ Добавить трек».", reply_markup=MENU)
        return

    lines = ["📦 Отслеживаемые треки:"]
    for t, info in tracks.items():
        st = info.get("status") or "unknown"
        lines.append(f"• {t} — {st}")
    await update.message.reply_text("\n".join(lines), reply_markup=MENU)

async def handle_menu_and_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_my_chat(update):
        return

    text = (update.message.text or "").strip()
    flags = get_flags(context)

    # --- menu clicks ---
    if text == "📦 Отслеживаемые":
        await show_tracks(update, context)
        return

    if text == "ℹ️ Помощь":
        await update.message.reply_text(HELP_TEXT, reply_markup=MENU)
        return

    if text == "➕ Добавить трек":
        flags["await_add"] = True
        flags.pop("await_remove", None)
        await update.message.reply_text(
            "Пришли ссылку вида:\nhttps://tracking.ozon.ru/?track=94044975-0220-1\nили просто трек-номер.",
            reply_markup=MENU,
        )
        return

    if text == "➖ Удалить трек":
        flags["await_remove"] = True
        flags.pop("await_add", None)
        await update.message.reply_text("Ок. Пришли трек-номер, который удалить.", reply_markup=MENU)
        return

    # --- expecting remove ---
    if flags.get("await_remove"):
        m = TRACK_RE.search(text)
        if not m:
            await update.message.reply_text("Это не похоже на трек-номер. Попробуй ещё раз.", reply_markup=MENU)
            return

        track = m.group(1)
        tracks = load_tracks()
        if track in tracks:
            tracks.pop(track, None)
            save_tracks(tracks)
            await update.message.reply_text(f"🗑️ Удалил: {track}", reply_markup=MENU)
        else:
            await update.message.reply_text(f"Не найдено в списке: {track}", reply_markup=MENU)

        flags["await_remove"] = False
        return

    # --- add by link/number (either direct, or after clicking menu) ---
    if flags.get("await_add") or "track=" in text or TRACK_RE.fullmatch(text) or TRACK_RE.search(text):
        m = TRACK_RE.search(text)
        if not m:
            await update.message.reply_text("Не вижу трек. Нужен трек-номер или ссылка с ?track=...", reply_markup=MENU)
            return

        track = m.group(1)
        tracks = load_tracks()

        if track in tracks:
            await update.message.reply_text(f"Уже отслеживается: {track}", reply_markup=MENU)
            flags["await_add"] = False
            return

        tracks[track] = {"status": None, "added_at": int(time.time())}
        save_tracks(tracks)
        await update.message.reply_text(f"✅ Добавил трек: {track}", reply_markup=MENU)
        flags["await_add"] = False
        return

    # default: ignore or gentle hint
    await update.message.reply_text("Жми кнопки 🙂", reply_markup=MENU)


# =========================
# Watcher loop
# =========================
def should_send_startup_ping() -> bool:
    """
    Render free-инстанс любит рестартиться.
    Чтобы не спамить “Бот запущен…”, шлём не чаще 1 раза в 6 часов.
    """
    meta = load_meta()
    last = int(meta.get("last_startup_ping", 0))
    now = int(time.time())
    if now - last >= 6 * 3600:
        meta["last_startup_ping"] = now
        save_meta(meta)
        return True
    return False

async def watcher_loop():
    # первичная инициализация статусов без спама в чат
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

    if should_send_startup_ping():
        tg_send("🤖 Бот запущен. Жми «➕ Добавить трек» или кидай ссылки tracking.ozon.ru/?track=...")

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


# =========================
# Entrypoint for bot_runner.py
# =========================
def run_bot() -> None:
    """
    Запускаем polling.
    watcher_loop стартуем через post_init (внутри loop приложения).
    """
    tg_app = ApplicationBuilder().token(BOT_TOKEN).build()

    tg_app.add_handler(CommandHandler("start", start_cmd))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_and_text))

    async def post_init(app_):
        app_.create_task(watcher_loop())

    tg_app.post_init = post_init

    tg_app.run_polling()


if __name__ == "__main__":
    run_bot()
