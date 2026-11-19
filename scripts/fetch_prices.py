#!/usr/bin/env python3
"""抓取台股、美股收盤價，輸出 data/prices.json、data/history.csv、data/weekly.json。

只用 Python 標準函式庫，不需 pip install。
台股：證交所 OpenAPI（上市／ETF）、櫃買中心（上櫃）。不需 key。
美股：Finnhub /quote。需要環境變數 FINNHUB_KEY。

重要原則：抓不到、日期不對、疑似盤後價的標的，一律標記 status，不得當成收盤價使用。
history.csv 只記 status=ok 的收盤；weekly.json 由 history.csv 與證交所資料計算（週報用）。
"""
import csv
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
# 台股兩邊都會查，分類記錯也抓得到；新增或賣出持股時改這裡。
TW_LISTED = ["0050", "00981A", "00631L", "2330", "2441", "6415", "4989"]   # 上市／ETF
TW_OTC = ["1815", "3081"]                                                   # 上櫃
US_HOLDINGS = ["QQQ", "SPY", "MU", "MRVL", "NVDA", "ARM", "NOK", "SPCX", "TSM"]
US_SECTOR_ETFS = ["SMH", "XLK", "XLF", "XLU", "XLP", "XLE", "TLT", "GLD"]   # 週報類股／資金輪動用
US_SYMBOLS = US_HOLDINGS + US_SECTOR_ETFS

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
OUT_PATH = os.path.join(DATA_DIR, "prices.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.csv")
WEEKLY_PATH = os.path.join(DATA_DIR, "weekly.json")
HISTORY_FIELDS = ["date", "symbol", "market", "close", "source"]
WINDOW_3M = 63  # 近 3 個月約 63 個交易日
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


# ---------------- 歷史紀錄 ----------------
def load_history():
    rows = {}
    path = os.path.abspath(HISTORY_PATH)
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                rows[(r["date"], r["symbol"])] = r
    return rows


def save_history(rows):
    path = os.path.abspath(HISTORY_PATH)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        w.writeheader()
        for k in sorted(rows, key=lambda k: (k[1], k[0])):
            w.writerow({x: rows[k].get(x, "") for x in HISTORY_FIELDS})


def update_history(rows, items):
    """只記 status=ok 的收盤，同一天同代號以最新一次為準。"""
    for sym, v in items.items():
        if v.get("status") == "ok" and v.get("close") is not None and v.get("data_date"):
            rows[(v["data_date"], sym)] = {
                "date": v["data_date"], "symbol": sym, "market": v.get("market", ""),
                "close": v["close"], "source": v.get("source", ""),
            }


def pct(a, b):
    return round((a - b) / b * 100, 2) if a is not None and b else None


def series_stats(series, expected_date):
    """series: [(date_iso, close)] 依日期排序。回傳本週／上週收盤、週漲跌、近 3 個月高點與回檔。"""
    if not series:
        return {"status": "no_history"}
    last_d, last_c = series[-1]
    d = datetime.strptime(last_d, "%Y-%m-%d").date()
    monday = (d - timedelta(days=d.weekday())).isoformat()
    prev = [x for x in series if x[0] < monday]
    win = series[-WINDOW_3M:]
    hi_d, hi_c = max(win, key=lambda x: (x[1], x[0]))
    out = {
        "latest_date": last_d, "latest_close": last_c,
        "latest_is_expected": last_d == expected_date,
        "prev_week_date": prev[-1][0] if prev else None,
        "prev_week_close": prev[-1][1] if prev else None,
        "week_change_pct": pct(last_c, prev[-1][1]) if prev else None,
        "high_3m": hi_c, "high_3m_date": hi_d,
        "drawdown_from_high_pct": pct(last_c, hi_c),
        "history_days": len(win), "history_start": win[0][0],
        "high_3m_complete": len(series) >= WINDOW_3M,
    }
    out["status"] = "ok" if out["latest_is_expected"] and prev else "incomplete"
    return out


# ---------------- 證交所：加權指數、成交金額、三大法人 ----------------
def twse_rwd(path, **params):
    params["response"] = "json"
    data = http_json("https://www.twse.com.tw/rwd/zh/" + path + "?" + urllib.parse.urlencode(params))
    if data.get("stat") != "OK":
        raise RuntimeError(f"{path} {params}: {data.get('stat')}")
    return data


def fetch_taiex_months(end_date, months=4):
    """回傳 [(date_iso, taiex, trade_value)]，涵蓋最近 months 個月。"""
    out, y, m = {}, end_date.year, end_date.month
    for _ in range(months):
        data = twse_rwd("afterTrading/FMTQIK", date=f"{y:04d}{m:02d}01")
        for r in data.get("data", []):
            di = roc_to_iso(r[0])
            if di:
                out[di] = (to_float(r[4]), to_float(r[2]))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        time.sleep(1)
    return sorted((d, v[0], v[1]) for d, v in out.items())


INST_MAP = {
    "foreign": ["外資及陸資(不含外資自營商)", "外資自營商"],
    "trust": ["投信"],
    "dealer": ["自營商(自行買賣)", "自營商(避險)"],
    "total": ["合計"],
}


def fetch_institutional(day_iso):
    data = twse_rwd("fund/BFI82U", type="day", dayDate=day_iso.replace("-", ""))
    by = {r[0]: to_float(r[3]) or 0.0 for r in data.get("data", [])}
    return {k: round(sum(by.get(n, 0.0) for n in names) / 1e8, 2) for k, names in INST_MAP.items()}


