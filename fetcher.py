#!/usr/bin/env python3
"""
МурГав Dashboard Fetcher
Pulls data from Ozon Seller API, Ozon Performance API, WB Analytics, WB Ads API.
Filters to 80×60 лежанки only (graphite / grey / brown).
Saves dashboard/data.json for the static dashboard.

SKU map (Ozon internal):
  sku=4389191917  offer_id=Лежанка_графит_80*60   → graphite
  sku=4389190181  offer_id=Лежанка_серая_80*60    → grey
  sku=4389192715  offer_id=Лежанка_коричневая_80*60 → brown
"""

import csv
import io
import json
import sys
import re
import time
import zipfile
import requests
from datetime import datetime, timedelta, date
from pathlib import Path

# ─── Paths ────────────────────────────────────────────────────────────────────
WORKSPACE        = Path(__file__).resolve().parent.parent   # aiagent_gosha_cgo/
ENV_FILE         = WORKSPACE / "data" / "api_keys.env"
OUT_FILE         = Path(__file__).resolve().parent / "data.json"
WB_CACHE_FILE    = Path(__file__).resolve().parent / "wb_stats_cache.json"
OZON_PERF_CACHE  = Path(__file__).resolve().parent / "ozon_perf_cache.json"
WB_ADS_CACHE     = Path(__file__).resolve().parent / "wb_ads_cache.json"

# ─── ZIP helper (Ozon Performance returns double-nested ZIP-in-ZIP) ───────────
def _extract_zip_text(data: bytes) -> str:
    """Recursively unwrap ZIP archives until a CSV text is found.

    Ozon Performance API sometimes returns a ZIP whose inner file is also
    a ZIP (double-nested). This function handles 0, 1 or 2 levels of nesting.
    """
    if data[:2] != b'PK':
        # Plain text (or UTF-8-BOM)
        return data.decode('utf-8-sig')
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        name = next((n for n in zf.namelist() if n.endswith('.csv')), zf.namelist()[0])
        inner = zf.read(name)
    # Recurse in case the inner file is also a ZIP
    return _extract_zip_text(inner)

# ─── Load env ─────────────────────────────────────────────────────────────────
def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.split("#")[0].strip()
    return env

ENV = load_env(ENV_FILE)

OZON_CLIENT_ID       = ENV["OZON_CLIENT_ID"]
OZON_API_KEY         = ENV["OZON_API_KEY"]
OZON_PERF_CLIENT_ID  = ENV["OZON_PERF_CLIENT_ID"]
OZON_PERF_SECRET     = ENV["OZON_PERF_CLIENT_SECRET"]
WB_TOKEN_ANALYTICS   = ENV["WB_TOKEN_ANALYTICS"]
WB_TOKEN_ADS         = ENV["WB_TOKEN_ADS"]
WB_TOKEN_MARKETPLACE = ENV.get("WB_TOKEN_MARKETPLACE", "")

# ─── SKU filter helpers ────────────────────────────────────────────────────────
SIZE_PATTERNS = [
    r"80\s*[xхх×*]\s*60",
    r"60\s*[xхх×*]\s*80",
    r"80[Хх]60",
]
COLOR_MAP = {
    "graphite": ["графит", "graphit", "anthracit", "antracit", "charcoal"],
    "grey":     ["серый", "серая", "серо", "grey", "gray", "silver"],
    "brown":    ["коричнев", "коричневая", "brown", "шоколад", "chocolate", "кофе"],
}

def is_target_80x60(text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in SIZE_PATTERNS)

def get_color(text: str) -> str:
    t = text.lower()
    for color, keywords in COLOR_MAP.items():
        if any(kw in t for kw in keywords):
            return color
    return "other"

# ─── Ozon Seller API ──────────────────────────────────────────────────────────
OZON_HEADERS = {
    "Client-Id": OZON_CLIENT_ID,
    "Api-Key":   OZON_API_KEY,
    "Content-Type": "application/json",
}

def fetch_ozon_sku_map() -> dict:
    """
    Returns {sku_str → color} for all 80×60 items.
    Uses v3/product/list; offer_id encodes size+color.
    """
    url = "https://api-seller.ozon.ru/v3/product/list"
    sku_map = {}
    last_id = ""
    while True:
        body = {"filter": {}, "last_id": last_id, "limit": 100}
        r = requests.post(url, headers=OZON_HEADERS, json=body, timeout=20)
        r.raise_for_status()
        data  = r.json()
        items = data.get("result", {}).get("items", [])
        if not items:
            break
        for item in items:
            offer_id = str(item.get("offer_id", ""))
            sku      = str(item.get("sku", ""))
            if is_target_80x60(offer_id):
                color = get_color(offer_id)
                sku_map[sku] = color
                print(f"      SKU {sku} → {color}  ({offer_id})")
        last_id = data.get("result", {}).get("last_id", "")
        if not last_id or len(items) < 100:
            break
    return sku_map

