import os
import re
import json
import time
import statistics
import warnings
import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# ---------- НАСТРОЙКИ ----------
MAX_PRICE = 5000          # только авто до этой цены, €
MIN_DISCOUNT = 0.20       # от 20% ниже медианы и больше
MAX_DISCOUNT = 1.0        # верхней границы нет
MIN_GAP = 500             # и разница с медианой минимум 500 €
MIN_PEERS = 5             # минимум похожих авто для расчёта медианы
YEAR_TOL = 2              # разница в годах +-
KM_TOL = 0.25             # разница в пробеге +-25%
MAX_PAGES = 12            # страниц на марку (на странице ~30 объявлений)
MAX_SEND = 15             # максимум сообщений за один запуск
DETAIL_BUDGET = 400       # максимум запросов к страницам объявлений за запуск
DELAY = 0.8               # пауза между запросами, сек
# Чтобы искать только по некоторым маркам: BRANDS="skoda,volkswagen,audi"
BRANDS = [b.strip().lower() for b in os.environ.get("BRANDS", "").split(",") if b.strip()]
# --------------------------------

BASE = "https://www.ss.lv"
CAT = BASE + "/ru/transport/cars/"
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
SKIP_SLUGS = {"today", "today-2", "today-5", "sell", "buy", "exchange", "wanted",
              "search", "filter", "new-cars", "all", "rent"}

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.8",
})
detail_requests = 0
DIAG = []


def note(url, status, body=""):
    if len(DIAG) < 4:
        DIAG.append(f"{url.replace(BASE, '')} -> {status}, {len(body)} симв., начало: "
                    f"{body[:120]!r}")


def fetch(url):
    for _ in range(3):
        try:
            r = S.get(url, timeout=25)
            time.sleep(DELAY)
            note(url, r.status_code, r.text)
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                return None
        except requests.RequestException as e:
            note(url, "ошибка " + type(e).__name__)
            time.sleep(2)
    return None


def to_int(s):
    d = re.sub(r"\D", "", s or "")
    return int(d) if d else None


def parse_km(s):
    n = to_int(s)
    if n is None:
        return None
    low = (s or "").lower()
    if "тыс" in low or "tūkst" in low or "tukst" in low or n < 2000:
        n *= 1000
    return n


def parse_engine(s):
    m = re.match(r"\s*(\d+(?:[.,]\d+)?)\s*([A-Za-zА-Яа-я]*)", s or "")
    if not m:
        return None, None
    vol = float(m.group(1).replace(",", "."))
    fuel = (m.group(2) or "P").upper()[:1]
    return vol, fuel


def parse_list(html):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.find_all("tr"):
        a = tr.find("a", href=re.compile(r"/msg/"))
        if not a:
            continue
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        pi = None
        for i in range(len(cells) - 1, -1, -1):
            if "€" in cells[i]:
                pi = i
                break
        if pi is None or pi < 3:
            continue
        year, eng, km, price = cells[pi - 3], cells[pi - 2], cells[pi - 1], cells[pi]
        href = re.sub(r"^https?://[^/]+", "", a.get("href", ""))
        m = re.search(r"/cars/([^/]+)/([^/]+)/[^/]+\.html", href)
        if not m:
            continue
        brand, model = m.group(1), m.group(2)
        y = re.search(r"(19|20)\d\d", year or "")
        vol, fuel = parse_engine(eng)
        p = to_int(price)
        k = parse_km(km)
        if not (y and vol and p and k) or p < 300:
            continue
        img = tr.find("img")
        src = ""
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src.startswith("//"):
                src = "https:" + src
            if not src.startswith("http"):
                src = ""
        out.append({"id": href, "brand": brand, "model": model, "year": int(y.group()),
                    "vol": vol, "fuel": fuel, "km": k, "price": p, "img": src,
                    "title": a.get_text(" ", strip=True)[:80]})
    return out


def get_brand_urls():
    html = fetch(CAT)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    seen, urls = set(), []
    for a in soup.select("a[href]"):
        h = re.sub(r"^https?://[^/]+", "", a["href"])
        m = re.match(r"^/ru/transport/cars/([a-z0-9\-]+)/$", h)
        if not m:
            continue
        slug = m.group(1)
        if slug in SKIP_SLUGS or slug in seen:
            continue
        if BRANDS and slug not in BRANDS:
            continue
        seen.add(slug)
        urls.append(BASE + h)
    return urls


def collect_ads():
    ads = {}
    for burl in get_brand_urls():
        for page in range(1, MAX_PAGES + 1):
            url = burl if page == 1 else f"{burl}page{page}.html"
            html = fetch(url)
            if not html:
                break
            rows = parse_list(html)
            new = [r for r in rows if r["id"] not in ads]
            if not new:
                break
            for r in new:
                ads[r["id"]] = r
    return list(ads.values())