def build_tw_market(exp_tw, errors):
    out = {"note": "加權指數與成交金額來自證交所 FMTQIK；三大法人來自 BFI82U，單位億元（正＝買超）。"}
    try:
        rows = fetch_taiex_months(exp_tw)
    except Exception as e:  # noqa: BLE001
        errors.append(f"TWSE FMTQIK: {e}")
        return {"status": "error", "note": str(e)[:200]}
    series = [(d, c) for d, c, _ in rows if c is not None]
    stats = series_stats(series, exp_tw.isoformat())
    stats["high_3m_complete"] = len(series) >= WINDOW_3M
    if stats.get("prev_week_date") and stats.get("latest_is_expected"):
        stats["status"] = "ok"
    out["taiex"] = stats
    if not series:
        return out
    last_d = datetime.strptime(series[-1][0], "%Y-%m-%d").date()
    monday = last_d - timedelta(days=last_d.weekday())
    prev_monday = monday - timedelta(days=7)
    this_week = [(d, c, v) for d, c, v in rows if d >= monday.isoformat()]
    prev_week = [(d, c, v) for d, c, v in rows if prev_monday.isoformat() <= d < monday.isoformat()]
    tv_this = sum(v for _, _, v in this_week if v)
    tv_prev = sum(v for _, _, v in prev_week if v)
    out["trade_value"] = {
        "this_week_days": [d for d, _, _ in this_week],
        "this_week_total_100m": round(tv_this / 1e8, 1),
        "this_week_avg_100m": round(tv_this / 1e8 / len(this_week), 1) if this_week else None,
        "prev_week_total_100m": round(tv_prev / 1e8, 1),
        "prev_week_avg_100m": round(tv_prev / 1e8 / len(prev_week), 1) if prev_week else None,
        "avg_change_pct": pct(tv_this / len(this_week), tv_prev / len(prev_week)) if this_week and prev_week else None,
    }
    daily, week_sum = {}, {k: 0.0 for k in INST_MAP}
    for d, _, _ in this_week:
        try:
            daily[d] = fetch_institutional(d)
            for k in INST_MAP:
                week_sum[k] += daily[d][k]
        except Exception as e:  # noqa: BLE001
            daily[d] = {"status": "error", "note": str(e)[:200]}
            errors.append(f"TWSE BFI82U {d}: {e}")
        time.sleep(1)
    out["institutional"] = {
        "daily_100m": daily,
        "week_total_100m": {k: round(v, 2) for k, v in week_sum.items()},
        "complete": all("status" not in v for v in daily.values()),
    }
    return out


def build_weekly(history, exp_tw, exp_us, now_tpe, tw_market):
    by_sym = {}
    for (d, sym), r in history.items():
        by_sym.setdefault(sym, []).append((d, float(r["close"]), r.get("market", "")))
    items = {}
    for sym, rows in sorted(by_sym.items()):
        rows.sort()
        market = rows[-1][2]
        exp = exp_tw if market == "TW" else exp_us
        st = series_stats([(d, c) for d, c, _ in rows], exp.isoformat())
        st["market"] = market
        if sym in US_SECTOR_ETFS:
            st["group"] = "sector_etf"
        items[sym] = st
    return {
        "generated_at_taipei": now_tpe.strftime("%Y-%m-%d %H:%M:%S"),
        "expected_tw_close_date": exp_tw.isoformat(),
        "expected_us_close_date": exp_us.isoformat(),
        "note": ("週漲跌＝最新收盤對上週最後一個交易日收盤。latest_is_expected=false 代表最新收盤不是預期日期，"
                 "不可當本週收盤。high_3m 取最近 63 個交易日；high_3m_complete=false 代表歷史資料不足 63 天，"
                 "高點需另行網頁核對。status=ok 才可直接使用。"),
        "tw_market": tw_market,
        "items": items,
    }


def main():
    now_utc = datetime.now(timezone.utc)
    now_tpe, now_ny = now_utc.astimezone(TAIPEI), now_utc.astimezone(NEW_YORK)
    exp_tw, exp_us = expected_tw_date(now_tpe), expected_us_date(now_ny)
    items, errors = {}, []

    # 台股：兩邊都查全部代號，哪邊找得到就用哪邊（避免上市／上櫃分類記錯）
    tw_all = sorted(set(TW_LISTED + TW_OTC))
    for name, fn in [("TWSE", fetch_twse), ("TPEx", fetch_tpex)]:
        try:
            res = fn(tw_all, exp_tw)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            res = {c: {"market": "TW", "status": "error", "note": str(e)[:200]} for c in tw_all}
        for c, v in res.items():
            old = items.get(c)
            if old is None or (old.get("status") != "ok" and v.get("status") != "missing"):
                items[c] = v

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

    # 歷史紀錄與週報資料（失敗不影響 prices.json）
    try:
        history = load_history()
        update_history(history, items)
        save_history(history)
        week_errors = []
        tw_market = build_tw_market(exp_tw, week_errors)
        weekly = build_weekly(history, exp_tw, exp_us, now_tpe, tw_market)
        weekly["errors"] = week_errors
        with open(os.path.abspath(WEEKLY_PATH), "w", encoding="utf-8") as f:
            json.dump(weekly, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        print(f"wrote weekly.json; history rows: {len(history)}; errors: {week_errors or 'none'}")
    except Exception as e:  # noqa: BLE001
        print(f"weekly build failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
