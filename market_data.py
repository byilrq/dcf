#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DCF market data adapter layer.

Only stable internal source keys are used between programs:
- A/ETF realtime: live_a1/live_a2/live_a3
- HK realtime: live_hk1/live_hk2/live_hk3
- A/ETF historical: historical_a1/historical_a2
- HK historical: historical_hk1/historical_hk2

Customer-facing names, source options, normalization and concrete API logic all live here.
"""
from __future__ import annotations

import json
import logging
import random
import time as time_module
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

BASE_DIR = Path(__file__).resolve().parent
SYSTEM_CONFIG_FILE = BASE_DIR / "system_config.json"
CACHE_DIR = BASE_DIR / "data" / "bars"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

SYSTEM_DEFAULTS = {
    "A_QUOTE_SOURCE": "live_a1",
    "HK_MARKET_SOURCE": "live_hk1",
    "A_BACKTEST_SOURCE": "historical_a1",
    "HK_BACKTEST_SOURCE": "historical_hk1",
    "XUEQIU_TOKEN": "",
}

SOURCE_OPTIONS: Dict[str, List[Tuple[str, str]]] = {
    "A_QUOTE_SOURCE": [
        ("live_a1", "腾讯实时"),
        ("live_a2", "雪球实时"),
        ("live_a3", "东方财富实时"),
    ],
    "HK_MARKET_SOURCE": [
        ("live_hk1", "腾讯港股"),
        ("live_hk2", "雪球港股"),
        ("live_hk3", "东方财富港股"),
    ],
    "A_BACKTEST_SOURCE": [
        ("historical_a1", "腾讯A股/ETF日K"),
        ("historical_a2", "新浪A股/ETF日K"),
    ],
    "HK_BACKTEST_SOURCE": [
        ("historical_hk1", "腾讯港股日K"),
        ("historical_hk2", "Yahoo港股日K"),
    ],
}

SOURCE_DISPLAY = {key: label for items in SOURCE_OPTIONS.values() for key, label in items}
DISPLAY_TO_KEY = {label: key for key, label in SOURCE_DISPLAY.items()}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

@dataclass
class MarketSnapshot:
    symbol: str
    source: str
    closes: List[float]
    price_scale: float = 1.0
    last_bar_date: Optional[str] = None
    error: str = ""
    trade_allowed: bool = True
    dates: Optional[List[str]] = None
    strategy_source: str = ""
    strategy_status: str = "OK"

    @property
    def ok(self) -> bool:
        return bool(self.closes)

    @property
    def current_price(self) -> float:
        return round(float(self.closes[-1]), 4) if self.closes else 0.0


def get_source_options(field: str) -> List[Tuple[str, str]]:
    return list(SOURCE_OPTIONS.get(str(field or ""), []))


def get_all_source_options() -> Dict[str, List[Tuple[str, str]]]:
    return {k: list(v) for k, v in SOURCE_OPTIONS.items()}


def get_source_display_name(source: str) -> str:
    s = str(source or "").strip()
    if not s:
        return "未知"
    if s.startswith("cache_"):
        return "本地缓存"
    return SOURCE_DISPLAY.get(s, SOURCE_DISPLAY.get(DISPLAY_TO_KEY.get(s, ""), s))


def get_source_canonical_key(source: str) -> str:
    s = str(source or "").strip()
    if not s:
        return ""
    if s.startswith("cache_"):
        return "cache"
    return DISPLAY_TO_KEY.get(s, s)


def normalize_system_source_value(field: str, value: str) -> str:
    raw = str(value or "").strip()
    allowed = {key for key, _ in SOURCE_OPTIONS.get(field, [])}
    if raw in allowed:
        return raw
    # Accept current customer-facing labels only. No legacy source-name compatibility.
    mapped = DISPLAY_TO_KEY.get(raw, raw)
    if mapped in allowed:
        return mapped
    return SYSTEM_DEFAULTS.get(field, raw)


def _load_system_config() -> dict:
    cfg = dict(SYSTEM_DEFAULTS)
    try:
        if SYSTEM_CONFIG_FILE.exists():
            raw = json.loads(SYSTEM_CONFIG_FILE.read_text(encoding="utf-8") or "{}")
            if isinstance(raw, dict):
                cfg.update({k: "" if v is None else str(v).strip() for k, v in raw.items()})
    except Exception as e:
        logging.debug(f"读取系统行情配置失败: {e}")
    for field in SOURCE_OPTIONS:
        cfg[field] = normalize_system_source_value(field, cfg.get(field, SYSTEM_DEFAULTS.get(field, "")))
    return cfg


def _headers(referer: str = "https://quote.eastmoney.com/") -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "close",
    }


def _preferred_order(items, preferred_key: str):
    preferred_key = str(preferred_key or "").strip()
    return [x for x in items if x[0] == preferred_key] + [x for x in items if x[0] != preferred_key]


def _cache_path(symbol: str) -> Path:
    safe = str(symbol or "").upper().replace("/", "_")
    return CACHE_DIR / f"{safe}.json"


def _write_cache(snapshot: MarketSnapshot) -> None:
    if not snapshot.closes or str(snapshot.source).startswith("cache_"):
        return
    payload = {
        "symbol": snapshot.symbol,
        "source": snapshot.source,
        "strategy_source": snapshot.strategy_source,
        "last_bar_date": snapshot.last_bar_date,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "dates": (snapshot.dates or [])[-800:],
        "closes": snapshot.closes[-800:],
    }
    try:
        _cache_path(snapshot.symbol).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logging.debug(f"写入行情缓存失败 {snapshot.symbol}: {e}")


def _read_cache(symbol: str, days: int, price_scale: float, reason: str) -> MarketSnapshot:
    path = _cache_path(symbol)
    if not path.exists():
        raise RuntimeError(reason + "；无本地缓存")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        closes = []
        for x in data.get("closes") or []:
            try:
                v = float(x)
                if v > 0:
                    closes.append(round(v, 4))
            except Exception:
                continue
        if len(closes) < 2:
            raise RuntimeError("缓存K线不足")
        n = max(int(days), 2)
        dates = data.get("dates") or []
        return MarketSnapshot(
            symbol=str(data.get("symbol") or symbol).upper(),
            source="cache_" + str(data.get("source") or "unknown"),
            closes=closes[-n:],
            price_scale=price_scale,
            last_bar_date=data.get("last_bar_date"),
            error=reason + "；已使用本地缓存，仅监控不交易",
            trade_allowed=False,
            dates=dates[-n:] if isinstance(dates, list) else [],
            strategy_source=data.get("strategy_source") or "",
            strategy_status="WARN",
        )
    except Exception as e:
        raise RuntimeError(reason + f"；读取本地缓存失败: {e}")


def _is_hk(symbol: str) -> bool:
    return str(symbol or "").upper().strip().startswith("HK")


def _hk_code(symbol_or_code: str) -> str:
    raw = str(symbol_or_code or "").upper().strip()
    if raw.startswith("HK"):
        raw = raw[2:]
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits.zfill(5)


def _tencent_a_symbol(symbol: str) -> str:
    raw = str(symbol or "").upper().strip()
    if raw.startswith("SH"):
        return "sh" + raw[2:]
    if raw.startswith("SZ"):
        return "sz" + raw[2:]
    if raw.isdigit() and len(raw) == 6:
        return ("sh" if raw.startswith("6") else "sz") + raw
    raise ValueError(f"不支持的A股代码格式: {symbol}")


def _sina_a_symbol(symbol: str) -> str:
    return _tencent_a_symbol(symbol)


def _eastmoney_a_secid(symbol: str) -> str:
    raw = str(symbol or "").upper().strip()
    if raw.startswith("SH"):
        return "1." + raw[2:]
    if raw.startswith("SZ"):
        return "0." + raw[2:]
    if raw.isdigit() and len(raw) == 6:
        return ("1." if raw.startswith("6") else "0.") + raw
    raise ValueError(f"不支持的A股代码格式: {symbol}")


def _eastmoney_hk_secids(symbol_or_code: str) -> List[str]:
    code = _hk_code(symbol_or_code)
    nozero = code.lstrip("0") or code
    return list(dict.fromkeys(["116." + code, "116." + nozero]))


def _parse_tencent_json(text: str) -> dict:
    text = str(text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError(f"腾讯返回无法解析: {text[:160]}")
    return json.loads(text[start:end + 1])


def _parse_volume(value, default=1.0) -> float:
    try:
        if value in (None, "", "-"):
            return default
        return float(value)
    except Exception:
        return default


def _fetch_tencent_a_snapshot(symbol: str, days: int, price_scale: float) -> MarketSnapshot:
    t_symbol = _tencent_a_symbol(symbol)
    lmt = max(int(days), 2)
    url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
    params = [
        f"{t_symbol},day,,,{min(max(lmt, 2), 800)},day",
        f"{t_symbol},day,,,{min(max(lmt, 2), 800)},",
    ]
    rows, last_err = [], None
    for param in params:
        try:
            resp = requests.get(url, params={"param": param, "r": str(random.random())}, headers=_headers("https://gu.qq.com/"), timeout=12)
            resp.raise_for_status()
            data = _parse_tencent_json(resp.text)
            node = (((data or {}).get("data") or {}).get(t_symbol) or {})
            rows = node.get("day") or node.get("qfqday") or node.get("hfqday") or []
            if rows:
                break
            last_err = f"empty response={str(data)[:220]}"
        except Exception as e:
            last_err = e
    dedup = {}
    for row in rows:
        try:
            date = str(row[0])
            close_price = float(row[2]) * float(price_scale)
            volume = _parse_volume(row[5] if len(row) > 5 else 1)
            if date and close_price > 0 and volume > 0:
                dedup[date] = round(close_price, 4)
        except Exception:
            continue
    dates = sorted(dedup.keys())
    closes = [dedup[d] for d in dates]
    if len(closes) < 2:
        raise RuntimeError(f"腾讯A股/ETF日K不足: {symbol}, count={len(closes)}, last_error={last_err}")
    n = min(lmt, len(closes))
    snap = MarketSnapshot(symbol.upper(), get_source_display_name("historical_a1"), closes[-n:], price_scale, dates[-1], dates=dates[-n:], strategy_source=get_source_display_name("historical_a1"))
    _write_cache(snap)
    return snap


def _fetch_sina_a_snapshot(symbol: str, days: int, price_scale: float) -> MarketSnapshot:
    raw = str(symbol or "").upper().strip()
    s_symbol = _sina_a_symbol(raw)
    n = max(int(days), 2)
    url = "https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData"
    resp = requests.get(url, params={"symbol": s_symbol, "scale": "240", "ma": "no", "datalen": str(n)}, headers=_headers("https://finance.sina.com.cn/"), timeout=12)
    resp.raise_for_status()
    data = resp.json()
    status = (((data or {}).get("result") or {}).get("status") or {})
    if status.get("code") not in (0, "0", None):
        raise RuntimeError(f"新浪A股/ETF日K接口错误: {status}")
    part = (((data or {}).get("result") or {}).get("data"))
    rows = part.get("data") if isinstance(part, dict) else part
    rows = rows or []
    pairs = []
    for row in rows:
        try:
            date = str(row.get("day") or row.get("date") or "")
            close_price = float(row.get("close")) * float(price_scale)
            volume = _parse_volume(row.get("volume", 1))
            if date and close_price > 0 and volume > 0:
                pairs.append((date, round(close_price, 4)))
        except Exception:
            continue
    pairs.sort(key=lambda x: x[0])
    if len(pairs) < 2:
        raise RuntimeError(f"新浪A股/ETF日K为空: {symbol}, count={len(pairs)}")
    dates = [d for d, _ in pairs[-n:]]
    closes = [c for _, c in pairs[-n:]]
    snap = MarketSnapshot(raw, get_source_display_name("historical_a2"), closes, price_scale, dates[-1] if dates else None, dates=dates, strategy_source=get_source_display_name("historical_a2"))
    _write_cache(snap)
    return snap


def _fetch_tencent_hk_snapshot(symbol_or_code: str, days: int, price_scale: float) -> MarketSnapshot:
    code = _hk_code(symbol_or_code)
    t_symbol = "hk" + code
    lmt = max(int(days), 2)
    url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
    params = [
        f"{t_symbol},day,,,{min(max(lmt, 2), 800)},day",
        f"{t_symbol},day,,,{min(max(lmt, 2), 800)},",
    ]
    rows, last_err = [], None
    for param in params:
        try:
            resp = requests.get(url, params={"param": param, "r": str(random.random())}, headers=_headers("https://gu.qq.com/"), timeout=12)
            resp.raise_for_status()
            data = _parse_tencent_json(resp.text)
            node = (((data or {}).get("data") or {}).get(t_symbol) or {})
            rows = node.get("day") or node.get("qfqday") or node.get("hfqday") or []
            if rows:
                break
            last_err = f"empty response={str(data)[:220]}"
        except Exception as e:
            last_err = e
    dedup = {}
    for row in rows:
        try:
            date = str(row[0])
            close_price = float(row[2]) * float(price_scale)
            volume = _parse_volume(row[5] if len(row) > 5 else 1)
            if date and close_price > 0 and volume > 0:
                dedup[date] = round(close_price, 4)
        except Exception:
            continue
    dates = sorted(dedup.keys())
    closes = [dedup[d] for d in dates]
    if len(closes) < 2:
        raise RuntimeError(f"腾讯港股日K不足: HK{code}, count={len(closes)}, last_error={last_err}")
    n = min(lmt, len(closes))
    snap = MarketSnapshot("HK" + code, get_source_display_name("historical_hk1"), closes[-n:], price_scale, dates[-1], dates=dates[-n:], strategy_source=get_source_display_name("historical_hk1"))
    _write_cache(snap)
    return snap


def _yahoo_hk_symbol(symbol_or_code: str) -> str:
    code = _hk_code(symbol_or_code)
    return f"{code[-4:].zfill(4)}.HK"


def _fetch_yahoo_hk_snapshot(symbol_or_code: str, days: int, price_scale: float) -> MarketSnapshot:
    code = _hk_code(symbol_or_code)
    yf_symbol = _yahoo_hk_symbol(code)
    n = max(int(days), 2)
    years = max(2, int(n / 220) + 2)
    period1 = int((datetime.now() - timedelta(days=years * 370)).timestamp())
    period2 = int((datetime.now() + timedelta(days=2)).timestamp())
    params = {
        "period1": str(period1),
        "period2": str(period2),
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
    }
    last_err = None
    data = None
    for host in ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]:
        try:
            resp = requests.get(f"https://{host}/v8/finance/chart/{yf_symbol}", params=params, headers=_headers("https://finance.yahoo.com/"), timeout=15)
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:
            last_err = e
    if data is None:
        raise RuntimeError(f"Yahoo港股日K下载失败 {yf_symbol}: {last_err}")
    chart = ((data or {}).get("chart") or {})
    if chart.get("error"):
        raise RuntimeError(f"Yahoo港股日K返回错误 {yf_symbol}: {chart.get('error')}")
    result = chart.get("result") or []
    if not result:
        raise RuntimeError(f"Yahoo港股日K为空 {yf_symbol}")
    node = result[0] or {}
    timestamps = node.get("timestamp") or []
    quote = (((node.get("indicators") or {}).get("quote") or [{}])[0] or {})
    closes_raw = quote.get("close") or []
    volumes = quote.get("volume") or []
    pairs = []
    for ts, close_value, volume_value in zip(timestamps, closes_raw, volumes):
        try:
            if close_value is None:
                continue
            close_price = float(close_value) * float(price_scale)
            volume = _parse_volume(volume_value, default=0.0)
            if close_price > 0 and volume > 0:
                date = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
                pairs.append((date, round(close_price, 4)))
        except Exception:
            continue
    pairs.sort(key=lambda x: x[0])
    if len(pairs) < 2:
        raise RuntimeError(f"Yahoo港股日K有效交易日不足 {yf_symbol}: count={len(pairs)}")
    dates = [d for d, _ in pairs[-n:]]
    closes = [c for _, c in pairs[-n:]]
    snap = MarketSnapshot("HK" + code, get_source_display_name("historical_hk2"), closes, price_scale, dates[-1] if dates else None, dates=dates, strategy_source=get_source_display_name("historical_hk2"))
    _write_cache(snap)
    return snap


def _fetch_tencent_a_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str]]:
    t_symbol = _tencent_a_symbol(symbol)
    last_err = None
    for url in [f"https://qt.gtimg.cn/q={t_symbol}", f"https://qt.gtimg.cn/q=r_{t_symbol}"]:
        try:
            resp = requests.get(url, headers=_headers("https://gu.qq.com/"), timeout=8)
            resp.raise_for_status()
            resp.encoding = "gbk"
            text = resp.text.strip()
            data = text.split('="', 1)[1].rsplit('"', 1)[0]
            fields = data.split("~")
            price = float(fields[3]) if len(fields) > 3 and fields[3] else 0.0
            if price <= 0:
                raise RuntimeError(f"价格为空: {text[:120]}")
            quote_date = None
            for item in fields:
                s = str(item).strip()
                if len(s) >= 8 and s[:8].isdigit():
                    quote_date = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
                    break
            return round(price * float(price_scale), 4), quote_date
        except Exception as e:
            last_err = e
    raise RuntimeError(f"腾讯实时失败: {last_err}")


def _xueqiu_symbol(symbol: str) -> str:
    raw = str(symbol or "").upper().strip()
    if raw.startswith(("SH", "SZ")):
        return raw
    if raw.startswith("HK"):
        return _hk_code(raw)
    if raw.isdigit() and len(raw) == 6:
        return ("SH" if raw.startswith("6") else "SZ") + raw
    if raw.isdigit() and len(raw) <= 5:
        return raw.zfill(5)
    return raw


def _xueqiu_headers(symbol: str) -> dict:
    headers = _headers(f"https://xueqiu.com/S/{symbol}")
    token = str(_load_system_config().get("XUEQIU_TOKEN", "") or "").strip()
    if token:
        headers["Cookie"] = token if ("=" in token or ";" in token) else f"xq_a_token={token}; xqat={token}"
    return headers


def _fetch_xueqiu_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str]]:
    x_symbol = _xueqiu_symbol(symbol)
    resp = requests.get("https://stock.xueqiu.com/v5/stock/realtime/quotec.json", params={"symbol": x_symbol}, headers=_xueqiu_headers(x_symbol), timeout=8)
    resp.raise_for_status()
    data = resp.json()
    payload = data.get("data")
    if isinstance(payload, list):
        if not payload:
            raise RuntimeError("雪球实时行情为空")
        node = payload[0] or {}
    elif isinstance(payload, dict):
        node = payload.get("quote") if isinstance(payload.get("quote"), dict) else payload
    else:
        node = {}
    raw = str(symbol or "").upper().strip()
    if raw.startswith("HK") or (raw.isdigit() and len(raw) <= 5):
        price = node.get("current")
    else:
        price = node.get("current") or node.get("price") or node.get("last")
    if price in (None, "", "-"):
        raise RuntimeError(f"雪球无可信实时价: response={str(data)[:180]}")
    price = float(price)
    if price <= 0:
        raise RuntimeError(f"雪球实时价无效: {price}")
    qdate = None
    for key in ("timestamp", "time", "updated"):
        try:
            ts = node.get(key)
            if ts:
                ts_i = int(float(ts))
                if ts_i > 10_000_000_000:
                    ts_i = ts_i // 1000
                qdate = datetime.fromtimestamp(ts_i).strftime("%Y-%m-%d")
                break
        except Exception:
            pass
    return round(price * float(price_scale), 4), qdate


def _decode_eastmoney_price(data: dict, symbol: str, price_scale: float) -> Tuple[float, Optional[str]]:
    node = ((data or {}).get("data") or {})
    raw_price = node.get("f43")
    if raw_price in (None, "", "-"):
        raise RuntimeError(f"东方财富实时行情无价格: {symbol}")
    price = float(raw_price)
    if price <= 0:
        raise RuntimeError(f"东方财富实时价无效: {price}")
    qdate = None
    try:
        ts = node.get("f86")
        if ts:
            qdate = datetime.fromtimestamp(int(float(ts))).strftime("%Y-%m-%d")
    except Exception:
        qdate = None
    return round(price * float(price_scale), 4), qdate


def _fetch_eastmoney_a_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str]]:
    secid = _eastmoney_a_secid(symbol)
    endpoints = [
        "https://push2.eastmoney.com/api/qt/stock/get",
        "https://push2.eastmoney.com/api/qt/ulist.np/get",
    ]
    common = {"fields": "f43,f57,f58,f59,f60,f86", "fltt": "2", "invt": "2"}
    last_err = None
    for endpoint in endpoints:
        try:
            params = dict(common)
            if endpoint.endswith("stock/get"):
                params["secid"] = secid
            else:
                params["secids"] = secid
            resp = requests.get(endpoint, params=params, headers=_headers(), timeout=8)
            resp.raise_for_status()
            data = resp.json()
            if endpoint.endswith("ulist.np/get"):
                diff = (((data or {}).get("data") or {}).get("diff") or [])
                if not diff:
                    raise RuntimeError("diff为空")
                data = {"data": diff[0]}
            return _decode_eastmoney_price(data, symbol, price_scale)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"东方财富A股/ETF实时失败: {last_err}")


def _fetch_tencent_hk_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str]]:
    code = _hk_code(symbol)
    last_err = None
    for url in [f"https://qt.gtimg.cn/q=hk{code}", f"https://qt.gtimg.cn/q=r_hk{code}", f"http://qt.gtimg.cn/q=hk{code}"]:
        try:
            resp = requests.get(url, headers=_headers("https://gu.qq.com/"), timeout=8)
            resp.raise_for_status()
            resp.encoding = "gbk"
            text = resp.text.strip()
            data = text.split('="', 1)[1].rsplit('"', 1)[0]
            fields = data.split("~")
            price = float(fields[3]) if len(fields) > 3 and fields[3] else 0.0
            if price <= 0:
                raise RuntimeError(f"价格为空: {text[:120]}")
            return round(price * float(price_scale), 4), datetime.now().strftime("%Y-%m-%d")
        except Exception as e:
            last_err = e
    raise RuntimeError(f"腾讯港股实时失败: {last_err}")


def _fetch_eastmoney_hk_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str]]:
    code = _hk_code(symbol)
    endpoints = [
        "https://push2.eastmoney.com/api/qt/stock/get",
        "https://33.push2.eastmoney.com/api/qt/stock/get",
        "http://push2.eastmoney.com/api/qt/stock/get",
    ]
    params_base = {
        "fields": "f43,f57,f58,f59,f60,f86,f169,f170,f152",
        "fltt": "2",
        "invt": "2",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }
    last_err = None
    for endpoint in endpoints:
        for secid in _eastmoney_hk_secids(code):
            try:
                params = dict(params_base)
                params["secid"] = secid
                resp = requests.get(endpoint, params=params, headers=_headers("https://quote.eastmoney.com/"), timeout=8)
                resp.raise_for_status()
                return _decode_eastmoney_price(resp.json(), "HK" + code, price_scale)
            except Exception as e:
                last_err = e
    raise RuntimeError(f"东方财富港股实时失败: {last_err}")


def _fetch_a_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str], str]:
    preferred = _load_system_config().get("A_QUOTE_SOURCE", "live_a1")
    sources = _preferred_order([
        ("live_a1", _fetch_tencent_a_realtime_price),
        ("live_a2", _fetch_xueqiu_realtime_price),
        ("live_a3", _fetch_eastmoney_a_realtime_price),
    ], preferred)
    errors = []
    for key, fn in sources:
        try:
            price, date = fn(symbol, price_scale)
            return price, date, get_source_display_name(key)
        except Exception as e:
            errors.append(f"{get_source_display_name(key)}={e}")
    raise RuntimeError("A股/ETF实时价全部失败: " + "；".join(errors))


def _fetch_hk_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str], str]:
    preferred = _load_system_config().get("HK_MARKET_SOURCE", "live_hk1")
    sources = _preferred_order([
        ("live_hk1", _fetch_tencent_hk_realtime_price),
        ("live_hk2", _fetch_xueqiu_realtime_price),
        ("live_hk3", _fetch_eastmoney_hk_realtime_price),
    ], preferred)
    errors = []
    for key, fn in sources:
        try:
            price, date = fn(symbol, price_scale)
            return price, date, get_source_display_name(key)
        except Exception as e:
            errors.append(f"{get_source_display_name(key)}={e}")
    raise RuntimeError("港股实时价全部失败: " + "；".join(errors))


def get_history_snapshot_by_source(symbol: str, days: int = 400, price_scale: float = 1.0, source: str = "") -> MarketSnapshot:
    raw = str(symbol or "").upper().strip()
    cfg = _load_system_config()
    if _is_hk(raw):
        key = normalize_system_source_value("HK_BACKTEST_SOURCE", source or cfg.get("HK_BACKTEST_SOURCE"))
        if key == "historical_hk1":
            return _fetch_tencent_hk_snapshot(raw, days, price_scale)
        if key == "historical_hk2":
            return _fetch_yahoo_hk_snapshot(raw, days, price_scale)
        raise ValueError(f"不支持的港股回测/策略数据源: {source or key}")
    key = normalize_system_source_value("A_BACKTEST_SOURCE", source or cfg.get("A_BACKTEST_SOURCE"))
    if key == "historical_a1":
        return _fetch_tencent_a_snapshot(raw, days, price_scale)
    if key == "historical_a2":
        return _fetch_sina_a_snapshot(raw, days, price_scale)
    raise ValueError(f"不支持的A股/ETF回测/策略数据源: {source or key}")


def _apply_realtime(snapshot: MarketSnapshot, symbol: str, price_scale: float) -> MarketSnapshot:
    if not snapshot.closes:
        return snapshot
    try:
        if _is_hk(symbol):
            live_price, quote_date, live_source = _fetch_hk_realtime_price(symbol, price_scale)
        else:
            live_price, quote_date, live_source = _fetch_a_realtime_price(symbol, price_scale)
    except Exception as e:
        logging.warning(f"实时价叠加失败 {symbol}: {e}；将使用日K最后收盘价。")
        return snapshot
    if live_price <= 0:
        return snapshot
    closes = list(snapshot.closes)
    closes[-1] = round(live_price, 4)
    dates = list(snapshot.dates or [])
    if quote_date and dates:
        dates[-1] = quote_date
    snap = MarketSnapshot(
        symbol=snapshot.symbol,
        source=live_source,
        closes=closes,
        price_scale=snapshot.price_scale,
        last_bar_date=quote_date or snapshot.last_bar_date,
        error=snapshot.error,
        trade_allowed=snapshot.trade_allowed,
        dates=dates or snapshot.dates,
        strategy_source=snapshot.strategy_source,
        strategy_status=snapshot.strategy_status,
    )
    _write_cache(snap)
    return snap


def get_market_snapshot(symbol: str, days: int = 400, price_scale: float = 1.0) -> MarketSnapshot:
    raw = str(symbol or "").upper().strip()
    try:
        snap = get_history_snapshot_by_source(raw, days, price_scale)
        return _apply_realtime(snap, raw, price_scale)
    except Exception as e:
        return _read_cache(raw, days, price_scale, f"行情数据源失败: {e}")


def get_reference_prices(symbol: str, price_scale: float = 1.0) -> List[dict]:
    raw = str(symbol or "").upper().strip()
    if not raw:
        return []
    cfg = _load_system_config()
    if _is_hk(raw):
        preferred = cfg.get("HK_MARKET_SOURCE", "live_hk1")
        sources = [
            ("live_hk1", _fetch_tencent_hk_realtime_price),
            ("live_hk2", _fetch_xueqiu_realtime_price),
            ("live_hk3", _fetch_eastmoney_hk_realtime_price),
        ]
    else:
        preferred = cfg.get("A_QUOTE_SOURCE", "live_a1")
        sources = [
            ("live_a1", _fetch_tencent_a_realtime_price),
            ("live_a2", _fetch_xueqiu_realtime_price),
            ("live_a3", _fetch_eastmoney_a_realtime_price),
        ]
    result = []
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for key, fn in sources:
        label = get_source_display_name(key)
        try:
            price, date = fn(raw, price_scale)
            result.append({"key": key, "label": label, "price": price, "source": label, "date": date or "", "updated_at": updated_at, "ok": True, "primary": key == preferred, "error": ""})
        except Exception as e:
            result.append({"key": key, "label": label, "price": None, "source": label, "date": "", "updated_at": updated_at, "ok": False, "primary": key == preferred, "error": str(e)[:180]})
    return result


def get_price_from_api(symbol: str, price_scale: float = 1.0, last_known_price=None) -> float:
    if _is_hk(symbol):
        price, _, _ = _fetch_hk_realtime_price(symbol, price_scale)
    else:
        price, _, _ = _fetch_a_realtime_price(symbol, price_scale)
    return price


def get_history_close(symbol: str, days: int = 400, price_scale: float = 1.0) -> List[float]:
    return get_market_snapshot(symbol, days, price_scale=price_scale).closes


def get_hk_history_close(symbol_or_code: str, days: int, price_scale: float = 1.0) -> List[float]:
    return get_history_snapshot_by_source(symbol_or_code, days, price_scale=price_scale, source="historical_hk1").closes


def get_a_history_close(symbol: str, days: int, price_scale: float = 1.0) -> List[float]:
    return get_history_snapshot_by_source(symbol, days, price_scale=price_scale, source="historical_a1").closes


def get_a_price(symbol: str, price_scale: float = 1.0) -> float:
    price, _, _ = _fetch_a_realtime_price(symbol, price_scale)
    return price


def get_hk_price(symbol: str, price_scale: float = 1.0) -> float:
    price, _, _ = _fetch_hk_realtime_price(symbol, price_scale)
    return price


def self_test(symbols: Optional[List[str]] = None, days: int = 30) -> int:
    symbols = symbols or ["SH600036", "HK00700"]
    ok = 0
    for sym in symbols:
        try:
            snap = get_market_snapshot(sym, days=days)
            print(f"PASS {sym}: source={snap.source}, strategy={snap.strategy_source}, count={len(snap.closes)}, last={snap.current_price}, date={snap.last_bar_date}, trade_allowed={snap.trade_allowed}")
            ok += 1
        except Exception as e:
            print(f"FAIL {sym}: {e}")
    return 0 if ok == len(symbols) else 1


if __name__ == "__main__":
    raise SystemExit(self_test())
