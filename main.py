import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests
from flask import Flask
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

# =========================
# ENV
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = str(os.environ["CHAT_ID"])  # чат, куда бот будет отвечать (только тебе)
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "600"))  # 10 минут
PORT = int(os.environ.get("PORT", "10000"))

STATE_FILE = Path("tracks.json")

# Ссылка или просто номер трека
TRACK_RE = re.compile(r"(?:[?&]track=)?(\d[\d\-]{6,})")

# Чтобы Render-рестарты не спамили "бот запущен"
STARTUP_COOLDOWN_SECONDS = int(os.environ.get("STARTUP_COOLDOWN_SECONDS", "1800"))  # 30 мин

# =========================
# Flask (Render Web Service ждёт открытый порт)
# =========================
app = Flask(__name__)

@app.get("/")
def home():
    return "ok", 200


# =========================
# UI (кнопки)
# =========================
BTN_ADD = "➕ Добавить трек"
BTN_LIST = "📦 Отслеживаемые"
BTN_REMOVE = "➖ Удалить трек"
BTN_HELP = "ℹ️ Помощь"

MAIN_KB = ReplyKeyboardMarkup(
    [[BTN_ADD, BTN_LIST], [BTN_REMOVE, BTN_HELP]],
    resize_keyboard=True,
)

# =========================
# State
# =========================
def load_state() -> Dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"tracks": {}, "meta": {}}
    return {"tracks": {}, "meta": {}}

def save_state(state: Dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def get_tracks(state: Dict) -> Dict[str, Dict]:
    return state.setdefault("tracks", {})

def tg_send(text: str) -> None:
    # Отправка “вне контекста” (для JobQueue)
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )

# =========================
# Ozon parsing
# =========================
# Список статусов (расширенный, из твоих скринов + частые)
STATUS_CANDIDATES = [
    # верхние/синие
    "создан",
    "передается в доставку",
    "передаётся в доставку",
    "в пути",
    "заказ принят перевозчиком",

    # серые этапы
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
    "заказ везут",  # общий
    "заказ передали в курьерскую доставку",

    # финалы/пункты
    "готово к выдаче",
    "на пункте выдачи",
    "прибыло",
    "передано",
    "получено",
    "доставлено",
    "заказ успешно доставлен получателю",

    # ещё частые формулировки
    "отправлено",
    "ожидает",
]

BLOCKED_HINTS = [
    "частный доступ",
    "access denied",
    "forbidden",
    "доступ ограничен",
    "bot",
    "captcha",
    "verify",
    "enable javascript",
]

def normalize_text(s: str) -> str:
    s = " ".join(s.split()).strip().lower()
    # иногда "ё" мешает
    s = s.replace("ё", "е")
    return s

async def ozon_get_status(track: str) -> Tuple[str, str]:
    """
    Returns (status, debug_reason)
    status: one of STATUS_CANDIDATES or "unknown" or "blocked"
    debug_reason: short reason for logs/user
    """
    url = f"https://tracking.ozon.ru/?track={track}&__rr=1"

    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = await browser.new_context(
                user_agent=user_agent,
                locale="ru-RU",
                timezone_id="Europe/Moscow",
                viewport={"width": 1280, "height": 800},
            )
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # Дадим JS шанс догрузить данные
            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except PlaywrightTimeoutError:
                pass

            # Иногда полезно подождать чуть-чуть
            await page.wait_for_timeout(1500)

            body_text = await page.inner_text("body")
            title = await page.title()

            await context.close()
            await browser.close()

        text = normalize_text(body_text)
        title_n = normalize_text(title)

        # антибот/заглушка
        for h in BLOCKED_HINTS:
            if h in text or h in title_n:
                return ("blocked", f"blocked: {h}")

        # пытаемся найти любой статус
        for c in STATUS_CANDIDATES:
            if normalize_text(c) in text:
                return (c, "ok")

        # иногда статус есть, но в другом регистре/с переносами — уже нормализовали
        return ("unknown", "no candidates matched")

    except Exception as e:
        return ("unknown", f"error: {type(e).__name__}")


# =========================
# Bot logic
# =========================
MODE_NONE = "none"
MODE_ADD = "add"
MODE_REMOVE = "remove"

