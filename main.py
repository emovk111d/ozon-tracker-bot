import asyncio
import json
import os
import re
import requests
from pathlib import Path
from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = str(os.environ["CHAT_ID"])  # строкой, чтобы сравнивать без сюрпризов

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))  # 5 минут
DATA_DIR = Path(os.environ.get("DATA_DIR", "."))  # на Render лучше /var/data (см. ниже)
STATE_FILE = DATA_DIR / "tracks.json"

TRACK_RE = re.compile(r"[?&]track=([\d\-]+)")

def load_tracks() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_tracks(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

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

    # Достаточно “крупных” статусов. Можно расширить позже.
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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1) Принимаем сообщения только от тебя
    if str(update.effective_chat.id) != CHAT_ID:
        return

    text = (update.message.text or "").strip()
    m = TRACK_RE.search(text)
    if not m:
        return

    track = m.group(1)
    tracks = load_tracks()

    if track in tracks:
        await update.message.reply_text(f"Уже отслеживается: {track}")
        return

    tracks[track] = {"status": None}
    save_tracks(tracks)
    await update.message.reply_text(f"✅ Добавил трек: {track}")

async def watcher_loop():
    # Первый запуск: не спамим — просто фиксируем текущие статусы
    tracks = load_tracks()
    changed_any = False
    for track, info in tracks.items():
        if info.get("status") is None:
            try:
                info["status"] = await ozon_get_status(track)
                changed_any = True
            except Exception:
                pass
    if changed_any:
        save_tracks(tracks)

    tg_send("🤖 Бот запущен и следит за Ozon-треками. Кидай ссылки tracking.ozon.ru")

    # Основной цикл
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
                # молча переживаем временные ошибки сети/страницы
                continue

        if updated:
            save_tracks(tracks)

        await asyncio.sleep(POLL_SECONDS)

async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # запускаем watcher параллельно с polling
    asyncio.create_task(watcher_loop())
    await app.run_polling(close_loop=False)

if __name__ == "__main__":
    asyncio.run(main())
