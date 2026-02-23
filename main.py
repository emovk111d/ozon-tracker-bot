import json
import os
import re
import time
from pathlib import Path
from typing import Dict, Tuple

import requests
from flask import Flask
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================
# ENV
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = str(os.environ.get("CHAT_ID", "")).strip()  # опционально: если задан — бот отвечает только в этот чат
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
BTN_LIST = "📦 Мои посылки"
BTN_REMOVE = "➖ Удалить трек"
BTN_CHECK = "🔄 Проверить сейчас"
BTN_HELP = "ℹ️ Помощь"

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(BTN_ADD, callback_data="add")],
            [InlineKeyboardButton(BTN_LIST, callback_data="list")],
            [InlineKeyboardButton(BTN_CHECK, callback_data="check_now")],
            [InlineKeyboardButton(BTN_REMOVE, callback_data="remove")],
            [InlineKeyboardButton(BTN_HELP, callback_data="help")],
        ]
    )

# =========================
# State
# =========================
def load_state() -> Dict:
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return migrate_state(state)
        except Exception:
            return {"tracks": {}, "meta": {}}
    return {"tracks": {}, "meta": {}}

def save_state(state: Dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def migrate_state(state: Dict) -> Dict:
    """Backward compatibility:
    - Old format: {"tracks": {"TRACK": {info}}, "meta": {...}} (single user, implied CHAT_ID)
    - New format: {"tracks": {"<chat_id>": {"TRACK": {info}}}, "meta": {...}}
    """
    tracks = state.get("tracks", {})
    # If keys look like track numbers rather than chat ids, wrap under CHAT_ID.
    if tracks and all(isinstance(k, str) and TRACK_RE.fullmatch(k) for k in tracks.keys()):
        wrapped_chat = CHAT_ID or "__legacy__"
        state["tracks"] = {wrapped_chat: tracks}
    state.setdefault("tracks", {})
    state.setdefault("meta", {})
    return state

def get_user_tracks(state: Dict, chat_id: str) -> Dict[str, Dict]:
    return state.setdefault("tracks", {}).setdefault(chat_id, {})

def tg_send(chat_id: str, text: str) -> None:
    # Отправка “вне контекста” (для JobQueue)
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text},
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

async def ozon_get_statuses(tracks: list[str]) -> Dict[str, Tuple[str, str]]:
    """Fetch statuses in one browser session (fast).

    Returns {track: (status, debug_reason)}
    status: one of STATUS_CANDIDATES or "unknown" or "blocked"
    """
    results: Dict[str, Tuple[str, str]] = {}

    if not tracks:
        return results

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

            for track in tracks:
                url = f"https://tracking.ozon.ru/?track={track}&__rr=1"
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=60000)

                    # Дадим JS шанс догрузить данные
                    try:
                        await page.wait_for_load_state("networkidle", timeout=30000)
                    except PlaywrightTimeoutError:
                        pass

                    await page.wait_for_timeout(800)
                    body_text = await page.inner_text("body")
                    title = await page.title()

                    text = normalize_text(body_text)
                    title_n = normalize_text(title)

                    # антибот/заглушка
                    blocked = None
                    for h in BLOCKED_HINTS:
                        if h in text or h in title_n:
                            blocked = h
                            break
                    if blocked:
                        results[track] = ("blocked", f"blocked: {blocked}")
                        continue

                    # пытаемся найти любой статус
                    found = None
                    for c in STATUS_CANDIDATES:
                        if normalize_text(c) in text:
                            found = c
                            break
                    if found:
                        results[track] = (found, "ok")
                    else:
                        results[track] = ("unknown", "no candidates matched")

                except Exception as e:
                    results[track] = ("unknown", f"error: {type(e).__name__}")

            await context.close()
            await browser.close()

    except Exception as e:
        for track in tracks:
            results[track] = ("unknown", f"error: {type(e).__name__}")

    return results


# =========================
# Bot logic
# =========================
MODE_NONE = "none"
MODE_ADD = "add"
MODE_REMOVE = "remove"

