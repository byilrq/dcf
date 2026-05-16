#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DCF market data adapter layer.

策略主体只应调用本文件暴露的 get_market_snapshot/get_history_close/get_price_from_api。
以后更换行情接口、调整备用源、修复参数、增加缓存，只改这个文件即可。

设计原则：
1. 返回统一 MarketSnapshot；当前价 = closes[-1]，MA 也用同一组 closes。
2. 不伪造历史 K 线；缓存只用于展示/观察，默认不允许交易。
3. 成功获取真实行情时写入本地缓存；全部接口失败时可返回缓存快照，但 trade_allowed=False。
4. 港股优先东方财富，其次 Yahoo 直连/yfinance，腾讯 proxy 作为最后真实备用。避免旧 web.ifzq.gtimg.cn。
"""

from __future__ import annotations

import json
import logging
import random
import time as time_module
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import requests

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "data" / "bars"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MARKET_DATA_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

EASTMONEY_A_ENDPOINTS = [
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
]
EASTMONEY_HK_ENDPOINTS = [
    "https://33.push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
]
# 默认不复权，避免把复权价当现价；只有 fqt=0 空时才尝试 1/2，仍会保留 source 标识。
EASTMONEY_FQT_OPTIONS = ["0", "1", "2"]


@dataclass
class MarketSnapshot:
    symbol: str
    source: str
    closes: List[float]
    price_scale: float = 1.0
    last_bar_date: Optional[str] = None
    error: str = ""
    trade_allowed: bool = True

    @property
    def ok(self) -> bool:
        return bool(self.closes)

    @property
    def current_price(self) -> float:
        return round(float(self.closes[-1]), 4) if self.closes else 0.0


def _headers(referer: str = "https://quote.eastmoney.com/") -> dict:
    return {
        "User-Agent": random.choice(MARKET_DATA_USER_AGENTS),
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


def _cache_path(symbol: str) -> Path:
    safe = str(symbol).upper().replace("/", "_")
    return CACHE_DIR / f"{safe}.json"


def _write_cache(snapshot: MarketSnapshot) -> None:
    if not snapshot.closes or snapshot.source.startswith("cache_"):
        return
    payload = {
        "symbol": snapshot.symbol,
        "source": snapshot.source,
        "last_bar_date": snapshot.last_bar_date,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "closes": snapshot.closes[-800:],
    }
    try:
        _cache_path(snapshot.symbol).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logging.debug(f"写入行情缓存失败 {snapshot.symbol}: {e}")


def _read_cache(symbol: str, days: int, price_scale: float, reason: str) -> MarketSnapshot:
    path = _cache_path(symbol)
    if not path.exists():
        raise RuntimeError(reason + "; 且无本地K线缓存")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        closes = [round(float(x), 4) for x in (data.get("closes") or []) if float(x) > 0]
        if len(closes) < 2:
            raise RuntimeError("缓存K线不足")
        return MarketSnapshot(
            symbol=str(data.get("symbol") or symbol).upper(),
            source="cache_" + str(data.get("source") or "unknown"),
            closes=closes[-max(int(days), 2):],
            price_scale=price_scale,
            last_bar_date=data.get("last_bar_date"),
            error=reason + "；已使用本地真实缓存，仅监控不交易",
            trade_allowed=False,
        )
    except Exception as e:
        raise RuntimeError(reason + f"; 读取本地缓存失败: {e}")


def _eastmoney_secid(symbol: str) -> str:
    raw = symbol.upper().strip()
    if raw.startswith("SH"):
        return "1." + raw[2:]
    if raw.startswith("SZ"):
        return "0." + raw[2:]
    if raw.isdigit() and len(raw) == 6:
        return ("1." if raw.startswith("6") else "0.") + raw
    raise ValueError(f"不支持的A股代码格式: {symbol}")


def _eastmoney_hk_secid(symbol_or_code: str) -> str:
    raw = str(symbol_or_code or "").upper().strip()
    if raw.startswith("HK"):
        raw = raw[2:]
    return "116." + raw.zfill(5)


def _parse_eastmoney_klines(data: dict, price_scale: float) -> Tuple[List[float], List[str]]:
    klines = (((data or {}).get("data") or {}).get("klines") or [])
    closes, dates = [], []
    for row in klines:
        parts = str(row).split(",")
        if len(parts) < 3:
            continue
        try:
            close_price = float(parts[2]) * float(price_scale)
            if close_price > 0:
                dates.append(parts[0])
                closes.append(round(close_price, 4))
        except Exception:
            continue
    return closes, dates


def _fetch_eastmoney_kline_snapshot(secid: str, symbol_label: str, days: int, price_scale: float, endpoints: List[str], source: str) -> MarketSnapshot:
    lmt = max(int(days), 2)
    last_err = None
    for endpoint in endpoints:
        for fqt in EASTMONEY_FQT_OPTIONS:
            headers = _headers("https://quote.eastmoney.com/")
            params = {
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101",
                "fqt": fqt,
                "end": "20500101",
                "lmt": str(lmt),
            }
            try:
                resp = requests.get(endpoint, params=params, headers=headers, timeout=12)
                resp.raise_for_status()
                data = resp.json()
                closes, dates = _parse_eastmoney_klines(data, price_scale)
                if len(closes) >= 2:
                    actual_source = f"{source}_fqt{fqt}"
                    snap = MarketSnapshot(symbol_label.upper(), actual_source, closes[-lmt:], price_scale, dates[-1] if dates else None)
                    _write_cache(snap)
                    return snap
                last_err = f"K线为空 fqt={fqt} response={str(data)[:220]}"
            except Exception as e:
                last_err = e
                logging.debug(f"东方财富K线失败 {symbol_label}: endpoint={endpoint}, fqt={fqt}, error={e}")
                continue
    raise RuntimeError(f"东方财富K线全部失败: {symbol_label}, last_error={last_err}")


def _sina_symbol(symbol: str) -> str:
    raw = symbol.upper().strip()
    if raw.startswith("SH"):
        return "sh" + raw[2:]
    if raw.startswith("SZ"):
        return "sz" + raw[2:]
    if raw.isdigit() and len(raw) == 6:
        return ("sh" if raw.startswith("6") else "sz") + raw
    raise ValueError(f"不支持的A股代码格式: {symbol}")


def _fetch_sina_a_snapshot(symbol: str, days: int, price_scale: float) -> MarketSnapshot:
    sina_symbol = _sina_symbol(symbol)
    lmt = max(int(days), 2)
    url = "https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData"
    params = {"symbol": sina_symbol, "scale": "240", "ma": "no", "datalen": str(lmt)}
    resp = requests.get(url, params=params, headers=_headers("https://finance.sina.com.cn/"), timeout=12)
    resp.raise_for_status()
    data = resp.json()
    part = (((data or {}).get("result") or {}).get("data"))
    klines = part.get("data") if isinstance(part, dict) else part
    closes, dates = [], []
    for k in klines or []:
        try:
            close_price = float(k.get("close")) * float(price_scale)
            if close_price > 0:
                dates.append(str(k.get("day") or k.get("date") or ""))
                closes.append(round(close_price, 4))
        except Exception:
            continue
    if len(closes) >= 2:
        snap = MarketSnapshot(symbol.upper(), "sina_a", closes[-lmt:], price_scale, dates[-1] if dates else None)
        _write_cache(snap)
        return snap
    raise RuntimeError(f"新浪A股K线数据不足: {symbol}, count={len(closes)}, response={str(data)[:220]}")



def _tencent_a_symbol(symbol: str) -> str:
    raw = str(symbol or "").upper().strip()
    if raw.startswith("SH"):
        return "sh" + raw[2:]
    if raw.startswith("SZ"):
        return "sz" + raw[2:]
    if raw.isdigit() and len(raw) == 6:
        return ("sh" if raw.startswith("6") else "sz") + raw
    raise ValueError(f"不支持的A股代码格式: {symbol}")


def _fetch_tencent_a_snapshot(symbol: str, days: int, price_scale: float) -> MarketSnapshot:
    """A股/ETF备用：腾讯 proxy newfqkline，不复权。"""
    t_symbol = _tencent_a_symbol(symbol)
    lmt = max(int(days), 2)
    url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
    candidates = [
        f"{t_symbol},day,,,{min(max(lmt, 2), 800)},day",
        f"{t_symbol},day,,,{min(max(lmt, 2), 800)},",
    ]
    now = datetime.now()
    years_back = max(3, int(lmt / 220) + 2)
    candidates.append(f"{t_symbol},day,{now.year-years_back}-01-01,{now.year+1}-12-31,800,day")
    rows, last_err = [], None
    for param in candidates:
        try:
            resp = requests.get(url, params={"param": param, "r": str(random.random())}, headers=_headers("https://gu.qq.com/"), timeout=12)
            resp.raise_for_status()
            data = _decode_tencent_json(resp.text)
            node = (((data or {}).get("data") or {}).get(t_symbol) or {})
            part = node.get("day") or node.get("qfqday") or node.get("hfqday") or []
            if part:
                rows.extend(part)
                break
            last_err = f"bad_or_empty response={str(data)[:220]}"
        except Exception as e:
            last_err = e
            continue
    dedup = {}
    for k in rows:
        try:
            date = str(k[0])
            close_price = float(k[2]) * float(price_scale)
            if close_price > 0:
                dedup[date] = round(close_price, 4)
        except Exception:
            continue
    dates = sorted(dedup.keys())
    closes = [dedup[d] for d in dates]
    if len(closes) >= 2:
        snap = MarketSnapshot(symbol.upper(), "tencent_a_unadjusted", closes[-lmt:], price_scale, dates[-1] if dates else None)
        _write_cache(snap)
        return snap
    raise RuntimeError(f"腾讯A股/ETF K线数据不足: {symbol}, count={len(closes)}, last_error={last_err}")

def _fetch_a_snapshot(symbol: str, days: int, price_scale: float) -> MarketSnapshot:
    errors = []
    for label, fn in [
        ("sina_a", _fetch_sina_a_snapshot),
        ("eastmoney_a", lambda sym, d, ps: _fetch_eastmoney_kline_snapshot(_eastmoney_secid(sym), sym.upper(), d, ps, EASTMONEY_A_ENDPOINTS, "eastmoney_a")),
        ("tencent_a", _fetch_tencent_a_snapshot),
    ]:
        try:
            snap = fn(symbol, days, price_scale)
            if errors:
                failed = "/".join(x.split("=", 1)[0] for x in errors)
                logging.info(f"A股/ETF {symbol.upper()} 已使用行情源 {snap.source}；备用原因: {failed} 不可用。")
            return snap
        except Exception as e:
            errors.append(f"{label}={e}")
            continue
    return _read_cache(symbol.upper(), days, price_scale, "A股/ETF日K全部数据源失败: " + "; ".join(errors))

def _hk_code(symbol_or_code: str) -> str:
    raw = str(symbol_or_code or "").upper().strip()
    if raw.startswith("HK"):
        raw = raw[2:]
    return raw.zfill(5)


def _fetch_eastmoney_hk_snapshot(symbol_or_code: str, days: int, price_scale: float) -> MarketSnapshot:
    code = _hk_code(symbol_or_code)
    return _fetch_eastmoney_kline_snapshot(_eastmoney_hk_secid(code), "HK" + code, days, price_scale, EASTMONEY_HK_ENDPOINTS, "eastmoney_hk")


def _yfinance_hk_symbol(symbol_or_code: str) -> str:
    """Convert HK code to Yahoo Finance format.

    Yahoo Finance keeps Hong Kong stock tickers as 4 digits plus .HK, e.g.
    HK00728 -> 0728.HK, HK00700 -> 0700.HK, HK01919 -> 1919.HK.
    Do not use int(code), otherwise leading zeros are lost and 00728 becomes
    the invalid 728.HK.
    """
    code5 = _hk_code(symbol_or_code)
    return f"{code5[-4:].zfill(4)}.HK"


def _fetch_yahoo_csv_hk_snapshot(symbol_or_code: str, days: int, price_scale: float) -> MarketSnapshot:
    """港股备用：Yahoo Finance 直连接口。

    实际使用 Yahoo chart JSON 端点读取未复权 Close，避免 yfinance 包装层偶发
    timezone/quoteSummary 失败。source 保持为 yahoo_csv_hk，便于和配置/日志顺序对应。
    """
    code = _hk_code(symbol_or_code)
    yf_symbol = _yfinance_hk_symbol(symbol_or_code)
    lmt = max(int(days), 2)
    years = max(2, int(lmt / 220) + 2)
    period1 = int((datetime.now() - timedelta(days=years * 370)).timestamp())
    period2 = int((datetime.now() + timedelta(days=2)).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
    params = {
        "period1": str(period1),
        "period2": str(period2),
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
    }
    try:
        resp = requests.get(url, params=params, headers=_headers("https://finance.yahoo.com/"), timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise RuntimeError(f"Yahoo直连下载失败 {yf_symbol}: {e}")
    chart = ((data or {}).get("chart") or {})
    if chart.get("error"):
        raise RuntimeError(f"Yahoo返回错误 {yf_symbol}: {chart.get('error')}")
    result = (chart.get("result") or [])
    if not result:
        raise RuntimeError(f"Yahoo无港股K线: {yf_symbol}")
    node = result[0] or {}
    timestamps = node.get("timestamp") or []
    quote = (((node.get("indicators") or {}).get("quote") or [{}])[0] or {})
    close_values = quote.get("close") or []
    closes, dates = [], []
    for ts, val in zip(timestamps, close_values):
        try:
            if val is None:
                continue
            close_price = float(val) * float(price_scale)
            if close_price > 0:
                dates.append(datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d"))
                closes.append(round(close_price, 4))
        except Exception:
            continue
    if len(closes) >= 2:
        snap = MarketSnapshot("HK" + code, "yahoo_csv_hk", closes[-lmt:], price_scale, dates[-1] if dates else None)
        _write_cache(snap)
        return snap
    raise RuntimeError(f"Yahoo港股K线数据不足: {yf_symbol}, count={len(closes)}")


def _fetch_yfinance_hk_snapshot(symbol_or_code: str, days: int, price_scale: float) -> MarketSnapshot:
    """港股备用：Yahoo/yfinance。使用未复权 Close，适合实际交易价口径。"""
    code = _hk_code(symbol_or_code)
    yf_symbol = _yfinance_hk_symbol(symbol_or_code)
    # 400交易日约等于2年自然日；多取一些。
    years = max(2, int(int(days) / 220) + 2)
    start = (datetime.now() - timedelta(days=years * 370)).strftime("%Y-%m-%d")
    try:
        import yfinance as yf
    except Exception as e:
        raise RuntimeError(f"未安装 yfinance: {e}")
    try:
        df = yf.download(yf_symbol, start=start, progress=False, auto_adjust=False, actions=False, threads=False)
    except Exception as e:
        raise RuntimeError(f"yfinance下载失败 {yf_symbol}: {e}")
    if df is None or df.empty or "Close" not in df.columns:
        raise RuntimeError(f"yfinance无港股K线: {yf_symbol}")
    closes, dates = [], []
    for idx, val in df["Close"].dropna().items():
        try:
            close_price = float(val) * float(price_scale)
            if close_price > 0:
                dates.append(str(idx.date() if hasattr(idx, "date") else idx)[:10])
                closes.append(round(close_price, 4))
        except Exception:
            continue
    if len(closes) >= 2:
        snap = MarketSnapshot("HK" + code, "yfinance_hk_close", closes[-max(int(days), 2):], price_scale, dates[-1] if dates else None)
        _write_cache(snap)
        return snap
    raise RuntimeError(f"yfinance港股K线数据不足: {yf_symbol}, count={len(closes)}")


def _decode_tencent_json(text: str) -> dict:
    text = str(text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError(f"腾讯返回无法解析: {text[:160]}")
    return json.loads(text[start:end + 1])


def _fetch_tencent_hk_snapshot(symbol_or_code: str, days: int, price_scale: float) -> MarketSnapshot:
    """港股最后备用：腾讯 proxy newfqkline，不复权。不同标的可能不可用。"""
    code = _hk_code(symbol_or_code)
    symbol = "hk" + code
    lmt = max(int(days), 2)
    url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
    # 腾讯接口对 count 位置敏感，港股优先用空起止 + count + day；失败再试日期段。
    candidates = [
        f"{symbol},day,,,{min(max(lmt, 2), 800)},day",
        f"{symbol},day,,,{min(max(lmt, 2), 800)},",
    ]
    now = datetime.now()
    years_back = max(3, int(lmt / 220) + 2)
    candidates.append(f"{symbol},day,{now.year-years_back}-01-01,{now.year+1}-12-31,800,day")
    rows, last_err = [], None
    for param in candidates:
        try:
            resp = requests.get(url, params={"param": param, "r": str(random.random())}, headers=_headers("https://gu.qq.com/"), timeout=12)
            resp.raise_for_status()
            data = _decode_tencent_json(resp.text)
            node = (((data or {}).get("data") or {}).get(symbol) or {})
            part = node.get("day") or node.get("qfqday") or node.get("hfqday") or []
            if part:
                rows.extend(part)
                break
            last_err = f"bad_or_empty response={str(data)[:220]}"
        except Exception as e:
            last_err = e
            continue
    dedup = {}
    for k in rows:
        try:
            # 常见格式 [date, open, close, high, low, volume, ...]
            date = str(k[0])
            close_price = float(k[2]) * float(price_scale)
            if close_price > 0:
                dedup[date] = round(close_price, 4)
        except Exception:
            continue
    dates = sorted(dedup.keys())
    closes = [dedup[d] for d in dates]
    if len(closes) >= 2:
        snap = MarketSnapshot("HK" + code, "tencent_hk_unadjusted", closes[-lmt:], price_scale, dates[-1] if dates else None)
        _write_cache(snap)
        return snap
    raise RuntimeError(f"腾讯港股K线数据不足: HK{code}, count={len(closes)}, last_error={last_err}")


def _fetch_hk_snapshot(symbol_or_code: str, days: int, price_scale: float) -> MarketSnapshot:
    code = _hk_code(symbol_or_code)
    errors = []
    for label, fn in [
        ("tencent_hk", _fetch_tencent_hk_snapshot),
        ("eastmoney_hk", _fetch_eastmoney_hk_snapshot),
        ("yahoo_csv_hk", _fetch_yahoo_csv_hk_snapshot),
        ("yfinance_hk", _fetch_yfinance_hk_snapshot),
    ]:
        try:
            snap = fn(code, days, price_scale)
            if errors:
                failed = "/".join(x.split("=", 1)[0] for x in errors)
                logging.info(f"港股 HK{code} 已使用行情源 {snap.source}；备用原因: {failed} 不可用。")
            return snap
        except Exception as e:
            errors.append(f"{label}={e}")
            continue
    return _read_cache("HK" + code, days, price_scale, "港股日K全部数据源失败: " + "; ".join(errors))


def get_market_snapshot(symbol: str, days: int = 400, price_scale: float = 1.0) -> MarketSnapshot:
    raw = str(symbol or "").upper().strip()
    if raw.startswith("HK"):
        return _fetch_hk_snapshot(raw, days, price_scale)
    return _fetch_a_snapshot(raw, days, price_scale)


def get_price_from_api(symbol: str, price_scale: float = 1.0, last_known_price=None) -> float:
    return get_market_snapshot(symbol, 2, price_scale=price_scale).current_price


def get_history_close(symbol: str, days: int = 400, price_scale: float = 1.0) -> List[float]:
    return get_market_snapshot(symbol, days, price_scale=price_scale).closes


def get_hk_history_close(symbol_or_code: str, days: int, price_scale: float = 1.0) -> List[float]:
    return _fetch_hk_snapshot(symbol_or_code, days, price_scale).closes


def get_a_history_close(symbol: str, days: int, price_scale: float = 1.0) -> List[float]:
    return _fetch_a_snapshot(symbol, days, price_scale).closes


def get_a_price(symbol: str, price_scale: float = 1.0) -> float:
    return _fetch_a_snapshot(symbol, 2, price_scale).current_price


def get_hk_price(symbol: str, price_scale: float = 1.0) -> float:
    return _fetch_hk_snapshot(symbol, 2, price_scale).current_price


def self_test(symbols: Optional[List[str]] = None, days: int = 30) -> int:
    symbols = symbols or ["SH600036", "SZ159232", "HK00728", "HK01919", "HK00700"]
    ok = 0
    for symbol in symbols:
        try:
            snap = get_market_snapshot(symbol, days=days, price_scale=1.0)
            print(f"PASS {symbol}: source={snap.source}, count={len(snap.closes)}, last={snap.current_price}, date={snap.last_bar_date}, trade_allowed={snap.trade_allowed}")
            ok += 1
        except Exception as e:
            print(f"FAIL {symbol}: {e}")
    return 0 if ok == len(symbols) else 1


if __name__ == "__main__":
    raise SystemExit(self_test())