def fetch_ozon_analytics(date_from: str, date_to: str, sku_map: dict) -> dict:
    """
    Returns {total: {orders, revenue, returns}, by_color: {}, daily: {date: {orders, revenue}}}
    Analytics row dimensions: [sku_id, day_id]
    """
    url  = "https://api-seller.ozon.ru/v1/analytics/data"
    body = {
        "date_from": date_from,
        "date_to":   date_to,
        "metrics":   ["ordered_units", "revenue", "returns", "delivered_units"],
        "dimension": ["sku", "day"],
        "filters":   [],
        "sort":      [{"key": "day", "order": "ASC"}],
        "limit":     1000,
        "offset":    0,
    }
    r = requests.post(url, headers=OZON_HEADERS, json=body, timeout=30)
    r.raise_for_status()
    rows = r.json().get("result", {}).get("data", [])

    total    = {"orders": 0, "revenue": 0.0, "returns": 0, "delivered": 0}
    by_color = {"graphite": 0, "grey": 0, "brown": 0}
    daily    = {}

    for row in rows:
        dims    = row.get("dimensions", [])
        sku_id  = dims[0]["id"] if len(dims) > 0 else ""
        day     = dims[1]["id"][:10] if len(dims) > 1 else ""
        if sku_id not in sku_map:
            continue
        color     = sku_map[sku_id]
        metrics   = row.get("metrics", [])
        orders    = int(metrics[0])   if len(metrics) > 0 else 0
        revenue   = float(metrics[1]) if len(metrics) > 1 else 0.0
        returns   = int(metrics[2])   if len(metrics) > 2 else 0
        delivered = int(metrics[3])   if len(metrics) > 3 else 0

        total["orders"]    += orders
        total["revenue"]   += revenue
        total["returns"]   += returns
        total["delivered"] += delivered
        if color in by_color:
            by_color[color] += orders
        if day:
            if day not in daily:
                daily[day] = {"orders": 0, "revenue": 0.0, "returns": 0, "delivered": 0, "by_color": {"graphite": 0, "grey": 0, "brown": 0}}
            daily[day]["orders"]    += orders
            daily[day]["revenue"]   += revenue
            daily[day]["returns"]   += returns  # Fix БАГ 2: track returns per-day so slice_sales can compute 7d %
            daily[day]["delivered"] += delivered
            if color in daily[day]["by_color"]:
                daily[day]["by_color"][color] += orders

    return {"total": total, "by_color": by_color, "daily": daily}

def fetch_ozon_stocks(sku_map: dict) -> dict:
    """Returns {graphite: N, grey: N, brown: N, total: N}"""
    url  = "https://api-seller.ozon.ru/v2/analytics/stock_on_warehouses"
    body = {"limit": 1000, "offset": 0, "warehouse_type": "ALL"}
    r    = requests.post(url, headers=OZON_HEADERS, json=body, timeout=20)
    r.raise_for_status()
    rows = r.json().get("result", {}).get("rows", [])

    stocks = {"graphite": 0, "grey": 0, "brown": 0}
    for row in rows:
        sku   = str(row.get("sku", ""))
        qty   = int(row.get("free_to_sell_amount", 0))
        if sku in sku_map:
            color = sku_map[sku]
            if color in stocks:
                stocks[color] += qty
    stocks["total"] = sum(stocks.values())
    return stocks

# ─── Cache helpers ────────────────────────────────────────────────────────────
def load_cache(path: Path, max_age_hours: float = 23.0) -> dict | None:
    """Load JSON cache if exists and fresh. Returns None on miss/stale/error."""
    if not path.exists():
        return None
    try:
        cache = json.loads(path.read_text())
        cached_at = datetime.fromisoformat(cache.get("cached_at", ""))
        age_h = (datetime.now() - cached_at).total_seconds() / 3600
        if age_h < max_age_hours:
            print(f"      ✅ Кэш {path.name} ({age_h:.1f}ч) → используем")
            return cache
        print(f"      ℹ️  Кэш {path.name} устарел ({age_h:.1f}ч) → перезапрашиваем")
    except Exception as exc:
        print(f"      ⚠️  Ошибка чтения кэша {path.name}: {exc}")
    return None

def save_cache(path: Path, data: dict):
    """Save dict to JSON cache with cached_at timestamp."""
    try:
        payload = {"cached_at": datetime.now().isoformat(timespec="seconds"), **data}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"      💾 Кэш сохранён → {path.name}")
    except Exception as exc:
        print(f"      ⚠️  Не удалось сохранить кэш {path.name}: {exc}")

# ─── Retry helper ─────────────────────────────────────────────────────────────
def api_get_with_retry(url, headers=None, params=None, timeout=20, max_retries=3, initial_delay=10) -> requests.Response:
    """GET with exponential backoff on 429; respects Retry-After header."""
    delay = initial_delay
    for attempt in range(max_retries):
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
        if r.status_code == 429:
            # honour Retry-After if present
            retry_after = r.headers.get("Retry-After") or r.headers.get("X-RateLimit-Reset-After")
            if retry_after:
                try:
                    delay = max(int(retry_after), delay)
                except ValueError:
                    pass
            print(f"      ⏳ 429 rate limit, waiting {delay}s (attempt {attempt+1}/{max_retries})...")
            time.sleep(delay)
            delay *= 2
            continue
        return r
    return r  # return last response even if still 429

def api_post_with_retry(url, headers=None, json=None, data=None, timeout=20, max_retries=3, initial_delay=10) -> requests.Response:
    """POST with exponential backoff on 429."""
    delay = initial_delay
    for attempt in range(max_retries):
        r = requests.post(url, headers=headers, json=json, data=data, timeout=timeout)
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After") or r.headers.get("X-RateLimit-Reset-After")
            if retry_after:
                try:
                    delay = max(int(retry_after), delay)
                except ValueError:
                    pass
            print(f"      ⏳ 429 rate limit, waiting {delay}s (attempt {attempt+1}/{max_retries})...")
            time.sleep(delay)
            delay *= 2
            continue
        return r
    return r

# ─── Ozon Performance API ─────────────────────────────────────────────────────
# Correct base URL: api-performance.ozon.ru (not performance.ozon.ru)
OZON_PERF_BASE = "https://api-performance.ozon.ru"

