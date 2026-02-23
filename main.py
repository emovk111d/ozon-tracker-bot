import asyncio
import json
import re
import requests

NEXT_DATA_RE = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# расширенный список статусов (ты просила "всё")
STATUS_CANDIDATES = [
    "создан",
    "передается в доставку",
    "передаётся в доставку",
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
    "заказ успешно доставлен получателю",
    "получено",
]

def _requests_fetch_tracking_html(track: str) -> str:
    url = f"https://tracking.ozon.ru/?track={track}&__rr=1"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }
    r = requests.get(url, headers=headers, timeout=25)
    r.raise_for_status()
    return r.text

def _extract_status_from_next_data(html: str) -> str | None:
    m = NEXT_DATA_RE.search(html)
    if not m:
        return None

    raw = m.group(1)
    try:
        data = json.loads(raw)
    except Exception:
        return None

    # Мы не знаем точную структуру JSON, поэтому ищем строки-статусы "грубой силой"
    # (зато работает даже если структура поменяется)
    blob = json.dumps(data, ensure_ascii=False).lower()
    for s in STATUS_CANDIDATES:
        if s.lower() in blob:
            # вернём самый "поздний" найденный статус: пробежимся по списку в порядке приоритета
            # (ниже сделаем умнее, когда поймаем XHR)
            pass

    # лучше: ищем самый "продвинутый" статус по нашему списку — берём последний встретившийся
    found = None
    for s in STATUS_CANDIDATES:
        if s.lower() in blob:
            found = s
    return found

def _extract_status_from_html_text(html: str) -> str | None:
    text = " ".join(re.sub(r"<[^>]+>", " ", html).split()).lower()
    found = None
    for s in STATUS_CANDIDATES:
        if s.lower() in text:
            found = s
    return found

def ozon_get_status_sync(track: str) -> tuple[str, str | None]:
    """
    Возвращает (status, error)
    status: строка или 'unknown'
    error: строка ошибки или None
    """
    try:
        html = _requests_fetch_tracking_html(track)
    except Exception as e:
        return ("unknown", f"fetch_failed: {repr(e)}")

    status = _extract_status_from_next_data(html) or _extract_status_from_html_text(html)
    if status:
        return (status, None)

    # Если вообще ничего не нашли — возможно, отдали заглушку/бот-чек
    # (например, "частный доступ" / капча / пустая страница)
    hint = "no_status_found"
    if "частный доступ" in html.lower():
        hint = "private_access_text_seen"
    if "captcha" in html.lower():
        hint = "captcha_seen"
    return ("unknown", hint)

async def ozon_get_status(track: str) -> tuple[str, str | None]:
    # чтобы не блокировать event loop телеграм-бота
    return await asyncio.to_thread(ozon_get_status_sync, track)
