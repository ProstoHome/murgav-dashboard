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

import json
import sys
import re
import time
import requests
from datetime import datetime, timedelta, date
from pathlib import Path

# ─── Paths ────────────────────────────────────────────────────────────────────
WORKSPACE = Path(__file__).resolve().parent.parent          # aiagent_gosha_cgo/
ENV_FILE   = WORKSPACE / "data" / "api_keys.env"
OUT_FILE   = Path(__file__).resolve().parent / "data.json"

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
        "metrics":   ["ordered_units", "revenue", "returns"],
        "dimension": ["sku", "day"],
        "filters":   [],
        "sort":      [{"key": "day", "order": "ASC"}],
        "limit":     1000,
        "offset":    0,
    }
    r = requests.post(url, headers=OZON_HEADERS, json=body, timeout=30)
    r.raise_for_status()
    rows = r.json().get("result", {}).get("data", [])

    total    = {"orders": 0, "revenue": 0.0, "returns": 0}
    by_color = {"graphite": 0, "grey": 0, "brown": 0}
    daily    = {}

    for row in rows:
        dims    = row.get("dimensions", [])
        sku_id  = dims[0]["id"] if len(dims) > 0 else ""
        day     = dims[1]["id"][:10] if len(dims) > 1 else ""
        if sku_id not in sku_map:
            continue
        color   = sku_map[sku_id]
        metrics = row.get("metrics", [])
        orders  = int(metrics[0]) if len(metrics) > 0 else 0
        revenue = float(metrics[1]) if len(metrics) > 1 else 0.0
        returns = int(metrics[2]) if len(metrics) > 2 else 0

        total["orders"]  += orders
        total["revenue"] += revenue
        total["returns"] += returns
        if color in by_color:
            by_color[color] += orders
        if day:
            if day not in daily:
                daily[day] = {"orders": 0, "revenue": 0.0}
            daily[day]["orders"]  += orders
            daily[day]["revenue"] += revenue

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

# ─── Retry helper ─────────────────────────────────────────────────────────────
def api_get_with_retry(url, headers=None, params=None, timeout=20, max_retries=3) -> requests.Response:
    """GET with exponential backoff on 429."""
    delay = 10
    for attempt in range(max_retries):
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
        if r.status_code == 429:
            print(f"      ⏳ 429 rate limit, waiting {delay}s (attempt {attempt+1}/{max_retries})...")
            time.sleep(delay)
            delay *= 2
            continue
        return r
    return r  # return last response even if 429

def api_post_with_retry(url, headers=None, json=None, data=None, timeout=20, max_retries=3) -> requests.Response:
    """POST with exponential backoff on 429."""
    delay = 10
    for attempt in range(max_retries):
        r = requests.post(url, headers=headers, json=json, data=data, timeout=timeout)
        if r.status_code == 429:
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
    """Returns {spend, campaigns: [{id, name, state, spend, orders}], daily: {date: spend}}"""
    try:
        token = get_ozon_perf_token()
    except Exception as e:
        print(f"      ⚠️  Ozon Performance auth failed: {e}")
        return {"spend": 0.0, "campaigns": [], "daily": {}}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }

    try:
        r = api_get_with_retry(
            f"{OZON_PERF_BASE}/api/client/campaign",
            headers=headers, timeout=20
        )
        r.raise_for_status()
        campaigns_raw = r.json().get("list", [])
    except Exception as e:
        print(f"      ⚠️  Ozon Performance campaign list failed: {e}")
        return {"spend": 0.0, "campaigns": [], "daily": {}}

    campaigns   = []
    total_spend = 0.0
    daily       = {}

    for camp in campaigns_raw:
        camp_id   = str(camp.get("id", ""))
        camp_name = camp.get("title", camp_id)
        state     = camp.get("state", "")

        try:
            r2 = api_get_with_retry(
                f"{OZON_PERF_BASE}/api/client/statistics/campaign/{camp_id}/daily",
                params={"dateFrom": date_from, "dateTo": date_to},
                headers=headers, timeout=20
            )
            r2.raise_for_status()
            stats_rows = r2.json().get("items", [])
        except Exception:
            stats_rows = []

        camp_spend  = 0.0
        camp_orders = 0
        for row in stats_rows:
            spend       = float(row.get("moneySpent", 0))
            orders      = int(row.get("orderCount", 0))
            day         = row.get("date", "")[:10]
            camp_spend  += spend
            camp_orders += orders
            if day:
                daily[day] = daily.get(day, 0.0) + spend

        campaigns.append({
            "id":     camp_id,
            "name":   camp_name,
            "state":  state,
            "spend":  round(camp_spend, 2),
            "orders": camp_orders,
        })
        total_spend += camp_spend

    return {
        "spend":     round(total_spend, 2),
        "campaigns": campaigns,
        "daily":     daily,
    }