def only_me(update: Update) -> bool:
    return (not CHAT_ID) or (str(update.effective_chat.id) == CHAT_ID)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not only_me(update):
        return
    await update.effective_message.reply_text(
        "🤖 Бот запущен.\n"
        "Жми кнопки снизу или присылай ссылку/трек вида:\n"
        "https://tracking.ozon.ru/?track=94044975-0220-1\n"
        "или просто: 94044975-0220-1",
        reply_markup=main_menu(),
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not only_me(update):
        return
    await update.effective_message.reply_text(
        "ℹ️ Помощь:\n"
        f"• «{BTN_ADD}» — добавить трек\n"
        f"• «{BTN_LIST}» — показать список\n"
        f"• «{BTN_CHECK}» — проверить вручную\n"
        f"• «{BTN_REMOVE}» — удалить трек\n\n"
        f"Опрос статусов раз в {POLL_SECONDS//60} мин.\n"
        "Можно присылать ссылку tracking.ozon.ru/?track=... или просто номер.",
        reply_markup=main_menu(),
    )

async def show_tracks(chat_id: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    tracks = get_user_tracks(state, chat_id)

    if not tracks:
        await update.effective_message.reply_text("📦 Пока нет отслеживаемых треков.", reply_markup=main_menu())
        return

    lines = ["📦 Отслеживаемые треки:"]
    for t, info in tracks.items():
        st = info.get("status") or "unknown"
        lines.append(f"• {t} — {st}")
    await update.effective_message.reply_text("\n".join(lines), reply_markup=main_menu())

def remove_menu(chat_id: str) -> InlineKeyboardMarkup:
    state = load_state()
    tracks = get_user_tracks(state, chat_id)
    rows = []
    for t in sorted(tracks.keys()):
        rows.append([InlineKeyboardButton(f"❌ {t}", callback_data=f"del:{t}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    if not only_me(update):
        await q.answer("Недоступно", show_alert=True)
        return
    await q.answer()

    chat_id = str(update.effective_chat.id)
    data = q.data or ""

    if data == "help":
        context.user_data["mode"] = MODE_NONE
        return await cmd_help(update, context)

    if data == "list":
        context.user_data["mode"] = MODE_NONE
        return await show_tracks(chat_id, update, context)

    if data == "add":
        context.user_data["mode"] = MODE_ADD
        return await q.message.reply_text(
            "Пришли ссылку/трек вида:\n"
            "https://tracking.ozon.ru/?track=94044975-0220-1\n"
            "или просто 94044975-0220-1",
            reply_markup=main_menu(),
        )

    if data == "remove":
        context.user_data["mode"] = MODE_REMOVE
        state = load_state()
        tracks = get_user_tracks(state, chat_id)
        if not tracks:
            context.user_data["mode"] = MODE_NONE
            return await q.message.reply_text("Удалять нечего — список пуст.", reply_markup=main_menu())
        return await q.message.reply_text("Выбери трек для удаления:", reply_markup=remove_menu(chat_id))

    if data.startswith("del:"):
        track = data.split(":", 1)[1]
        state = load_state()
        tracks = get_user_tracks(state, chat_id)
        if track in tracks:
            tracks.pop(track, None)
            save_state(state)
            await q.message.reply_text(f"✅ Удалил трек: {track}", reply_markup=main_menu())
        else:
            await q.message.reply_text("Такого трека нет в списке.", reply_markup=main_menu())
        context.user_data["mode"] = MODE_NONE
        return

    if data == "check_now":
        context.user_data["mode"] = MODE_NONE
        await q.message.reply_text("⏳ Проверяю…", reply_markup=main_menu())
        await check_user_tracks(chat_id)
        return await q.message.reply_text("Готово ✅", reply_markup=main_menu())

    if data == "back":
        context.user_data["mode"] = MODE_NONE
        return await q.message.reply_text("Ок.", reply_markup=main_menu())

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not only_me(update):
        return

    text = (update.message.text or "").strip()
    mode = context.user_data.get("mode", MODE_NONE)

    chat_id = str(update.effective_chat.id)

    # режим удаления
    if mode == MODE_REMOVE:
        m = TRACK_RE.search(text)
        if not m:
            return await update.message.reply_text("Не вижу номер трека. Пришли его ещё раз.", reply_markup=main_menu())

        track = m.group(1)
        state = load_state()
        tracks = get_user_tracks(state, chat_id)

        if track not in tracks:
            context.user_data["mode"] = MODE_NONE
            return await update.message.reply_text("Такого трека нет в списке.", reply_markup=main_menu())

        tracks.pop(track, None)
        save_state(state)
        context.user_data["mode"] = MODE_NONE
        return await update.message.reply_text(f"✅ Удалил трек: {track}", reply_markup=main_menu())

    # добавление (или просто прислали трек без режима — тоже добавим)
    m = TRACK_RE.search(text)
    if not m:
        # если не трек и не кнопка — мягко подскажем
        return await update.message.reply_text("Я жду трек/ссылку tracking.ozon.ru/?track=... или кнопки 🙂", reply_markup=main_menu())

    track = m.group(1)
    state = load_state()
    tracks = get_user_tracks(state, chat_id)

    if track in tracks:
        context.user_data["mode"] = MODE_NONE
        return await update.message.reply_text(f"Уже отслеживается: {track}", reply_markup=main_menu())

    tracks[track] = {"status": None, "added_at": int(time.time())}
    save_state(state)

    context.user_data["mode"] = MODE_NONE
    await update.message.reply_text(f"✅ Добавил трек: {track}", reply_markup=main_menu())

    # сразу попробуем получить статус один раз (чтобы не ждать 10 минут)
    await update.message.reply_text("⏳ Проверяю статус…", reply_markup=main_menu())
    status_map = await ozon_get_statuses([track])
    status, reason = status_map.get(track, ("unknown", "no result"))

    # сохраним
    state = load_state()
    tracks = get_user_tracks(state, chat_id)
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
            reply_markup=main_menu(),
        )
    elif status == "unknown":
        await update.message.reply_text(
            "🤷 Пока не смог вытащить статус (unknown).\n"
            f"Причина: {reason}\n"
            "Я буду пробовать дальше по расписанию.",
            reply_markup=main_menu(),
        )
    else:
        await update.message.reply_text(f"📦 Статус сейчас: {status}", reply_markup=main_menu())


# =========================
# Periodic checker (JobQueue)
# =========================
async def check_all_tracks(context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    all_users = state.get("tracks", {})
    if not all_users:
        return

    changed_any = False

    flat: list[str] = []
    for _chat, tr in all_users.items():
        flat.extend(list(tr.keys()))
    uniq = list(dict.fromkeys(flat))
    status_map = await ozon_get_statuses(uniq)

    for chat_id, user_tracks in list(all_users.items()):
        for track, info in list(user_tracks.items()):
            old = info.get("status")
            status, reason = status_map.get(track, ("unknown", "no result"))

            info["last_check_reason"] = reason
            info["last_check_at"] = int(time.time())

            # Если blocked/unknown — просто сохраняем, но не спамим
            if status in ("blocked", "unknown"):
                if info.get("status") != status:
                    info["status"] = status
                    changed_any = True
                continue

            if old is None:
                info["status"] = status
                changed_any = True
            elif old != status:
                info["status"] = status
                changed_any = True
                tg_send(chat_id, f"📦 {track}: {old} → {status}")

    if changed_any:
        save_state(state)


async def check_user_tracks(chat_id: str) -> None:
    state = load_state()
    tracks = get_user_tracks(state, chat_id)
    if not tracks:
        return
    status_map = await ozon_get_statuses(list(tracks.keys()))
    changed = False
    for track, info in tracks.items():
        status, reason = status_map.get(track, ("unknown", "no result"))
        info["last_check_reason"] = reason
        info["last_check_at"] = int(time.time())
        if status not in ("blocked", "unknown") and info.get("status") not in (None, status):
            tg_send(chat_id, f"📦 {track}: {info.get('status')} → {status}")
        if info.get("status") != status:
            info["status"] = status
            changed = True
    if changed:
        save_state(state)


def maybe_send_startup_message():
    """
    Чтобы Render не спамил "бот запущен" при рестартах.
    """
    state = load_state()
    meta = state.setdefault("meta", {})
    last = int(meta.get("last_startup_notify", 0))
    now = int(time.time())

    if now - last >= STARTUP_COOLDOWN_SECONDS and CHAT_ID:
        tg_send(CHAT_ID, "🤖 Бот запущен. Жми кнопки или кидай трек/ссылку tracking.ozon.ru/?track=...")
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
    app_tg.add_handler(CallbackQueryHandler(on_button))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # планировщик
    app_tg.job_queue.run_repeating(check_all_tracks, interval=POLL_SECONDS, first=10)

    maybe_send_startup_message()
    app_tg.run_polling()


if __name__ == "__main__":
    run_bot()
