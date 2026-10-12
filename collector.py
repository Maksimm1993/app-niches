#!/usr/bin/env python3
"""
Поиск ниш iOS-приложений в App Store США.

Запускается раз в день из GitHub Actions. Менять здесь ничего не нужно:
настройки — в config.yaml, поисковые запросы — в keywords.txt.

Порядок работы за один запуск:
  1. Скачать чарты США (Free / Paid / Grossing) по всем категориям.
  2. Поиск по ключевым словам + расширение списка автоподсказками App Store.
  3. Обход «соседей»: другие приложения тех же разработчиков и «похожие» со страниц App Store.
  4. Замер всех отслеживаемых приложений через iTunes Lookup (число оценок и т.д.).
  5. Расчёт прироста оценок, оценка выручки, отбор кандидатов.
  6. Сохранение базы (папка state/) и данных для сайта (site/data.js).
"""

import csv
import datetime as dt
import html
import json
import math
import re
import statistics
import time
import traceback
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
SITE = ROOT / "site"
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

START = time.time()
TODAY = dt.datetime.now(dt.timezone.utc).date()
DAY0 = dt.date(2020, 1, 1)
TODAY_N = (TODAY - DAY0).days  # номер сегодняшнего дня (для истории)

SEARCH_URL = "https://itunes.apple.com/search"
LOOKUP_URL = "https://itunes.apple.com/lookup"
HINTS_URL = "https://search.itunes.apple.com/WebObjects/MZSearchHints.woa/wa/hints"
RSS_URL = "https://itunes.apple.com/us/rss/{feed}/limit=200{genre}/json"
PAGE_URL = "https://apps.apple.com/us/app/id{id}"

FEEDS = {
    "free": "topfreeapplications",
    "paid": "toppaidapplications",
    "grossing": "topgrossingapplications",
}

APP_GENRES = {
    6000: "Business", 6001: "Weather", 6002: "Utilities", 6003: "Travel",
    6004: "Sports", 6005: "Social Networking", 6006: "Reference",
    6007: "Productivity", 6008: "Photo & Video", 6009: "News",
    6010: "Navigation", 6011: "Music", 6012: "Lifestyle",
    6013: "Health & Fitness", 6014: "Games", 6015: "Finance",
    6016: "Entertainment", 6017: "Education", 6018: "Books",
    6020: "Medical", 6021: "Magazines & Newspapers", 6023: "Food & Drink",
    6024: "Shopping", 6026: "Developer Tools", 6027: "Graphics & Design",
}
GAME_GENRES = {
    7001: "Action", 7002: "Adventure", 7003: "Casual", 7004: "Board",
    7005: "Card", 7006: "Casino", 7009: "Family", 7011: "Music",
    7012: "Puzzle", 7013: "Racing", 7014: "Role Playing", 7015: "Simulation",
    7016: "Sports", 7017: "Strategy", 7018: "Trivia", 7019: "Word",
}


# ---------------------------------------------------------------------------
#  Вспомогательное
# ---------------------------------------------------------------------------

def log(*args):
    print(f"[{(time.time() - START) / 60:5.1f} мин]", *args, flush=True)


def minutes_left():
    return CFG["time_budget_minutes"] - (time.time() - START) / 60


def days_since(date_str):
    try:
        return (TODAY - dt.date.fromisoformat(str(date_str)[:10])).days
    except (ValueError, TypeError):
        return None


def load(name, default):
    p = STATE / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            log(f"Не удалось прочитать {name}, начинаю его заново")
    return default


def save(name, obj):
    STATE.mkdir(exist_ok=True)
    tmp = STATE / (name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(STATE / name)


def phase(title, fn, *args):
    """Запускает этап; если он упал — пишет ошибку и продолжает работу."""
    log(f"=== {title} ===")
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        log(f"!! Ошибка на этапе «{title}»: {e!r}. Продолжаю со следующего этапа.")
        traceback.print_exc()
        return None


# ---------------------------------------------------------------------------
#  Сеть: аккуратные запросы с паузами и повторами
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers["User-Agent"] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"
)
_last_call = {}
PAUSES = {"api": CFG["pause_api_sec"], "rss": CFG["pause_rss_sec"], "page": CFG["pause_page_sec"]}


