#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DCF market data adapter layer.

策略主体只应调用本文件暴露的 get_market_snapshot/get_history_close/get_price_from_api。
以后更换行情接口、调整备用源、修复参数、增加缓存，只改这个文件即可。

设计原则：
1. 返回统一 MarketSnapshot；策略仍只使用 snapshot.closes[-1] 作为当前价。
2. A股/ETF 在交易时段必须叠加实时价，实时价顺序：腾讯 -> 雪球 -> 东方财富。
3. 不伪造历史 K 线；缓存只用于展示/观察，默认不允许交易。
4. 成功获取真实行情时写入本地缓存；全部接口失败时可返回缓存快照，但 trade_allowed=False。
5. 港股优先腾讯不复权日K；A股/ETF 优先腾讯/东方财富/网易历史日K，并叠加实时价。
6. A股/ETF 不再使用新浪日K；新浪日K盘中滞后，不能作为实盘行情源。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
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
SYSTEM_CONFIG_FILE = BASE_DIR / "system_config.json"

SYSTEM_DEFAULTS = {
    "A_QUOTE_SOURCE": "tencent_quote_a",
    "HK_MARKET_SOURCE": "tencent_hk_unadjusted",
    "A_BACKTEST_SOURCE": "tencent_a_unadjusted",
    "HK_BACKTEST_SOURCE": "tencent_hk_unadjusted",
    "XUEQIU_TOKEN": "",
}


def _load_system_config() -> dict:
    cfg = dict(SYSTEM_DEFAULTS)
    try:
        if SYSTEM_CONFIG_FILE.exists():
            raw = json.loads(SYSTEM_CONFIG_FILE.read_text(encoding="utf-8") or "{}")
            if isinstance(raw, dict):
                cfg.update({k: "" if v is None else str(v).strip() for k, v in raw.items()})
    except Exception as e:
        logging.debug(f"读取系统行情配置失败: {e}")
    return cfg


def _preferred_order(items, preferred_key: str):
    if not preferred_key:
        return items
    preferred_key = str(preferred_key).strip()
    head = [item for item in items if item[0] == preferred_key]
    tail = [item for item in items if item[0] != preferred_key]
    return head + tail

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
    "https://84.push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://17.push2his.eastmoney.com/api/qt/stock/kline/get",
    "http://push2his.eastmoney.com/api/qt/stock/kline/get",
]
NETEASE_A_ENDPOINTS = [
    "https://quotes.money.163.com/service/chddata.html",
    "http://quotes.money.163.com/service/chddata.html",
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
    dates: Optional[List[str]] = None

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
        "Connection": "close",
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
        raise RuntimeError(reason + "; 且无本地K线缓存")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        closes = [round(float(x), 4) for x in (data.get("closes") or []) if float(x) > 0]
        if len(closes) < 2:
            raise RuntimeError("缓存K线不足")
        cache_dates = data.get("dates") or []
        sliced_closes = closes[-max(int(days), 2):]
        sliced_dates = cache_dates[-len(sliced_closes):] if isinstance(cache_dates, list) else []
        return MarketSnapshot(
            symbol=str(data.get("symbol") or symbol).upper(),
            source="cache_" + str(data.get("source") or "unknown"),
            closes=sliced_closes,
            price_scale=price_scale,
            last_bar_date=data.get("last_bar_date"),
            error=reason + "；已使用本地真实缓存，仅监控不交易",
            trade_allowed=False,
            dates=sliced_dates,
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


def _eastmoney_hk_secid_candidates(symbol_or_code: str) -> List[str]:
    raw = str(symbol_or_code or "").upper().strip()
    if raw.startswith("HK"):
        raw = raw[2:]
    digits = "".join(ch for ch in raw if ch.isdigit())
    five = digits.zfill(5)
    nozero = digits.lstrip("0") or digits
    return list(dict.fromkeys(["116." + five, "116." + nozero]))


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
                    snap = MarketSnapshot(symbol_label.upper(), actual_source, closes[-lmt:], price_scale, dates[-1] if dates else None, dates=dates[-lmt:])
                    _write_cache(snap)
                    return snap
                last_err = f"K线为空 fqt={fqt} response={str(data)[:220]}"
            except Exception as e:
                last_err = e
                logging.debug(f"东方财富K线失败 {symbol_label}: endpoint={endpoint}, fqt={fqt}, error={e}")
                continue
    raise RuntimeError(f"东方财富K线全部失败: {symbol_label}, last_error={last_err}")



def _tencent_a_symbol(symbol: str) -> str:
    raw = str(symbol or "").upper().strip()
    if raw.startswith("SH"):
        return "sh" + raw[2:]
    if raw.startswith("SZ"):
        return "sz" + raw[2:]
    if raw.isdigit() and len(raw) == 6:
        return ("sh" if raw.startswith("6") else "sz") + raw
    raise ValueError(f"不支持的A股代码格式: {symbol}")


def _fetch_tencent_a_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str]]:
    """腾讯 A股/ETF 实时价。

    新浪/东方财富日K在盘中经常只返回上一交易日收盘价，导致 9:30-15:00
    期间 current_price 偏旧。这里用腾讯实时 quote 作为 A股/ETF 的盘中
    最新价，并把它写回 snapshot.closes[-1]，让策略仍保持统一快照口径。
    """
    t_symbol = _tencent_a_symbol(symbol)
    urls = [
        f"https://qt.gtimg.cn/q={t_symbol}",
        f"https://qt.gtimg.cn/q=r_{t_symbol}",
    ]
    headers = _headers("https://gu.qq.com/")
    last_err = None
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=8)
            resp.raise_for_status()
            resp.encoding = "gbk"
            text = resp.text.strip()
            start = text.find('="')
            end = text.rfind('"')
            if start < 0 or end <= start + 2:
                raise RuntimeError(f"腾讯实时行情返回格式异常: {text[:120]}")
            fields = text[start + 2:end].split("~")
            price = None
            # 常见格式：...~名称~代码~当前价~昨收~今开~成交量...
            if len(fields) > 3:
                try:
                    price = float(fields[3])
                except Exception:
                    price = None
            if price is None or price <= 0:
                for item in fields:
                    try:
                        val = float(item)
                    except Exception:
                        continue
                    if val > 0:
                        price = val
                        break
            if price is None or price <= 0:
                raise RuntimeError(f"腾讯实时行情无法解析价格: {text[:160]}")
            # 日期字段不同市场/接口位置可能略有差异，这里只作展示，不作强依赖。
            quote_date = None
            for item in fields:
                s = str(item).strip()
                if len(s) >= 8 and s[:8].isdigit():
                    quote_date = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
                    break
            return round(price * float(price_scale), 4), quote_date
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"腾讯A股/ETF实时行情失败: {symbol}, last_error={last_err}")




