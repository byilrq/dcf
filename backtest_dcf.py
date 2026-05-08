#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import math
import json
import argparse
import logging
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

# 导入策略模块
from strategy import (
    get_zone,
    normalize_position_amount,
    calculate_pyramid_sell_plan,
    get_pyramid_sell_target_step,
    get_trend_sell_decision,
    get_add_trade_decision,
    POSITION_EPSILON,
)


# ===========================
# 配置读取
# ===========================
def load_config(path: str):
    try:
        import yaml
    except ImportError:
        import json5 as json_mod
        with open(path, "r", encoding="utf-8") as f:
            cfg = json_mod.load(f)
    else:
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

    dcf_cfg = cfg.get("ETF_CONFIG", {}) or {}
    strategy_cfg = cfg.get("STRATEGY", {}) or {}
    common_cfg = cfg.get("COMMON_BACKTEST_CONFIG", {}) or {}
    return dcf_cfg, strategy_cfg, common_cfg


# ===========================
# 符号映射
# ===========================
def normalize_symbol(symbol: str) -> str:
    return symbol.upper().strip()


def to_yahoo_symbol(symbol: str) -> str:
    s = normalize_symbol(symbol)
    if s.startswith("SH"):
        return f"{s[2:]}.SS"
    if s.startswith("SZ"):
        return f"{s[2:]}.SZ"
    if s.startswith("HK"):
        code = s[2:].strip()
        code = code.zfill(4)[-4:]
        return f"{code}.HK"
    raise ValueError(f"不支持的 symbol: {symbol}，仅支持 SHxxxxxx / SZxxxxxx / HKxxxxx")


