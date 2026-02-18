import asyncio
import json
import os
import requests
from playwright.async_api import async_playwright

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

TRACK = os.environ.get("OZON_TRACK", "94044975-0210-1")
URL = f"https://tracking.ozon.ru/?track={TRACK}&__rr=1"

STATE_FILE = "state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_status": None}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def tg_send(text: str):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=20
    )
    r.raise_for_status()

async def ozon_status():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(URL, wait_until="networkidle", timeout=60000)
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

async def main():
    state = load_state()

    # Первый старт: запоминаем статус и не спамим
    if state["last_status"] is None:
        try:
            state["last_status"] = await ozon_status()
            save_state(state)
        except Exception:
            pass
        tg_send(f"Бот запущен ✅ Слежу за Ozon треком {TRACK}.")
    else:
        tg_send("Бот перезапущен ✅ Продолжаю слежение.")

    while True:
        try:
            status = await ozon_status()
            prev = state["last_status"]
            if status != "unknown" and prev != status:
                tg_send(f"Ozon {TRACK}: {prev} → {status}")
                state["last_status"] = status
                save_state(state)
        except Exception:
            # без спама ошибками в чат
            pass

        await asyncio.sleep(300)  # 5 минут

if __name__ == "__main__":
    asyncio.run(main())