def get(url, params=None, kind="api", headers=None, as_json=True, tries=4):
    for attempt in range(tries):
        wait = _last_call.get(kind, 0) + PAUSES[kind] - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[kind] = time.time()
        try:
            r = SESSION.get(url, params=params, headers=headers, timeout=30)
        except requests.RequestException as e:
            log(f"  сетевая ошибка: {e.__class__.__name__}, повтор")
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code in (403, 429, 500, 502, 503):
            pause = 45 * (attempt + 1)
            log(f"  Apple просит притормозить (код {r.status_code}), жду {pause} с")
            time.sleep(pause)
            continue
        if r.status_code != 200:
            return None
        if not as_json:
            return r.text
        try:
            return r.json()
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
#  База приложений
# ---------------------------------------------------------------------------

def add_app(apps, hist, r, source, force=False):
    """Сохраняет одно приложение из ответа iTunes и записывает замер оценок."""
    if r.get("wrapperType") != "software" or r.get("kind") != "software":
        return False
    tid = r.get("trackId")
    if not tid:
        return False
    i = str(tid)
    cnt = int(r.get("userRatingCount") or 0)
    if i not in apps and not force and cnt < CFG["min_ratings_to_track"]:
        return False
    a = apps.get(i) or {"src": source, "first": TODAY_N}
    a.update(
        n=r.get("trackName", ""),
        dev=r.get("artistName", ""),
        did=r.get("artistId"),
        cat=r.get("primaryGenreName", ""),
        cid=int(r.get("primaryGenreId") or 0),
        gids=[str(g) for g in (r.get("genreIds") or [])],
        price=float(r.get("price") or 0),
        cnt=cnt,
        rat=round(float(r.get("averageUserRating") or 0), 2),
        rel=(r.get("releaseDate") or "")[:10],
        upd=(r.get("currentVersionReleaseDate") or "")[:10],
        seen=TODAY_N,
        miss=0,
    )
    apps[i] = a
    h = hist.setdefault(i, [])
    if h and h[-1][0] == TODAY_N:
        h[-1][1] = cnt
    else:
        h.append([TODAY_N, cnt])
    if len(h) > CFG["history_points"]:
        del h[: len(h) - CFG["history_points"]]
    return True


def lookup(ids, apps, hist, source, force_ids=frozenset(), keep_minutes=0.0):
    """Замер приложений пачками по 150 через iTunes Lookup."""
    ids = list(dict.fromkeys(ids))
    done = 0
    for k in range(0, len(ids), 150):
        if minutes_left() < keep_minutes:
            log(f"  время на исходе, замерено {done} из {len(ids)}")
            break
        chunk = ids[k:k + 150]
        data = get(LOOKUP_URL, params={"id": ",".join(chunk), "country": "us"})
        if data is None:
            continue  # ошибка сети — не считаем приложения пропавшими
        returned = set()
        for r in data.get("results", []):
            returned.add(str(r.get("trackId")))
            add_app(apps, hist, r, source, force=str(r.get("trackId")) in force_ids)
        for i in chunk:
            if i in apps and i not in returned:
                apps[i]["miss"] = apps[i].get("miss", 0) + 1
        done += len(chunk)
    return done


def refresh_minutes_needed(apps):
    todo = sum(1 for a in apps.values() if a.get("seen") != TODAY_N)
    return todo / 150 * CFG["pause_api_sec"] / 60 * 1.3 + 4


def can_explore(apps):
    return minutes_left() > refresh_minutes_needed(apps)


# ---------------------------------------------------------------------------
#  Этап 1. Чарты
# ---------------------------------------------------------------------------

def fetch_chart(feed, genre):
    url = RSS_URL.format(feed=feed, genre=f"/genre={genre}" if genre else "")
    data = get(url, kind="rss")
    entries = ((data or {}).get("feed") or {}).get("entry") or []
    if isinstance(entries, dict):
        entries = [entries]
    ids = []
    for e in entries:
        try:
            ids.append(str(e["id"]["attributes"]["im:id"]))
        except (KeyError, TypeError):
            pass
    return ids


def collect_charts(apps, hist):
    genres = [None] + [g for g in APP_GENRES if CFG["include_games"] or g != 6014]
    if CFG["include_games"]:
        genres += list(GAME_GENRES)
    charts = {kind: {} for kind in FEEDS}
    for kind, feed in FEEDS.items():
        for g in genres:
            ids = fetch_chart(feed, g)
            if ids:
                charts[kind]["all" if g is None else str(g)] = ids
        log(f"  {kind}: {len(charts[kind])} чартов")
    chart_ids = {i for kind in charts.values() for ids in kind.values() for i in ids}
    if not charts["grossing"].get("all"):
        log("!! Общий Grossing-чарт не загрузился — выручка будет считаться по запасной формуле")
    log(f"  в чартах {len(chart_ids)} разных приложений, замеряю их")
    lookup(chart_ids, apps, hist, "chart", force_ids=chart_ids)
    return charts