def get_transmission(ad_id, cache):
    global detail_requests
    if ad_id in cache:
        return cache[ad_id]
    if detail_requests >= DETAIL_BUDGET:
        return None
    detail_requests += 1
    html = fetch(BASE + ad_id)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    val = "?"
    for td in soup.select("td.ads_opt_name"):
        name = td.get_text(strip=True).lower()
        if "коробка" in name or "ātrumkārba" in name or "atrumkarba" in name:
            nxt = td.find_next_sibling("td")
            if nxt:
                t = nxt.get_text(strip=True).lower()
                if "авто" in t or "automāt" in t or "automat" in t:
                    val = "автомат"
                elif "руч" in t or "мех" in t or "manu" in t or "mehān" in t:
                    val = "механика"
                else:
                    val = t or "?"
    cache[ad_id] = val
    return val


def peers_of(ad, group):
    return [p for p in group
            if p["id"] != ad["id"]
            and abs(p["year"] - ad["year"]) <= YEAR_TOL
            and abs(p["km"] - ad["km"]) <= KM_TOL * ad["km"]]


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def tg(text, photo=None):
    global CHAT_ID
    if not CHAT_ID:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=25).json()
        for u in reversed(r.get("result", [])):
            msg = u.get("message") or u.get("channel_post")
            if msg:
                CHAT_ID = str(msg["chat"]["id"])
                print("CHAT_ID =", CHAT_ID)
                break
        if not CHAT_ID:
            raise SystemExit("Напиши своему боту любое сообщение в Telegram и запусти снова.")
    if photo:
        # сначала крупная версия фото, потом миниатюра
        variants = []
        big = re.sub(r"\.t\.jpg$", ".800.jpg", photo)
        if big != photo:
            variants.append(big)
        variants.append(photo)
        for url in variants:
            try:
                r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
                                  data={"chat_id": CHAT_ID, "photo": url,
                                        "caption": text[:1000]}, timeout=40)
                if r.ok and r.json().get("ok"):
                    return
            except requests.RequestException:
                pass
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                  data={"chat_id": CHAT_ID, "text": text}, timeout=25)


def main():
    if not TOKEN:
        raise SystemExit("Нет BOT_TOKEN")
    first_run = not os.path.exists("seen.json")
    seen = set(load("seen.json", []))
    cache = load("cache.json", {})

    ads = collect_ads()
    print("Собрано объявлений:", len(ads))
    if len(ads) < 100:
        tg(f"⚠️ Бот собрал только {len(ads)} объявлений.\n\nДиагностика:\n" + "\n".join(DIAG))
        return

    groups = {}
    for a in ads:
        groups.setdefault((a["brand"], a["model"], a["fuel"], a["vol"]), []).append(a)

    # этап 1: быстрый отбор без учёта КПП (с запасом по диапазону)
    cands = []
    for a in ads:
        if a["price"] > MAX_PRICE or a["id"] in seen:
            continue
        peers = peers_of(a, groups[(a["brand"], a["model"], a["fuel"], a["vol"])])
        if len(peers) < MIN_PEERS:
            continue
        med = statistics.median(p["price"] for p in peers)
        d = 1 - a["price"] / med
        if d >= 0.15 and med - a["price"] >= MIN_GAP * 0.8:
            cands.append((d, a))
    cands.sort(key=lambda x: -x[0])
    print("Кандидатов:", len(cands))

    # этап 2: уточнение по КПП
    sent = 0
    for _, a in cands:
        if sent >= MAX_SEND:
            break
        trans = get_transmission(a["id"], cache)
        if not trans or trans == "?":
            continue
        group = groups[(a["brand"], a["model"], a["fuel"], a["vol"])]
        peers = sorted(peers_of(a, group), key=lambda p: abs(p["km"] - a["km"]))
        same = []
        for p in peers:
            t = get_transmission(p["id"], cache)
            if t == trans:
                same.append(p)
        if len(same) < MIN_PEERS:
            continue
        med = statistics.median(p["price"] for p in same)
        d = 1 - a["price"] / med
        if not (MIN_DISCOUNT <= d <= MAX_DISCOUNT) or med - a["price"] < MIN_GAP:
            continue
        tg(f"🚗 {a['brand'].title()} {a['model'].title()} {a['year']} · "
           f"{a['vol']}{'' if a['fuel'] == 'P' else a['fuel']} · {trans}\n"
           f"Цена: {a['price']} € · медиана аналогов: {int(med)} € (−{int(med - a['price'])} €, −{int(d * 100)}%)\n"
           f"Пробег: {a['km']} км · аналогов: {len(same)}\n"
           f"{BASE + a['id']}", photo=a.get("img"))
        seen.add(a["id"])
        sent += 1

    save("seen.json", sorted(seen))
    save("cache.json", cache)
    if first_run:
        tg(f"✅ Бот запущен. Объявлений собрано: {len(ads)}, групп: {len(groups)}, "
           f"кандидатов: {len(cands)}, отправлено: {sent}.")


if __name__ == "__main__":
    main()