def _decode_eastmoney_quote_price(data: dict, symbol: str, price_scale: float) -> Tuple[float, Optional[str]]:
    node = ((data or {}).get("data") or {})
    raw_price = node.get("f43")
    decimal = node.get("f59")
    if raw_price in (None, "", "-"):
        raise RuntimeError(f"东方财富实时行情无 f43: {symbol}, response={str(data)[:180]}")
    try:
        raw_price_f = float(raw_price)
    except Exception:
        raise RuntimeError(f"东方财富实时行情 f43 无法解析: {symbol}, f43={raw_price!r}")
    try:
        decimal_i = int(decimal)
    except Exception:
        decimal_i = 2
    if decimal_i < 0 or decimal_i > 6:
        decimal_i = 2
    # 这里请求参数使用 fltt=2，东方财富 f43 返回的就是真实小数价格。
    # 旧版按 f59 再除一次会把 36.900 错算成 0.369。
    price = raw_price_f
    # 兼容极少数接口未按 fltt=2 返回、仍返回整数缩放价的情况。
    if price > 3000 and decimal_i >= 2:
        price = raw_price_f / (10 ** decimal_i)
    if price <= 0:
        raise RuntimeError(f"东方财富实时行情价格无效: {symbol}, price={price}")
    quote_date = None
    ts = node.get("f86")
    try:
        if ts:
            quote_date = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except Exception:
        quote_date = None
    return round(price * float(price_scale), 4), quote_date


def _fetch_eastmoney_a_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str]]:
    """东方财富 A股/ETF 实时价，作为腾讯实时 quote 的备用。"""
    secid = _eastmoney_secid(symbol)
    endpoints = [
        "https://push2.eastmoney.com/api/qt/stock/get",
        "https://push2.eastmoney.com/api/qt/ulist.np/get",
    ]
    params_common = {
        "fltt": "2",
        "invt": "2",
        "fields": "f43,f57,f58,f59,f60,f86",
    }
    last_err = None
    for endpoint in endpoints:
        try:
            if endpoint.endswith("stock/get"):
                params = dict(params_common)
                params["secid"] = secid
            else:
                params = dict(params_common)
                params["secids"] = secid
            resp = requests.get(endpoint, params=params, headers=_headers("https://quote.eastmoney.com/"), timeout=8)
            resp.raise_for_status()
            data = resp.json()
            # ulist 接口返回 diff 列表。
            if endpoint.endswith("ulist.np/get"):
                diff = (((data or {}).get("data") or {}).get("diff") or [])
                if not diff:
                    raise RuntimeError(f"东方财富实时行情 diff 为空: {symbol}, response={str(data)[:180]}")
                data = {"data": diff[0]}
            return _decode_eastmoney_quote_price(data, symbol, price_scale)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"东方财富A股/ETF实时行情失败: {symbol}, last_error={last_err}")


