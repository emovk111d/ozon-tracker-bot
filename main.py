import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional, Tuple

import requests
from flask import Flask
from telegram import ReplyKeyboardMarkup, KeyboardButton, Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# ------------------ ENV ------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = str(os.environ.get("CHAT_ID", "")).strip()  # можно пустым => бот будет отвечать всем
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "600"))
PORT = int(os.environ.get("PORT", "10000"))

STATE_FILE = Path("tracks.json")

TRACK_RE = re.compile(r"(?:(?:https?://)?tracking\.ozon\.ru/\?track=)?([\d\-]{6,})", re.I)

# ------------------ Flask app for Render ------------------
app = Flask(__name__)

@app.get("/")
def home():
    return "ok", 200


# ------------------ Storage ------------------
def load_tracks() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_tracks(data: dict) -> None:
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------ Telegram helpers ------------------
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Добавить трек"), KeyboardButton("📦 Отслеживаемые")],
            [KeyboardButton("➖ Удалить трек"), KeyboardButton("ℹ️ Помощь")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Кинь ссылку tracking.ozon.ru/?track=... или трек-номер",
    )

def allowed_chat(update: Update) -> bool:
    if not CHAT_ID:
        return True
    try:
        return str(update.effective_chat.id) == CHAT_ID
    except Exception:
        return False


# ------------------ Ozon status parsing ------------------
CANDIDATES = [
    # верхние "живые" статусы
    "доставлено",
    "готово к выдаче",
    "на пункте выдачи",
    "в пути",
    "передается в доставку",
    "передано в доставку",
    "создан",
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
    "заказ успешно доставлен получателю",
    # общие
    "прибыло",
    "передано",
    "получено",
    "ожидает",
    "отправлено",
]

def _find_next_data(html: str) -> Optional[dict]:
    """
    Ищем <script id="__NEXT_DATA__" type="application/json">...</script>
    """
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S | re.I)
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        return json.loads(raw)
    except Exception:
        return None

def _walk_strings(obj: Any):
    """
    Генератор всех строк внутри JSON/словарей/списков.
    """
    if obj is None:
        return
    if isinstance(obj, str):
        yield obj
        return
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
        return
    if isinstance(obj, list):
        for it in obj:
            yield from _walk_strings(it)
        return

def _best_status_from_text(text: str) -> str:
    t = " ".join(text.split()).lower()
    for c in CANDIDATES:
        if c in t:
            return c
    return "unknown"

def ozon_get_status_direct(track: str) -> Tuple[str, str]:
    """
    Возвращает (status, debug_reason)
    """
    url = f"https://tracking.ozon.ru/?track={track}&__rr=1"

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/122 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }

    r = requests.get(url, headers=headers, timeout=30)
    html = r.text or ""

    # 1) Пробуем Next.js данные
    next_data = _find_next_data(html)
    if next_data:
        joined = " ".join(s.lower() for s in _walk_strings(next_data) if isinstance(s, str))
        status = _best_status_from_text(joined)
        if status != "unknown":
            return status, "next_data"

    # 2) Фолбэк: просто по HTML/тексту страницы
    status = _best_status_from_text(html)
    if status != "unknown":
        return status, "html_text"

    # 3) Если вообще ничего
    return "unknown", f"http_{r.status_code}"

# ------------------ Bot logic ------------------
ADD_WAITING = 1
DEL_WAITING = 2

async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not allowed_chat(update):
        return
    await update.message.reply_text(
        "Я отслеживаю статусы заказов Ozon по публичному треку.\n\n"
        "Кнопки:\n"
        "• ➕ Добавить трек — пришли ссылку или номер\n"
        "• 📦 Отслеживаемые — список текущих\n"
        "• ➖ Удалить трек — удали по номеру\n\n"
        f"Опрос статусов раз в {POLL_SECONDS//60} мин.",
        reply_markup=main_menu_kb(),
        disable_web_page_preview=True,
    )