# ---------------------------------------------------------------------------
#  Этап 2. Поиск по ключевым словам и автоподсказки
# ---------------------------------------------------------------------------

def read_keywords():
    out = []
    for line in (ROOT / "keywords.txt").read_text(encoding="utf-8").splitlines():
        s = line.split("#", 1)[0].strip().lower()
        if s and s not in out:
            out.append(s)
    return out


def read_known():
    """Реальная выручка из открытых источников (known_revenue.csv) → {id: {rev, label, url}}, в $/мес по США."""
    p = ROOT / "known_revenue.csv"
    if not p.exists():
        return {}
    lines = [s for s in p.read_text(encoding="utf-8").splitlines() if s.strip() and not s.startswith("#")]
    out = {}
    for r in csv.DictReader(lines):
        try:
            age = days_since(r["month"].strip() + "-15")
            rev = float(r["revenue_usd_month"])
        except (KeyError, ValueError, AttributeError):
            continue
        if age is None or age > CFG["known_max_age_days"] or rev <= 0:
            continue
        if r.get("region", "").strip().lower() != "us":
            rev *= CFG["us_share_of_world"]
        sure = (r.get("verified") or "").strip().lower() in ("yes", "да", "1")
        out[r["app_id"].strip()] = {"rev": rev, "url": (r.get("source") or "").strip(),
                                    "label": "известно, " + ("подтверждено" if sure else "со слов автора")}
    return out


def search_keywords(apps, hist, meta, keywords):
    if not keywords:
        return
    pos = meta.get("kw_pos", 0) % len(keywords)
    n = min(CFG["keywords_per_day"], len(keywords))
    added = 0
    for k in range(n):
        if not can_explore(apps):
            log("  останавливаю поиск, чтобы успеть замерить базу")
            break
        kw = keywords[(pos + k) % len(keywords)]
        data = get(SEARCH_URL, params={"term": kw, "country": "us", "entity": "software", "limit": 200})
        for r in (data or {}).get("results", []):
            if str(r.get("trackId")) not in apps and add_app(apps, hist, r, "search"):
                added += 1
        meta["kw_pos"] = (pos + k + 1) % len(keywords)
    log(f"  запросов: {n}, новых приложений: {added}, всего в базе: {len(apps)}")


def suggestions(term):
    txt = get(HINTS_URL, params={"clientApplication": "Software", "term": term},
              headers={"X-Apple-Store-Front": "143441-1,29"}, as_json=False, tries=1)
    if txt is None:
        return None  # подсказки недоступны
    if not txt:
        return []
    found = re.findall(r"<key>term</key>\s*<string>(.*?)</string>", txt, re.S)
    return [html.unescape(s).strip().lower() for s in found]


def expand_keywords(apps, meta, base, auto):
    everything = base + auto
    if not everything:
        return
    known = set(everything)
    pos = meta.get("sug_pos", 0) % len(everything)
    new = 0
    for k in range(min(CFG["suggest_seeds_per_day"], len(everything))):
        if len(auto) >= CFG["max_auto_keywords"] or not can_explore(apps):
            break
        seed = everything[(pos + k) % len(everything)]
        found = suggestions(seed)
        if found is None:
            log("  Apple не отдаёт подсказки, пропускаю этот этап до завтра")
            break
        for s in found:
            if len(s) >= 3 and not s.isdigit() and s not in known:
                known.add(s)
                auto.append(s)
                new += 1
        meta["sug_pos"] = (pos + k + 1) % len(everything)
    log(f"  новых запросов из подсказок: {new}, всего автозапросов: {len(auto)}")


# ---------------------------------------------------------------------------
#  Этап 3. Соседи: разработчики и «похожие приложения»
# ---------------------------------------------------------------------------

