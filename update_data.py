import os
import json
import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

ASSETS = {
    "qld": ("QLD", "QLD"),
    "qqq": ("QQQ", "QQQ"),
    "voo": ("VOO", "VOO"),
    "brkb": ("BRK-B", "BRK.B"),
    "visa": ("V", "Visa"),
}

KRX_GOLD_URL = "https://data-dbg.krx.co.kr/svc/apis/gen/gold_bydd_trd"


def now_kst():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))


def now_ny():
    return datetime.datetime.now(ZoneInfo("America/New_York"))


def clean_number(value):
    if value is None:
        return 0.0
    s = str(value).replace(",", "").strip()
    if not s or s == "-":
        return 0.0
    return float(s)


def latest_price(ticker):
    try:
        h = yf.Ticker(ticker).history(period="1d", interval="5m", prepost=False)
        close = h["Close"].dropna() if not h.empty else []
        if len(close):
            return round(float(close.iloc[-1]), 4)
    except Exception as e:
        print(f"{ticker} intraday error: {e}")

    try:
        h = yf.Ticker(ticker).history(period="5d", interval="1d")
        close = h["Close"].dropna() if not h.empty else []
        if len(close):
            return round(float(close.iloc[-1]), 4)
    except Exception as e:
        print(f"{ticker} daily fallback error: {e}")

    return 0


def calculate_monthly_heikin_ashi(history):
    """
    QQQ의 완료된 월봉만 사용해 표준 Heikin Ashi 상태를 계산한다.
    현재 진행 중인 달은 신호에 포함하지 않는다.
    """
    empty = {
        "state": None,
        "previous_state": None,
        "changed": False,
        "transition": "none",
        "completed_month": None,
        "ha_open": 0,
        "ha_close": 0,
    }

    if history is None or history.empty:
        return empty

    df = history[["Open", "High", "Low", "Close"]].dropna().copy()
    if df.empty:
        return empty

    idx = df.index
    if getattr(idx, "tz", None) is not None:
        naive_idx = idx.tz_convert("America/New_York").tz_localize(None)
    else:
        naive_idx = idx

    current_period = pd.Period(now_ny().strftime("%Y-%m"), freq="M")
    periods = naive_idx.to_period("M")
    mask = periods < current_period
    df = df.loc[mask]
    periods = periods[mask]

    if df.empty:
        return empty

    monthly = df.groupby(periods).agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
    }).dropna()

    if len(monthly) < 2:
        return empty

    ha_opens = []
    ha_closes = []

    for row in monthly.itertuples():
        ha_close = (float(row.Open) + float(row.High) + float(row.Low) + float(row.Close)) / 4
        if not ha_opens:
            ha_open = (float(row.Open) + float(row.Close)) / 2
        else:
            ha_open = (ha_opens[-1] + ha_closes[-1]) / 2
        ha_opens.append(ha_open)
        ha_closes.append(ha_close)

    states = ["green" if c > o else "red" for o, c in zip(ha_opens, ha_closes)]
    state = states[-1]
    previous_state = states[-2]
    changed = state != previous_state

    if previous_state == "red" and state == "green":
        transition = "red_to_green"
    elif previous_state == "green" and state == "red":
        transition = "green_to_red"
    else:
        transition = "none"

    return {
        "state": state,
        "previous_state": previous_state,
        "changed": changed,
        "transition": transition,
        "completed_month": str(monthly.index[-1]),
        "ha_open": round(ha_opens[-1], 4),
        "ha_close": round(ha_closes[-1], 4),
    }


def fetch_krx_gold():
    """KRX 금시장 '금 99.99_1kg'의 최근 거래일 종가(원/g)를 반환."""
    auth_key = os.getenv("KRX_API_KEY", "").strip()
    if not auth_key:
        print("KRX_API_KEY is missing.")
        return None

    headers = {"AUTH_KEY": auth_key}
    today = now_kst().date()

    for days_back in range(0, 10):
        d = today - datetime.timedelta(days=days_back)
        if d.weekday() >= 5:
            continue

        bas_dd = d.strftime("%Y%m%d")
        try:
            r = requests.get(
                KRX_GOLD_URL,
                headers=headers,
                params={"basDd": bas_dd},
                timeout=20,
            )
            r.raise_for_status()
            payload = r.json()
            rows = payload.get("OutBlock_1", [])

            if not isinstance(rows, list) or not rows:
                continue

            target = next(
                (x for x in rows if str(x.get("ISU_NM", "")).strip() == "금 99.99_1kg"),
                None,
            )
            if target is None:
                target = next(
                    (
                        x for x in rows
                        if "1kg" in str(x.get("ISU_NM", ""))
                        and "99.99" in str(x.get("ISU_NM", ""))
                    ),
                    None,
                )

            if target is None:
                continue

            price = clean_number(target.get("TDD_CLSPRC"))
            if price <= 0:
                continue

            return {
                "ticker": target.get("ISU_CD"),
                "name": target.get("ISU_NM", "금 99.99_1kg"),
                "price": round(price, 2),
                "currency": "KRW",
                "unit": "g",
                "market_date": target.get("BAS_DD", bas_dd),
                "change": clean_number(target.get("CMPPREVDD_PRC")),
                "pct": clean_number(target.get("FLUC_RT")),
                "status": "ok",
            }

        except requests.HTTPError as e:
            print(f"KRX gold HTTP error for {bas_dd}: {e}")
            if r.status_code in (401, 403):
                break
        except Exception as e:
            print(f"KRX gold error for {bas_dd}: {e}")

    return None