def get_ozon_perf_token() -> str:
    url  = f"{OZON_PERF_BASE}/api/client/token"
    body = {
        "client_id":     OZON_PERF_CLIENT_ID,
        "client_secret": OZON_PERF_SECRET,
        "grant_type":    "client_credentials",
    }
    r = api_post_with_retry(url, json=body, timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]

def fetch_ozon_performance(date_from: str, date_to: str) -> dict:
    """
    Returns {spend, campaigns: [{id, name, state, spend, orders}], daily: {date: spend}}

    Uses the async UUID-based statistics endpoint:
      POST /api/client/statistics  → {"UUID": "...", "vendor": false}
      GET  /api/client/statistics/{UUID} → poll until state != "RUNNING"
    """
    try:
        token = get_ozon_perf_token()
    except Exception as e:
        print(f"      ⚠️  Ozon Performance auth failed: {e}")
        return None  # None = signal failure

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }

    # ── Step 1: get campaign list ──────────────────────────────────────────────
    try:
        r = api_get_with_retry(
            f"{OZON_PERF_BASE}/api/client/campaign",
            headers=headers, timeout=20, initial_delay=30
        )
        r.raise_for_status()
        campaigns_raw = r.json().get("list", [])
    except Exception as e:
        print(f"      ⚠️  Ozon Performance campaign list failed: {e}")
        return None  # None = signal failure

    if not campaigns_raw:
        return {"spend": 0.0, "campaigns": [], "daily": {}}  # 0 campaigns is valid (not a failure)

    camp_meta  = {str(c.get("id", "")): c for c in campaigns_raw if c.get("id")}

    # Only request stats for RUNNING campaigns — inactive ones cause 429 pileup with no useful data
    RUNNING_STATES = {"CAMPAIGN_STATE_RUNNING", "RUNNING", "ACTIVE"}
    running_ids = [cid for cid, meta in camp_meta.items()
                   if meta.get("state", "") in RUNNING_STATES]
    camp_ids = running_ids if running_ids else list(camp_meta.keys())  # fallback to all if none running
    print(f"      ℹ️  Ozon Performance: {len(camp_ids)} RUNNING кампаний из {len(camp_meta)} всего")

    # ── Step 2: POST async stats requests — 1 campaign per batch for per-campaign spend tracking ──────
    BATCH_SIZE      = 1   # 1 campaign per request → each UUID maps to exactly 1 campaign
    batches         = [camp_ids[i:i+BATCH_SIZE] for i in range(0, len(camp_ids), BATCH_SIZE)]
    uuids           = []
    uuid_to_camp_id = {}  # uuid → campaign_id (valid only when BATCH_SIZE=1)
    camp_spend      = {}  # camp_id → total spend accumulated from CSV

    for batch_num, batch in enumerate(batches):
        if batch_num > 0:
            time.sleep(3)  # avoid 429 between batches
        try:
            body = {
                "campaigns": batch,
                "dateFrom":  date_from,
                "dateTo":    date_to,
                "groupBy":   "DATE",
            }
            r2 = api_post_with_retry(
                f"{OZON_PERF_BASE}/api/client/statistics",
                headers=headers, json=body, timeout=30, initial_delay=30
            )
            r2.raise_for_status()
            uuid = r2.json().get("UUID") or r2.json().get("uuid")
            if not uuid:
                print(f"      ⚠️  Ozon Performance stats batch {batch_num+1}: no UUID — {r2.text[:200]}")
                continue
            uuids.append(uuid)
            if len(batch) == 1:
                uuid_to_camp_id[uuid] = batch[0]
        except Exception as e:
            print(f"      ⚠️  Ozon Performance stats POST batch {batch_num+1} failed: {e}")
            continue

    if not uuids:
        return None  # All batches failed → signal failure (429 etc.)

    # ── Step 3: poll all UUIDs, then download CSV report ─────────────────────
    # Status endpoint returns only metadata: {"UUID":"...", "state":"OK", "link":"..."}
    # Actual spend data is at /api/client/statistics/report?UUID=... as a semicolon-delimited CSV
    # CSV columns: День;sku;Название товара;Цена товара, ₽;Показы;Клики;CTR, %;
    #              Добавления в корзину;Средняя стоимость клика, ₽;Расход, ₽, с НДС;Продано товаров;...
    # Date format in CSV: DD.MM.YYYY
    total_spend = 0.0
    daily       = {}

    for uuid in uuids:
        poll_url   = f"{OZON_PERF_BASE}/api/client/statistics/{uuid}"
        report_url = f"{OZON_PERF_BASE}/api/client/statistics/report?UUID={uuid}"
        succeeded  = False
        for attempt in range(40):  # up to ~600s total — API backend can take 3–5 min
            wait_s = 20  # wait before polling
            time.sleep(wait_s)
            try:
                r3 = requests.get(poll_url, headers=headers, timeout=20)
                r3.raise_for_status()
                resp  = r3.json()
                state = resp.get("state", resp.get("status", "RUNNING")).upper()
                if state in ("OK", "DONE", "COMPLETED", "SUCCESS"):
                    # Download the CSV report
                    r_csv = requests.get(report_url, headers=headers, timeout=60)
                    if r_csv.status_code != 200:
                        print(f"      ⚠️  CSV download UUID={uuid}: HTTP {r_csv.status_code}")
                        break
                    # API returns ZIP archive (sometimes double-nested ZIP-in-ZIP)
                    content = r_csv.content
                    print(f"      🔍 Ozon Perf response head: {repr(content[:10])}")
                    text = _extract_zip_text(content)
                    # \r внутри неквотированных полей роняет csv.reader на StringIO
                    # («new-line character seen in unquoted field») — нормализуем
                    text   = text.replace('\r\n', '\n').replace('\r', '\n')
                    print(f"      🔍 Ozon Perf CSV head (300 chars): {repr(text[:300])}")
                    reader = csv.reader(io.StringIO(text, newline=''), delimiter=';')
                    header = next(reader, None)  # skip header row
                    print(f"      🔍 Ozon Perf CSV columns: {header}")
                    csv_rows = 0
                    for row in reader:
                        if len(row) < 11:
                            continue
                        date_raw   = row[0].strip()
                        spend_raw  = (row[9].strip()
                                      .replace('\xa0', '').replace(' ', '')
                                      .replace(' ', '').replace(',', '.'))
                        orders_raw = row[10].strip()
                        try:
                            day    = datetime.strptime(date_raw, "%d.%m.%Y").strftime("%Y-%m-%d")
                            spend  = float(spend_raw) if spend_raw else 0.0
                            total_spend        += spend
                            daily[day]          = daily.get(day, 0.0) + spend
                            camp_key = uuid_to_camp_id.get(uuid, "")
                            if camp_key:
                                camp_spend[camp_key] = camp_spend.get(camp_key, 0.0) + spend
                            csv_rows           += 1
                        except (ValueError, IndexError):
                            continue
                    print(f"      ✅ UUID={uuid}: CSV распарсен, {csv_rows} строк, расход {total_spend:.0f}₽")
                    succeeded = True
                    break
                elif state in ("ERROR", "FAILED", "CANCELLED"):
                    print(f"      ⚠️  Ozon Performance UUID={uuid} failed state={state}")
                    break
                else:
                    # NOT_STARTED or RUNNING — still processing
                    if attempt == 0:
                        print(f"      ⏳ UUID={uuid} state={state}, ждём...")
            except Exception as e:
                print(f"      ⚠️  Ozon Performance poll UUID={uuid} attempt {attempt+1}: {e}")
                continue
        if not succeeded:
            print(f"      ⚠️  UUID={uuid}: превышен лимит ожидания, пропускаем")

    # ── Step 4: build campaigns list from metadata ─────────────────────────────
    # Per-campaign spend is populated from uuid_to_camp_id mapping (BATCH_SIZE=1 guarantees 1:1)
    campaigns = []
    for camp_id, meta in camp_meta.items():
        campaigns.append({
            "id":    camp_id,
            "name":  meta.get("title", camp_id),
            "state": meta.get("state", ""),
            "spend": round(camp_spend.get(camp_id, 0.0), 2),
        })

    return {
        "spend":     round(total_spend, 2),
        "campaigns": campaigns,
        "daily":     {k: round(v, 2) for k, v in sorted(daily.items())},
    }