def _xueqiu_symbol(symbol: str) -> str:
    raw = str(symbol or "").upper().strip()
    if raw.startswith(("SH", "SZ")):
        return raw
    if raw.startswith("HK"):
        # 雪球港股通常使用 5 位纯数字代码，例如 00700，而不是 HK00700。
        return raw[2:].zfill(5)
    if raw.isdigit() and len(raw) == 6:
        return ("SH" if raw.startswith("6") else "SZ") + raw
    if raw.isdigit() and len(raw) <= 5:
        return raw.zfill(5)
    raise ValueError(f"不支持的证券代码格式: {symbol}")


def _xueqiu_hk_symbol_candidates(symbol_or_code: str) -> List[str]:
    code = _hk_code(symbol_or_code)
    four = code[-4:].zfill(4)
    five = code.zfill(5)
    # pysnowball/A股与港股在雪球接口里的 symbol 规则不完全一致。
    # 对港股优先尝试 HK00700 / HK:00700，再尝试纯数字。
    # 之前 HK00728 用纯数字 00728 会拿到 0.160 这类错误对象，
    # 所以把带 HK 前缀的候选放在前面。
    return list(dict.fromkeys([
        "HK" + five,
        "HK:" + five,
        five,
        four + ".HK",
        five + ".HK",
        four,
    ]))


def _fetch_xueqiu_a_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str]]:
    """雪球 A股/ETF 实时价，作为第二备用。"""
    return _fetch_xueqiu_realtime_price(symbol, price_scale)

def _fetch_a_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str], str]:
    """A股/ETF 实时价统一入口：腾讯优先，雪球第二，东方财富第三。"""
    errors = []
    preferred = str(_load_system_config().get("A_QUOTE_SOURCE", "tencent_quote_a") or "tencent_quote_a").strip()
    quote_sources = _preferred_order([
        ("tencent_quote_a", _fetch_tencent_a_realtime_price),
        ("xueqiu_quote_a", _fetch_xueqiu_a_realtime_price),
        ("eastmoney_quote_a", _fetch_eastmoney_a_realtime_price),
    ], preferred)
    for source, fn in quote_sources:
        try:
            price, quote_date = fn(symbol, price_scale)
            if errors:
                failed = "/".join(x.split("=", 1)[0] for x in errors)
                logging.info(f"A股/ETF {symbol.upper()} 实时价已使用 {source}；备用原因: {failed} 不可用。")
            return price, quote_date, source
        except Exception as e:
            errors.append(f"{source}={e}")
            continue
    raise RuntimeError("A股/ETF实时价全部失败: " + "; ".join(errors))

def _apply_a_realtime_price(snapshot: MarketSnapshot, symbol: str, price_scale: float) -> MarketSnapshot:
    """把 A股/ETF 的实时价合并进同一个 MarketSnapshot。

    历史K线用于 MA，实时价用于当前价。合并后 current_price 仍然等于
    closes[-1]，dcf.py/strategy.py 不需要知道底层是否叠加了实时 quote。
    """
    if not snapshot.closes:
        return snapshot
    try:
        live_price, quote_date, live_source = _fetch_a_realtime_price(symbol, price_scale)
    except Exception as e:
        logging.warning(f"A股/ETF实时价叠加失败 {symbol}: {e}；将使用历史日K最后收盘价，仅作行情源降级处理。")
        return snapshot
    if live_price <= 0:
        return snapshot
    closes = list(snapshot.closes)
    old_price = closes[-1]
    closes[-1] = round(live_price, 4)
    # 对外显示简短源名。历史K线仍用于 MA，实时价源用于 current_price。
    display_map = {
        "tencent_quote_a": "tencent_api",
        "eastmoney_quote_a": "eastmoney_api",
        "xueqiu_quote_a": "xueqiu_api",
    }
    source = display_map.get(live_source, live_source)
    # 如果实时日期取不到，保留原K线日期；取到了则使用实时日期，便于状态页看到今天数据。
    last_bar_date = quote_date or snapshot.last_bar_date
    snap = MarketSnapshot(
        symbol=snapshot.symbol,
        source=source,
        closes=closes,
        price_scale=snapshot.price_scale,
        last_bar_date=last_bar_date,
        error=snapshot.error,
        trade_allowed=snapshot.trade_allowed,
        dates=snapshot.dates,
    )
    if abs(live_price - old_price) > 1e-6:
        logging.debug(f"A股/ETF实时价叠加 {symbol}: {old_price} -> {live_price}, source={source}")
    _write_cache(snap)
    return snap


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
        snap = MarketSnapshot(symbol.upper(), "tencent_a_unadjusted", closes[-lmt:], price_scale, dates[-1] if dates else None, dates=dates[-lmt:])
        _write_cache(snap)
        return snap
    raise RuntimeError(f"腾讯A股/ETF K线数据不足: {symbol}, count={len(closes)}, last_error={last_err}")