def get_market_data():
    print("Market data fetching started...")

    fx_rate = fx_change = fx_pct = 0
    try:
        fx_hist = yf.Ticker("KRW=X").history(period="10d")
        if len(fx_hist) >= 2:
            current_fx = float(fx_hist["Close"].iloc[-1])
            prev_fx = float(fx_hist["Close"].iloc[-2])
            fx_rate = round(current_fx, 2)
            fx_change = round(current_fx - prev_fx, 2)
            if prev_fx > 0:
                fx_pct = round((current_fx - prev_fx) / prev_fx * 100, 2)
    except Exception as e:
        print(f"FX Fetch Error: {e}")

    # QQQ: 현재 가격 + 참고용 120/200일선 + 핵심 전략인 확정 월봉 HA
    qqq_price = qqq_change = qqq_pct = sma_120 = sma_200 = 0
    qqq_ha = {
        "state": None,
        "previous_state": None,
        "changed": False,
        "transition": "none",
        "completed_month": None,
        "ha_open": 0,
        "ha_close": 0,
    }
    try:
        qqq_hist = yf.Ticker("QQQ").history(period="10y", interval="1d", auto_adjust=True)
        closes = qqq_hist["Close"].dropna()
        if len(closes) >= 200:
            current_close = float(closes.iloc[-1])
            prev_close = float(closes.iloc[-2])
            qqq_price = round(current_close, 2)
            qqq_change = round(current_close - prev_close, 2)
            if prev_close > 0:
                qqq_pct = round((current_close - prev_close) / prev_close * 100, 2)
            sma_120 = round(float(closes.rolling(120).mean().iloc[-1]), 2)
            sma_200 = round(float(closes.rolling(200).mean().iloc[-1]), 2)
            qqq_ha = calculate_monthly_heikin_ashi(qqq_hist)
        else:
            print(f"QQQ Fetch Error: not enough history ({len(closes)} rows)")
    except Exception as e:
        print(f"QQQ Fetch Error: {e}")

    # VXN은 매매조건이 아니라 참고용 변동성 지표
    vxn_price = vxn_change = vxn_pct = 0
    try:
        vxn_hist = yf.Ticker("^VXN").history(period="10d", interval="1d")
        vxn_closes = vxn_hist["Close"].dropna()
        if len(vxn_closes) >= 2:
            current_vxn = float(vxn_closes.iloc[-1])
            prev_vxn = float(vxn_closes.iloc[-2])
            vxn_price = round(current_vxn, 2)
            vxn_change = round(current_vxn - prev_vxn, 2)
            if prev_vxn > 0:
                vxn_pct = round((current_vxn - prev_vxn) / prev_vxn * 100, 2)
    except Exception as e:
        print(f"VXN Fetch Error: {e}")

    stamp = now_kst().strftime("%Y-%m-%d %H:%M:%S KST")

    prices = {}
    for key, (ticker, name) in ASSETS.items():
        prices[key] = {
            "ticker": ticker,
            "name": name,
            "price": latest_price(ticker),
            "currency": "USD",
            "unit": "주",
            "updated_at": stamp,
        }

    gold = fetch_krx_gold()
    if gold:
        gold["updated_at"] = stamp
        prices["gold"] = gold
    else:
        prices["gold"] = {
            "ticker": None,
            "name": "KRX 금",
            "price": 0,
            "currency": "KRW",
            "unit": "g",
            "updated_at": stamp,
            "status": "unavailable",
        }

    data = {
        "updated_at": stamp,
        "strategy_version": "qqq_monthly_heikin_ashi_v1",
        "fx_rate": fx_rate,
        "fx_change": fx_change,
        "fx_pct": fx_pct,
        "qqq": {
            "price": qqq_price,
            "change": qqq_change,
            "pct": qqq_pct,
            "sma_120": sma_120,
            "sma_200": sma_200,
            "ha_monthly": qqq_ha,
        },
        "vxn": {
            "price": vxn_price,
            "change": vxn_change,
            "pct": vxn_pct,
        },
        "prices": prices,
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

    print(f"QQQ monthly HA: {qqq_ha}")
    print("data.json updated successfully!")


if __name__ == "__main__":
    get_market_data()