# ─── WB Analytics API ────────────────────────────────────────────────────────
WB_ANALYTICS_HEADERS = {
    "Authorization": WB_TOKEN_ANALYTICS,
    "Content-Type":  "application/json",
}
WB_MARKETPLACE_HEADERS = {
    "Authorization": WB_TOKEN_MARKETPLACE,
    "Content-Type":  "application/json",
}

def fetch_wb_sales(date_from: str) -> dict:
    """Returns {total: {orders, revenue}, by_color: {}, daily: {}}
    Uses /api/v1/supplier/orders — matches what seller sees in WB personal cabinet.
    Filters: isCancel=False only. No 80x60 size filter — all products in this
    WB account are МурГав 80x60 лежанки; size filter would silently drop orders
    when supplierArticle/subject/brand fields don't contain the size pattern.
    NOTE: WB LK "Заказы" counts ALL orders incl. cancelled — our count will be
    ~10-15% lower (cancelled orders excluded), this is expected and correct behavior.
    Revenue = totalPrice * (1 - discountPercent/100).
    """
    url    = "https://statistics-api.wildberries.ru/api/v1/supplier/orders"
    params = {
        "dateFrom": date_from,
        "flag":     0,   # 0 = filter by order date (created), not lastChangeDate
    }
    try:
        r = api_get_with_retry(url, headers=WB_ANALYTICS_HEADERS, params=params, timeout=30,
                               max_retries=3, initial_delay=60)
        r.raise_for_status()
        raw  = r.json()
        rows = raw if isinstance(raw, list) else []
    except Exception as e:
        print(f"      ⚠️  WB orders fetch failed: {e}")
        return None  # None = signal failure (vs real 0 orders)

    total    = {"orders": 0, "revenue": 0.0}
    by_color = {"graphite": 0, "grey": 0, "brown": 0}
    daily    = {}
    cancelled_count = 0

    for row in rows:
        if not isinstance(row, dict):
            continue
        # Skip cancelled orders (isCancel=True).
        # WB LK shows all orders incl. cancelled — that's why LK count > our count.
        if row.get("isCancel", False):
            cancelled_count += 1
            continue

        sa_name = row.get("supplierArticle", "")
        subject = row.get("subject", "")
        nm_name = row.get("brand", "")
        full    = f"{sa_name} {subject} {nm_name}"
        # Фильтр: только лежанки. Аккаунт содержит столы, пуфы и другие товары —
        # берём только позиции где supplierArticle/subject/brand содержат "лежанк".
        if "лежанк" not in full.lower():
            continue

        color       = get_color(full)
        price_full  = float(row.get("totalPrice", 0) or 0)
        discount    = float(row.get("discountPercent", 0) or 0)
        retail      = round(price_full * (1 - discount / 100), 2)
        day         = (row.get("date") or row.get("lastChangeDate", ""))[:10]

        total["orders"]  += 1
        total["revenue"] += retail
        if color in by_color:
            by_color[color] += 1
        if day:
            if day not in daily:
                daily[day] = {"orders": 0, "revenue": 0.0, "by_color": {"graphite": 0, "grey": 0, "brown": 0}}
            daily[day]["orders"]  += 1
            daily[day]["revenue"] += retail
            if color in daily[day]["by_color"]:
                daily[day]["by_color"][color] += 1

    print(f"      ✅ WB orders: {total['orders']} заказов за период (отменённых: {cancelled_count}, итого в API: {total['orders'] + cancelled_count})")
    return {"total": total, "by_color": by_color, "daily": daily}