# ─── WB Analytics API ────────────────────────────────────────────────────────
WB_ANALYTICS_HEADERS = {
    "Authorization": WB_TOKEN_ANALYTICS,
    "Content-Type":  "application/json",
}

def fetch_wb_sales(date_from: str) -> dict:
    """Returns {total: {orders, revenue}, by_color: {}, daily: {}}"""
    url    = "https://statistics-api.wildberries.ru/api/v5/supplier/reportDetailByPeriod"
    params = {
        "dateFrom": date_from,
        "dateTo":   date.today().isoformat(),
        "limit":    100000,
        "rrdid":    0,
    }
    try:
        r = api_get_with_retry(url, headers=WB_ANALYTICS_HEADERS, params=params, timeout=30)
        r.raise_for_status()
        rows = r.json() if isinstance(r.json(), list) else []
    except Exception as e:
        print(f"      ⚠️  WB sales fetch failed: {e}")
        return {"total": {"orders": 0, "revenue": 0.0}, "by_color": {"graphite": 0, "grey": 0, "brown": 0}, "daily": {}}

    total    = {"orders": 0, "revenue": 0.0}
    by_color = {"graphite": 0, "grey": 0, "brown": 0}
    daily    = {}

    for row in rows:
        doc_type = row.get("doc_type_name", "")
        if doc_type not in ("Продажа", "Sale"):
            continue
        sa_name  = row.get("sa_name", "")     # supplier article name
        subject  = row.get("subject_name", "")
        nm_name  = row.get("nm_name", "")
        full     = f"{sa_name} {subject} {nm_name}"
        if not is_target_80x60(full):
            continue

        color    = get_color(full)
        quantity = int(row.get("quantity", 0))
        retail   = float(row.get("retail_amount", 0))
        day      = row.get("rr_dt", "")[:10]

        total["orders"]  += quantity
        total["revenue"] += retail
        if color in by_color:
            by_color[color] += quantity
        if day:
            if day not in daily:
                daily[day] = {"orders": 0, "revenue": 0.0}
            daily[day]["orders"]  += quantity
            daily[day]["revenue"] += retail

    return {"total": total, "by_color": by_color, "daily": daily}

def fetch_wb_stocks() -> dict:
    """Returns {graphite: N, grey: N, brown: N, total: N} for 80×60 items."""
    url    = "https://statistics-api.wildberries.ru/api/v1/supplier/stocks"
    params = {"dateFrom": (date.today() - timedelta(days=1)).isoformat()}
    time.sleep(2)  # avoid hitting WB rate limit back-to-back
    try:
        r = api_get_with_retry(url, headers=WB_ANALYTICS_HEADERS, params=params, timeout=20)
        r.raise_for_status()
        rows = r.json() if isinstance(r.json(), list) else []
    except Exception as e:
        print(f"      ⚠️  WB stocks fetch failed: {e}")
        return {"graphite": 0, "grey": 0, "brown": 0, "total": 0}

    stocks = {"graphite": 0, "grey": 0, "brown": 0}
    for row in rows:
        sa_name = row.get("supplierArticle", "")
        subject = row.get("subject", "")
        nm_name = row.get("brand", "")
        full    = f"{sa_name} {subject} {nm_name}"
        qty     = int(row.get("quantity", 0))
        if not is_target_80x60(full):
            continue
        color = get_color(full)
        if color in stocks:
            stocks[color] += qty
    stocks["total"] = sum(stocks.values())
    return stocks

# ─── WB Ads API ──────────────────────────────────────────────────────────────
WB_ADS_HEADERS = {
    "Authorization": WB_TOKEN_ADS,
    "Content-Type":  "application/json",
}

