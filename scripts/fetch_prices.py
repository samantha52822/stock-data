#!/usr/bin/env python3
"""抓取台股、美股收盤價，輸出 data/prices.json。

只用 Python 標準函式庫，不需 pip install。
台股：證交所 OpenAPI（上市／ETF）、櫃買中心（上櫃）。不需 key。
美股：Finnhub /quote。需要環境變數 FINNHUB_KEY。

重要原則：抓不到、日期不對、疑似盤後價的標的，一律標記 status，不得當成收盤價使用。
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

TAIPEI = ZoneInfo("Asia/Taipei") if ZoneInfo else timezone(timedelta(hours=8))
NEW_YORK = ZoneInfo("America/New_York") if ZoneInfo else timezone(timedelta(hours=-4))

# ---- 追蹤清單（只放公開行情代號，不放股數成本） ----
TW_LISTED = ["0050", "00981A", "00631L", "2330", "2441", "6415"]   # 上市／ETF
TW_OTC = ["1815", "3081", "4989"]                                   # 上櫃
US_SYMBOLS = ["QQQ", "SPY", "MU", "MRVL", "NVDA", "ARM", "NOK", "SPCX", "TSM"]

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "prices.json")
UA = {"User-Agent": "Mozilla/5.0 (stock-data personal script)"}


def http_json(url, retries=3, timeout=30):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed: {url.split('?')[0]} -> {last}")


def to_float(s):
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if s in ("", "--", "-", "N/A", "---"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def roc_to_iso(s):
    """'1151027' 或 '115/10/27' -> '2026-10-27'"""
    s = str(s).strip().replace("/", "")
    if len(s) == 7 and s.isdigit():
        return f"{int(s[:3]) + 1911:04d}-{s[3:5]}-{s[5:7]}"
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def prev_weekday(d):
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def expected_tw_date(now_tpe):
    """台北時間 14:30 之後算當日收盤，否則算前一個工作日（不含國定假日判斷）。"""
    d = now_tpe.date()
    if now_tpe.weekday() < 5 and (now_tpe.hour, now_tpe.minute) >= (14, 30):
        return d
    return prev_weekday(d)


def expected_us_date(now_ny):
    """美東 16:15 之後算當日收盤，否則算前一個工作日（不含國定假日判斷）。"""
    d = now_ny.date()
    if now_ny.weekday() < 5 and (now_ny.hour, now_ny.minute) >= (16, 15):
        return d
    return prev_weekday(d)


# ---------------- 台股 ----------------
def fetch_twse(codes, exp_date):
    data = http_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
    by_code = {str(r.get("Code", "")).strip(): r for r in data}
    out = {}
    for c in codes:
        r = by_code.get(c)
        if not r:
            out[c] = {"market": "TW", "status": "missing", "note": "證交所資料中找不到此代號", "source": "TWSE OpenAPI"}
            continue
        close = to_float(r.get("ClosingPrice"))
        chg = to_float(r.get("Change"))
        date_iso = roc_to_iso(r.get("Date", ""))
        item = {
            "market": "TW", "name": r.get("Name"), "close": close, "change": chg,
            "prev_close": round(close - chg, 4) if close is not None and chg is not None else None,
            "data_date": date_iso, "source": "TWSE OpenAPI STOCK_DAY_ALL",
        }
        item["status"] = classify(close, date_iso, exp_date)
        out[c] = item
    return out


def fetch_tpex(codes, exp_date):
    roc = f"{exp_date.year - 1911}/{exp_date.month:02d}/{exp_date.day:02d}"
    url = ("https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/"
           "stk_quote_result.php?" + urllib.parse.urlencode({"l": "zh-tw", "d": roc, "o": "json"}))
    data = http_json(url)
    rows = data.get("aaData")
    if rows is None and data.get("tables"):
        rows = data["tables"][0].get("data", [])
    rows = rows or []
    date_iso = roc_to_iso(data.get("reportDate") or data.get("date") or roc)
    by_code = {str(r[0]).strip(): r for r in rows if r}
    out = {}
    for c in codes:
        r = by_code.get(c)
        if not r:
            out[c] = {"market": "TW", "status": "missing", "note": "櫃買中心資料中找不到此代號", "source": "TPEx"}
            continue
        close = to_float(r[2])
        chg = to_float(str(r[3]).replace("+", ""))
        item = {
            "market": "TW", "name": r[1], "close": close, "change": chg,
            "prev_close": round(close - chg, 4) if close is not None and chg is not None else None,
            "data_date": date_iso, "source": "TPEx daily_close_quotes",
        }
        item["status"] = classify(close, date_iso, exp_date)
        out[c] = item
    return out


def classify(close, date_iso, exp_date):
    if close is None:
        return "no_close"  # 當日無成交或欄位空白
    if date_iso != exp_date.isoformat():
        return "stale"     # 資料日期不是預期的收盤日（可能休市或尚未更新）
    return "ok"


# ---------------- 美股 ----------------
def fetch_finnhub(symbols, exp_date, key):
    out = {}
    for s in symbols:
        url = "https://finnhub.io/api/v1/quote?" + urllib.parse.urlencode({"symbol": s, "token": key})
        try:
            q = http_json(url)
        except Exception as e:  # noqa: BLE001
            out[s] = {"market": "US", "status": "error", "note": str(e)[:200], "source": "Finnhub quote"}
            continue
        t = q.get("t")
        c, pc = q.get("c"), q.get("pc")
        if not t or not c:
            out[s] = {"market": "US", "status": "no_data", "source": "Finnhub quote", "raw": q}
            time.sleep(1.1)
            continue
        qt = datetime.fromtimestamp(t, tz=timezone.utc).astimezone(NEW_YORK)
        item = {
            "market": "US", "close": c, "prev_close": pc,
            "change": round(c - pc, 4) if pc else None,
            "change_pct": round((c - pc) / pc * 100, 2) if pc else None,
            "quote_time_ny": qt.strftime("%Y-%m-%d %H:%M:%S"),
            "data_date": qt.date().isoformat(), "source": "Finnhub quote",
        }
        if qt.date() != exp_date:
            item["status"] = "stale"
        elif (qt.hour, qt.minute) > (16, 1):
            item["status"] = "possible_after_hours"  # 報價時間晚於 16:01，可能是盤後價，不可當收盤
        else:
            item["status"] = "ok"
        out[s] = item
        time.sleep(1.1)  # 免費方案每分鐘 60 次，保守間隔
    return out


def main():
    now_utc = datetime.now(timezone.utc)
    now_tpe, now_ny = now_utc.astimezone(TAIPEI), now_utc.astimezone(NEW_YORK)
    exp_tw, exp_us = expected_tw_date(now_tpe), expected_us_date(now_ny)
    items, errors = {}, []

    for name, fn, arg in [("TWSE", fetch_twse, TW_LISTED), ("TPEx", fetch_tpex, TW_OTC)]:
        try:
            items.update(fn(arg, exp_tw))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            for c in arg:
                items[c] = {"market": "TW", "status": "error", "note": str(e)[:200]}

    key = os.environ.get("FINNHUB_KEY", "").strip()
    if key:
        try:
            items.update(fetch_finnhub(US_SYMBOLS, exp_us, key))
        except Exception as e:  # noqa: BLE001
            errors.append(f"Finnhub: {e}")
    else:
        errors.append("FINNHUB_KEY 未設定")
        for s in US_SYMBOLS:
            items[s] = {"market": "US", "status": "error", "note": "FINNHUB_KEY 未設定"}

    bad = sorted(k for k, v in items.items() if v.get("status") != "ok")
    result = {
        "generated_at_taipei": now_tpe.strftime("%Y-%m-%d %H:%M:%S"),
        "expected_tw_close_date": exp_tw.isoformat(),
        "expected_us_close_date": exp_us.isoformat(),
        "all_ok": not bad and not errors,
        "not_ok_symbols": bad,
        "errors": errors,
        "note": "status=ok 才可當收盤價使用；其他狀態一律視為缺漏。未判斷國定假日，休市日會顯示 stale。",
        "items": items,
    }
    path = os.path.abspath(OUT_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(f"wrote {path}; not ok: {bad or 'none'}; errors: {errors or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