def fetch_wb_stocks() -> dict:
    """Returns {graphite: N, grey: N, brown: N, total: N} for лежанки.

    Uses marketplace-api.wildberries.ru/api/v3/stocks/{warehouseId} (POST).
    Запрашивает известные штрихкоды лежанок у всех FBS-складов.
    NOTE: МурГав работает по FBW (WB хранит остатки на своих складах) — seller-склады пустые,
    поэтому этот endpoint вернёт 0 для всех. FBW-остатки через seller API недоступны.
    Токен: WB_TOKEN_MARKETPLACE (права «Маркетплейс»).
    """
    if not WB_TOKEN_MARKETPLACE:
        print("      ⚠️  WB stocks: WB_TOKEN_MARKETPLACE не задан в api_keys.env")
        return None

    # Известные штрихкоды лежанок МурГав (из Content API)
    LEZHANKA_BARCODES = {
        "2051453067308": "graphite",  # 80×60 графит
        "2051453067674": "grey",      # 80×60 серый
        "2051453067117": "brown",     # 80×60 коричневый
        "2053199498712": "graphite",  # 60×40 графит
        "2053199498705": "grey",      # 60×40 серый
        "2053199498729": "brown",     # 60×40 коричневый
    }
    # Склады поставщика (FBS-склады; FBW-склады через этот API не доступны)
    WAREHOUSE_IDS = [21178, 1428021]

    stocks = {"graphite": 0, "grey": 0, "brown": 0}
    found_any = False

    for wh_id in WAREHOUSE_IDS:
        url  = f"https://marketplace-api.wildberries.ru/api/v3/stocks/{wh_id}"
        body = {"skus": list(LEZHANKA_BARCODES.keys())}
        try:
            r = api_post_with_retry(url, headers=WB_MARKETPLACE_HEADERS, json=body, timeout=20,
                                    max_retries=2, initial_delay=30)
            r.raise_for_status()
            wh_stocks = r.json().get("stocks", [])
            for item in wh_stocks:
                barcode = str(item.get("sku", ""))
                qty     = int(item.get("amount", 0))
                color   = LEZHANKA_BARCODES.get(barcode)
                if color and qty > 0:
                    stocks[color] += qty
                    found_any = True
        except Exception as e:
            print(f"      ⚠️  WB stocks wh={wh_id} failed: {e}")

    stocks["total"] = sum(stocks.values())
    if not found_any:
        print("      ℹ️  WB stocks: 0 по всем FBS-складам (МурГав использует FBW — остатки на складах WB через API недоступны)")
        return None  # None = не кэшируем нули, попробуем в следующий раз
    print(f"      ✅ WB stocks: {stocks}")
    return stocks


def fetch_wb_buyouts(date_from: str) -> dict:
    """Returns {total: {count, revenue}, daily: {date: {count, revenue}}} for 80×60 buyouts.
    Uses /api/v1/supplier/sales (flag=1 = filter by sale/buyout date).
    saleID starting with 'S' = actual sale (buyout); 'R' = return — count only 'S'.
    """
    url     = "https://statistics-api.wildberries.ru/api/v1/supplier/sales"
    params  = {"dateFrom": date_from, "flag": 1}
    headers = {"Authorization": WB_TOKEN_ANALYTICS}  # Fix: WB_TOKEN undefined → WB_TOKEN_ANALYTICS
    time.sleep(10)
    try:
        r = requests.get(url, headers=headers, params=params, timeout=60)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        print(f"      ⚠️  WB buyouts fetch failed: {e}")
        return {"total": {"count": 0, "revenue": 0.0}, "daily": {}}

    total = {"count": 0, "revenue": 0.0}
    daily = {}
    for row in rows:
        sale_id = row.get("saleID", "")
        if not sale_id.startswith("S"):
            continue  # skip returns (R...) and other types
        sa_name = row.get("supplierArticle", "")
        subject = row.get("subject", "")
        brand   = row.get("brand", "")
        full    = f"{sa_name} {subject} {brand}"
        # Фильтр: только лежанки (аккаунт содержит столы, пуфы и другие товары)
        if "лежанк" not in full.lower():
            continue
        revenue = float(row.get("finishedPrice", 0) or 0)
        day     = (row.get("date") or row.get("lastChangeDate", ""))[:10]
        total["count"]   += 1
        total["revenue"] += revenue
        if day:
            if day not in daily:
                daily[day] = {"count": 0, "revenue": 0.0}
            daily[day]["count"]   += 1
            daily[day]["revenue"] += revenue

    print(f"      ✅ WB buyouts: {total['count']} выкупов / {total['revenue']:,.0f} ₽")
    return {"total": total, "daily": daily}


def slice_buyouts(full: dict, cutoff: str) -> dict:
    """Slice a 30d fetch_wb_buyouts result to the window >= cutoff."""
    daily_sliced = {d: v for d, v in full["daily"].items() if d >= cutoff}
    count   = sum(v["count"]   for v in daily_sliced.values())
    revenue = sum(v["revenue"] for v in daily_sliced.values())
    return {
        "total": {"count": count, "revenue": round(revenue, 2)},
        "daily": daily_sliced,
    }


# ─── WB Ads API ──────────────────────────────────────────────────────────────
WB_ADS_HEADERS = {
    "Authorization": WB_TOKEN_ADS,
    "Content-Type":  "application/json",
}