def explore_order(apps, res):
    lo, hi = CFG["explore_min_revenue"], CFG["explore_max_revenue"]
    items = []
    for i, m in res.items():
        if i not in apps or m["rev"] < lo:
            continue
        weight = (m["rpd"] + 0.1) * (1 if m["rev"] <= hi else 0.3) * (2 if m["cand"] else 1)
        if m["cand"] and "mon" not in apps[i]:
            weight *= 100  # сначала проверяем, есть ли у кандидатов платные функции
        items.append((weight, i))
    items.sort(reverse=True)
    return [i for _, i in items]


def explore_developers(apps, hist, meta, order):
    seen = meta.setdefault("dev_seen", {})
    done = added = 0
    for i in order:
        if done >= CFG["developers_per_day"] or not can_explore(apps):
            break
        did = str(apps[i].get("did") or "")
        if not did or TODAY_N - seen.get(did, -9999) < CFG["revisit_days"]:
            continue
        data = get(LOOKUP_URL, params={"id": did, "entity": "software", "country": "us", "limit": 200})
        seen[did] = TODAY_N
        done += 1
        for r in (data or {}).get("results", []):
            if str(r.get("trackId")) not in apps and add_app(apps, hist, r, "developer"):
                added += 1
    log(f"  разработчиков просмотрено: {done}, новых приложений: {added}")


def parse_page(app_id, txt):
    """Похожие приложения, медианная цена покупок и есть ли покупки вообще (None — не удалось понять)."""
    ids = set(re.findall(r"/app/(?:[^/\"'\s<>]+/)?id(\d{6,12})", txt))
    ids.discard(app_id)
    block = re.search(r'"title":"In-App Purchases","summary":"[^"]*","items":\[(.*?)\]', txt)
    if block:
        prices = [float(p) for p in re.findall(r'"trailingText":"\$(\d{1,4}\.\d{2})"', block.group(1))]
        prices = [p for p in prices if 0 < p < 1000]
        return ids, (round(statistics.median(prices), 2) if prices else None), True
    # блок «Seller» есть на любой нормально загруженной странице; если он есть, а покупок нет — их нет
    return ids, None, (False if '"title":"Seller"' in txt else None)


def explore_pages(apps, hist, order):
    done = 0
    pending = set()
    for i in order:
        if done >= CFG["pages_per_day"] or not can_explore(apps):
            break
        a = apps[i]
        if TODAY_N - a.get("pg", -9999) < CFG["revisit_days"] and "mon" in a:
            continue
        txt = get(PAGE_URL.format(id=i), kind="page", as_json=False)
        a["pg"] = TODAY_N
        done += 1
        if not txt:
            continue
        ids, iap, mon = parse_page(i, txt)
        if iap:
            a["iap"] = iap
        if mon is not None:
            a["mon"] = mon
        pending |= {x for x in ids if x not in apps}
    log(f"  страниц открыто: {done}, найдено новых ID: {len(pending)}")
    before = len(apps)
    lookup(sorted(pending), apps, hist, "similar", keep_minutes=refresh_minutes_needed(apps))
    log(f"  из них добавлено в базу: {len(apps) - before}")


# ---------------------------------------------------------------------------
#  Этап 5. Расчёты
# ---------------------------------------------------------------------------

def growth(i, a, hist):
    """Новых оценок в день. Второе значение: True — по истории замеров, False — грубо, по возрасту."""
    h = hist.get(i) or []
    for k in range(len(h) - 1, 0, -1):
        if h[k][1] < h[k - 1][1]:  # разработчик сбросил оценки — считаем с момента сброса
            h = h[k:]
            break
    cnt = a.get("cnt", 0)
    window, min_days = CFG["growth_window_days"], CFG["growth_min_days"]
    old = [p for p in h if TODAY_N - p[0] >= min_days]
    if old:
        p = min(old, key=lambda q: abs((TODAY_N - q[0]) - window))
        return max(0.0, (cnt - p[1]) / (TODAY_N - p[0])), True
    age = max(30, days_since(a.get("rel")) or 30)
    return cnt / age, False


def accuracy(pairs):
    """Точность оценок по парам (оценка, правда): типичная ошибка «в N раз», доля в пределах 2 раз, перекос."""
    logs = [math.log(p / t) for p, t in pairs if p > 0 and t > 0]
    if len(logs) < 5:
        return None
    return {
        "n": len(logs),
        "typical": round(math.exp(statistics.median(abs(x) for x in logs)), 2),
        "within2": round(100 * sum(abs(x) <= math.log(2) for x in logs) / len(logs)),
        "bias": round(math.exp(statistics.median(logs)), 2),
    }