def _netease_a_code(symbol: str) -> str:
    """网易历史行情代码。上海=0+代码，深圳=1+代码。"""
    raw = str(symbol or "").upper().strip()
    if raw.startswith("SH"):
        return "0" + raw[2:]
    if raw.startswith("SZ"):
        return "1" + raw[2:]
    if raw.isdigit() and len(raw) == 6:
        return ("0" if raw.startswith("6") else "1") + raw
    raise ValueError(f"不支持的A股代码格式: {symbol}")


def _fetch_netease_a_snapshot(symbol: str, days: int, price_scale: float) -> MarketSnapshot:
    """A股/ETF备用：网易历史日线 CSV，不复权。

    仅作为腾讯/东方财富都不可用时的第三备用源。成功后仍会叠加
    腾讯实时价，保证盘中 current_price 不停留在上一交易日。
    """
    code = _netease_a_code(symbol)
    lmt = max(int(days), 2)
    # 自然日多取一些，避免节假日导致交易日不足。
    start_date = (datetime.now() - timedelta(days=max(900, lmt * 2))).strftime("%Y%m%d")
    end_date = (datetime.now() + timedelta(days=2)).strftime("%Y%m%d")
    params = {
        "code": code,
        "start": start_date,
        "end": end_date,
        "fields": "TCLOSE",
    }
    last_err = None
    rows = []
    for endpoint in NETEASE_A_ENDPOINTS:
        try:
            resp = requests.get(endpoint, params=params, headers=_headers("https://quotes.money.163.com/"), timeout=15)
            resp.raise_for_status()
            text = resp.content.decode("gbk", errors="ignore")
            if "收盘价" not in text:
                raise RuntimeError(f"网易CSV无收盘价字段: {text[:160]}")
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
            if rows:
                break
            last_err = f"empty csv: {text[:160]}"
        except Exception as e:
            last_err = e
            continue
    dedup = {}
    for row in rows:
        try:
            date = str(row.get("日期") or row.get("date") or "").strip()
            close_raw = row.get("收盘价") or row.get("TCLOSE") or row.get("close")
            close_price = float(str(close_raw).strip()) * float(price_scale)
            if date and close_price > 0:
                dedup[date] = round(close_price, 4)
        except Exception:
            continue
    dates = sorted(dedup.keys())
    closes = [dedup[d] for d in dates]
    if len(closes) >= 2:
        snap = MarketSnapshot(symbol.upper(), "netease_a_unadjusted", closes[-lmt:], price_scale, dates[-1] if dates else None, dates=dates[-lmt:])
        _write_cache(snap)
        return snap
    raise RuntimeError(f"网易A股/ETF K线数据不足: {symbol}, count={len(closes)}, last_error={last_err}")

def _fetch_a_snapshot(symbol: str, days: int, price_scale: float) -> MarketSnapshot:
    errors = []
    # A股/ETF 盘中必须优先使用腾讯实时价叠加后的快照。
    # 新浪日K盘中经常滞后到上一交易日，实盘不再使用。
    for label, fn in [
        ("tencent_a", _fetch_tencent_a_snapshot),
        ("eastmoney_a", lambda sym, d, ps: _fetch_eastmoney_kline_snapshot(_eastmoney_secid(sym), sym.upper(), d, ps, EASTMONEY_A_ENDPOINTS, "eastmoney_a")),
        ("netease_a", _fetch_netease_a_snapshot),
    ]:
        try:
            snap = fn(symbol, days, price_scale)
            snap = _apply_a_realtime_price(snap, symbol, price_scale)
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
    params = {
        "period1": str(period1),
        "period2": str(period2),
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
    }
    data = None
    last_err = None
    for host in ["query2.finance.yahoo.com", "query1.finance.yahoo.com"]:
        url = f"https://{host}/v8/finance/chart/{yf_symbol}"
        try:
            resp = requests.get(url, params=params, headers=_headers("https://finance.yahoo.com/"), timeout=15)
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:
            last_err = e
            continue
    if data is None:
        raise RuntimeError(f"Yahoo直连下载失败 {yf_symbol}: {last_err}")
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
        snap = MarketSnapshot("HK" + code, "yahoo_csv_hk", closes[-lmt:], price_scale, dates[-1] if dates else None, dates=dates[-lmt:])
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
        snap = MarketSnapshot("HK" + code, "yfinance_hk", closes[-max(int(days), 2):], price_scale, dates[-1] if dates else None, dates=dates[-max(int(days), 2):])
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
        snap = MarketSnapshot("HK" + code, "tencent_hk_unadjusted", closes[-lmt:], price_scale, dates[-1] if dates else None, dates=dates[-lmt:])
        _write_cache(snap)
        return snap
    raise RuntimeError(f"腾讯港股K线数据不足: HK{code}, count={len(closes)}, last_error={last_err}")