def fetch_wb_ads(date_from: str, date_to: str) -> dict:
    """Returns {spend, campaigns: [{id, name, spend, views, clicks}], daily: {date: spend}}"""
    # Step 1: get campaign IDs via count endpoint (correct endpoint, not /promotion/adverts)
    time.sleep(30)  # give WB rate limit time to recover before ads calls
    try:
        r = api_get_with_retry(
            "https://advert-api.wildberries.ru/adv/v1/promotion/count",
            headers=WB_ADS_HEADERS, timeout=20,
            max_retries=3, initial_delay=60
        )
        r.raise_for_status()
        count_data = r.json()
        # Response: {"adverts": [{"type":9, "status":7, "count":N, "advert_list":[{"advertId":N,...}]}]}
        # Status 9 = active/running on WB ads; only fetch stats for active campaigns
        ACTIVE_STATUS = {9}
        all_pairs = [
            (item.get("status", 0), ad["advertId"])
            for item in count_data.get("adverts", [])
            for ad in item.get("advert_list", [])
        ]
        camp_ids = [cid for status, cid in all_pairs if status in ACTIVE_STATUS]
        if not camp_ids:
            camp_ids = [cid for _, cid in all_pairs]  # fallback: все кампании
        camp_names = {cid: str(cid) for _, cid in all_pairs}
        print(f"      ℹ️  WB ads: {len(camp_ids)} активных кампаний из {len(all_pairs)} всего")
    except Exception as e:
        print(f"      ⚠️  WB ads campaign list failed: {e}")
        return {"spend": 0.0, "campaigns": [], "daily": {}}

    if not camp_ids:
        print(f"      ℹ️  WB ads: нет кампаний")
        return {"spend": 0.0, "campaigns": [], "daily": {}}

    # Step 2: GET /adv/v3/fullstats?ids=ID1,ID2,...&from=YYYY-MM-DD&to=YYYY-MM-DD
    # ВАЖНО: endpoint принимает GET (не POST — 405). Запятые в ids НЕ кодировать (%2C).
    # Max 50 campaign IDs per request; rate-limit: 1 req/min global per seller
    time.sleep(90)  # WB adv API global limiter — 90s гарантирует сброс rate-limit

    ADS_BATCH = 50
    stat_list = []
    fullstats_base = "https://advert-api.wildberries.ru/adv/v3/fullstats"
    for b_num, batch in enumerate([camp_ids[i:i+ADS_BATCH] for i in range(0, len(camp_ids), ADS_BATCH)]):
        if b_num > 0:
            time.sleep(65)
        try:
            ids_str  = ",".join(str(cid) for cid in batch)
            # URL строим вручную — requests.params кодирует запятые как %2C, WB не принимает
            full_url = f"{fullstats_base}?ids={ids_str}&from={date_from}&to={date_to}"
            r2 = api_get_with_retry(
                full_url,
                headers=WB_ADS_HEADERS,
                timeout=60,
                max_retries=3, initial_delay=120
            )
            r2.raise_for_status()
            raw = r2.json()
            batch_stat = raw if isinstance(raw, list) else []
            stat_list.extend(batch_stat)
            print(f"      ✅ WB ads v3 fullstats batch {b_num+1}: {len(batch_stat)} записей")
        except Exception as e:
            print(f"      ⚠️  WB ads fullstats batch {b_num+1} failed: {e}")
            continue

    campaigns   = []
    total_spend = 0.0
    daily       = {}

    # Debug: show raw v3 structure on first item so we can verify field names
    if stat_list:
        first = stat_list[0]
        first_keys = list(first.keys())
        first_days_sample = (first.get("days") or first.get("statistics") or [])[:1]
        print(f"      🔍 WB ads v3 sample: keys={first_keys}, days_sample={first_days_sample}")

    for s in stat_list:
        # v3 может использовать "advertId" или "id"
        camp_id   = s.get("advertId") or s.get("id") or 0
        camp_name = camp_names.get(camp_id, str(camp_id))
        camp_spend  = 0.0
        camp_views  = 0
        camp_clicks = 0
        # v3 может использовать "days" или "statistics"
        day_stats = s.get("days") or s.get("statistics") or []
        for day_stat in day_stats:
            day         = day_stat.get("date", "")[:10]
            spend       = float(day_stat.get("sum", 0))
            views       = int(day_stat.get("views", 0))
            clicks      = int(day_stat.get("clicks", 0))
            camp_spend  += spend
            camp_views  += views
            camp_clicks += clicks
            if day:
                daily[day] = daily.get(day, 0.0) + spend

        campaigns.append({
            "id":     str(camp_id),
            "name":   camp_name,
            "spend":  round(camp_spend, 2),
            "views":  camp_views,
            "clicks": camp_clicks,
        })
        total_spend += camp_spend

    return {
        "spend":     round(total_spend, 2),
        "campaigns": campaigns,
        "daily":     daily,
    }