def compute(apps, hist, charts, chist, known):
    R200, B = CFG["revenue_at_overall_rank_200"], CFG["rank_exponent"]
    gross = charts.get("grossing", {}) if charts else {}
    overall = {i: r for r, i in enumerate(gross.get("all", []), 1)}

    def rev_overall(rank):
        return R200 * (200 / rank) ** B

    # выручка последнего места общего чарта (Apple отдаёт 100 мест)
    last_overall = rev_overall(len(overall) or 200)

    # Масштаб каждого категорийного чарта: калибруем по приложениям,
    # которые есть и в категорийном, и в общем Grossing-чарте.
    scale, cat_rank = {}, {}
    for g, ids in gross.items():
        if g == "all":
            continue
        vals = [rev_overall(overall[i]) * r ** B for r, i in enumerate(ids, 1) if i in overall]
        # если пересечений нет — даже №1 категории зарабатывает меньше последнего места общего чарта
        scale[g] = statistics.median(vals) if vals else last_overall * 0.9
        for r, i in enumerate(ids, 1):
            cat_rank.setdefault(i, []).append((g, r))

    mem = CFG["chart_memory_days"]
    for i in list(chist):
        if i not in apps:
            del chist[i]
    res = {}
    for i, a in apps.items():
        rpd, from_hist = growth(i, a, hist)
        chart_rev, rank = None, ""
        if i in overall:
            chart_rev, rank = rev_overall(overall[i]), f"#{overall[i]} общий"
        elif i in cat_rank:
            ests = [scale[g] * r ** -B for g, r in cat_rank[i]]
            chart_rev = min(statistics.median(ests), last_overall * 0.98)
            g, r = min(cat_rank[i], key=lambda x: x[1])
            name = APP_GENRES.get(int(g)) or GAME_GENRES.get(int(g), g)
            rank = f"#{r} {name}"
        # Память чартов: выручка по чарту за последние дни (сглаживает скачки места в чарте)
        h = [p for p in chist.get(i, []) if p[0] != TODAY_N and TODAY_N - p[0] <= mem]
        if chart_rev:
            h.append([TODAY_N, round(chart_rev)])
            chart_rev = statistics.median(p[1] for p in h if TODAY_N - p[0] < CFG["growth_window_days"])
        if h:
            chist[i] = h
        else:
            chist.pop(i, None)
        res[i] = {"rpd": rpd, "hist": from_hist, "chart": chart_rev, "rank": rank,
                  "last_chart": max(p[0] for p in h) if h else None}

    # Уровень цен: медианная цена встроенных покупок (если удалось прочитать) или цена приложения.
    def level(a):
        return a.get("iap") or (a["price"] if a.get("price", 0) > 0 else None)

    by_cat = {}
    for a in apps.values():
        if level(a):
            by_cat.setdefault(a.get("cid"), []).append(level(a))
    cat_level = {c: statistics.median(v) for c, v in by_cat.items() if len(v) >= 5}

    def price_factor(a):
        lv, med = level(a), cat_level.get(a.get("cid"))
        return min(2.5, max(0.4, lv / med)) if lv and med else 1.0

    # Калибровка: сколько $ выручки в месяц приходится на одну новую оценку (по категориям).
    samples = {}
    for i, a in apps.items():
        m = res[i]
        if m["chart"] and m["rpd"] > 0:
            k = m["chart"] / (m["rpd"] * 30 * price_factor(a))
            samples.setdefault(a.get("cid"), []).append((k, m["hist"], i))

    def usable(lst):
        good = [k for k, h, _ in lst if h]
        return good if len(good) >= 3 else [k for k, _, _ in lst]

    all_k = usable([s for lst in samples.values() for s in lst])
    global_k = statistics.median(all_k) if all_k else CFG["default_revenue_per_new_rating"]

    def k_for(c, skip=None):
        ks = usable([s for s in samples.get(c, []) if s[2] != skip])
        if not ks:
            return global_k
        k = statistics.median(ks) if len(ks) >= 5 else math.sqrt(statistics.median(ks) * global_k)
        return min(3000.0, max(3.0, k))

    k_cat = {c: k_for(c) for c in samples}
    calib = sorted(([APP_GENRES.get(c, str(c)), round(k_cat[c], 1), len(usable(samples[c]))] for c in samples),
                   key=lambda x: -x[2])

    dpr = CFG["downloads_per_rating"]

    def model_rev(i, a, k):
        rev = res[i]["rpd"] * 30 * k * price_factor(a)
        if a.get("price", 0) > 0:
            rev = max(rev, res[i]["rpd"] * dpr * 30 * a["price"])
        return rev

    # Самопроверка 1: приложения из нижней половины чартов категорий оцениваем по оценкам,
    # как будто их нет в чарте (без них самих в калибровке), и сравниваем с выручкой по чарту.
    check_chart = accuracy([
        (model_rev(i, apps[i], k_for(apps[i].get("cid"), skip=i)), m["chart"])
        for i, m in res.items()
        if m["chart"] and m["rpd"] > 0 and i not in overall and i in cat_rank
        and min(r for _, r in cat_rank[i]) > 50
    ])

    # Реальная выручка из открытых источников: поправка модели для приложений вне чартов
    # и самопроверка 2 (каждое приложение проверяем по поправке, посчитанной без него).
    kn = {i: model_rev(i, apps[i], k_cat.get(apps[i].get("cid"), global_k))
          for i in known if i in apps and res[i]["rpd"] > 0}

    def correction(skip=None):
        logs = [math.log(known[i]["rev"] / v) for i, v in kn.items() if i != skip and v > 0]
        return min(5.0, max(0.2, math.exp(statistics.median(logs)))) if len(logs) >= 5 else 1.0

    corr = correction()
    check_known = accuracy([(v * correction(skip=i), known[i]["rev"]) for i, v in kn.items()])

    # Потолок для тех, кого нет в чарте: меньше, чем у последнего места своей категории.
    cap_cat = {g: scale[g] * len(ids) ** -B for g, ids in gross.items() if g != "all" and len(ids) >= 90}

    cc = CFG["candidates"]
    for i, a in apps.items():
        m = res[i]
        if i in known:
            rev, m["src"] = known[i]["rev"], "known"
            m["rank"], m["url"] = known[i]["label"], known[i]["url"]
        elif m["chart"]:
            rev, m["src"] = m["chart"], "chart"
        else:
            rev, m["src"] = model_rev(i, a, k_cat.get(a.get("cid"), global_k)) * corr, "model"
            caps = [cap_cat[g] for g in [str(a.get("cid"))] + [g for g in a.get("gids", []) if g.startswith("7")]
                    if g in cap_cat]
            rev = min([rev, last_overall * 0.98] + caps)
            if m["last_chart"] is not None and caps:
                # недавно был в чарте — значит, зарабатывает около его последнего места
                rev = max(rev, 0.5 * min(caps))
                m["src"], m["rank"] = "recent", f"был в чарте {TODAY_N - m['last_chart']} дн. назад"
        m["rev"] = rev
        age = days_since(a.get("rel"))
        m["mom"] = m["rpd"] * 30 / max(a.get("cnt", 0), 1)
        m["cand"] = bool(
            age is not None and age <= cc["max_age_days"]
            and cc["min_revenue"] <= rev <= cc["max_revenue"]
            and m["rpd"] >= cc["min_ratings_per_day"]
            and a.get("cnt", 0) <= cc["max_total_ratings"]
            and (CFG["include_games"] or a.get("cid") != 6014)
            and (a.get("mon") is not False or a.get("price", 0) > 0)  # бесплатное без покупок — не кандидат
        )
    return res, {"global_k": round(global_k, 1), "calib": calib, "chart_ok": bool(overall),
                 "check_chart": check_chart, "check_known": check_known,
                 "corr": round(corr, 2), "known_n": len(kn)}