def _fetch_tencent_hk_realtime_price(symbol_or_code: str, price_scale: float) -> Tuple[float, Optional[str]]:
    """腾讯港股实时价，仅用于状态页人工参考。

    策略所需的 400 根历史 K 线仍由历史行情源负责；状态页参考价只看
    轻量实时 quote，避免为了展示参考价去拉完整 K 线。
    """
    code = _hk_code(symbol_or_code)
    headers = _headers("https://gu.qq.com/")
    urls = [
        f"https://qt.gtimg.cn/q=hk{code}",
        f"https://qt.gtimg.cn/q=r_hk{code}",
        f"http://qt.gtimg.cn/q=hk{code}",
    ]
    last_err = None
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=6)
            resp.raise_for_status()
            resp.encoding = "gbk"
            text = resp.text.strip()
            # 典型格式: v_hk00700="...~449.200~..."
            if '="' not in text:
                raise RuntimeError(f"腾讯港股实时返回格式异常: {text[:120]!r}")
            data_str = text.split('="', 1)[1].rsplit('"', 1)[0]
            fields = data_str.split("~")
            price = None
            if len(fields) > 3:
                try:
                    price = float(fields[3])
                except Exception:
                    price = None
            if price is None:
                for item in fields:
                    try:
                        val = float(item)
                    except Exception:
                        continue
                    # 港股实时价通常不会是 0，且当前价一般在字段前部。
                    if val > 0:
                        price = val
                        break
            if price is None or price <= 0:
                raise RuntimeError(f"腾讯港股实时价格为空: HK{code}, response={text[:160]!r}")
            return round(price * float(price_scale), 4), datetime.now().strftime("%Y-%m-%d")
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"腾讯港股实时行情失败: HK{code}, last_error={last_err}")


def _fetch_eastmoney_hk_realtime_price(symbol_or_code: str, price_scale: float) -> Tuple[float, Optional[str]]:
    """东方财富港股实时价，仅用于状态页人工参考。

    港股历史 K 线在部分服务器上会 RemoteDisconnected，但 quote 接口通常更轻。
    """
    code = _hk_code(symbol_or_code)
    secid_candidates = _eastmoney_hk_secid_candidates(code)
    endpoints = [
        "https://push2.eastmoney.com/api/qt/stock/get",
        "https://push2.eastmoney.com/api/qt/ulist.np/get",
        "https://push2.eastmoney.com/api/qt/ulist/get",
        "https://33.push2.eastmoney.com/api/qt/stock/get",
        "https://33.push2.eastmoney.com/api/qt/ulist.np/get",
        "http://push2.eastmoney.com/api/qt/stock/get",
        "http://push2.eastmoney.com/api/qt/ulist.np/get",
    ]
    params_common = {
        "fltt": "2",
        "invt": "2",
        "fields": "f43,f57,f58,f59,f60,f86,f169,f170,f152",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }
    last_err = None
    for endpoint in endpoints:
        for secid in secid_candidates:
            try:
                params = dict(params_common)
                if "ulist" in endpoint:
                    params["secids"] = secid
                else:
                    params["secid"] = secid
                resp = requests.get(endpoint, params=params, headers=_headers("https://quote.eastmoney.com/"), timeout=8)
                resp.raise_for_status()
                data = resp.json()
                if "ulist" in endpoint:
                    diff = (((data or {}).get("data") or {}).get("diff") or [])
                    if not diff:
                        raise RuntimeError(f"东方财富港股实时 diff 为空: HK{code}, secid={secid}, response={str(data)[:180]}")
                    node = diff[0]
                else:
                    node = ((data or {}).get("data") or {})
                raw_price = node.get("f43")
                if raw_price in (None, "", "-"):
                    raise RuntimeError(f"东方财富港股实时价格为空: HK{code}, secid={secid}, response={str(data)[:180]}")
                price = float(raw_price) * float(price_scale)
                if price <= 0:
                    raise RuntimeError(f"东方财富港股实时价格无效: {price}")
                qdate = None
                try:
                    ts = node.get("f86")
                    if ts:
                        qdate = datetime.fromtimestamp(int(float(ts))).strftime("%Y-%m-%d")
                except Exception:
                    pass
                return round(price, 4), qdate
            except Exception as e:
                last_err = e
                continue
    raise RuntimeError(f"东方财富港股实时行情失败: HK{code}, last_error={last_err}")