# ===========================
# 工具函数
# ===========================
def round_to_lot(qty: int, lot_size: int = 100) -> int:
    if qty <= 0:
        return 0
    rounded_qty = int(qty // lot_size) * lot_size
    if rounded_qty == 0 and qty > 0:
        rounded_qty = lot_size
    return rounded_qty


def calculate_new_avg_cost(old_position, old_avg_cost, add_units, add_price):
    if old_position <= 0:
        return add_price
    total_cost_before = old_position * old_avg_cost
    total_cost_after = total_cost_before + add_units * add_price
    return total_cost_after / (old_position + add_units)


def _safe_float(value, default=0.0):
    try:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return default
        return float(value)
    except Exception:
        return default


def get_position_mode(cfg):
    base = cfg.get("base_units", 0)
    target = cfg.get("target_units", 0)

    if isinstance(base, str) and base.strip().endswith("%"):
        return "percent"
    if isinstance(target, str) and target.strip().endswith("%"):
        return "percent"

    try:
        base_f = float(base)
        target_f = float(target)
        if 0 <= base_f <= 1 and 0 <= target_f <= 1:
            return "percent"
    except Exception:
        pass

    return "absolute"


def parse_position_value(value, mode=None):
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return 0.0
        if s.endswith("%"):
            return float(s[:-1]) / 100.0
        return float(s)
    return _safe_float(value, 0.0)


def get_base_units(cfg):
    return parse_position_value(cfg.get("base_units", 0))


def get_target_units(cfg):
    return parse_position_value(cfg.get("target_units", 0))


def get_double_target(cfg):
    return get_target_units(cfg) * _safe_float(cfg.get("double_target_factor", 2.0), 2.0)


def get_trend_multiple(cfg):
    return _safe_float(cfg.get("trend_multiple", 1.2), 1.2)


def get_sell_multiple(cfg):
    return _safe_float(cfg.get("sell_multiple", 1.5), 1.5)


def get_add_box_step(cfg):
    return _safe_float(cfg.get("add_box_step", 0.05), 0.05)


def get_add_box_units_percent(cfg):
    return _safe_float(cfg.get("add_box_units_percent", 0.1), 0.1)


def get_trend_zone_step_percent(cfg):
    return _safe_float(cfg.get("trend_zone_step_percent", 0.01), 0.01)


def get_trend_zone_sell_percent(cfg):
    return _safe_float(cfg.get("trend_zone_sell_percent", 0.05), 0.05)


def get_clear_zone_step_percent(cfg):
    return _safe_float(cfg.get("clear_zone_step_percent", 0.08), 0.08)


def get_box_grid_enabled(cfg):
    value = str((cfg or {}).get("box_grid_enabled", "no")).strip().lower()
    return value in {"yes", "true", "1", "on"}


def get_pyramid_add_enabled(cfg):
    value = str(cfg.get("pyramid_add_enabled", "no")).strip().lower()
    if value in {"yes", "auto"}:
        return value
    return "no"


def format_percent_ratio(value, digits=2):
    pct = _safe_float(value, 0.0) * 100.0
    s = f"{pct:.{digits}f}".rstrip("0").rstrip(".")
    if s in {"", "-0"}:
        s = "0"
    return f"{s}%"


def format_units_for_display(value, mode):
    if mode == "percent":
        return format_percent_ratio(value)
    return f"{int(round(_safe_float(value, 0.0))):,}股"


def serialize_numeric(value, digits=6):
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    s = f"{float(value):.{digits}f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _format_config_value_for_report(value):
    if isinstance(value, float):
        return serialize_numeric(value)
    if isinstance(value, list):
        return ", ".join(_format_config_value_for_report(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None:
        return ""
    return str(value)


def write_config_section_to_report(rf, cfg: dict):
    rf.write("\n================ 标的配置参数 ================\n")
    preferred_keys = [
        "symbol", "price_scale", "strategy_run",
        "base_units", "target_units", "double_target_factor", "current_units", "current_avg_cost",
        "k150", "sideways_window_30", "sideways_window_60", "sideways_weight_60", "sideways_min_k150",
        "trend_multiple", "sell_multiple",
        "add_box_step", "add_box_units_percent",
        "box_grid_enabled", "grid_box_percent", "grid_box_units_percent",
        "trend_zone_step_percent", "trend_zone_sell_percent",
        "clear_zone_step_percent", "pyramid_steps", "pyramid_weights", "pyramid_add_enabled",
        "backtest_pyramid_add_start", "ignored_live_pyramid_add_enabled",
        "fee_rate", "slippage_bp", "lot_size", "initial_cash", "init_avg_cost",
    ]
    written = set()
    for key in preferred_keys:
        if key in cfg:
            rf.write(f"{key}: {_format_config_value_for_report(cfg.get(key))}\n")
            written.add(key)
    for key in sorted(k for k in cfg.keys() if k not in written):
        rf.write(f"{key}: {_format_config_value_for_report(cfg.get(key))}\n")


def get_total_value(cash, units, avg_cost, price, mode):
    if mode == "absolute":
        return cash + units * price
    if units <= POSITION_EPSILON:
        return cash
    if avg_cost > 0:
        return cash + units * price / avg_cost
    return cash + units


def get_market_weight(units, avg_cost, price, mode):
    if mode == "absolute":
        return units * price
    if units <= POSITION_EPSILON:
        return 0.0
    if avg_cost > 0:
        return units * price / avg_cost
    return units


# ===========================
# MA计算 & 横盘评分
# ===========================
def calc_ma_with_coef(closes, length, min_coef=None, reference_ma=None):
    if len(closes) >= length:
        ma_value = sum(closes[-length:]) / length
        return ma_value, 'p' if len(closes) < length * 2 else 'f'
    elif len(closes) >= max(5, length // 2):
        ma_value = sum(closes) / len(closes)
        return ma_value, 'p'
    elif min_coef is not None and reference_ma is not None:
        ma_value = reference_ma * min_coef
        return ma_value, 'c'
    else:
        return None, 'insufficient_data'


def _compute_ma_series(closes, period):
    if len(closes) < period:
        return []
    ma_series = []
    window_sum = sum(closes[:period])
    ma_series.append(window_sum / period)
    for i in range(period, len(closes)):
        window_sum += closes[i] - closes[i - period]
        ma_series.append(window_sum / period)
    return ma_series


def _ma_directional_sideways_score(ma_series, window):
    n = len(ma_series)
    if n < window + 1:
        return 0.5
    seg = ma_series[-(window + 1):]
    deltas = [seg[i + 1] - seg[i] for i in range(window)]
    sum_abs = sum(abs(d) for d in deltas)
    if sum_abs == 0:
        return 1.0
    dir_strength = abs(sum(deltas)) / sum_abs
    sideways_score = 1.0 - dir_strength
    return max(0.0, min(1.0, sideways_score))


def compute_sideways_index(closes, cfg):
    period30 = 30
    period60 = 60
    window30 = int(cfg.get("sideways_window_30", 30))
    window60 = int(cfg.get("sideways_window_60", 20))
    weight60 = _safe_float(cfg.get("sideways_weight_60", 0.6), 0.6)
    weight60 = max(0.0, min(1.0, weight60))
    weight30 = 1.0 - weight60

    need_len = max(period30 + window30 + 1, period60 + window60 + 1)
    if len(closes) < need_len:
        return 0.0

    ma30_series = _compute_ma_series(closes, period30)
    ma60_series = _compute_ma_series(closes, period60)
    if not ma30_series or not ma60_series:
        return 0.0

    s30 = _ma_directional_sideways_score(ma30_series, window30)
    s60 = _ma_directional_sideways_score(ma60_series, window60)
    sideways_score = weight30 * s30 + weight60 * s60
    return max(0.0, min(1.0, sideways_score))


# ===========================
# 市场数据
# ===========================
def fetch_market_data(symbol: str, days: int) -> pd.DataFrame:
    yahoo_symbol = to_yahoo_symbol(symbol)
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=max(days * 3, 1200))
    ticker = yf.Ticker(yahoo_symbol)
    hist = ticker.history(
        start=start_dt.strftime("%Y-%m-%d"),
        end=(end_dt + timedelta(days=1)).strftime("%Y-%m-%d"),
        interval="1d",
        auto_adjust=False,
        actions=True,
        repair=True,
    )
    if hist is None or hist.empty:
        raise RuntimeError(f"无法拉取历史数据: {symbol} -> {yahoo_symbol}")
    hist = hist.copy().reset_index()
    date_col = "Date" if "Date" in hist.columns else hist.columns[0]
    hist["date"] = pd.to_datetime(hist[date_col]).dt.strftime("%Y-%m-%d")
    if "Close" not in hist.columns:
        raise RuntimeError(f"{symbol} 缺少 Close 列")
    if "Adj Close" not in hist.columns:
        hist["Adj Close"] = hist["Close"]
    if "Dividends" not in hist.columns:
        hist["Dividends"] = 0.0
    if "Stock Splits" not in hist.columns:
        hist["Stock Splits"] = 0.0
    out = hist[["date", "Close", "Adj Close", "Dividends", "Stock Splits"]].copy()
    out = out.rename(columns={
        "Close": "raw_close",
        "Adj Close": "adj_close",
        "Dividends": "dividend",
        "Stock Splits": "split_ratio",
    })
    out["raw_close"] = pd.to_numeric(out["raw_close"], errors="coerce")
    out["adj_close"] = pd.to_numeric(out["adj_close"], errors="coerce")
    out["dividend"] = pd.to_numeric(out["dividend"], errors="coerce").fillna(0.0)
    out["split_ratio"] = pd.to_numeric(out["split_ratio"], errors="coerce").fillna(0.0)
    out = out.dropna(subset=["raw_close", "adj_close"]).copy()
    out = out[out["raw_close"] > 0].copy()
    out = out[out["adj_close"] > 0].copy()
    out["split_ratio"] = out["split_ratio"].apply(lambda x: x if x and x > 0 else 1.0)
    out = out.tail(days).reset_index(drop=True)
    if len(out) < 10:
        raise RuntimeError(f"历史数据太少：{symbol} 仅 {len(out)} 条")
    return out


# ===========================
# 回测主逻辑
# ===========================
def backtest(symbol: str, name: str, cfg: dict, strategy: dict, days: int, outdir: Path):
    cfg = dict(cfg or {})
    # Historical backtests must not inherit the live monitor's manual pyramid switch.
    # Live monitoring may keep pyramid_add_enabled=yes, but every backtest starts from auto.
    # This prevents an initial BOX_ZONE bar from buying simply because the live config is yes.
    live_pyramid_add_enabled = get_pyramid_add_enabled(cfg)
    report_cfg = dict(cfg)
    report_cfg["pyramid_add_enabled"] = "auto"
    report_cfg["backtest_pyramid_add_start"] = "auto"
    if live_pyramid_add_enabled != "auto":
        report_cfg["ignored_live_pyramid_add_enabled"] = live_pyramid_add_enabled
    cfg["pyramid_add_enabled"] = "auto"

    outdir.mkdir(parents=True, exist_ok=True)
    log_path = outdir / f"backtest_{symbol}.log"
    logger = logging.getLogger(f"bt_{symbol}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fh = logging.FileHandler(str(log_path), encoding="utf-8", mode="w")
    fh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(fh)

    df = fetch_market_data(symbol, days)
    dates = df["date"].tolist()
    raw_all = df["raw_close"].tolist()
    adj_all = df["adj_close"].tolist()
    div_all = df["dividend"].tolist()
    split_all = df["split_ratio"].tolist()

    ma_short_len = int(strategy.get("ma_period_short", 150))

    position_mode = get_position_mode(cfg)
    target_units = get_target_units(cfg)
    base_units = get_base_units(cfg)
    double_target = get_double_target(cfg)

    fee_rate = _safe_float(cfg.get("fee_rate", 0.0), 0.0)
    slippage_bp = _safe_float(cfg.get("slippage_bp", 0.0), 0.0)
    initial_cash = _safe_float(cfg.get("initial_cash", 0.0), 0.0)
    lot_size = int(_safe_float(cfg.get("lot_size", 100), 100))

    if position_mode == "percent":
        cash = initial_cash if 0.0 < initial_cash <= 1.0 else max(1.0 - base_units, 0.0)
    else:
        cash = initial_cash

    units = normalize_position_amount(base_units, position_mode, lot_size)

    # Backtest cost basis is independent from live monitoring cost.
    # current_avg_cost is reserved for live monitoring only and must not pollute historical backtests.
    # For historical backtests, the initial base position is costed at the first raw price in the backtest window.
    initial_avg_cost = raw_all[0]

    avg_cost = initial_avg_cost if units > POSITION_EPSILON else 0.0
    initial_stock_units = units
    initial_stock_return_base = (initial_stock_units * avg_cost) if position_mode == "absolute" else initial_stock_units
    last_trade_price = raw_all[0]
    last_trade_side = "buy"
    pyramid_step = 0
    clear_step = 0
    realized_pnl = 0.0
    # Cash/account contribution used only for stock-app style diluted cost.
    # It resets when the position is fully cleared, so old closed-cycle profits do not
    # artificially reduce the cost of a later new position.
    dilution_credit = 0.0
    total_buy_qty_raw = 0.0
    total_sell_qty_raw = 0.0
    total_buy_cost = 0.0
    last_add_price = raw_all[0]
    pyramid_active = False
    target_reached_once = False

    trades_csv = outdir / f"trades_{symbol}.csv"
    with open(trades_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "date", "symbol", "name",
            "action", "raw_price", "qty", "holding",
            "avg_cost_after", "zone", "reason",
            "restor_price",
            "ma150",
            "last_trade_price_before", "last_add_price_before",
            "dividend", "split_ratio",
        ])

    actions_csv = outdir / f"actions_{symbol}.csv"
    with open(actions_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "date", "symbol", "name",
            "event_type", "value",
            "units_before", "units_after",
            "cash_before", "cash_after",
            "avg_cost_before", "avg_cost_after",
            "raw_price", "restor_price",
            "position_mode",
        ])

    daily_records = []

    def _append_trade_row(dt, action, px, qty, holding, zone, reason, raw_close, adj_close,
                          ma150_val, last_trade_before, last_add_before,
                          dividend, split_ratio):
        with open(trades_csv, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                dt, symbol, name,
                action, f"{raw_close:.4f}", format_units_for_display(qty, position_mode), format_units_for_display(holding, position_mode),
                f"{avg_cost:.4f}" if avg_cost > 0 else "0",
                zone, reason,
                f"{adj_close:.4f}",
                f"{ma150_val:.4f}" if ma150_val is not None else "",
                f"{last_trade_before:.4f}" if last_trade_before is not None else "",
                f"{last_add_before:.4f}" if last_add_before is not None else "",
                f"{dividend:.6f}" if dividend else "0",
                f"{split_ratio:.6f}" if split_ratio and split_ratio != 1.0 else "1.0",
            ])

    def _append_action_row(dt, event_type, value, units_before, units_after,
                           cash_before, cash_after, avg_cost_before, avg_cost_after,
                           raw_close, adj_close):
        with open(actions_csv, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                dt, symbol, name,
                event_type, serialize_numeric(value),
                serialize_numeric(units_before), serialize_numeric(units_after),
                serialize_numeric(cash_before), serialize_numeric(cash_after),
                serialize_numeric(avg_cost_before), serialize_numeric(avg_cost_after),
                f"{raw_close:.4f}", f"{adj_close:.4f}",
                position_mode,
            ])

    def _exec_buy(dt, trade_price, qty, zone, reason, raw_close, adj_close,
                  ma150_val, dividend, split_ratio):
        nonlocal cash, units, avg_cost, last_trade_price, last_trade_side, first_trade_date, first_trade_raw_price, first_trade_adj_price, total_buy_qty_raw, total_buy_cost
        qty = normalize_position_amount(qty, position_mode, lot_size)
        if qty <= POSITION_EPSILON:
            return
        px = trade_price * (1.0 + slippage_bp / 10000.0)
        fee = (px * qty * fee_rate) if position_mode == "absolute" else (qty * fee_rate)
        cash -= (px * qty + fee) if position_mode == "absolute" else (qty + fee)
        total_buy_qty_raw += qty
        total_buy_cost += (px * qty) if position_mode == "absolute" else qty
        avg_cost = calculate_new_avg_cost(units, avg_cost, qty, px)
        if first_trade_date is None:
            first_trade_date = dt
            first_trade_raw_price = raw_close
            first_trade_adj_price = adj_close
        units = normalize_position_amount(units + qty, position_mode, lot_size)
        last_trade_before = last_trade_price
        last_add_before = last_add_price
        last_trade_price = px
        last_trade_side = "buy"
        _append_trade_row(
            dt, "BUY", px, qty, units, zone, reason, raw_close, adj_close,
            ma150_val, last_trade_before, last_add_before,
            dividend, split_ratio
        )
        logger.info(
            f"[{dt}] BUY  {format_units_for_display(qty, position_mode):<8} @ {px:.4f} | "
            f"fee={serialize_numeric(fee)} | cash={serialize_numeric(cash)} | pos={format_units_for_display(units, position_mode)} | "
            f"{zone} | {reason} | raw={raw_close:.4f} restor={adj_close:.4f} | "
            f"MA150={(f'{ma150_val:.4f}' if ma150_val is not None else '')} | "
            f"last_trade_before={(f'{last_trade_before:.4f}' if last_trade_before is not None else '')} "
            f"last_add_before={(f'{last_add_before:.4f}' if last_add_before is not None else '')}"
        )

    def _exec_sell(dt, trade_price, qty, zone, reason, raw_close, adj_close,
                   ma150_val, dividend, split_ratio):
        nonlocal cash, units, avg_cost, last_trade_price, last_trade_side, realized_pnl, dilution_credit, first_trade_date, first_trade_raw_price, first_trade_adj_price, total_sell_qty_raw
        if units <= POSITION_EPSILON:
            return
        qty = min(_safe_float(qty, 0.0), units)
        qty = normalize_position_amount(qty, position_mode, lot_size)
        qty = min(qty, units)
        if qty <= POSITION_EPSILON:
            return
        px = trade_price * (1.0 - slippage_bp / 10000.0)
        avg_cost_before = avg_cost
        total_sell_qty_raw += qty
        if first_trade_date is None:
            first_trade_date = dt
            first_trade_raw_price = raw_close
            first_trade_adj_price = adj_close
        trade_realized_pnl = 0.0
        if position_mode == "absolute":
            fee = px * qty * fee_rate
            cash += (px * qty - fee)
            if avg_cost_before > 0:
                trade_realized_pnl = (px - avg_cost_before) * qty
        else:
            sale_value = qty * (px / avg_cost_before) if avg_cost_before > 0 else qty
            fee = sale_value * fee_rate
            cash += (sale_value - fee)
            if avg_cost_before > 0:
                trade_realized_pnl = qty * (px / avg_cost_before - 1.0)
        realized_pnl += trade_realized_pnl
        dilution_credit += trade_realized_pnl
        units = normalize_position_amount(max(units - qty, 0.0), position_mode, lot_size)
        if units <= POSITION_EPSILON:
            units = 0.0
            avg_cost = 0.0
            dilution_credit = 0.0
        last_trade_before = last_trade_price
        last_add_before = last_add_price
        last_trade_price = px
        last_trade_side = "sell"
        _append_trade_row(
            dt, "SELL", px, qty, units, zone, reason, raw_close, adj_close,
            ma150_val, last_trade_before, last_add_before,
            dividend, split_ratio
        )
        logger.info(
            f"[{dt}] SELL {format_units_for_display(qty, position_mode):<8} @ {px:.4f} | "
            f"fee={serialize_numeric(fee)} | cash={serialize_numeric(cash)} | pos={format_units_for_display(units, position_mode)} | "
            f"{zone} | {reason} | raw={raw_close:.4f} restor={adj_close:.4f} | "
            f"MA150={(f'{ma150_val:.4f}' if ma150_val is not None else '')} | "
            f"last_trade_before={(f'{last_trade_before:.4f}' if last_trade_before is not None else '')} "
            f"last_add_before={(f'{last_add_before:.4f}' if last_add_before is not None else '')}"
        )

    start_value = get_total_value(cash, units, avg_cost, raw_all[0], position_mode)
    if start_value <= POSITION_EPSILON:
        start_value = 1.0

    peak_value = None
    max_dd_ref = 0.0
    start_raw_price = raw_all[0]
    start_adj_price = adj_all[0]

    # “首次建仓”用于表示本次回测区间内初始持仓的建仓基准点，
    # 不是第一次由策略触发的加仓日期。
    # 如果回测起点已有 base_units/current_units 等初始仓位，则首次建仓日应为
    # 当前回测数据的第一天：当回测天数短于上市历史时是回测窗口首日；
    # 当回测天数超过上市历史时自然就是上市首日。
    # 如果回测起点没有初始仓位，则仍由第一笔 BUY/SELL 交易回填。
    if units > POSITION_EPSILON:
        first_trade_date = dates[0]
        first_trade_raw_price = start_raw_price
        first_trade_adj_price = start_adj_price
    else:
        first_trade_date = None
        first_trade_raw_price = None
        first_trade_adj_price = None

    logger.info("=" * 80)
    logger.info(f"Backtest start: {symbol} ({name})")
    logger.info(
        f"mode={position_mode} | Bars={len(df)} | base={format_units_for_display(base_units, position_mode)} | "
        f"target={format_units_for_display(target_units, position_mode)} | upper={format_units_for_display(double_target, position_mode)} | "
        f"trend_multiple={get_trend_multiple(cfg):.2f} | sell_multiple={get_sell_multiple(cfg):.2f}"
    )
    if position_mode == "absolute":
        logger.info(f"initial_cash={cash:.2f} | fee_rate={fee_rate} | slippage_bp={slippage_bp} | lot_size={lot_size}")
    else:
        logger.info(f"initial_cash_weight={format_percent_ratio(cash)} | fee_rate={fee_rate} | slippage_bp={slippage_bp}")
    logger.info("MODE: 信号=Adj Close；成交/估值=Close；事件=Dividends + Stock Splits")
    logger.info(f"backtest_pyramid_add_start=auto | live_config_pyramid_add_enabled={live_pyramid_add_enabled}")
    logger.info("=" * 80)

    for i in range(len(df)):
        dt = dates[i]
        raw_price = float(raw_all[i])
        adj_price = float(adj_all[i])
        dividend = float(div_all[i] or 0.0)
        split_ratio = float(split_all[i] or 1.0)

        # 拆股处理
        if split_ratio != 1.0 and units > POSITION_EPSILON:
            units_before = units
            cash_before = cash
            avg_cost_before = avg_cost
            if position_mode == "absolute":
                units = int(round(units * split_ratio))
            if avg_cost > 0:
                avg_cost = avg_cost / split_ratio
            _append_action_row(
                dt, "SPLIT", split_ratio,
                units_before, units,
                cash_before, cash,
                avg_cost_before, avg_cost,
                raw_price, adj_price,
            )
            logger.info(
                f"[{dt}] ACTION SPLIT ratio={split_ratio:.6f} | units {format_units_for_display(units_before, position_mode)} -> {format_units_for_display(units, position_mode)} | "
                f"avg_cost {avg_cost_before:.6f} -> {avg_cost:.6f}"
            )

        # 分红处理
        if dividend > 0 and units > POSITION_EPSILON:
            units_before = units
            cash_before = cash
            avg_cost_before = avg_cost
            if position_mode == "absolute":
                cash += units * dividend
                dividend_value = units * dividend
            else:
                dividend_value = units * dividend / avg_cost if avg_cost > 0 else 0.0
                cash += dividend_value
            dilution_credit += dividend_value
            _append_action_row(
                dt, "DIVIDEND", dividend_value,
                units_before, units,
                cash_before, cash,
                avg_cost_before, avg_cost,
                raw_price, adj_price,
            )
            logger.info(
                f"[{dt}] ACTION DIVIDEND value={serialize_numeric(dividend_value)} | units={format_units_for_display(units, position_mode)} | "
                f"cash {serialize_numeric(cash_before)} -> {serialize_numeric(cash)}"
            )

        closes_adj = adj_all[: i + 1]

        ma150_raw, _src150 = calc_ma_with_coef(closes_adj, ma_short_len)
        if ma150_raw is None:
            ma150 = adj_price
            zone = 'BOX_ZONE'
        else:
            sideways_score = float(compute_sideways_index(closes_adj, cfg))
            base_k150 = _safe_float(cfg.get("k150", 1.0), 1.0)
            min_k150 = _safe_float(cfg.get("sideways_min_k150", 0.85), 0.85)
            if base_k150 < min_k150:
                min_k150 = base_k150
            dynamic_k150 = min_k150 + (base_k150 - min_k150) * (1.0 - sideways_score)
            ma150 = ma150_raw * dynamic_k150
            zone = get_zone(adj_price, ma150, cfg)

        # 构建状态字典供策略函数使用（策略只在 strategy.py 中定义）
        state_dict = {
            "current_units": units,
            "last_trade_price": last_trade_price,
            "last_add_price": last_add_price,
            "pyramid_step": pyramid_step,
            "pyramid_active": pyramid_active,
            "target_reached_once": target_reached_once,
            "clear_step": clear_step,
            "initial_entry_price": first_trade_raw_price if first_trade_raw_price is not None else raw_all[0],
        }

        # In historical backtests, BOX_ZONE must not initiate add trades while pyramid is still auto.
        # This guard also protects the backtest if an older strategy.py still contains legacy BOX_ZONE_ADD logic.
        if zone == "BOX_ZONE" and get_pyramid_add_enabled(cfg) != "yes":
            add_qty, add_reason = 0.0, ""
            new_state, cfg_updates, events = state_dict.copy(), {}, []
        else:
            add_qty, add_reason, new_state, cfg_updates, events = get_add_trade_decision(
                state_dict, cfg, target_units, double_target, raw_price, ma150, zone, position_mode, lot_size
            )
        for k, v in cfg_updates.items():
            cfg[k] = v
        if add_qty > POSITION_EPSILON:
            _exec_buy(dt, raw_price, add_qty, zone, add_reason, raw_price, adj_price, ma150, dividend, split_ratio)

        # Persist pyramid runtime state even on days without a trade. This is important
        # for auto mode: once CHANCE_ZONE activates the pyramid state, the later BOX/CHANCE
        # steps must continue from that state instead of resetting every bar.
        pyramid_step = new_state.get("pyramid_step", pyramid_step)
        last_add_price = new_state.get("last_add_price", last_add_price)
        target_reached_once = new_state.get("target_reached_once", target_reached_once)
        pyramid_active = new_state.get("pyramid_active", pyramid_active)
        state_dict.update(new_state)
        state_dict["current_units"] = units

        # ========== 卖出决策 ==========
        sell_qty = 0.0
        if zone == "TREND_ZONE":
            sell_qty, new_state = get_trend_sell_decision(
                state_dict, cfg, target_units, position_mode, raw_price, ma150, lot_size
            )
            if sell_qty > 0:
                _exec_sell(dt, raw_price, sell_qty, zone, "TREND_ZONE_SELL",
                           raw_price, adj_price, ma150, dividend, split_ratio)
                last_trade_price = new_state.get("last_trade_price", last_trade_price)
        elif zone == "SELL_ZONE":
            pyramid_weights = cfg.get("pyramid_weights", [0.03, 0.055, 0.08, 0.105, 0.13, 0.155, 0.18, 0.205, 0.23, 0.255])
            sell_plan = calculate_pyramid_sell_plan(target_units, pyramid_weights, position_mode, lot_size)
            total_steps = len(sell_plan)
            target_step = get_pyramid_sell_target_step(raw_price, ma150, cfg, total_steps)
            if target_step > clear_step:
                for step_info in sell_plan[clear_step:target_step]:
                    step_units = min(step_info["units"], units)
                    if step_units <= POSITION_EPSILON:
                        clear_step = step_info["step"]
                        continue
                    _exec_sell(dt, raw_price, step_units, zone, f"SELL_ZONE_PYRAMID_STEP_{step_info['step']}",
                               raw_price, adj_price, ma150, dividend, split_ratio)
                    clear_step = step_info["step"]
                    if units <= POSITION_EPSILON:
                        break

        # 更新价值/回撤
        cur_value = get_total_value(cash, units, avg_cost, raw_price, position_mode)
        if peak_value is None or cur_value > peak_value:
            peak_value = cur_value
        if peak_value and peak_value > 0:
            max_dd_ref = max(max_dd_ref, (peak_value - cur_value) / peak_value)

        # 记录每日详情
        daily_records.append({
            "date": dt,
            "raw_price": raw_price,
            "restor_price": adj_price,
            "ma150": round(ma150, 4) if ma150 is not None else None,
            "zone": zone,
            "holding": format_units_for_display(units, position_mode),
            "pyramid": get_pyramid_add_enabled(cfg),
        })

    # 生成每日详情 CSV
    daily_details_path = outdir / f"daily_details_{symbol}.csv"
    with open(daily_details_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "raw_price", "restor_price", "ma150", "zone", "holding", "pyramid"])
        for rec in daily_records:
            writer.writerow([
                rec["date"],
                f"{rec['raw_price']:.4f}",
                f"{rec['restor_price']:.4f}",
                f"{rec['ma150']:.4f}" if rec["ma150"] is not None else "",
                rec["zone"] if rec["zone"] is not None else "",
                rec["holding"],
                rec.get("pyramid", ""),
            ])
    logger.info(f"每日详情 CSV 已保存: {daily_details_path}")

    end_value = get_total_value(cash, units, avg_cost, raw_all[-1], position_mode)
    final_market_weight = get_market_weight(units, avg_cost, raw_all[-1], position_mode)
    floating_pnl = (raw_all[-1] - avg_cost) * units if position_mode == "absolute" and units > POSITION_EPSILON and avg_cost > 0 else 0.0
    if position_mode == "percent" and units > POSITION_EPSILON and avg_cost > 0:
        floating_pnl = units * (raw_all[-1] / avg_cost - 1.0)

    # Raw ending average cost before cost dilution.
    # This is kept for internal PnL decomposition and market-weight estimation.
    raw_backtest_avg_cost = avg_cost if units > POSITION_EPSILON else 0.0

    total_fee = 0.0
    buy_cnt = 0
    sell_cnt = 0
    with open(trades_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["action"] == "BUY":
                buy_cnt += 1
            elif row["action"] == "SELL":
                sell_cnt += 1

    dividend_events = 0
    split_events = 0
    dividend_total = 0.0
    with open(actions_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["event_type"] == "DIVIDEND":
                dividend_events += 1
                dividend_total += _safe_float(row.get("value", 0.0), 0.0)
            elif row["event_type"] == "SPLIT":
                split_events += 1

    # Stock-return denominator: initial base position plus additional buys during backtest.
    # The old logic used only total_buy_cost/total_buy_qty_raw after any buy,
    # which omitted the initial base position and overstated stock-level returns.
    if position_mode == "absolute":
        stock_return_base = initial_stock_return_base + total_buy_cost
        if stock_return_base <= POSITION_EPSILON and units > POSITION_EPSILON and avg_cost > 0:
            stock_return_base = units * avg_cost
    else:
        stock_return_base = initial_stock_return_base + total_buy_qty_raw
        if stock_return_base <= POSITION_EPSILON and units > POSITION_EPSILON:
            stock_return_base = units
    if stock_return_base <= POSITION_EPSILON:
        stock_return_base = 1.0

    # Stock-app style diluted ending cost basis.
    # IMPORTANT: only cash/account contributions are used to reduce cost here.
    # Do NOT use dividend_stock_return or realized_stock_return in this formula, because
    # those are already divided by stock_return_base and would double-scale the result.
    #
    # absolute mode:
    #   remaining cost amount = ending shares * raw ending average cost - cash dividends - realized PnL
    # percent mode:
    #   units is a cost-weight position, while dividend_total and realized_pnl are account-weight
    #   contributions. Therefore the cost-weight to dilute is: units - dividend_total - realized_pnl.
    dividend_profit_contribution = dividend_total
    realized_profit_contribution = realized_pnl
    total_profit_contribution = dividend_total + realized_pnl + floating_pnl
    # For diluted cost, use the live credit ledger from the current open position cycle.
    # This usually equals dividend_total + realized_pnl when the position was never fully cleared.
    # If the strategy fully liquidates and later re-enters, earlier closed-cycle credits are reset
    # and will not make the new position's cost unrealistically low.
    dilution_profit_contribution = dilution_credit

    diluted_backtest_avg_cost = raw_backtest_avg_cost
    diluted_cost_reduction = 0.0
    diluted_cost_fully_recovered = False
    if units > POSITION_EPSILON and raw_backtest_avg_cost > POSITION_EPSILON:
        if position_mode == "absolute":
            original_cost_amount = units * raw_backtest_avg_cost
            remaining_cost_amount = original_cost_amount - dilution_profit_contribution
            if remaining_cost_amount > POSITION_EPSILON:
                diluted_backtest_avg_cost = remaining_cost_amount / units
                diluted_cost_reduction = raw_backtest_avg_cost - diluted_backtest_avg_cost
            else:
                # The current open position's dividends + realized gains have already
                # recovered all remaining position cost. Showing 0.00% return is misleading;
                # downstream report/web should display this as "cost recovered" instead of
                # forcing a divide-by-zero result to zero.
                diluted_backtest_avg_cost = 0.0
                diluted_cost_reduction = raw_backtest_avg_cost
                diluted_cost_fully_recovered = True
        else:
            original_cost_weight = units
            remaining_cost_weight = original_cost_weight - dilution_profit_contribution
            if remaining_cost_weight > POSITION_EPSILON:
                diluted_backtest_avg_cost = raw_backtest_avg_cost * remaining_cost_weight / original_cost_weight
                diluted_cost_reduction = raw_backtest_avg_cost - diluted_backtest_avg_cost
            else:
                # In percent mode, dividend_total/realized_pnl are account-weight credits.
                # If credits exceed the remaining cost-weight position, the software-style
                # diluted cost is zero/negative, meaning principal has been recovered.
                diluted_backtest_avg_cost = 0.0
                diluted_cost_reduction = raw_backtest_avg_cost
                diluted_cost_fully_recovered = True

    final_holding_return_note = ""
    if units > POSITION_EPSILON and diluted_backtest_avg_cost > POSITION_EPSILON:
        # Normal stock-app style: current price / diluted cost - 1.
        final_holding_return_rate = raw_all[-1] / diluted_backtest_avg_cost - 1.0
    elif units > POSITION_EPSILON and diluted_cost_fully_recovered:
        # When dividends + realized gains have reduced the diluted cost to zero or below,
        # a pure "price / diluted_cost - 1" calculation becomes undefined.
        # Keep a numeric current-position result by measuring the current open position's
        # final benefit against the original remaining position cost:
        #   (ending market value + current-cycle cash credits - original remaining cost)
        #   / original remaining cost
        # This preserves the user's "stock software-like" intent without showing 0.00%
        # while the account still holds shares/position.
        if position_mode == "absolute":
            original_holding_cost = units * raw_backtest_avg_cost
            ending_market_value = units * raw_all[-1]
        else:
            original_holding_cost = units
            ending_market_value = final_market_weight
        if original_holding_cost > POSITION_EPSILON:
            final_holding_return_rate = (ending_market_value + dilution_profit_contribution - original_holding_cost) / original_holding_cost
            final_holding_return_note = "摊薄成本已降至0，期末持仓收益率按(期末市值+分红/已实现收益贡献-原始持仓成本)/原始持仓成本计算"
        else:
            final_holding_return_rate = 0.0
            final_holding_return_note = "摊薄成本已降至0，但原始持仓成本为0，无法计算期末持仓收益率"
    else:
        final_holding_return_rate = 0.0

    dividend_stock_return = (dividend_total / stock_return_base) if stock_return_base > POSITION_EPSILON else 0.0
    realized_stock_return = (realized_pnl / stock_return_base) if stock_return_base > POSITION_EPSILON else 0.0
    holding_stock_return = (floating_pnl / stock_return_base) if stock_return_base > POSITION_EPSILON else 0.0
    stock_total_return = dividend_stock_return + realized_stock_return + holding_stock_return

    # 构建收益率分解字符串
    stock_return_explain = f"{dividend_stock_return * 100:.2f}% + {realized_stock_return * 100:.2f}% + {holding_stock_return * 100:.2f}% = {stock_total_return * 100:.2f}%"

    summary = {
        "symbol": symbol,
        "name": name,
        "position_mode": position_mode,
        "bars": len(df),
        "backtest_pyramid_add_start": "auto",
        "live_pyramid_add_enabled": live_pyramid_add_enabled,
        "runtime_pyramid_add_final": get_pyramid_add_enabled(cfg),
        "start_value": start_value,
        "end_value": end_value,
        "realized_pnl": realized_pnl,
        "floating_pnl": floating_pnl,
        "holding_stock_return": holding_stock_return,
        "final_holding_return_rate": final_holding_return_rate,
        "final_holding_return_note": final_holding_return_note,
        "diluted_cost_fully_recovered": diluted_cost_fully_recovered,
        "dividend_total": dividend_total,
        "dividend_stock_return": dividend_stock_return,
        "realized_stock_return": realized_stock_return,
        "stock_total_return": stock_total_return,
        "stock_return_base": stock_return_base,
        "initial_stock_return_base": initial_stock_return_base,
        "total_profit_contribution": total_profit_contribution,
        "dilution_credit": dilution_credit,
        "additional_buy_return_base": total_buy_cost if position_mode == "absolute" else total_buy_qty_raw,
        "stock_return_explain": stock_return_explain,
        "total_return": (end_value / start_value - 1.0) if start_value > 0 else 0.0,
        "max_drawdown_ref": max_dd_ref,
        "buy_trades": buy_cnt,
        "sell_trades": sell_cnt,
        "dividend_events": dividend_events,
        "split_events": split_events,
        "total_fee": total_fee,
        "final_cash": cash,
        "final_units": units,
        "final_market_weight": final_market_weight,
        "backtest_avg_cost": diluted_backtest_avg_cost if units > POSITION_EPSILON else 0.0,
        "raw_backtest_avg_cost": raw_backtest_avg_cost,
        "diluted_cost_reduction": diluted_cost_reduction if units > POSITION_EPSILON else 0.0,
        "dilution_profit_contribution": dilution_profit_contribution,
        "dividend_profit_contribution": dividend_profit_contribution,
        "realized_profit_contribution": realized_profit_contribution,
        "realized_and_dividend_profit": dilution_profit_contribution,
        "start_raw_price": start_raw_price,
        "start_adj_price": start_adj_price,
        "first_trade_date": first_trade_date,
        "first_trade_raw_price": first_trade_raw_price,
        "first_trade_adj_price": first_trade_adj_price,
        "last_raw_price": raw_all[-1],
        "last_adj_price": adj_all[-1],
        "log_path": str(log_path),
        "trades_csv": str(trades_csv),
        "actions_csv": str(actions_csv),
        "daily_details_csv": str(daily_details_path),
    }

    report_path = outdir / f"backtest_report_{symbol}.txt"
    with open(report_path, "w", encoding="utf-8") as rf:
        rf.write("================ 回测结果 ================\n\n")
        rf.write(f"标的: {summary['symbol']} | 名称: {summary['name']}\n")
        rf.write(f"模式: {'百分比仓位' if summary['position_mode'] == 'percent' else '股数仓位'}\n")
        rf.write(f"K线数量: {summary['bars']}\n")
        rf.write(f"回测倒金字塔起始开关: {summary['backtest_pyramid_add_start']}\n")
        if summary.get('live_pyramid_add_enabled') != summary.get('backtest_pyramid_add_start'):
            rf.write(f"实盘倒金字塔开关: {summary['live_pyramid_add_enabled']}（回测起步已忽略）\n")
        # 按 Web 摘要顺序输出核心指标
        rf.write(f"期末持仓收益率: {summary['final_holding_return_rate'] * 100:.2f}%\n")
        rf.write(f"综合收益率: {summary['stock_total_return'] * 100:.2f}%\n")
        rf.write(f"买入次数: {summary['buy_trades']}\n")
        rf.write(f"卖出次数: {summary['sell_trades']}\n")
        rf.write(f"分红收益率: {summary['dividend_stock_return'] * 100:.2f}%\n")
        rf.write(f"交易实现收益率: {summary['realized_stock_return'] * 100:.2f}%\n")
        rf.write(f"持仓收益率: {summary['holding_stock_return'] * 100:.2f}%\n")
        rf.write("综合收益率说明: 综合收益率 = 分红收益率 + 交易实现收益率 + 持仓收益率\n")
        rf.write(f"综合收益率计算: {summary['stock_return_explain']}\n")
        if summary['position_mode'] == 'percent':
            rf.write(f"累计投入仓位: {format_percent_ratio(summary['stock_return_base'])}\n")
            rf.write(f"收益贡献: {format_percent_ratio(summary['total_profit_contribution'])}\n")
            rf.write(f"摊薄成本收益贡献: {format_percent_ratio(summary['dilution_credit'])}\n")
            if summary.get('diluted_cost_fully_recovered'):
                rf.write(f"摊薄成本状态: {summary.get('final_holding_return_note', '摊薄成本已降至0，已按当前持仓最终收益口径计算')}\n")
        else:
            rf.write(f"累计投入金额: {summary['stock_return_base']:.4f}\n")
            rf.write(f"收益贡献金额: {summary['total_profit_contribution']:.4f}\n")
            rf.write(f"摊薄成本收益贡献金额: {summary['dilution_credit']:.4f}\n")
            if summary.get('diluted_cost_fully_recovered'):
                rf.write(f"摊薄成本状态: {summary.get('final_holding_return_note', '摊薄成本已降至0，已按当前持仓最终收益口径计算')}\n")
        rf.write(f"最新价格: {summary['last_raw_price']:.4f}\n")
        if summary.get('first_trade_date'):
            rf.write(f"首次建仓原始价: {summary['first_trade_raw_price']:.4f}\n")
            rf.write(f"首次建仓复权价: {summary['first_trade_adj_price']:.4f}\n")
            rf.write(f"首次建仓日: {summary['first_trade_date']}\n")
        rf.write(f"分红事件: {summary['dividend_events']}\n")
        rf.write(f"拆股事件: {summary['split_events']}\n")
        rf.write(f"最大回撤: {summary['max_drawdown_ref'] * 100:.2f}%\n")
        if summary['position_mode'] == 'percent':
            rf.write(f"期末现金权重: {format_percent_ratio(summary['final_cash'])}\n")
            rf.write(f"期末持仓: {format_units_for_display(summary['final_units'], 'percent')}\n")
            rf.write(f"摊薄后持仓成本: {summary['backtest_avg_cost']:.4f}\n")
            rf.write(f"期末市值权重: {format_percent_ratio(summary['final_market_weight'])}\n")
        else:
            rf.write(f"期末现金: {summary['final_cash']:.2f}\n")
            rf.write(f"期末持仓: {format_units_for_display(summary['final_units'], 'absolute')}\n")
            rf.write(f"摊薄后持仓成本: {summary['backtest_avg_cost']:.4f}\n")
            rf.write(f"期末市值: {summary['final_market_weight']:.2f}\n")
        rf.write(f"起始原始价: {summary['start_raw_price']:.4f}\n")
        rf.write(f"起始复权价: {summary['start_adj_price']:.4f}\n")
        rf.write(f"日志: {summary['log_path']}\n")
        rf.write(f"交易明细: {summary['trades_csv']}\n")
        rf.write(f"事件明细: {summary['actions_csv']}\n")
        rf.write(f"每日详情: {summary['daily_details_csv']}\n")
        write_config_section_to_report(rf, report_cfg)
    summary["report_path"] = str(report_path)

    summary_path = outdir / f"summary_{symbol}.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    logger.info("=" * 80)
    logger.info("SUMMARY")
    logger.info(json.dumps(summary, ensure_ascii=False, indent=2))
    logger.info("=" * 80)

    return summary


# ===========================
# 从 dcf.yaml 中按 symbol 找配置
# ===========================
def find_cfg_by_symbol(etf_config: dict, symbol: str):
    symbol = symbol.upper().strip()
    for name, cfg in etf_config.items():
        if str(cfg.get("symbol", "")).upper().strip() == symbol:
            return name, cfg
    return None, None


def resolve_backtest_cfg(symbol: str, etf_config: dict, common_cfg: dict):
    name, cfg = find_cfg_by_symbol(etf_config, symbol)
    if cfg is not None:
        resolved = dict(common_cfg or {})
        resolved.update(dict(cfg))
        return name, resolved, False

    if common_cfg:
        resolved = dict(common_cfg)
        resolved["symbol"] = symbol
        resolved.setdefault("strategy_run", "no")
        name = str(common_cfg.get("name", "通用回测参数"))
        return name, resolved, True

    return None, None, False


def derive_target_from_base(base):
    if isinstance(base, str) and base.strip().endswith("%"):
        pct = float(base.strip()[:-1])
        return f"{pct * 2:.6f}%"
    return str(parse_position_value(base) * 2.0)


# ===========================
# CLI入口
# ===========================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="dcf.yaml", help="path to dcf.yaml")
    ap.add_argument("--symbol", required=True, help="SH600519 / SZ000001 / HK00700")
    ap.add_argument("--days", type=int, default=800, help="history length")
    ap.add_argument("--outdir", default="backtest_out", help="output directory")
    ap.add_argument("--base-units", default=None, help="覆盖配置中的初始仓位，如 2.5%")
    ap.add_argument("--target-units", default=None, help="覆盖配置中的目标仓位，如 5%")
    ap.add_argument("--double-target-factor", type=float, default=None, help="覆盖最大仓位倍数")
    args = ap.parse_args()

    base_dir = Path(__file__).resolve().parent
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = base_dir / args.config

    etf_cfg, strategy_cfg, common_cfg = load_config(str(config_path))
    symbol = args.symbol.upper().strip()

    name, cfg, used_common_cfg = resolve_backtest_cfg(symbol, etf_cfg, common_cfg)
    if cfg is None:
        raise SystemExit(
            f"❌ dcf.yaml 中既找不到 symbol={symbol} 的 ETF_CONFIG 专属配置，也没有 COMMON_BACKTEST_CONFIG 通用配置。\n"
            f"请先在 dcf.yaml 中补充该标的配置，或添加 COMMON_BACKTEST_CONFIG。"
        )

    cfg = dict(cfg)
    if args.base_units is not None:
        cfg["base_units"] = args.base_units.strip()
        if args.target_units is None:
            cfg["target_units"] = derive_target_from_base(cfg["base_units"])

    if args.target_units is not None:
        cfg["target_units"] = args.target_units.strip()

    if args.double_target_factor is not None:
        cfg["double_target_factor"] = args.double_target_factor

    outdir = base_dir / args.outdir / symbol
    summary = backtest(symbol=symbol, name=name, cfg=cfg, strategy=strategy_cfg, days=args.days, outdir=outdir)

    print("\n================ 回测结果 ================\n")
    if used_common_cfg:
        print(f"提示: {symbol} 未在 ETF_CONFIG 中配置，已自动回退到 COMMON_BACKTEST_CONFIG 通用参数。")
    print(f"标的: {summary['symbol']} | 名称: {summary['name']}")
    print(f"模式: {'百分比仓位' if summary['position_mode'] == 'percent' else '股数仓位'}")
    print(f"K线数量: {summary['bars']}")
    print(f"期末持仓收益率: {summary['final_holding_return_rate'] * 100:.2f}%")
    print(f"综合收益率: {summary['stock_total_return'] * 100:.2f}%")
    print(f"买入次数: {summary['buy_trades']}")
    print(f"卖出次数: {summary['sell_trades']}")
    print(f"分红收益率: {summary['dividend_stock_return'] * 100:.2f}%")
    print(f"交易实现收益率: {summary['realized_stock_return'] * 100:.2f}%")
    print(f"持仓收益率: {summary['holding_stock_return'] * 100:.2f}%")
    print("综合收益率说明: 综合收益率 = 分红收益率 + 交易实现收益率 + 持仓收益率")
    print(f"综合收益率计算: {summary['stock_return_explain']}")
    if summary['position_mode'] == 'percent':
        print(f"累计投入仓位: {format_percent_ratio(summary['stock_return_base'])}")
        print(f"收益贡献: {format_percent_ratio(summary['total_profit_contribution'])}")
        print(f"摊薄成本收益贡献: {format_percent_ratio(summary['dilution_credit'])}")
        if summary.get('diluted_cost_fully_recovered'):
            print(f"摊薄成本状态: {summary.get('final_holding_return_note', '摊薄成本已降至0，已按当前持仓最终收益口径计算')}")
    else:
        print(f"累计投入金额: {summary['stock_return_base']:.4f}")
        print(f"收益贡献金额: {summary['total_profit_contribution']:.4f}")
        print(f"摊薄成本收益贡献金额: {summary['dilution_credit']:.4f}")
        if summary.get('diluted_cost_fully_recovered'):
            print(f"摊薄成本状态: {summary.get('final_holding_return_note', '摊薄成本已降至0，已按当前持仓最终收益口径计算')}")
    print(f"最新价格: {summary['last_raw_price']:.4f}")
    if summary.get('first_trade_date'):
        print(f"首次建仓原始价: {summary['first_trade_raw_price']:.4f}")
        print(f"首次建仓复权价: {summary['first_trade_adj_price']:.4f}")
        print(f"首次建仓日: {summary['first_trade_date']}")
    print(f"分红事件: {summary['dividend_events']}")
    print(f"拆股事件: {summary['split_events']}")
    print(f"最大回撤: {summary['max_drawdown_ref'] * 100:.2f}%")

    if summary['position_mode'] == 'absolute':
        print(f"起始价值(参考): {summary['start_value']:.2f}")
        print(f"结束价值(参考): {summary['end_value']:.2f}")
        print(f"期末现金: {summary['final_cash']:.2f}")
        print(f"期末持仓: {format_units_for_display(summary['final_units'], 'absolute')}")
        print(f"摊薄后持仓成本: {summary['backtest_avg_cost']:.4f}")
        print(f"期末市值: {summary['final_market_weight']:.2f}")
    else:
        print(f"起始总权益(参考): {format_percent_ratio(summary['start_value'])}")
        print(f"结束总权益(参考): {format_percent_ratio(summary['end_value'])}")
        print(f"期末现金权重: {format_percent_ratio(summary['final_cash'])}")
        print(f"期末持仓: {format_units_for_display(summary['final_units'], 'percent')}")
        print(f"摊薄后持仓成本: {summary['backtest_avg_cost']:.4f}")
        print(f"期末市值权重: {format_percent_ratio(summary['final_market_weight'])}")

    print(f"起始原始价: {summary['start_raw_price']:.4f}")
    print(f"起始复权价: {summary['start_adj_price']:.4f}")
    print(f"\n日志: {summary['log_path']}")
    print(f"交易明细: {summary['trades_csv']}")
    print(f"事件明细: {summary['actions_csv']}")
    print(f"每日详情: {summary['daily_details_csv']}")
    print("\n说明：")
    print("1) 价格口径：信号和区间使用 Adj Close；成交、估值、持仓成本使用 Close；分红现金入账，拆股调整仓位和成本。")
    print("2) 区间：CHANCE=价格<MA150；BOX=MA150~MA150*trend_multiple；TREND=MA150*trend_multiple~MA150*sell_multiple；SELL=价格≥MA150*sell_multiple。")
    print("3) 倒金字塔加仓：历史回测每次从 pyramid_add_enabled=auto 起步，忽略 dcf.yaml 中实盘 yes；首次进入 CHANCE_ZONE 后才切到 yes。")
    print("4) 箱体区规则：回测起步在 BOX_ZONE 时不会因实盘 yes 直接补仓；只有已由 CHANCE_ZONE 激活的倒金字塔模式，才可在 CHANCE/BOX 中继续按步长加仓。")
    print("5) 卖出规则：TREND_ZONE 只卖出高于目标仓位的机动仓；SELL_ZONE 按 clear_zone_step_percent 推进倒金字塔卖出步数。")
    print("6) 回测成本：历史初始持仓成本使用回测窗口第一天 Close；current_avg_cost 仅用于实盘监控，不参与回测成本初始化。")
    print("7) 收益口径：期末持仓收益率=最新价格/摊薄后持仓成本-1；综合收益率按累计投入计算。")
    print("8) 百分比模式下 qty 表示仓位比例；交易日志保留上一次成交价和上一次加仓价。")
    print("=========================================\n")


if __name__ == "__main__":
    main()