async def show_tracks(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not allowed_chat(update):
        return
    tracks = load_tracks()
    if not tracks:
        await update.message.reply_text("📦 Пока нет отслеживаемых треков.", reply_markup=main_menu_kb())
        return
    lines = ["📦 Отслеживаемые треки:"]
    for tr, info in tracks.items():
        st = info.get("status") or "unknown"
        lines.append(f"• {tr} — {st}")
    await update.message.reply_text("\n".join(lines), reply_markup=main_menu_kb())

async def start_add(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not allowed_chat(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "Пришли ссылку/трек вида:\n"
        "https://tracking.ozon.ru/?track=94044975-0220-1\n"
        "или просто 94044975-0220-1",
        reply_markup=main_menu_kb(),
        disable_web_page_preview=True,
    )
    return ADD_WAITING

async def add_track(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not allowed_chat(update):
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    m = TRACK_RE.search(text)
    if not m:
        await update.message.reply_text("Не вижу трек. Пришли номер вида 94044975-0220-1.")
        return ADD_WAITING

    track = m.group(1)
    tracks = load_tracks()

    if track in tracks:
        await update.message.reply_text(f"Уже отслеживается: {track}", reply_markup=main_menu_kb())
        return ConversationHandler.END

    tracks[track] = {"status": None, "last_checked": None}
    save_tracks(tracks)

    await update.message.reply_text(f"✅ Добавил трек: {track}\n⏳ Проверяю статус…", reply_markup=main_menu_kb())

    try:
        status, reason = ozon_get_status_direct(track)
        tracks = load_tracks()
        if track in tracks:
            tracks[track]["status"] = status
            tracks[track]["last_checked"] = int(time.time())
            save_tracks(tracks)

        if status == "unknown":
            await update.message.reply_text(
                f"🤷 Пока не смог вытащить статус (unknown).\nПричина: {reason}\n"
                "Я буду пробовать дальше по расписанию.",
                reply_markup=main_menu_kb(),
            )
        else:
            await update.message.reply_text(
                f"📦 {track}: {status} (источник: {reason})",
                reply_markup=main_menu_kb(),
            )
    except Exception as e:
        await update.message.reply_text(
            f"🤷 Ошибка при проверке: {type(e).__name__}: {e}\n"
            "Я буду пробовать дальше по расписанию.",
            reply_markup=main_menu_kb(),
        )

    return ConversationHandler.END

async def start_del(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not allowed_chat(update):
        return ConversationHandler.END
    await update.message.reply_text("Пришли номер трека, который удалить (например 94044975-0220-1).")
    return DEL_WAITING

async def del_track(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not allowed_chat(update):
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    m = TRACK_RE.search(text)
    if not m:
        await update.message.reply_text("Не вижу трек. Пришли номер вида 94044975-0220-1.")
        return DEL_WAITING

    track = m.group(1)
    tracks = load_tracks()

    if track not in tracks:
        await update.message.reply_text("Такого трека нет в списке.", reply_markup=main_menu_kb())
        return ConversationHandler.END

    del tracks[track]
    save_tracks(tracks)
    await update.message.reply_text(f"🗑 Удалил трек: {track}", reply_markup=main_menu_kb())
    return ConversationHandler.END

async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_chat(update):
        return

    text = (update.message.text or "").strip()

    if text == "📦 Отслеживаемые":
        await show_tracks(update, context)
        return

    if text == "ℹ️ Помощь":
        await cmd_help(update, context)
        return

    # Если человек просто кидает ссылку/номер без кнопки — считаем как "добавить"
    m = TRACK_RE.search(text)
    if m:
        # имитируем “добавление” без ConversationHandler
        await add_track(update, context)
        return

    await update.message.reply_text("Нажми кнопку или пришли ссылку/трек.", reply_markup=main_menu_kb())


# ------------------ Scheduler job ------------------
async def check_all_tracks(context: ContextTypes.DEFAULT_TYPE) -> None:
    tracks = load_tracks()
    if not tracks:
        return

    changed_any = False

    for tr, info in list(tracks.items()):
        old = info.get("status")

        try:
            new, reason = ozon_get_status_direct(tr)
        except Exception:
            continue

        tracks = load_tracks()
        if tr not in tracks:
            continue

        tracks[tr]["last_checked"] = int(time.time())

        if new != "unknown" and old and new != old:
            # уведомляем только при реальной смене
            await context.bot.send_message(chat_id=CHAT_ID or context._chat_id, text=f"📦 {tr}: {old} → {new}")
            tracks[tr]["status"] = new
            changed_any = True
        elif old is None and new != "unknown":
            tracks[tr]["status"] = new
            changed_any = True

        save_tracks(tracks)

    if changed_any:
        # на будущее: можно логировать/метрики
        pass


# ------------------ Bot runner (imported by bot_runner.py) ------------------
def run_bot() -> None:
    """
    Эту функцию вызывает bot_runner.py: from main import run_bot
    """
    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^➕ Добавить трек$"), start_add),
            MessageHandler(filters.Regex(r"^➖ Удалить трек$"), start_del),
        ],
        states={
            ADD_WAITING: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_track)],
            DEL_WAITING: [MessageHandler(filters.TEXT & ~filters.COMMAND, del_track)],
        },
        fallbacks=[MessageHandler(filters.Regex(r"^ℹ️ Помощь$"), cmd_help)],
        allow_reentry=True,
    )

    app_tg.add_handler(conv)
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))

    # стартовое сообщение при запуске
    async def post_init(app_):
        # job-queue должен быть установлен через python-telegram-bot[job-queue]
        if app_.job_queue:
            app_.job_queue.run_repeating(check_all_tracks, interval=POLL_SECONDS, first=10)

    app_tg.post_init = post_init

    # Важно: только ОДИН экземпляр polling должен работать
    app_tg.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run_bot()