def _fetch_yahoo_hk_realtime_price(symbol_or_code: str, price_scale: float) -> Tuple[float, Optional[str]]:
    """Yahoo 港股轻量参考价。优先 chart range=5d，减少 period1/period2 导致的 429。"""
    yf_symbol = _yfinance_hk_symbol(symbol_or_code)
    last_err = None
    hosts = ["query2.finance.yahoo.com", "query1.finance.yahoo.com"]
    # 先用 range 方式，通常比 period1/period2 更轻。
    for host in hosts:
        try:
            url = f"https://{host}/v8/finance/chart/{yf_symbol}"
            params = {"range": "5d", "interval": "1d", "includePrePost": "false"}
            resp = requests.get(url, params=params, headers=_headers("https://finance.yahoo.com/"), timeout=8)
            resp.raise_for_status()
            data = resp.json()
            result = (((data or {}).get("chart") or {}).get("result") or [])
            if not result:
                raise RuntimeError(f"Yahoo无quote结果: {str(data)[:160]}")
            node = result[0] or {}
            meta = node.get("meta") or {}
            price = meta.get("regularMarketPrice")
            qdate = None
            try:
                ts = meta.get("regularMarketTime")
                if ts:
                    qdate = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
            except Exception:
                pass
            if price is None:
                quote = (((node.get("indicators") or {}).get("quote") or [{}])[0] or {})
                closes = [x for x in (quote.get("close") or []) if x is not None]
                if closes:
                    price = closes[-1]
            if price is None:
                raise RuntimeError(f"Yahoo quote价格为空: {str(data)[:160]}")
            price = float(price) * float(price_scale)
            if price <= 0:
                raise RuntimeError(f"Yahoo quote价格无效: {price}")
            return round(price, 4), qdate
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Yahoo港股参考价失败 {yf_symbol}: {last_err}")


def _xueqiu_session_headers(x_symbol: str) -> Tuple[requests.Session, dict]:
    """Build a Xueqiu session using the manually configured token/cookie."""
    sess = requests.Session()
    headers = {
        "User-Agent": random.choice(MARKET_DATA_USER_AGENTS),
        "Referer": f"https://xueqiu.com/S/{x_symbol}",
        "Origin": "https://xueqiu.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    try:
        import os
        cookie = os.getenv("XUEQIU_COOKIE", "").strip()
        token = os.getenv("XQ_A_TOKEN", "").strip()
        saved = str(_load_system_config().get("XUEQIU_TOKEN", "") or "").strip()
        if not cookie and not token and saved:
            if "=" in saved or ";" in saved:
                cookie = saved
            else:
                token = saved
        if cookie:
            headers["Cookie"] = cookie
        elif token:
            headers["Cookie"] = f"xq_a_token={token}"
        else:
            sess.get("https://xueqiu.com/", headers=headers, timeout=8)
    except Exception:
        pass
    return sess, headers


def _extract_xueqiu_quote_price(data: dict, x_symbol: str, price_scale: float) -> Tuple[float, Optional[str]]:
    """Parse Xueqiu quote responses from realtime/quotec or quote.json."""
    payload = (data or {}).get("data")
    if isinstance(payload, list):
        if not payload:
            raise RuntimeError(f"雪球实时行情为空 {x_symbol}: response={str(data)[:180]}")
        node = payload[0] or {}
    elif isinstance(payload, dict):
        # quotec.json 常见 data 为列表；quote.json 常见 data.quote 为行情对象。
        # 部分接口会返回 data.items / data.list。
        if isinstance(payload.get("quote"), dict):
            node = payload.get("quote") or {}
        elif isinstance(payload.get("market"), dict):
            node = payload.get("market") or {}
        elif isinstance(payload.get("items"), list) and payload.get("items"):
            node = payload.get("items")[0] or {}
        elif isinstance(payload.get("list"), list) and payload.get("list"):
            node = payload.get("list")[0] or {}
        else:
            node = payload
    else:
        raise RuntimeError(f"雪球实时行情返回格式异常 {x_symbol}: response={str(data)[:180]}")
    # 实时参考价只接受真正的当前价字段，避免把 last_close/历史 close
    # 误当成实时价。此前 HK00728 曾从雪球返回 0.160 这类异常口径，
    # 就是因为兜底字段过宽。
    raw_price = None
    # 港股只信任 current 字段。其它字段在部分非热门港股上可能是昨收、权证、
    # 分红或其它非现价字段，曾导致 HK00728 被误读为 0.160。
    if str(x_symbol).upper().startswith("HK") or str(x_symbol).isdigit() or ".HK" in str(x_symbol).upper():
        val = node.get("current")
        if val not in (None, "", "-"):
            raw_price = val
    else:
        for key in ("current", "price", "last", "last_price", "now"):
            val = node.get(key)
            if val not in (None, "", "-"):
                raw_price = val
                break
    if raw_price in (None, "", "-"):
        raise RuntimeError(f"雪球实时行情无可信当前价字段 {x_symbol}: response={str(data)[:180]}")
    price = float(raw_price)
    if price <= 0:
        raise RuntimeError(f"雪球实时行情价格无效 {x_symbol}: {price}")
    quote_date = None
    for key in ("timestamp", "time", "updated"):  # timestamp is usually ms
        try:
            ts = node.get(key)
            if ts:
                ts_i = int(float(ts))
                if ts_i > 10_000_000_000:
                    ts_i = ts_i // 1000
                quote_date = datetime.fromtimestamp(ts_i).strftime("%Y-%m-%d")
                break
        except Exception:
            pass
    return round(price * float(price_scale), 4), quote_date


def _fetch_xueqiu_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str]]:
    """雪球实时价，支持 A股/ETF 与港股。港股会尝试多种代码格式。"""
    raw = str(symbol or "").upper().strip()
    candidates = _xueqiu_hk_symbol_candidates(raw) if raw.startswith("HK") or (raw.isdigit() and len(raw) <= 5) else [_xueqiu_symbol(raw)]
    last_err = None
    for x_symbol in candidates:
        sess, headers = _xueqiu_session_headers(x_symbol)
        requests_to_try = [
            ("https://stock.xueqiu.com/v5/stock/realtime/quotec.json", {"symbol": x_symbol}),
            ("https://stock.xueqiu.com/v5/stock/quote.json", {"symbol": x_symbol, "extend": "detail"}),
            # 某些接口变体接受 symbols 复数参数。
            ("https://stock.xueqiu.com/v5/stock/realtime/quotec.json", {"symbols": x_symbol}),
        ]
        for url, params in requests_to_try:
            try:
                resp = sess.get(url, params=params, headers=headers, timeout=8)
                resp.raise_for_status()
                data = resp.json()
                return _extract_xueqiu_quote_price(data, x_symbol, price_scale)
            except Exception as e:
                last_err = e
                continue
    raise RuntimeError(f"雪球实时行情失败 {symbol}: last_error={last_err}")