# ─── Slice helpers ────────────────────────────────────────────────────────────
def slice_sales(full: dict, cutoff: str) -> dict:
    """
    From a 30d fetch_wb_sales / fetch_ozon_analytics result, return a new dict
    with only rows >= cutoff (YYYY-MM-DD), totals recomputed from sliced daily.
    """
    daily_sliced = {d: v for d, v in full["daily"].items() if d >= cutoff}
    orders  = sum(v["orders"]  for v in daily_sliced.values())
    revenue = sum(v["revenue"] for v in daily_sliced.values())
    # Recompute by_color from per-day data (if available); fallback to full 30d by_color
    if daily_sliced and "by_color" in next(iter(daily_sliced.values())):
        by_color = {"graphite": 0, "grey": 0, "brown": 0}
        for v in daily_sliced.values():
            for c, n in v["by_color"].items():
                by_color[c] = by_color.get(c, 0) + n
    else:
        by_color = full.get("by_color", {"graphite": 0, "grey": 0, "brown": 0})
    # Fix БАГ 2: compute returns from the 7d slice (not from full 30d total)
    returns   = sum(v.get("returns",   0) for v in daily_sliced.values())
    delivered = sum(v.get("delivered", 0) for v in daily_sliced.values())
    return {
        "total":    {"orders": orders, "revenue": round(revenue, 2), "returns": returns, "delivered": delivered},
        "by_color": by_color,
        "daily":    daily_sliced,
    }