# ---------------------------------------------------------------------------
#  Этап 6. Чистка базы и выгрузка для сайта
# ---------------------------------------------------------------------------

def prune(apps, hist, res):
    gone = [i for i, a in apps.items() if a.get("miss", 0) >= 3]
    for i in gone:
        apps.pop(i, None)
        hist.pop(i, None)
    extra = len(apps) - CFG["max_tracked_apps"]
    if extra > 0:
        def keep(i):
            m = res.get(i, {})
            fresh = TODAY_N - apps[i].get("first", TODAY_N) < 14
            return (1 if m.get("src") in ("chart", "known", "recent") else 0, 1 if fresh else 0, m.get("rev", 0))
        for i in sorted(apps, key=keep)[:extra]:
            apps.pop(i, None)
            hist.pop(i, None)
    log(f"  удалено пропавших из App Store: {len(gone)}, слабых: {max(extra, 0)}")


def export(apps, res, info, meta, n_keywords, n_auto):
    dpr = CFG["downloads_per_rating"]
    rows = []
    for i, a in apps.items():
        m = res.get(i)
        if not m:
            continue
        if not (m["chart"] or m["cand"] or m["src"] == "known" or m["rev"] >= CFG["export_min_revenue"]):
            continue
        rows.append([
            int(i), a.get("n", ""), a.get("dev", ""), a.get("cat", ""), a.get("price", 0),
            a.get("cnt", 0), a.get("rat", 0), round(m["rpd"], 2), 1 if m["hist"] else 0,
            round(m["rev"]), 1 if m["chart"] else 0, m["rank"], a.get("rel", ""), a.get("upd", ""),
            round(m["mom"], 4), 1 if m["cand"] else 0, a.get("iap"), round(m["rpd"] * dpr),
            {True: 1, False: 0}.get(a.get("mon")), m["src"], m.get("url"),
        ])
    rows.sort(key=lambda r: -r[9])
    rows = rows[: CFG["export_max_rows"]]
    data = {
        "updated": TODAY.isoformat(),
        "tracked": len(apps),
        "exported": len(rows),
        "candidates": sum(r[15] for r in rows),
        "history_days": TODAY_N - meta.get("first_day", TODAY_N) + 1,
        "keywords": n_keywords,
        "auto_keywords": n_auto,
        "chart_ok": info.get("chart_ok"),
        "global_k": info.get("global_k"),
        "calib": info.get("calib", []),
        "check_chart": info.get("check_chart"),
        "check_known": info.get("check_known"),
        "corr": info.get("corr"),
        "known_n": info.get("known_n"),
        "target": [CFG["candidates"]["min_revenue"], CFG["candidates"]["max_revenue"]],
        "rows": rows,
    }
    SITE.mkdir(exist_ok=True)
    js = "window.APP_DATA = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n"
    (SITE / "data.js").write_text(js, encoding="utf-8")
    log(f"  на сайт выгружено {len(rows)} приложений, кандидатов: {data['candidates']}")


