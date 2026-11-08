"""Забирает графики ДТЕК Одеські електромережі через реальный браузер (Imperva/hCaptcha).

Страница https://www.dtek-oem.com.ua/ua/shutdowns содержит JS-объект
window.DisconSchedule с полями:
  preset — недельный план:  data[GPVx.y][weekday 1..7][hour 1..24] = статус
  fact   — подтверждённый:  data[unix_day][GPVx.y][hour 1..24] = статус, update, today
Статусы часа: yes | no | first | second | maybe | mfirst | msecond
"""
import asyncio
from datetime import datetime, timezone, timedelta
from playwright.async_api import async_playwright

URL = "https://www.dtek-oem.com.ua/ua/shutdowns"
KYIV = timezone(timedelta(hours=2))  # дата дня берётся из unix-метки ДТЕК, сдвиг только для строки

# час → две получасовые ячейки: 1 = свет есть, 0 = нет, 2 = возможно нет
HOUR_MAP = {
    "yes": (1, 1), "no": (0, 0),
    "first": (0, 1), "second": (1, 0),
    "maybe": (2, 2), "mfirst": (2, 1), "msecond": (1, 2),
}


def hours_to_slots(hours: dict) -> list[int]:
    slots = []
    for h in range(1, 25):
        slots.extend(HOUR_MAP.get(str(hours.get(str(h), "yes")), (1, 1)))
    return slots


def norm_group(key: str) -> str:
    return key.replace("GPV", "").strip()


async def fetch_raw(headless: bool = True, timeout_ms: int = 90000) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            locale="uk-UA",
            timezone_id="Europe/Kyiv",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
        )
        page = await ctx.new_page()
        await page.goto(URL, wait_until="domcontentloaded", timeout=timeout_ms)
        # ждём, пока WAF пропустит и объект появится
        await page.wait_for_function(
            "() => window.DisconSchedule && window.DisconSchedule.fact",
            timeout=timeout_ms,
        )
        raw = await page.evaluate(
            "() => JSON.parse(JSON.stringify({preset: DisconSchedule.preset, fact: DisconSchedule.fact}))"
        )
        await browser.close()
        return raw


def normalize(raw: dict) -> dict:
    fact = raw.get("fact") or {}
    preset = raw.get("preset") or {}
    groups: dict[str, dict[str, list[int]]] = {}

    for day_ts, by_group in (fact.get("data") or {}).items():
        day = datetime.fromtimestamp(int(day_ts), KYIV).strftime("%Y-%m-%d")
        for gkey, hours in by_group.items():
            groups.setdefault(norm_group(gkey), {})[day] = hours_to_slots(hours)

    week: dict[str, dict[str, list[int]]] = {}
    for gkey, days in (preset.get("data") or {}).items():
        week[norm_group(gkey)] = {str(wd): hours_to_slots(h) for wd, h in days.items()}

    return {
        "dtek_update": fact.get("update"),
        "groups": dict(sorted(groups.items())),
        "week_plan": dict(sorted(week.items())),
    }


def fetch(headless: bool = True) -> dict:
    return normalize(asyncio.run(fetch_raw(headless)))