def only_me(update: Update) -> bool:
    return str(update.effective_chat.id) == CHAT_ID

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not only_me(update):
        return
    await update.message.reply_text(
        "🤖 Бот запущен.\n"
        "Жми кнопки снизу или присылай ссылку/трек вида:\n"
        "https://tracking.ozon.ru/?track=94044975-0220-1\n"
        "или просто: 94044975-0220-1",
        reply_markup=MAIN_KB,
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not only_me(update):
        return
    await update.message.reply_text(
        "ℹ️ Помощь:\n"
        f"• «{BTN_ADD}» — добавить трек\n"
        f"• «{BTN_LIST}» — показать список\n"
        f"• «{BTN_REMOVE}» — удалить трек\n\n"
        f"Опрос статусов раз в {POLL_SECONDS//60} мин.\n"
        "Можно присылать ссылку tracking.ozon.ru/?track=... или просто номер.",
        reply_markup=MAIN_KB,
    )

async def show_tracks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    tracks = get_tracks(state)

    if not tracks:
        await update.message.reply_text("📦 Пока нет отслеживаемых треков.", reply_markup=MAIN_KB)
        return

    lines = ["📦 Отслеживаемые треки:"]
    for t, info in tracks.items():
        st = info.get("status") or "unknown"
        lines.append(f"• {t} — {st}")
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KB)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not only_me(update):
        return

    text = (update.message.text or "").strip()
    mode = context.user_data.get("mode", MODE_NONE)

    # кнопки
    if text == BTN_HELP:
        context.user_data["mode"] = MODE_NONE
        return await cmd_help(update, context)

    if text == BTN_LIST:
        context.user_data["mode"] = MODE_NONE
        return await show_tracks(update, context)

    if text == BTN_ADD:
        context.user_data["mode"] = MODE_ADD
        return await update.message.reply_text(
            "Пришли ссылку/трек вида:\n"
            "https://tracking.ozon.ru/?track=94044975-0220-1\n"
            "или просто 94044975-0220-1",
            reply_markup=MAIN_KB,
        )

    if text == BTN_REMOVE:
        context.user_data["mode"] = MODE_REMOVE
        return await update.message.reply_text(
            "Пришли номер трека, который удалить (например 94044975-0220-1).",
            reply_markup=MAIN_KB,
        )

    # режим удаления
    if mode == MODE_REMOVE:
        m = TRACK_RE.search(text)
        if not m:
            return await update.message.reply_text("Не вижу номер трека. Пришли его ещё раз.", reply_markup=MAIN_KB)

        track = m.group(1)
        state = load_state()
        tracks = get_tracks(state)

        if track not in tracks:
            context.user_data["mode"] = MODE_NONE
            return await update.message.reply_text("Такого трека нет в списке.", reply_markup=MAIN_KB)

        tracks.pop(track, None)
        save_state(state)
        context.user_data["mode"] = MODE_NONE
        return await update.message.reply_text(f"✅ Удалил трек: {track}", reply_markup=MAIN_KB)

    # добавление (или просто прислали трек без режима — тоже добавим)
    m = TRACK_RE.search(text)
    if not m:
        # если не трек и не кнопка — мягко подскажем
        return await update.message.reply_text("Я жду трек/ссылку tracking.ozon.ru/?track=... или кнопки снизу 🙂", reply_markup=MAIN_KB)

    track = m.group(1)
    state = load_state()
    tracks = get_tracks(state)

    if track in tracks:
        context.user_data["mode"] = MODE_NONE
        return await update.message.reply_text(f"Уже отслеживается: {track}", reply_markup=MAIN_KB)

    tracks[track] = {"status": None, "added_at": int(time.time())}
    save_state(state)

    context.user_data["mode"] = MODE_NONE
    await update.message.reply_text(f"✅ Добавил трек: {track}", reply_markup=MAIN_KB)

    # сразу попробуем получить статус один раз (чтобы не ждать 10 минут)
    await update.message.reply_text("⏳ Проверяю статус…", reply_markup=MAIN_KB)
    status, reason = await ozon_get_status(track)

    # сохраним
    state = load_state()
    tracks = get_tracks(state)
    if track in tracks:
        tracks[track]["status"] = status
        tracks[track]["last_check_reason"] = reason
        tracks[track]["last_check_at"] = int(time.time())
        save_state(state)

    if status == "blocked":
        await update.message.reply_text(
            "⚠️ Ozon не отдал страницу боту (похоже на антибот/«частный доступ»).\n"
            f"Причина: {reason}\n"
            "Я всё равно буду пробовать дальше по расписанию.",
            reply_markup=MAIN_KB,
        )
    elif status == "unknown":
        await update.message.reply_text(
            "🤷 Пока не смог вытащить статус (unknown).\n"
            f"Причина: {reason}\n"
            "Я буду пробовать дальше по расписанию.",
            reply_markup=MAIN_KB,
        )
    else:
        await update.message.reply_text(f"📦 Статус сейчас: {status}", reply_markup=MAIN_KB)


# =========================
# Periodic checker (JobQueue)
# =========================
async def check_all_tracks(context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    tracks = get_tracks(state)
    if not tracks:
        return

    changed_any = False

    for track, info in list(tracks.items()):
        old = info.get("status")
        status, reason = await ozon_get_status(track)

        info["last_check_reason"] = reason
        info["last_check_at"] = int(time.time())

        # Если blocked/unknown — просто сохраняем, но не спамим
        if status in ("blocked", "unknown"):
            info["status"] = status
            changed_any = True
            continue

        # нормальный статус
        if old is None:
            info["status"] = status
            changed_any = True
        elif old != status:
            info["status"] = status
            changed_any = True
            tg_send(f"📦 {track}: {old} → {status}")

    if changed_any:
        save_state(state)


def maybe_send_startup_message():
    """
    Чтобы Render не спамил "бот запущен" при рестартах.
    """
    state = load_state()
    meta = state.setdefault("meta", {})
    last = int(meta.get("last_startup_notify", 0))
    now = int(time.time())

    if now - last >= STARTUP_COOLDOWN_SECONDS:
        tg_send("🤖 Бот запущен. Жми кнопки или кидай трек/ссылку tracking.ozon.ru/?track=...")
        meta["last_startup_notify"] = now
        save_state(state)


def run_bot() -> None:
    """
    Запуск Telegram polling.
    Это вызывай из bot_runner.py (или локально python main.py).
    """
    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()

    app_tg.add_handler(CommandHandler("start", cmd_start))
    app_tg.add_handler(CommandHandler("help", cmd_help))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # планировщик
    app_tg.job_queue.run_repeating(check_all_tracks, interval=POLL_SECONDS, first=10)

    maybe_send_startup_message()
    app_tg.run_polling()


if __name__ == "__main__":
    run_bot()