# ---------------------------------------------------------------------------
#  Главная функция
# ---------------------------------------------------------------------------

def main():
    log(f"Старт. Дата: {TODAY}. Бюджет времени: {CFG['time_budget_minutes']} мин")
    apps = load("apps.json", {})
    hist = load("history.json", {})
    meta = load("meta.json", {})
    auto = load("keywords_auto.json", [])
    meta.setdefault("first_day", TODAY_N)
    chist = load("chart_history.json", {})
    base = read_keywords()
    known = read_known()
    log(f"В базе {len(apps)} приложений, запросов: {len(base)} своих + {len(auto)} авто, "
        f"с известной выручкой: {len(known)}")

    charts = phase("Чарты США", collect_charts, apps, hist) or {}
    save("charts.json", charts)
    phase("Приложения с известной выручкой", lookup, list(known), apps, hist, "known", frozenset(known))

    phase("Поиск по ключевым словам", search_keywords, apps, hist, meta,
          list(dict.fromkeys(base + auto)))
    phase("Автоподсказки App Store", expand_keywords, apps, meta, base, auto)

    computed = phase("Предварительный расчёт", compute, apps, hist, charts, chist, known)
    if computed:
        order = explore_order(apps, computed[0])
        phase("Другие приложения разработчиков", explore_developers, apps, hist, meta, order)
        phase("Похожие приложения", explore_pages, apps, hist, order)

    def refresh():
        todo = [i for i, a in apps.items() if a.get("seen") != TODAY_N]
        log(f"  нужно замерить: {len(todo)}")
        lookup(todo, apps, hist, "refresh", keep_minutes=1.5)
    phase("Ежедневный замер всей базы", refresh)

    final = phase("Итоговый расчёт", compute, apps, hist, charts, chist, known)
    if final:
        res, info = final
        phase("Чистка базы", prune, apps, hist, res)
        phase("Выгрузка для сайта", export, apps, res, info, meta, len(base) + len(auto), len(auto))

    save("apps.json", apps)
    save("history.json", hist)
    save("meta.json", meta)
    save("keywords_auto.json", auto)
    save("chart_history.json", chist)
    log("Готово.")


if __name__ == "__main__":
    main()