def fetch_wb_ads(date_from: str, date_to: str) -> dict:
    """Returns {spend, campaigns: [{id, name, spend, views, clicks}], daily: {date: spend}}"""
    # Step 1: get campaign IDs via count endpoint (correct endpoint, not /promotion/adverts)
    time.sleep(2)  # avoid rate limit
    try:
        r = api_get_with_retry(
            "https://advert-api.wildberries.ru/adv/v1/promotion/count",
            headers=WB_ADS_HEADERS, timeout=20
        )
        r.raise_for_status()
        count_data = r.json()
        # Response: {"adverts": [{"type":9, "status":7, "count":N, "advert_list":[{"advertId":N,...}]}]}
        camp_ids = [
            ad["advertId"]
            for item in count_data.get("adverts", [])
            for ad in item.get("advert_list", [])
        ]
        # Build name map from advert_list (only has advertId and changeTime)
        camp_names = {ad["advertId"]: str(ad["advertId"]) for item in count_data.get("adverts", []) for ad in item.get("advert_list", [])}
    except Exception as e:
        print(f"      ⚠️  WB ads campaign list failed: {e}")
        return {"spend": 0.0, "campaigns": [], "daily": {}}

    if not camp_ids:
        print(f"      ℹ️  WB ads: нет кампаний")
        return {"spend": 0.0, "campaigns": [], "daily": {}}

    # Step 2: batch stats for all campaigns at once
    time.sleep(2)
    try:
        body      = [{"id": cid, "interval": {"begin": date_from, "end": date_to}} for cid in camp_ids]
        r2        = api_post_with_retry(
            "https://advert-api.wildberries.ru/adv/v2/fullstats",
            headers=WB_ADS_HEADERS, json=body, timeout=30
        )
        r2.raise_for_status()
        stat_list = r2.json() if isinstance(r2.json(), list) else []
    except Exception as e:
        print(f"      ⚠️  WB ads fullstats failed: {e}")
        stat_list = []

    campaigns   = []
    total_spend = 0.0
    daily       = {}

    for s in stat_list:
        camp_id   = s.get("advertId", 0)
        camp_name = camp_names.get(camp_id, str(camp_id))
        camp_spend  = 0.0
        camp_views  = 0
        camp_clicks = 0
        for day_stat in (s.get("days") or []):
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

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    today    = date.today()
    d7_from  = (today - timedelta(days=7)).isoformat()
    d30_from = (today - timedelta(days=30)).isoformat()
    date_to  = today.isoformat()

    print("[1/8] Загружаю SKU-карту Ozon (80×60 фильтр)...")
    sku_map = fetch_ozon_sku_map()
    print(f"      Найдено SKU: {len(sku_map)}")

    print("[2/8] Аналитика Ozon за 7 дней...")
    ozon_7d  = fetch_ozon_analytics(d7_from,  date_to, sku_map)
    print("[3/8] Аналитика Ozon за 30 дней...")
    ozon_30d = fetch_ozon_analytics(d30_from, date_to, sku_map)

    print("[4/8] Остатки Ozon...")
    ozon_stocks = fetch_ozon_stocks(sku_map)

    print("[5/8] Ozon Performance (реклама)...")
    ozon_perf_7d  = fetch_ozon_performance(d7_from,  date_to)
    ozon_perf_30d = fetch_ozon_performance(d30_from, date_to)

    print("[6/8] WB продажи...")
    wb_7d  = fetch_wb_sales(d7_from)
    time.sleep(3)
    wb_30d = fetch_wb_sales(d30_from)

    print("[7/8] WB остатки...")
    wb_stocks = fetch_wb_stocks()

    print("[8/8] WB реклама...")
    wb_ads_7d  = fetch_wb_ads(d7_from,  date_to)
    time.sleep(3)
    wb_ads_30d = fetch_wb_ads(d30_from, date_to)

    # Compute DRR (ads spend / revenue × 100)
    def drr(spend, revenue):
        if not revenue:
            return None
        return round(spend / revenue * 100, 1)

    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "ozon": {
            "7d": {
                "orders":   ozon_7d["total"]["orders"],
                "revenue":  round(ozon_7d["total"]["revenue"], 2),
                "returns":  ozon_7d["total"]["returns"],
                "by_color": ozon_7d["by_color"],
                "daily":    ozon_7d["daily"],
            },
            "30d": {
                "orders":   ozon_30d["total"]["orders"],
                "revenue":  round(ozon_30d["total"]["revenue"], 2),
                "returns":  ozon_30d["total"]["returns"],
                "by_color": ozon_30d["by_color"],
                "daily":    ozon_30d["daily"],
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
            },
            "30d": {
                "orders":   wb_30d["total"]["orders"],
                "revenue":  round(wb_30d["total"]["revenue"], 2),
                "by_color": wb_30d["by_color"],
                "daily":    wb_30d["daily"],
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
    print(f"   WB   7д:  {wb_7d['total']['orders']:3} шт / {wb_7d['total']['revenue']:,.0f} ₽")
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