def _fetch_xueqiu_hk_snapshot(symbol_or_code: str, days: int, price_scale: float) -> MarketSnapshot:
    """港股第二顺位：雪球日K + 雪球实时价。需要系统页填写 xq_a_token/Cookie 才稳定。

    雪球港股代码格式和 A 股不同，常用 5 位纯数字（如 00700）。
    这里会自动尝试 00700 / HK00700 / 0700.HK，并同时兼容 item/items 字段。
    """
    code = _hk_code(symbol_or_code)
    lmt = max(int(days), 2)
    url = "https://stock.xueqiu.com/v5/stock/chart/kline.json"
    last_err = None
    for x_symbol in _xueqiu_hk_symbol_candidates(code):
        sess, headers = _xueqiu_session_headers(x_symbol)
        for ktype in ["normal", "before", "after"]:
            params = {
                "symbol": x_symbol,
                "begin": str(int(time_module.time() * 1000)),
                "period": "day",
                "type": ktype,
                "count": str(-max(lmt, 2)),
                "indicator": "kline,pe,pb,ps,pcf,market_capital,agt,ggt,balance",
            }
            try:
                resp = sess.get(url, params=params, headers=headers, timeout=12)
                resp.raise_for_status()
                data = resp.json()
                node = (data or {}).get("data") or {}
                columns = [str(x).lower() for x in (node.get("column") or node.get("columns") or [])]
                items = node.get("item") or node.get("items") or []
                if not items:
                    raise RuntimeError(f"雪球港股K线为空 symbol={x_symbol}, type={ktype}, response={str(data)[:200]}")
                ts_idx = columns.index("timestamp") if "timestamp" in columns else 0
                close_idx = columns.index("close") if "close" in columns else 5
                closes, dates = [], []
                for row in items:
                    try:
                        ts = row[ts_idx]
                        val = row[close_idx]
                        close_price = float(val) * float(price_scale)
                        if close_price > 0:
                            dates.append(datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d"))
                            closes.append(round(close_price, 4))
                    except Exception:
                        continue
                if len(closes) >= 2:
                    try:
                        live_price, live_date = _fetch_xueqiu_realtime_price("HK" + code, price_scale)
                        if live_price > 0:
                            closes[-1] = round(live_price, 4)
                            if live_date:
                                dates[-1] = live_date
                    except Exception:
                        pass
                    snap = MarketSnapshot("HK" + code, "xueqiu_hk", closes[-lmt:], price_scale, dates[-1] if dates else None, dates=dates[-lmt:])
                    _write_cache(snap)
                    return snap
                last_err = f"雪球港股K线数据不足 symbol={x_symbol}, type={ktype}, count={len(closes)}"
            except Exception as e:
                last_err = e
                continue
    raise RuntimeError(f"雪球港股K线失败: HK{code}, last_error={last_err}")


def _fetch_hk_snapshot(symbol_or_code: str, days: int, price_scale: float) -> MarketSnapshot:
    code = _hk_code(symbol_or_code)
    errors = []
    preferred = str(_load_system_config().get("HK_MARKET_SOURCE", "tencent_hk_unadjusted") or "tencent_hk_unadjusted").strip()
    if preferred != "tencent_hk_unadjusted":
        preferred = "tencent_hk_unadjusted"
    hk_sources = [("tencent_hk_unadjusted", _fetch_tencent_hk_snapshot)]
    for label, fn in hk_sources:
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



# ===========================
# 对外辅助：状态页参考价格 / 回测历史源
# ===========================
def get_reference_prices(symbol: str, price_scale: float = 1.0) -> List[dict]:
    """Return reference prices for Web status page without affecting strategy state."""
    raw = str(symbol or "").upper().strip()
    cfg = _load_system_config()
    result = []
    if not raw:
        return result
    if raw.startswith("HK"):
        preferred = str(cfg.get("HK_MARKET_SOURCE", "tencent_hk_unadjusted") or "tencent_hk_unadjusted").strip()
        if preferred != "tencent_hk_unadjusted":
            preferred = "tencent_hk_unadjusted"
        # 港股实时参考价只保留腾讯主源。
        sources = [
            ("tencent_hk_unadjusted", "腾讯港股", "tencent_hk_quote", _fetch_tencent_hk_realtime_price),
        ]
        for key, label, source_name, fn in sources:
            try:
                price, date = fn(raw, price_scale)
                result.append({
                    "key": key,
                    "label": label,
                    "price": price,
                    "source": source_name,
                    "date": date,
                    "ok": True,
                    "primary": key == preferred,
                    "error": "",
                })
            except Exception as e:
                result.append({
                    "key": key,
                    "label": label,
                    "price": None,
                    "source": source_name,
                    "date": "",
                    "ok": False,
                    "primary": key == preferred,
                    "error": str(e)[:180],
                })
        return result
    preferred = str(cfg.get("A_QUOTE_SOURCE", "tencent_quote_a") or "tencent_quote_a").strip()
    sources = [
        ("tencent_quote_a", "腾讯实时", _fetch_tencent_a_realtime_price),
        ("xueqiu_quote_a", "雪球实时", _fetch_xueqiu_a_realtime_price),
        ("eastmoney_quote_a", "东方财富实时", _fetch_eastmoney_a_realtime_price),
    ]
    for key, label, fn in sources:
        try:
            price, date = fn(raw, price_scale)
            result.append({"key": key, "label": label, "price": price, "source": {"tencent_quote_a":"tencent_api","eastmoney_quote_a":"eastmoney_api","xueqiu_quote_a":"xueqiu_api"}.get(key, key), "date": date, "ok": True, "primary": key == preferred, "error": ""})
        except Exception as e:
            result.append({"key": key, "label": label, "price": None, "source": key, "date": "", "ok": False, "primary": key == preferred, "error": str(e)[:160]})
    return result


def get_history_snapshot_by_source(symbol: str, days: int = 400, price_scale: float = 1.0, source: str = "") -> MarketSnapshot:
    """Fetch historical daily closes from a specific source for backtest comparison."""
    raw = str(symbol or "").upper().strip()
    source = str(source or "").strip()
    if raw.startswith("HK"):
        key = source or str(_load_system_config().get("HK_BACKTEST_SOURCE", "tencent_hk_unadjusted") or "tencent_hk_unadjusted")
        if key != "tencent_hk_unadjusted":
            key = "tencent_hk_unadjusted"
        return _fetch_tencent_hk_snapshot(raw, days, price_scale)
    key = source or str(_load_system_config().get("A_BACKTEST_SOURCE", "tencent_a_unadjusted") or "tencent_a_unadjusted")
    # 回测/策略指标的 A股/ETF 历史源只保留腾讯；旧配置自动回退腾讯，避免刷新旧缓存或失败源。
    if key != "tencent_a_unadjusted":
        key = "tencent_a_unadjusted"
    return _fetch_tencent_a_snapshot(raw, days, price_scale)

def self_test(symbols: Optional[List[str]] = None, days: int = 30) -> int:
    symbols = symbols or ["SH600036", "SZ159232", "SZ002847", "HK00728", "HK01919", "HK00700"]
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
