"""Ночной обход адресов ДТЕК Одеські електромережі: улица → дом → группа.

Открывает страницу графиков в реальном браузере (проходит Imperva), берёт список улиц
из DisconSchedule.streets и для каждой улицы делает тот же AJAX-запрос, что и форма
на сайте (method=getHomeNum). Запросы идут через fetch() внутри страницы, поэтому
у них есть все куки WAF и CSRF-токен.

Выход (stdout): {"updated", "dtek_update", "city", "partial",
                  "streets": {"<улица>": {"<дом>": "3.1" | "3.1,4.2"}}}
Диагностика — в stderr (видно в логе Actions).
"""
import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone

from playwright.async_api import async_playwright

from dtek import URL, norm_group

CITY_HINT = "Одес"
CONCURRENCY = 3
PAUSE = 0.25          # пауза после каждого запроса, сек
REOPEN_AFTER = 20     # столько ошибок подряд — заново открыть страницу (новые куки WAF)

JS_META = """() => {
  const f = DisconSchedule.fact || {};
  const m = document.querySelector('meta[name="csrf-token"]');
  return {
    streets: JSON.parse(JSON.stringify(DisconSchedule.streets || null)),
    update: f.update || '',
    csrf: m ? m.content : ''
  };
}"""

JS_HOUSES = """async ({city, street, update, csrf}) => {
  const b = new URLSearchParams();
  let i = 0;
  b.append('method', 'getHomeNum');
  if (city) { b.append(`data[${i}][name]`, 'city'); b.append(`data[${i}][value]`, city); i++; }
  b.append(`data[${i}][name]`, 'street'); b.append(`data[${i}][value]`, street); i++;
  if (update) { b.append(`data[${i}][name]`, 'updateFact'); b.append(`data[${i}][value]`, update); }
  const r = await fetch('/ua/ajax', {
    method: 'POST', credentials: 'same-origin',
    headers: {
      'X-CSRF-Token': csrf,
      'X-Requested-With': 'XMLHttpRequest',
      'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
    },
    body: b.toString()
  });
  return {status: r.status, text: await r.text()};
}"""


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def pick_streets(raw):
    """streets бывает списком улиц, словарём улица→…, или словарём город→улицы."""
    if isinstance(raw, list):
        return None, [str(x) for x in raw]
    if not isinstance(raw, dict) or not raw:
        return None, []
    vals = list(raw.values())
    is_city_map = sum(isinstance(v, (list, dict)) and len(v) > 5 for v in vals) > len(vals) / 2
    if not is_city_map:
        return None, [str(k) for k in raw]
    keys = sorted((k for k in raw if CITY_HINT in k), key=lambda k: (not k.startswith("м."), len(k)))
    if not keys:
        log("город с 'Одес' не найден, ключи:", list(raw)[:20])
        return None, []
    v = raw[keys[0]]
    return keys[0], [str(x) for x in (v.keys() if isinstance(v, dict) else v)]


def parse(text):
    """None — ответ не тот (WAF/ошибка), {} — улица без домов, иначе дом→группы."""
    try:
        j = json.loads(text)
    except ValueError:
        return None
    if not isinstance(j, dict):
        return None
    data = j.get("data")
    if not isinstance(data, dict):
        return {} if "result" in j else None
    out = {}
    for house, info in data.items():
        if not isinstance(info, dict):
            continue
        reasons = info.get("sub_type_reason") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        gs = sorted({norm_group(str(r)) for r in reasons if str(r).startswith("GPV")})
        if gs:
            out[str(house).strip()] = ",".join(gs)
    return out


async def crawl(headless: bool, limit: int) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless, args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(
            locale="uk-UA", timezone_id="Europe/Kyiv",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"))
        page = await ctx.new_page()

        async def open_page():
            await page.goto(URL, wait_until="domcontentloaded", timeout=90000)
            try:
                await page.wait_for_function(
                    "() => typeof DisconSchedule !== 'undefined' && DisconSchedule.fact", timeout=90000)
            except Exception:
                raise RuntimeError(f"нет DisconSchedule. title={await page.title()!r}")
            return await page.evaluate(JS_META)

        meta = await open_page()
        raw = meta["streets"]
        city, streets = pick_streets(raw)
        log(f"streets: тип={type(raw).__name__} город={city!r} улиц={len(streets)} "
            f"пример={streets[:3]} update={meta['update']!r} csrf={'да' if meta['csrf'] else 'нет'}")
        if not streets:
            log("сырой streets (начало):", json.dumps(raw, ensure_ascii=False)[:1500])
            raise RuntimeError("список улиц пуст")
        if limit:
            streets = streets[:limit]

        args = lambda s: {"city": city, "street": s, "update": meta["update"], "csrf": meta["csrf"]}

        # пробный запрос — сырой ответ в лог, чтобы видеть формат
        probe = await page.evaluate(JS_HOUSES, args(streets[0]))
        log(f"проба {streets[0]!r}: HTTP {probe['status']} {probe['text'][:600]}")

        result, fails = {}, []
        state = {"bad": 0, "done": 0}
        sem, reopen_lock = asyncio.Semaphore(CONCURRENCY), asyncio.Lock()
        t0 = time.time()

        async def one(street):
            for attempt in range(4):
                async with sem:
                    try:
                        r = await page.evaluate(JS_HOUSES, args(street))
                    except Exception as e:
                        r = {"status": 0, "text": str(e)}
                    await asyncio.sleep(PAUSE)
                houses = parse(r["text"]) if r["status"] == 200 else None
                if houses is not None:
                    state["bad"] = 0
                    if houses:
                        result[street] = houses
                    break
                state["bad"] += 1
                if attempt == 0:
                    log(f"ошибка {street!r}: HTTP {r['status']} {r['text'][:200]}")
                if state["bad"] >= REOPEN_AFTER:
                    async with reopen_lock:
                        if state["bad"] >= REOPEN_AFTER:
                            log("много ошибок подряд — открываю страницу заново")
                            await asyncio.sleep(30)
                            m = await open_page()
                            meta["csrf"], meta["update"] = m["csrf"], m["update"]
                            state["bad"] = 0
                await asyncio.sleep(5 * (attempt + 1))
            else:
                fails.append(street)
            state["done"] += 1
            if state["done"] % 100 == 0:
                log(f"{state['done']}/{len(streets)} улиц, {time.time() - t0:.0f} c")

        await asyncio.gather(*(one(s) for s in streets))
        await browser.close()

    houses = sum(len(v) for v in result.values())
    log(f"готово: улиц с домами {len(result)}, домов {houses}, неудач {len(fails)} {fails[:10]}")
    if not houses:
        raise RuntimeError("ни одного дома — формат ответа не распознан, см. «проба» выше")
    return {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dtek_update": meta["update"],
        "city": city,
        "partial": bool(limit) or len(fails) > len(streets) * 0.1,
        "failed": fails,
        "streets": dict(sorted(result.items())),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="сколько улиц (0 = все)")
    a = ap.parse_args()
    data = asyncio.run(crawl(headless=not a.headed, limit=a.limit))
    json.dump(data, sys.stdout, ensure_ascii=False, separators=(",", ":"))