def slice_ads(full: dict, cutoff: str) -> dict:
    """
    From a 30d fetch_ozon_performance / fetch_wb_ads result, return a sliced version
    with spend and daily recomputed for the window >= cutoff.
    """
    daily_sliced = {d: v for d, v in full["daily"].items() if d >= cutoff}
    spend = sum(daily_sliced.values())
    return {
        "spend":     round(spend, 2),
        "campaigns": full.get("campaigns", []),
        "daily":     daily_sliced,
    }


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    today    = date.today()
    d7_from  = (today - timedelta(days=7)).isoformat()
    d30_from = (today - timedelta(days=30)).isoformat()
    date_to  = today.isoformat()

    print("[1/6] Загружаю SKU-карту Ozon (80×60 фильтр)...")
    sku_map = fetch_ozon_sku_map()
    print(f"      Найдено SKU: {len(sku_map)}")

    print("[2/6] Аналитика Ozon за 30 дней (7д нарежем из неё)...")
    ozon_30d = fetch_ozon_analytics(d30_from, date_to, sku_map)
    ozon_7d  = slice_sales(ozon_30d, d7_from)

    print("[3/6] Остатки Ozon...")
    ozon_stocks = fetch_ozon_stocks(sku_map)

    print("[4/6] Ozon Performance за 30 дней (7д нарежем из неё)...")
    _OZON_PERF_EMPTY = {"spend": 0.0, "campaigns": [], "daily": {}}
    ozon_perf_cache = load_cache(OZON_PERF_CACHE)
    # Fix: инвалидируем кэш если spend=0 — не раздаём устаревшие нули весь день
    if ozon_perf_cache and ozon_perf_cache.get("ozon_perf_30d", {}).get("spend", 0.0) == 0.0:
        print("      ℹ️  Кэш Ozon Perf: spend=0 → принудительное обновление")
        ozon_perf_cache = None
    if ozon_perf_cache:
        ozon_perf_30d = ozon_perf_cache["ozon_perf_30d"]
    else:
        ozon_perf_30d = fetch_ozon_performance(d30_from, date_to)
        if ozon_perf_30d is not None:
            if ozon_perf_30d.get("spend", 0.0) > 0.0:
                # Кэшируем только ненулевой результат
                save_cache(OZON_PERF_CACHE, {"ozon_perf_30d": ozon_perf_30d})
            else:
                print("      ℹ️  Ozon Perf spend=0 — кэш не сохранён (повторный запрос при следующем запуске)")
        else:
            print("      ⚠️  Ozon Performance: используем нули (API недоступен, кэша нет)")
            ozon_perf_30d = _OZON_PERF_EMPTY
    ozon_perf_7d = slice_ads(ozon_perf_30d, d7_from)

    print("[5/6] WB продажи + остатки + выкупы (кэш 23ч, один вызов на 30д)...")
    _WB_SALES_EMPTY   = {"total": {"orders": 0, "revenue": 0.0, "returns": 0}, "by_color": {"graphite": 0, "grey": 0, "brown": 0}, "daily": {}}
    _WB_STOCKS_EMPTY  = {"graphite": 0, "grey": 0, "brown": 0, "total": 0}
    _WB_BUYOUTS_EMPTY = {"total": {"count": 0, "revenue": 0.0}, "daily": {}}
    wb_cache = load_cache(WB_CACHE_FILE)
    if wb_cache:
        wb_30d         = wb_cache["wb_30d"]
        wb_stocks      = wb_cache.get("wb_stocks")  # None если не было в кэше (API упал в прошлый раз)
        wb_buyouts_30d = wb_cache.get("wb_buyouts_30d", _WB_BUYOUTS_EMPTY)
        if wb_stocks is None or wb_stocks.get("total", 0) == 0:
            print("      ⚠️  WB остатки: нули/отсутствуют в кэше")
            print("         ⚡ Нужен новый токен: seller.wildberries.ru → Настройки → Доступ к API → 'Маркетплейс'")
            wb_stocks = _WB_STOCKS_EMPTY
    else:
        # WB Statistics API: ~1 req/10 min per token — fetch 30d once, slice 7d
        wb_30d    = fetch_wb_sales(d30_from)
        # лимит ~1 запрос/мин на метод — после продаж ждём минуту
        time.sleep(61)
        wb_stocks      = fetch_wb_stocks()
        time.sleep(61)
        wb_buyouts_30d = fetch_wb_buyouts(d30_from)
        if wb_30d is not None:
            # Fix: НЕ кэшируем нули когда API упал — иначе нули живут 23ч
            cache_data = {"wb_30d": wb_30d}
            if wb_stocks is not None and wb_stocks.get("total", 0) > 0:
                cache_data["wb_stocks"] = wb_stocks
            else:
                print("      ℹ️  WB остатки: не кэшируем нули (устаревший API / нужен новый токен)")
            if wb_buyouts_30d is not None:
                cache_data["wb_buyouts_30d"] = wb_buyouts_30d
            save_cache(WB_CACHE_FILE, cache_data)
        # Replace None with safe empty dicts so payload construction doesn't crash
        if wb_30d is None:
            print("      ⚠️  WB заказы: используем нули (API недоступен, кэша нет)")
            wb_30d = _WB_SALES_EMPTY
        if wb_stocks is None:
            print("      ⚠️  WB остатки: используем нули (устаревший API, нужен новый токен)")
            wb_stocks = _WB_STOCKS_EMPTY
        if wb_buyouts_30d is None:
            wb_buyouts_30d = _WB_BUYOUTS_EMPTY
    wb_7d         = slice_sales(wb_30d, d7_from)
    wb_buyouts_7d = slice_buyouts(wb_buyouts_30d, d7_from)

    print("[6/6] WB реклама за 7 дней (WB fullstats не принимает диапазон >7д)...")
    # Кэшируем на 23ч — WB adv API rate-limit 1 req/min, без кэша каждый запуск даёт 429
    _wb_ads_cache = load_cache(WB_ADS_CACHE)
    if _wb_ads_cache:
        print("      ✅ Кэш wb_ads_cache.json — используем, пропускаем fetch_wb_ads()")
        wb_ads_30d = _wb_ads_cache["wb_ads_data"]
    else:
        wb_ads_30d = fetch_wb_ads(d7_from, date_to)
        # Fix БАГ 5: кэшируем WB ads только если spend > 0
        # Раньше [] is not None → нули кэшировались и висели 23ч
        # Теперь: пустой ответ (campaigns=[] или spend=0) → не кэшируем → следующий запуск перепроверит
        if wb_ads_30d.get("spend", 0) > 0:
            save_cache(WB_ADS_CACHE, {"wb_ads_data": wb_ads_30d})
    wb_ads_7d  = slice_ads(wb_ads_30d, d7_from)

    # Compute DRR (ads spend / revenue × 100)
    def drr(spend, revenue):
        if not revenue:
            return None
        return round(spend / revenue * 100, 1)

    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "ozon": {
            "7d": {
                "orders":    ozon_7d["total"]["orders"],
                "revenue":   round(ozon_7d["total"]["revenue"], 2),
                "returns":   ozon_7d["total"]["returns"],
                "delivered": ozon_7d["total"].get("delivered", 0),
                "by_color":  ozon_7d["by_color"],
                "daily":     ozon_7d["daily"],
            },
            "30d": {
                "orders":    ozon_30d["total"]["orders"],
                "revenue":   round(ozon_30d["total"]["revenue"], 2),
                "returns":   ozon_30d["total"]["returns"],
                "delivered": ozon_30d["total"].get("delivered", 0),
                "by_color":  ozon_30d["by_color"],
                "daily":     ozon_30d["daily"],
            },
            "stocks": ozon_stocks,
            "ads": {
                "7d": {
                    "spend":     ozon_perf_7d["spend"],
                    "drr_pct":   drr(ozon_perf_7d["spend"], ozon_7d["total"]["revenue"]),
                    "daily":     ozon_perf_7d["daily"],
                    "campaigns": ozon_perf_7d["campaigns"],
                },
                "30d": {
                    "spend":     ozon_perf_30d["spend"],
                    "drr_pct":   drr(ozon_perf_30d["spend"], ozon_30d["total"]["revenue"]),
                    "daily":     ozon_perf_30d["daily"],
                    "campaigns": ozon_perf_30d["campaigns"],
                },
            },
        },
        "wb": {
            "7d": {
                "orders":   wb_7d["total"]["orders"],
                "revenue":  round(wb_7d["total"]["revenue"], 2),
                "by_color": wb_7d["by_color"],
                "daily":    wb_7d["daily"],
                "buyouts":  wb_buyouts_7d["total"],
            },
            "30d": {
                "orders":   wb_30d["total"]["orders"],
                "revenue":  round(wb_30d["total"]["revenue"], 2),
                "by_color": wb_30d["by_color"],
                "daily":    wb_30d["daily"],
                "buyouts":  wb_buyouts_30d["total"],
            },
            "stocks": wb_stocks,
            "ads": {
                "7d": {
                    "spend":     wb_ads_7d["spend"],
                    "drr_pct":   drr(wb_ads_7d["spend"], wb_7d["total"]["revenue"]),
                    "daily":     wb_ads_7d["daily"],
                    "campaigns": wb_ads_7d["campaigns"],
                },
                "30d": {
                    "spend":     wb_ads_30d["spend"],
                    "drr_pct":   drr(wb_ads_30d["spend"], wb_30d["total"]["revenue"]),
                    "daily":     wb_ads_30d["daily"],
                    "campaigns": wb_ads_30d["campaigns"],
                },
            },
        },
    }

    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"\n✅ data.json сохранён → {OUT_FILE}")
    print(f"   Ozon 7д:  {ozon_7d['total']['orders']:3} шт / {ozon_7d['total']['revenue']:,.0f} ₽")
    print(f"   WB   7д:  {wb_7d['total']['orders']:3} шт / {wb_7d['total']['revenue']:,.0f} ₽  (выкупов: {wb_buyouts_7d['total']['count']} шт / {wb_buyouts_7d['total']['revenue']:,.0f} ₽)")
    print(f"   Ozon ДРР 7д: {drr(ozon_perf_7d['spend'], ozon_7d['total']['revenue'])}%")
    print(f"   WB   ДРР 7д: {drr(wb_ads_7d['spend'], wb_7d['total']['revenue'])}%")
    print(f"   Ozon остатки: {ozon_stocks}")
    print(f"   WB   остатки: {wb_stocks}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"\n❌ Ошибка: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
