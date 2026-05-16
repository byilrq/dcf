#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time as time_module
import json
from pathlib import Path
from datetime import datetime, time, timedelta
import requests
import logging
import os
import math
import csv
import shutil
import sys
import re
import random
from datetime import datetime
from email.header import Header
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 导入策略模块
from strategy import (
    get_zone,
    normalize_position_amount,
    calculate_pyramid_sell_plan,
    get_pyramid_sell_target_step,
    get_trend_sell_decision,
    get_pyramid_add_enabled,
    get_add_trade_decision,
    POSITION_EPSILON,
)

# ===========================
# 路径配置
# ===========================
BASE_DIR = Path(__file__).parent
config_path = os.path.join(BASE_DIR, "dcf.yaml")
STATE_FILE = BASE_DIR / "dcf_monitor_state.json"
LOG_DIR = BASE_DIR / "log"
TRADE_LOG_FILE = BASE_DIR / "trade_log.csv"
LOG_DIR.mkdir(exist_ok=True)

# ===========================
# 辅助函数
# ===========================
def calculate_new_avg_cost(old_position, old_avg_cost, add_units, add_price):
    if old_position == 0:
        return add_price
    total_cost_before = old_position * old_avg_cost
    total_cost_after = total_cost_before + add_units * add_price
    return total_cost_after / (old_position + add_units)

def _parse_hm(s: str):
    try:
        h, m = s.split(":")
        return int(h), int(m)
    except Exception:
        return 9, 30

def round_to_lot(qty, lot_size=100):
    if qty <= 0:
        return 0
    rounded_qty = int(qty // lot_size) * lot_size
    if rounded_qty == 0 and qty > 0:
        rounded_qty = lot_size
    return rounded_qty

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

def normalize_strategy_run_value(value, default="on"):
    """运行开关只允许 on/off；无效或缺失值按 default 处理，默认 on。"""
    s = str(value if value is not None else "").strip().lower()
    if s in {"on", "off"}:
        return s
    return default

def is_strategy_on(cfg):
    return normalize_strategy_run_value(cfg.get("strategy_run", "on"), "on") == "on"

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
    value = str(cfg.get("box_grid_enabled", "no")).strip().lower()
    return value in {"yes", "true", "1", "on"}

def get_live_current_units(cfg):
    if "current_units" not in cfg or cfg.get("current_units") in (None, ""):
        return None
    mode = get_position_mode(cfg)
    return normalize_position_amount(parse_position_value(cfg.get("current_units", 0)), mode)

def get_live_current_avg_cost(cfg):
    if "current_avg_cost" not in cfg or cfg.get("current_avg_cost") in (None, ""):
        return None
    return max(_safe_float(cfg.get("current_avg_cost", 0.0), 0.0), 0.0)

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

def build_default_symbol_state(cfg):
    base_units = get_base_units(cfg)
    live_units = get_live_current_units(cfg)
    live_avg_cost = get_live_current_avg_cost(cfg)
    return {
        "last_price": None,
        "last_trade_price": None,
        "last_trade_side": "buy",
        "tick": 0,
        "current_units": live_units if live_units is not None else base_units,
        "avg_cost": live_avg_cost if live_avg_cost is not None else 0.0,
        "ma_short": None,
        "last_status_msg": None,
        "pyramid_step": 0,
        "clear_step": 0,
        "strategy_run": normalize_strategy_run_value(cfg.get("strategy_run", "on"), "on"),
        "position_mode": get_position_mode(cfg),
        "last_add_price": None,
        "pyramid_active": False,
        "target_reached_once": False,
    }

def normalize_symbol_state(name, cfg, entry):
    mode = get_position_mode(cfg)
    base_units = get_base_units(cfg)
    live_units = get_live_current_units(cfg)
    live_avg_cost = get_live_current_avg_cost(cfg)
    double_target = get_double_target(cfg)
    legacy_mode = entry.get("position_mode")
    reset_reason = None
    if "last_trade_price" not in entry:
        entry["last_trade_price"] = None
    if "last_trade_side" not in entry:
        entry["last_trade_side"] = "buy"
    if "avg_cost" not in entry:
        entry["avg_cost"] = 0.0
    if "strategy_run" not in entry:
        entry["strategy_run"] = normalize_strategy_run_value(cfg.get("strategy_run", "on"), "on")
    if "pyramid_step" not in entry:
        entry["pyramid_step"] = 0
    if "clear_step" not in entry:
        entry["clear_step"] = 0
    if "last_add_price" not in entry:
        entry["last_add_price"] = None
    if "pyramid_active" not in entry:
        entry["pyramid_active"] = False
    if "target_reached_once" not in entry:
        entry["target_reached_once"] = False
    current_units = entry.get("current_units", live_units if live_units is not None else base_units)
    try:
        current_units = float(current_units)
    except Exception:
        reset_reason = "current_units 无法解析"
        current_units = base_units
    if legacy_mode and legacy_mode != mode:
        reset_reason = f"状态文件仓位模式为 {legacy_mode}，当前配置为 {mode}"
    if mode == "percent":
        if current_units < -POSITION_EPSILON:
            reset_reason = "current_units 为负数"
        elif current_units > max(1.0, double_target * 5):
            reset_reason = "检测到旧版按股数状态，无法自动换算为百分比仓位"
    else:
        if current_units < 0:
            reset_reason = "current_units 为负数"
    if reset_reason:
        logging.warning(
            f"⚠️ {name}: {reset_reason}，已重置为基准仓位 {format_units_for_display(live_units if live_units is not None else base_units, mode)}"
        )
        entry["current_units"] = live_units if live_units is not None else base_units
        entry["avg_cost"] = live_avg_cost if live_avg_cost is not None else 0.0
        entry["pyramid_step"] = 0
        entry["clear_step"] = 0
        entry["last_add_price"] = None
        entry["pyramid_active"] = False
        entry["target_reached_once"] = False
    else:
        entry["current_units"] = normalize_position_amount(current_units, mode)
        if live_units is not None:
            entry["current_units"] = live_units
        if live_avg_cost is not None:
            entry["avg_cost"] = live_avg_cost
        elif entry["current_units"] <= POSITION_EPSILON:
            entry["avg_cost"] = 0.0
    entry["position_mode"] = mode
    return entry

# ===========================
# 日志轮转与备份函数
# ===========================
def rotate_and_backup_logs(now: datetime = None):
    if now is None:
        now = strategy_now()
    log_file = BASE_DIR / "dcf.log"
    backup_date = (now.date() - timedelta(days=1))
    backup_file = LOG_DIR / f"dcf.{backup_date.strftime('%Y%m%d')}.log"
    if not log_file.exists():
        return False
    try:
        shutil.copy2(log_file, backup_file)
        logger = logging.getLogger()
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)
        with open(log_file, 'w', encoding='utf-8') as f:
            f.truncate(0)
        setup_logging()
        logging.info("=" * 60)
        logging.info(f"🔄 日志轮转完成 - {now.strftime('%Y-%m-%d %H:%M:%S')}")
        logging.info(f"📁 日志已备份至: {backup_file.name}")
        logging.info("=" * 60)
        return True
    except Exception as e:
        print(f"日志轮转失败: {e}")
        setup_logging()
        return False

def setup_logging():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    if logger.hasHandlers():
        logger.handlers.clear()
    log_file = BASE_DIR / "dcf.log"
    file_handler = logging.FileHandler(
        filename=str(log_file),
        encoding="utf-8",
        mode='a'
    )
    file_handler.setLevel(logging.INFO)
    formatter = logging.Formatter("%(message)s")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger

logger = setup_logging()

# ===========================
# 读取配置文件
# ===========================
def load_config(path):
    try:
        import yaml
    except ImportError:
        import json5 as json_mod
        with open(path, "r", encoding="utf-8") as f:
            cfg = json_mod.load(f)
    else:
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    cfg = cfg or {}
    dcf_cfg = cfg.get("ETF_CONFIG", {}) or {}
    strategy_cfg = cfg.get("STRATEGY", {}) or {}
    return dcf_cfg, strategy_cfg, cfg

def save_full_config(full_cfg, path=None):
    target = path or config_path
    try:
        import yaml
    except ImportError:
        return False
    with open(target, "w", encoding="utf-8") as f:
        yaml.safe_dump(full_cfg, f, allow_unicode=True, sort_keys=False)
    return True

def persist_runtime_position_to_config(name, current_units, avg_cost):
    global FULL_CONFIG, ETF_CONFIG
    if not isinstance(FULL_CONFIG, dict):
        return False
    etf_cfg = FULL_CONFIG.setdefault("ETF_CONFIG", {})
    if name not in etf_cfg or not isinstance(etf_cfg.get(name), dict):
        return False
    mode = get_position_mode(etf_cfg[name])
    etf_cfg[name]["current_units"] = format_units_for_display(current_units, mode) if mode == "percent" else int(round(_safe_float(current_units, 0.0)))
    etf_cfg[name]["current_avg_cost"] = round(_safe_float(avg_cost, 0.0), 6) if _safe_float(avg_cost, 0.0) > 0 else 0.0
    ETF_CONFIG[name]["current_units"] = etf_cfg[name]["current_units"]
    ETF_CONFIG[name]["current_avg_cost"] = etf_cfg[name]["current_avg_cost"]
    return save_full_config(FULL_CONFIG)

FULL_CONFIG = {}
STRATEGY = {
    "loop_interval": 60,
    "fetch_history_days": 400,
    "ma_period_short": 150,
    "ma_period_long": 300,  # 保留但不再使用
    "session_start": "09:30",
    "session_end": "16:00",
    "daily_push_time": "09:00",
    "log_rotate_time": "09:00",
    # 所有策略时间参数均按该时区解释；不依赖服务器系统时区。
    # 可选示例：Asia/Shanghai、Asia/Tokyo、Asia/Singapore、America/Los_Angeles。
    "timezone": "Asia/Shanghai",
}

TIMEZONE_ALIASES = {
    "shanghai": "Asia/Shanghai",
    "上海": "Asia/Shanghai",
    "china": "Asia/Shanghai",
    "cn": "Asia/Shanghai",
    "tokyo": "Asia/Tokyo",
    "东京": "Asia/Tokyo",
    "japan": "Asia/Tokyo",
    "jp": "Asia/Tokyo",
    "singapore": "Asia/Singapore",
    "新加坡": "Asia/Singapore",
    "sg": "Asia/Singapore",
    "los_angeles": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles",
    "la": "America/Los_Angeles",
    "洛杉矶": "America/Los_Angeles",
}

def get_strategy_timezone_name() -> str:
    raw = str(STRATEGY.get("timezone", "Asia/Shanghai") or "Asia/Shanghai").strip()
    if not raw:
        return "Asia/Shanghai"
    return TIMEZONE_ALIASES.get(raw.lower(), raw)

def resolve_strategy_timezone():
    name = get_strategy_timezone_name()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logging.warning(f"⚠️ 策略时区配置无效: {name}，已回退到 Asia/Shanghai")
        return ZoneInfo("Asia/Shanghai")

def strategy_now() -> datetime:
    """Return current time in configured strategy timezone, independent of server timezone."""
    return datetime.now(resolve_strategy_timezone())

# ===========================
# 时间控制函数
# ===========================
def in_trade_session(now: datetime = None) -> bool:
    if now is None:
        now = strategy_now()
    start_str = STRATEGY.get("session_start", "09:30")
    end_str = STRATEGY.get("session_end", "16:00")
    sh, sm = _parse_hm(start_str)
    eh, em = _parse_hm(end_str)
    t = now.time()
    start_t = time(sh, sm)
    end_t = time(eh, em)
    return start_t <= t <= end_t

def should_rotate_logs(state: dict, now: datetime = None) -> bool:
    if now is None:
        now = strategy_now()
    rotate_str = STRATEGY.get("log_rotate_time", "09:00")
    rh, rm = _parse_hm(rotate_str)
    rotate_t = time(rh, rm)
    today = now.date().isoformat()
    meta = state.get("_meta", {})
    last_rotate_date = meta.get("last_log_rotate_date")
    if now.time() >= rotate_t and last_rotate_date != today:
        return True
    return False

def should_do_daily_push(state: dict, now: datetime = None) -> bool:
    if now is None:
        now = strategy_now()
    push_str = STRATEGY.get("daily_push_time", "09:00")
    ph, pm = _parse_hm(push_str)
    push_t = time(ph, pm)
    today = now.date().isoformat()
    meta = state.get("_meta", {})
    last_date = meta.get("last_daily_push_date")
    if now.time() >= push_t and last_date != today:
        return True
    return False

# ===========================
# 状态文件读写
# ===========================
def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            for name, cfg in ETF_CONFIG.items():
                if name not in state or not isinstance(state.get(name), dict):
                    state[name] = build_default_symbol_state(cfg)
                else:
                    state[name] = normalize_symbol_state(name, cfg, state[name])
            if "_meta" not in state:
                state["_meta"] = {
                    "last_daily_push_date": None,
                    "last_log_rotate_date": None
                }
            elif "last_log_rotate_date" not in state["_meta"]:
                state["_meta"]["last_log_rotate_date"] = None
            return state
        except Exception as e:
            logging.error(f"加载状态文件失败: {e}")
            return {}
    initial_state = {}
    for name, cfg in ETF_CONFIG.items():
        initial_state[name] = build_default_symbol_state(cfg)
    initial_state["_meta"] = {
        "last_daily_push_date": None,
        "last_log_rotate_date": None
    }
    return initial_state

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def read_state_raw():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logging.error(f"读取状态刷新请求失败: {e}")
    return {}

def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default

def apply_runtime_config_reload_if_needed(state, last_seen_seq):
    """Apply config reload requests written by dcf_web.py without restarting dcf.py."""
    disk_state = read_state_raw()
    disk_meta = disk_state.get("_meta", {}) if isinstance(disk_state, dict) else {}
    seq = _safe_int(disk_meta.get("config_reload_seq", 0), 0)
    if seq <= last_seen_seq:
        return last_seen_seq
    state.setdefault("_meta", {})
    state["_meta"].update(disk_meta)
    requested = disk_meta.get("config_reload_symbols") or []
    if isinstance(requested, str):
        requested = [requested]
    if not requested:
        requested = [disk_meta.get("config_reload_symbol_key", "")]
    if "__ALL__" in requested:
        target_names = list(ETF_CONFIG.keys())
    else:
        target_names = [name for name in requested if name in ETF_CONFIG]
    if not target_names:
        logging.info(f"🔁 收到参数刷新请求 seq={seq}，但未匹配到标的，已忽略。")
        return seq
    for name in target_names:
        old_entry = state.get(name, {}) if isinstance(state.get(name, {}), dict) else {}
        preserved_last_price = old_entry.get("last_price")
        new_entry = build_default_symbol_state(ETF_CONFIG[name])
        if preserved_last_price is not None:
            new_entry["last_price"] = preserved_last_price
        state[name] = normalize_symbol_state(name, ETF_CONFIG[name], new_entry)
        state[name]["last_status_msg"] = None
        logging.info(f"🔁 参数已即时刷新: {name}，运行状态已按最新 dcf.yaml 重置。")
    save_state(state)
    return seq

# ===========================
# 构建每日快照
# ===========================
def build_daily_snapshot(state: dict) -> str:
    lines = []
    current_time = strategy_now().strftime("%Y.%m.%d.%H:%M")
    for name in ETF_CONFIG.keys():
        dcf_state = state.get(name, {})
        status = dcf_state.get("last_status_msg")
        cfg = ETF_CONFIG.get(name, {})
        strategy_run = normalize_strategy_run_value(cfg.get("strategy_run", "on"), "on")
        if status:
            old_time_match = re.search(r'🕒时间:\s*(\d{4}\.\d{2}\.\d{2}\.\d{2}:\d{2})', status)
            if old_time_match:
                status = status.replace(old_time_match.group(1), current_time)
            if strategy_run == "off":
                lines.append(f"[仅监控] {status}")
            else:
                lines.append(status)
        else:
            if strategy_run == "off":
                lines.append(f"[仅监控] {name}: 暂无状态记录")
            else:
                lines.append(f"{name}: 暂无状态记录")
    snapshot_header = f"🎯 每日快照 - {strategy_now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return snapshot_header + "\n\n".join(lines)

# ===========================
# 行情数据获取 - 独立接口层
# ===========================
try:
    from market_data import (
        MarketSnapshot,
        get_market_snapshot,
        get_price_from_api,
        get_history_close,
        get_hk_history_close,
        get_a_history_close,
        get_a_price,
        get_hk_price,
    )
except Exception as _market_import_error:
    raise RuntimeError(f"无法导入行情接口文件 market_data.py: {_market_import_error}")


def _is_price_jump_suspicious(current_price, reference_price, max_ratio=0.25):
    try:
        cur = float(current_price)
        ref = float(reference_price)
    except Exception:
        return False, 1.0
    if cur <= 0 or ref <= 0:
        return False, 1.0
    ratio = cur / ref
    if ratio > (1.0 + max_ratio) or ratio < (1.0 - max_ratio):
        return True, ratio
    return False, ratio


def _format_market_message_lines(name, symbol, level, reason, current_price=None, last_known_price=None, closes_count=None, source=None, last_bar_date=None):
    if level == "warn":
        head = f"🟡[WARN]【{name}】 ({symbol})"
        status = "⚠️行情源观察中，本轮只监控不交易。"
    else:
        head = f"🎯[ERROR]【{name}】 ({symbol})"
        status = "❌行情数据异常，已跳过本轮策略，不会触发交易。"
    lines = [
        head,
        f"🕒时间: {strategy_now().strftime('%Y.%m.%d.%H:%M')}",
        status,
        f"原因: {reason}",
    ]
    if source:
        lines.append(f"📡行情源: {source}")
    if last_bar_date:
        lines.append(f"🧾最新K线日期: {last_bar_date}")
    if current_price is not None:
        try:
            lines.append(f"当前价: {float(current_price):.3f}")
        except Exception:
            lines.append(f"当前价: {current_price}")
    if last_known_price is not None:
        try:
            lines.append(f"上次有效价: {float(last_known_price):.3f}")
        except Exception:
            lines.append(f"上次有效价: {last_known_price}")
    if closes_count is not None:
        lines.append(f"历史数据条数: {closes_count}")
    return lines


def _build_market_data_error_message(name, symbol, reason, current_price=None, last_known_price=None, closes_count=None, source=None, last_bar_date=None):
    return chr(10).join(_format_market_message_lines(name, symbol, "error", reason, current_price, last_known_price, closes_count, source, last_bar_date))


def _build_market_data_warn_message(name, symbol, reason, current_price=None, last_known_price=None, closes_count=None, source=None, last_bar_date=None):
    return chr(10).join(_format_market_message_lines(name, symbol, "warn", reason, current_price, last_known_price, closes_count, source, last_bar_date))


def _is_all_sources_failed_reason(reason):
    text = str(reason or "")
    return ("全部数据源失败" in text) or ("全部行情源失败" in text) or ("全部数据源" in text and "失败" in text)


def _maybe_market_alert(dcf_state, msg, reason_key):
    """只有全部行情源失败才推送，并按标的/自然日去重。"""
    if not _is_all_sources_failed_reason(reason_key):
        return []
    day_key = strategy_now().strftime("%Y%m%d")
    alert_key = f"{day_key}|all_sources_failed"
    if dcf_state.get("last_market_all_sources_alert_key") == alert_key:
        return []
    dcf_state["last_market_all_sources_alert_key"] = alert_key
    return [msg]


def _mark_market_error(dcf_state, msg, reason, source=""):
    dcf_state["last_status_msg"] = msg
    dcf_state["market_status"] = "error"
    dcf_state["market_error"] = str(reason)[:500]
    if source:
        dcf_state["market_source"] = source


def _mark_market_warn(dcf_state, msg, reason, source=""):
    dcf_state["last_status_msg"] = msg
    dcf_state["market_status"] = "warn"
    dcf_state["market_error"] = str(reason)[:500]
    if source:
        dcf_state["market_source"] = source


def _check_market_source_switch(dcf_state, snapshot, cfg):
    """行情源切换首轮禁止交易，连续确认后才允许新源进入策略。"""
    new_source = snapshot.source
    last_source = dcf_state.get("last_valid_market_source") or dcf_state.get("market_source")
    if not last_source or last_source == new_source:
        dcf_state["pending_market_source"] = ""
        dcf_state["pending_market_source_count"] = 0
        return True, ""

    try:
        required = int(_safe_float(cfg.get("market_source_switch_confirmations", STRATEGY.get("market_source_switch_confirmations", 2)), 2))
    except Exception:
        required = 2
    required = max(2, required)

    pending_source = dcf_state.get("pending_market_source")
    pending_count = int(_safe_float(dcf_state.get("pending_market_source_count", 0), 0))
    if pending_source == new_source:
        pending_count += 1
    else:
        pending_source = new_source
        pending_count = 1
    dcf_state["pending_market_source"] = pending_source
    dcf_state["pending_market_source_count"] = pending_count

    if pending_count < required:
        return False, f"行情源从 {last_source} 切换到 {new_source}，等待连续确认 {pending_count}/{required}；本轮只监控不交易"

    return True, f"行情源从 {last_source} 切换到 {new_source}，已连续确认 {pending_count}/{required}"


def _mark_market_ok(dcf_state, snapshot):
    dcf_state["market_status"] = "ok"
    dcf_state["market_error"] = ""
    dcf_state["market_source"] = snapshot.source
    dcf_state["last_valid_market_source"] = snapshot.source
    dcf_state["last_valid_price"] = snapshot.current_price
    dcf_state["last_valid_bar_date"] = snapshot.last_bar_date
    dcf_state["pending_market_source"] = ""
    dcf_state["pending_market_source_count"] = 0

# ===========================
# 计算简单移动平均线 MA（带数据不足处理）
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

# ===========================
# 横盘指数
# ===========================
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
    deltas = [seg[i+1] - seg[i] for i in range(window)]
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
    weight60 = float(cfg.get("sideways_weight_60", 0.6))
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
# 推送功能
# ===========================
PUSH_CONFIG_FILE = Path("/root/dcf/push.conf")
PUSH_LOG_FILE = Path("/root/dcf/push.log")
PUSHPLUS_URL = "http://www.pushplus.plus/send"


def _strip_shell_quotes(value):
    value = str(value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def load_push_config():
    """读取 /root/dcf/push.conf，并用环境变量作为兜底。"""
    cfg = {
        "PUSH_ENABLED": os.getenv("PUSH_ENABLED", "yes"),
        "PUSH_CHANNEL": os.getenv("PUSH_CHANNEL", "gotify"),
        "PUSHPLUS_TOKEN": os.getenv("PUSHPLUS_TOKEN", ""),
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", ""),
        "GOTIFY_URL": os.getenv("GOTIFY_URL", "https://sharq.eu.org:2084"),
        "GOTIFY_TOKEN": os.getenv("GOTIFY_TOKEN", ""),
        "GOTIFY_PRIORITY": os.getenv("GOTIFY_PRIORITY", "10"),
        "NTFY_URL": os.getenv("NTFY_URL", "http://127.0.0.1:8083"),
        "NTFY_TOPIC": os.getenv("NTFY_TOPIC", "let-rss"),
        "NTFY_USERNAME": os.getenv("NTFY_USERNAME", ""),
        "NTFY_PASSWORD": os.getenv("NTFY_PASSWORD", ""),
        "NTFY_PRIORITY": os.getenv("NTFY_PRIORITY", "4"),
        "NTFY_TAGS": os.getenv("NTFY_TAGS", "dcf,chart_with_upwards_trend"),
    }
    path_candidates = [PUSH_CONFIG_FILE, BASE_DIR / "push.conf"]
    for path in path_candidates:
        if not path.exists():
            continue
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key in cfg:
                    cfg[key] = _strip_shell_quotes(value)
            break
        except Exception as e:
            logging.error(f"读取推送配置失败 {path}: {e}")
    cfg["PUSH_ENABLED"] = str(cfg.get("PUSH_ENABLED", "yes")).strip().lower()
    cfg["PUSH_CHANNEL"] = str(cfg.get("PUSH_CHANNEL", "gotify")).strip().lower()
    if cfg["PUSH_CHANNEL"] == "both":
        cfg["PUSH_CHANNEL"] = "pushplus"
    elif cfg["PUSH_CHANNEL"] == "all":
        cfg["PUSH_CHANNEL"] = "gotify"
    if cfg["PUSH_CHANNEL"] not in {"telegram", "gotify", "ntfy", "pushplus", "none"}:
        cfg["PUSH_CHANNEL"] = "gotify"
    try:
        cfg["GOTIFY_PRIORITY"] = str(int(float(str(cfg.get("GOTIFY_PRIORITY", "10") or "10"))))
    except Exception:
        cfg["GOTIFY_PRIORITY"] = "10"
    try:
        ntfy_priority = int(float(str(cfg.get("NTFY_PRIORITY", "4") or "4")))
        cfg["NTFY_PRIORITY"] = str(max(1, min(5, ntfy_priority)))
    except Exception:
        cfg["NTFY_PRIORITY"] = "4"
    cfg["NTFY_URL"] = str(cfg.get("NTFY_URL", "")).strip().rstrip("/") or "http://127.0.0.1:8083"
    cfg["NTFY_TOPIC"] = str(cfg.get("NTFY_TOPIC", "")).strip().strip("/") or "let-rss"
    return cfg




def append_push_log(channel, success, detail):
    try:
        PUSH_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        line = f"{strategy_now().strftime('%Y-%m-%d %H:%M:%S')} | {channel} | {'成功' if success else '失败'} | {str(detail).replace(chr(10), ' ')[:500]}\n"
        with PUSH_LOG_FILE.open('a', encoding='utf-8') as f:
            f.write(line)
    except Exception as e:
        logging.error(f"写入推送日志失败: {e}")

def escape_markdown_v2(text: str) -> str:
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    result = []
    for char in text:
        if char in escape_chars:
            result.append('\\' + char)
        else:
            result.append(char)
    return ''.join(result)


def _truncate_msg(text, limit=4000):
    if len(text) > limit:
        return text[:limit] + "\n...\n(消息过长，已截断)"
    return text


def _send_pushplus(msg, cfg):
    token = str(cfg.get("PUSHPLUS_TOKEN", "")).strip()
    if not token:
        logging.info("未配置 PushPlus Token，跳过该通道推送。")
        return False
    payload = {
        "token": token,
        "title": "QT",
        "content": _truncate_msg(msg, 4000),
        "template": "txt",
    }
    try:
        resp = requests.post(PUSHPLUS_URL, json=payload, timeout=10)
        resp_data = resp.json()
        if resp_data.get("code") != 200:
            logging.error(f"PushPlus 推送失败: {resp_data.get('msg', '未知错误')}")
            return False
        logging.info("✅ PushPlus 推送成功。")
        return True
    except Exception as e:
        logging.error(f"PushPlus 推送异常: {e}")
        return False


def _send_telegram(msg, cfg):
    token = str(cfg.get("TELEGRAM_BOT_TOKEN", "")).strip()
    chat_id = str(cfg.get("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not chat_id:
        missing = []
        if not token:
            missing.append("Bot Token")
        if not chat_id:
            missing.append("Chat ID")
        logging.info(f"未配置 Telegram {', '.join(missing)}，跳过该通道推送。")
        return False
    try:
        lines = msg.split('\n')
        filtered_lines = [line for line in lines if "🚦策略运行状态:" not in line]
        plain_msg = '\n'.join(filtered_lines)
        escaped_msg = escape_markdown_v2(plain_msg)
        telegram_payload = {
            "chat_id": chat_id,
            "text": _truncate_msg(escaped_msg, 4000),
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
            "disable_notification": False,
        }
        telegram_api_url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(telegram_api_url, json=telegram_payload, timeout=30)
        if resp.status_code == 200:
            logging.info("✅ Telegram 推送成功。")
            return True
        try:
            error_msg = resp.json().get('description', '未知错误')
        except Exception:
            error_msg = resp.text or '无返回信息'
        logging.error(f"❌ Telegram 推送失败 (状态码{resp.status_code}): {error_msg}")
        if resp.status_code == 400 and "can't parse entities" in error_msg:
            logging.error("可能是消息格式问题，尝试使用纯文本格式...")
            telegram_payload["parse_mode"] = None
            telegram_payload["text"] = _truncate_msg(plain_msg, 4000)
            resp2 = requests.post(telegram_api_url, json=telegram_payload, timeout=30)
            if resp2.status_code == 200:
                logging.info("✅ Telegram 纯文本推送成功。")
                return True
        return False
    except Exception as e:
        logging.error(f"❌ Telegram 推送异常: {e}")
        return False


def _send_gotify(msg, cfg):
    gotify_url = str(cfg.get("GOTIFY_URL", "")).strip().rstrip("/")
    gotify_token = str(cfg.get("GOTIFY_TOKEN", "")).strip()
    if not gotify_url or not gotify_token:
        missing = []
        if not gotify_url:
            missing.append("URL")
        if not gotify_token:
            missing.append("Token")
        logging.info(f"未配置 Gotify {', '.join(missing)}，跳过该通道推送。")
        return False
    try:
        priority = int(float(str(cfg.get("GOTIFY_PRIORITY", "10") or "10")))
    except Exception:
        priority = 10
    payload = {
        "title": "DCF 推送",
        "message": _truncate_msg(msg, 6000),
        "priority": priority,
        "extras": {"client::display": {"contentType": "text/markdown"}},
    }
    try:
        resp = requests.post(
            f"{gotify_url}/message",
            params={"token": gotify_token},
            json=payload,
            timeout=15,
        )
        if 200 <= resp.status_code < 300:
            logging.info("✅ Gotify 推送成功。")
            return True
        logging.error(f"❌ Gotify 推送失败 (状态码{resp.status_code}): {resp.text[:300]}")
        return False
    except Exception as e:
        logging.error(f"❌ Gotify 推送异常: {e}")
        return False



def _encode_http_header_value(value):
    """Encode non-ASCII HTTP header values for ntfy/urllib/requests.

    HTTP header values are latin-1/ASCII at the client-library layer. ntfy supports
    RFC 2047 encoded headers, so Chinese titles must be encoded instead of sent raw.
    """
    text = str(value or "")
    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        return Header(text, "utf-8").encode()

def _send_ntfy(msg, cfg):
    ntfy_url = str(cfg.get("NTFY_URL", "")).strip().rstrip("/")
    topic = str(cfg.get("NTFY_TOPIC", "")).strip().strip("/")
    if not ntfy_url or not topic:
        missing = []
        if not ntfy_url:
            missing.append("URL")
        if not topic:
            missing.append("Topic")
        logging.info(f"未配置 ntfy {', '.join(missing)}，跳过该通道推送。")
        return False
    try:
        priority = int(float(str(cfg.get("NTFY_PRIORITY", "4") or "4")))
    except Exception:
        priority = 4
    priority = max(1, min(5, priority))
    headers = {
        "Title": _encode_http_header_value("DCF 推送"),
        "Priority": str(priority),
        "Markdown": "yes",
    }
    tags = str(cfg.get("NTFY_TAGS", "")).strip()
    if tags:
        headers["Tags"] = _encode_http_header_value(tags)
    auth = None
    username = str(cfg.get("NTFY_USERNAME", "")).strip()
    password = str(cfg.get("NTFY_PASSWORD", ""))
    if username:
        auth = (username, password)
    try:
        resp = requests.post(
            f"{ntfy_url}/{topic}",
            data=_truncate_msg(msg, 6000).encode("utf-8"),
            headers=headers,
            auth=auth,
            timeout=15,
        )
        if 200 <= resp.status_code < 300:
            logging.info("✅ ntfy 推送成功。")
            return True
        logging.error(f"❌ ntfy 推送失败 (状态码{resp.status_code}): {resp.text[:300]}")
        return False
    except Exception as e:
        logging.error(f"❌ ntfy 推送异常: {e}")
        return False


def send_notification(msg):
    cfg = load_push_config()
    enabled = str(cfg.get("PUSH_ENABLED", "yes")).strip().lower()
    channel = str(cfg.get("PUSH_CHANNEL", "gotify")).strip().lower()
    if enabled in {"no", "false", "0", "off"} or channel == "none":
        logging.info("推送已关闭，跳过通知。")
        append_push_log(channel, True, "推送已关闭，跳过通知")
        return True

    all_success = True
    results = []
    if channel == "telegram":
        ok = _send_telegram(msg, cfg)
        all_success = ok and all_success
        results.append(f"Telegram:{'成功' if ok else '失败'}")
    elif channel == "gotify":
        ok = _send_gotify(msg, cfg)
        all_success = ok and all_success
        results.append(f"Gotify:{'成功' if ok else '失败'}")
    elif channel == "ntfy":
        ok = _send_ntfy(msg, cfg)
        all_success = ok and all_success
        results.append(f"ntfy:{'成功' if ok else '失败'}")
    elif channel == "pushplus":
        ok = _send_pushplus(msg, cfg)
        all_success = ok and all_success
        results.append(f"PushPlus:{'成功' if ok else '失败'}")
    append_push_log(channel, all_success, "；".join(results) or "未执行任何推送通道")
    return all_success

# ===========================
# 辅助消息生成（与策略无关，保留在实盘中）
# ===========================
def build_status_message(name, symbol, now_str, zone, current_price, last_trade_price, last_trade_side,
                        current_units, current_avg_cost, ma150, ma150_source,
                        target_units, double_target, sell_price, clear_price,
                        position_mode="absolute", extra_info=""):
    if last_trade_side == "buy":
        side_label = "（b）"
    elif last_trade_side == "sell":
        side_label = "（s）"
    else:
        side_label = "（买）"
    last_trade_price_msg = f"{last_trade_price:.3f}{side_label}" if last_trade_price is not None else "无"
    msg = (
        f"🟢[INFO]【{name}】 ({symbol})\n"
        f"🕒时间: {now_str}\n"
        f"🍭区间: {zone}\n"
        f"💲当前: {current_price:.3f},上次:{last_trade_price_msg}\n"
        f"⚖️持仓: {format_units_for_display(current_units, position_mode)},成本: {current_avg_cost:.3f}, "
        f"目标: {format_units_for_display(target_units, position_mode)}, 上限: {format_units_for_display(double_target, position_mode)}\n"
        f"🔀MA150={ma150:.3f}({ma150_source}), sell={sell_price:.3f}, Clear={clear_price:.3f}"
    )
    if extra_info:
        msg += f"\n{extra_info}"
    return msg

def build_trade_message(name, symbol, now_str, zone, trade_action, trade_price, trade_qty,
                       last_trade_price, last_trade_side, position_after, avg_cost_after, ma150, ma150_source,
                       target_units, double_target, sell_price, clear_price,
                       position_mode="absolute", extra_info=""):
    if last_trade_side == "buy":
        side_label = "（B）"
    elif last_trade_side == "sell":
        side_label = "（S）"
    else:
        side_label = "（B）"
    last_trade_price_msg = f"{last_trade_price:.3f}{side_label}" if last_trade_price is not None else f"{trade_price:.3f}{side_label}"
    trade_qty_display = format_units_for_display(trade_qty, position_mode)
    msg = (
        f"🎯[TRADE]【{name}】 ({symbol})\n"
        f"🕒时间: {now_str}\n"
        f"🍭区间: {zone}\n"
        f"🗞交易: {trade_action} {trade_qty_display} @ {trade_price:.3f}\n"
        f"💲当前: {trade_price:.3f},上次: {last_trade_price_msg}\n"
        f"⚖️持仓: {format_units_for_display(position_after, position_mode)}, 成本: {avg_cost_after:.3f}, "
        f"目标: {format_units_for_display(target_units, position_mode)}, 上限: {format_units_for_display(double_target, position_mode)}\n"
        f"🔀MA150={ma150:.3f}({ma150_source}), sell={sell_price:.3f}, Clear={clear_price:.3f}"
    )
    if extra_info:
        msg += f"\n{extra_info}"
    return msg

def build_stop_message(name, symbol, now_str, zone, current_price, last_trade_price, last_trade_side, ma150, ma150_source, sell_price, clear_price):
    if last_trade_side == "buy":
        side_label = "（b）"
    elif last_trade_side == "sell":
        side_label = "（s）"
    else:
        side_label = "（买）"
    last_trade_price_msg = f"{last_trade_price:.3f}{side_label}" if last_trade_price is not None else f"{current_price:.3f}{side_label}"
    return (
        f"🎯[STOP]【{name}】 ({symbol})\n"
        f"🕒时间: {now_str}\n"
        f"🍭区间: {zone}\n"
        f"🛑操作: 停止所有交易\n"
        f"💲当前: {current_price:.3f},上次: {last_trade_price_msg}\n"
        f"🔀MA150={ma150:.3f}({ma150_source}), sell={sell_price:.3f}, Clear={clear_price:.3f}"
    )

def log_trade(dcf_name, symbol, price, qty, side, reason, zone=None,
              pos_before=None, pos_after=None,
              avg_cost_before=None, avg_cost_after=None,
              last_trade_price_before=None, last_trade_price_after=None,
              last_trade_side_before=None, last_trade_side_after=None,
              raw_price=None, restor_price=None,
              ma150=None, dividend=None, split_ratio=None,
              last_add_price_before=None):
    file_exists = os.path.isfile(TRADE_LOG_FILE)
    with open(TRADE_LOG_FILE, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "date",
                "dcf_name",
                "symbol",
                "action",
                "price",
                "qty",
                "avg_cost_after",
                "zone",
                "reason",
                "raw_price",
                "restor_price",
                "ma150",
                "last_trade_price_before",
                "last_add_price_before",
                "dividend",
                "split_ratio",
            ])
        now_str = strategy_now().strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow([
            now_str,
            dcf_name,
            symbol,
            side,
            f"{price:.3f}",
            serialize_numeric(qty),
            f"{avg_cost_after:.3f}" if avg_cost_after is not None else "",
            zone if zone is not None else "",
            reason,
            f"{raw_price:.4f}" if raw_price is not None else "",
            f"{restor_price:.4f}" if restor_price is not None else "",
            f"{ma150:.4f}" if ma150 is not None else "",
            f"{last_trade_price_before:.4f}" if last_trade_price_before is not None else "",
            f"{last_add_price_before:.4f}" if last_add_price_before is not None else "",
            f"{dividend:.6f}" if dividend is not None else "",
            f"{split_ratio:.6f}" if split_ratio is not None else "",
        ])

# ===========================
# 核心策略逻辑（调用 strategy 模块）
# ===========================
def strategy_for_dcf(name, cfg, state):
    symbol = cfg["symbol"]
    position_mode = get_position_mode(cfg)
    base_units = get_base_units(cfg)
    target_units = get_target_units(cfg)
    double_target = get_double_target(cfg)
    strategy_run = normalize_strategy_run_value(cfg.get("strategy_run", "on"), "on")
    dcf_state = state.setdefault(name, build_default_symbol_state(cfg))
    dcf_state = normalize_symbol_state(name, cfg, dcf_state)
    tick = dcf_state.get("tick", 0) + 1
    dcf_state["tick"] = tick
    last_trade_price = dcf_state.get("last_trade_price")
    last_trade_side = dcf_state.get("last_trade_side", "buy")
    last_known_price = dcf_state.get("last_price")
    current_units = normalize_position_amount(dcf_state.get("current_units", base_units), position_mode)
    current_avg_cost = dcf_state.get("avg_cost", 0.0)
    live_units = get_live_current_units(cfg)
    live_avg_cost = get_live_current_avg_cost(cfg)
    if live_units is not None and abs(live_units - current_units) > POSITION_EPSILON:
        current_units = live_units
        dcf_state["current_units"] = current_units
        if live_avg_cost is not None:
            current_avg_cost = live_avg_cost
            dcf_state["avg_cost"] = current_avg_cost
        elif current_units <= POSITION_EPSILON:
            current_avg_cost = 0.0
            dcf_state["avg_cost"] = 0.0
    elif live_avg_cost is not None and abs(live_avg_cost - current_avg_cost) > POSITION_EPSILON:
        current_avg_cost = live_avg_cost
        dcf_state["avg_cost"] = current_avg_cost
    price_scale = cfg.get("price_scale", 1.0)
    fetch_days = STRATEGY.get("fetch_history_days", 400)
    ma_short_len = STRATEGY.get("ma_period_short", 150)
    last_valid_price = dcf_state.get("last_valid_price") or last_known_price

    try:
        snapshot = get_market_snapshot(symbol, fetch_days, price_scale=price_scale)
    except Exception as e:
        reason = f"日K获取失败: {e}"
        msg = _build_market_data_error_message(
            name, symbol, reason, last_known_price=last_valid_price
        )
        logging.info(msg)
        _mark_market_error(dcf_state, msg, reason)
        return _maybe_market_alert(dcf_state, msg, reason)

    if not getattr(snapshot, "trade_allowed", True):
        reason = getattr(snapshot, "error", "行情来自缓存或观察源，本轮只监控不交易") or "行情来自缓存或观察源，本轮只监控不交易"
        msg = _build_market_data_error_message(
            name, symbol, reason, current_price=getattr(snapshot, "current_price", None),
            last_known_price=last_valid_price, closes_count=len(getattr(snapshot, "closes", []) or []),
            source=getattr(snapshot, "source", ""), last_bar_date=getattr(snapshot, "last_bar_date", None)
        )
        logging.warning(msg)
        _mark_market_error(dcf_state, msg, reason, getattr(snapshot, "source", ""))
        return _maybe_market_alert(dcf_state, msg, reason)

    closes = snapshot.closes
    current_price = snapshot.current_price
    if current_price <= 0:
        reason = "当前价为空或小于等于0"
        msg = _build_market_data_error_message(
            name, symbol, reason, current_price=current_price,
            last_known_price=last_valid_price, closes_count=len(closes),
            source=snapshot.source, last_bar_date=snapshot.last_bar_date
        )
        logging.info(msg)
        _mark_market_error(dcf_state, msg, reason, snapshot.source)
        return _maybe_market_alert(dcf_state, msg, reason)

    source_ok, source_reason = _check_market_source_switch(dcf_state, snapshot, cfg)
    if not source_ok:
        msg = _build_market_data_warn_message(
            name, symbol, source_reason, current_price=current_price,
            last_known_price=last_valid_price, closes_count=len(closes),
            source=snapshot.source, last_bar_date=snapshot.last_bar_date
        )
        logging.warning(msg)
        _mark_market_warn(dcf_state, msg, source_reason, snapshot.source)
        # 行情源切换首轮只监控不交易，也不更新 last_price / last_trade_price / last_add_price；WARN 不推送。
        return []
    elif source_reason:
        logging.warning(f"{name} {symbol}: {source_reason}，本轮继续执行行情质量检查。")

    max_jump = _safe_float(cfg.get("max_price_jump_ratio", STRATEGY.get("max_price_jump_ratio", 0.25)), 0.25)
    suspicious, jump_ratio = _is_price_jump_suspicious(current_price, last_valid_price, max_jump)
    if suspicious:
        reason = f"当前价相对上次有效价跳变过大，ratio={jump_ratio:.3f}，阈值={max_jump:.2f}"
        msg = _build_market_data_error_message(
            name, symbol, reason,
            current_price=current_price, last_known_price=last_valid_price, closes_count=len(closes),
            source=snapshot.source, last_bar_date=snapshot.last_bar_date
        )
        logging.warning(msg)
        _mark_market_error(dcf_state, msg, reason, snapshot.source)
        # 关键：异常行情不更新交易锚点，不触发交易。
        return _maybe_market_alert(dcf_state, msg, reason)

    if len(closes) >= 2:
        suspicious_prev, prev_ratio = _is_price_jump_suspicious(current_price, closes[-2], max(max_jump, 0.35))
        if suspicious_prev:
            reason = f"当前价相对上一根日K跳变过大，ratio={prev_ratio:.3f}"
            msg = _build_market_data_error_message(
                name, symbol, reason,
                current_price=current_price, last_known_price=last_valid_price, closes_count=len(closes),
                source=snapshot.source, last_bar_date=snapshot.last_bar_date
            )
            logging.warning(msg)
            _mark_market_error(dcf_state, msg, reason, snapshot.source)
            return _maybe_market_alert(dcf_state, msg, reason)

    if last_trade_price is None or last_trade_price <= 0:
        last_trade_price = current_price
        dcf_state["last_trade_price"] = current_price
        dcf_state["last_trade_side"] = "buy"

    ma150_raw, ma150_source = calc_ma_with_coef(closes, ma_short_len)
    if ma150_raw is None:
        reason = "历史数据严重不足，无法计算MA150"
        msg = _build_market_data_error_message(
            name, symbol, reason,
            current_price=current_price, last_known_price=last_valid_price, closes_count=len(closes),
            source=snapshot.source, last_bar_date=snapshot.last_bar_date
        )
        logging.info(msg)
        _mark_market_error(dcf_state, msg, reason, snapshot.source)
        return _maybe_market_alert(dcf_state, msg, reason)
    sideways_score = float(compute_sideways_index(closes, cfg))
    base_k150 = float(cfg.get("k150", 1.0))
    min_k150 = float(cfg.get("sideways_min_k150", 0.85))
    if base_k150 < min_k150:
        min_k150 = base_k150
    dynamic_k150 = min_k150 + (base_k150 - min_k150) * (1.0 - sideways_score)
    ma150 = ma150_raw * dynamic_k150
    dcf_state["ma_short"] = ma150
    dcf_state["k150"] = base_k150
    dcf_state["dynamic_k150"] = dynamic_k150
    dcf_state["sideways_score"] = sideways_score
    dcf_state["last_price"] = current_price
    _mark_market_ok(dcf_state, snapshot)
    dcf_state["current_units"] = current_units
    dcf_state["avg_cost"] = current_avg_cost
    dcf_state["ma_short_source"] = ma150_source
    dcf_state["position_mode"] = position_mode
    if current_units > 0 and current_avg_cost == 0:
        current_avg_cost = current_price
        dcf_state["avg_cost"] = current_avg_cost
    zone = get_zone(current_price, ma150, cfg)
    now_str = strategy_now().strftime("%Y.%m.%d.%H:%M")
    sell_price = ma150 * get_trend_multiple(cfg)
    clear_price = ma150 * get_sell_multiple(cfg)
    def _pct_text(value):
        return format_percent_ratio(value, digits=2)

    def _build_zone_extra_info():
        dynamic_info = f"⏳动态K={dynamic_k150:.3f}，横盘评分={sideways_score:.2f}"
        lines = []
        pyramid_steps_cfg = int(_safe_float(cfg.get("pyramid_steps", 0), 0))
        pyramid_weights = cfg.get("pyramid_weights", []) or []
        total_pyramid_steps = pyramid_steps_cfg if pyramid_steps_cfg > 0 else len(pyramid_weights)
        if pyramid_weights:
            total_pyramid_steps = min(total_pyramid_steps, len(pyramid_weights)) if total_pyramid_steps > 0 else len(pyramid_weights)

        if zone == "BOX_ZONE":
            grid_status = "已开启" if get_box_grid_enabled(cfg) else "未开启"
            grid_step = _safe_float(cfg.get("grid_box_percent", 0.0), 0.0)
            grid_units = _safe_float(cfg.get("grid_box_units_percent", 0.0), 0.0)
            if get_box_grid_enabled(cfg):
                lines.append(f"📦箱体网格: {grid_status}，步长{_pct_text(grid_step)}，单次{_pct_text(grid_units)}")
            else:
                lines.append(f"📦箱体网格: {grid_status}")
        elif zone == "CHANCE_ZONE":
            pyramid_mode = get_pyramid_add_enabled(cfg)
            if pyramid_mode == "yes":
                pyramid_status = "已开启"
            elif pyramid_mode == "auto":
                pyramid_status = "已触发" if bool(dcf_state.get("pyramid_active", False)) else "auto待触发"
            else:
                pyramid_status = "未开启"
            cur_step = int(dcf_state.get("pyramid_step", 0) or 0)
            step_pct = _safe_float(cfg.get("add_box_step", 0.05), 0.05)
            lines.append(f"🧱机会倒金字塔: {pyramid_status}，加仓{cur_step}/{total_pyramid_steps}步，步长{_pct_text(step_pct)}")
        elif zone == "TREND_ZONE":
            step_pct = _safe_float(cfg.get("trend_zone_step_percent", 0.01), 0.01)
            sell_pct = _safe_float(cfg.get("trend_zone_sell_percent", 0.05), 0.05)
            cur_step = 0
            if last_trade_price is not None and last_trade_price > 0 and step_pct > 0:
                cur_step = max(0, int((current_price - last_trade_price) / (last_trade_price * step_pct)))
            lines.append(f"📈趋势卖出: 步长{_pct_text(step_pct)}，当前{cur_step}步，单次目标{_pct_text(sell_pct)}")
        elif zone == "SELL_ZONE":
            pyramid_weights_for_sell = cfg.get("pyramid_weights", [0.03, 0.055, 0.08, 0.105, 0.13, 0.155, 0.18, 0.205, 0.23, 0.255])
            sell_plan = calculate_pyramid_sell_plan(target_units, pyramid_weights_for_sell, position_mode, 100)
            total_steps = len(sell_plan)
            cur_clear_step = int(dcf_state.get("clear_step", 0) or 0)
            target_clear_step = get_pyramid_sell_target_step(current_price, ma150, cfg, total_steps)
            clear_step_pct = _safe_float(cfg.get("clear_zone_step_percent", 0.08), 0.08)
            lines.append(f"🧹离场倒金字塔: 已卖{cur_clear_step}/{total_steps}步，目标{target_clear_step}步，步长{_pct_text(clear_step_pct)}")

        lines.append(dynamic_info)
        return "\n".join(lines)

    extra_info_full = _build_zone_extra_info()
    market_line = f"📡行情源: {snapshot.source}，数据状态: OK。"
    extra_info_full = (extra_info_full + "\n" if extra_info_full else "") + market_line
    status_suffix = f"\n🚦策略运行状态: {strategy_run.upper()}"
    status_msg = build_status_message(
        name=name, symbol=symbol, now_str=now_str, zone=zone,
        current_price=current_price, last_trade_price=last_trade_price, last_trade_side=last_trade_side,
        current_units=current_units, current_avg_cost=current_avg_cost,
        ma150=ma150, ma150_source=ma150_source,
        target_units=target_units, double_target=double_target,
        sell_price=sell_price, clear_price=clear_price,
        position_mode=position_mode, extra_info=extra_info_full
    )
    logging.info(status_msg + "\n")
    dcf_state["last_status_msg"] = status_msg
    if strategy_run == "off":
        return []
    messages = []
    raw_price = current_price
    restor_price = current_price
    dividend = 0.0
    split_ratio = 1.0
    # ========== 倒金字塔加仓相关状态 ==========
    pyramid_step = dcf_state.get("pyramid_step", 0)
    clear_step = dcf_state.get("clear_step", 0)
    last_add_price = dcf_state.get("last_add_price")
    if last_add_price is None or last_add_price <= 0:
        last_add_price = current_price
    pyramid_active = dcf_state.get("pyramid_active", False)
    target_reached_once = dcf_state.get("target_reached_once", False)

    state_dict = {
        "current_units": current_units,
        "last_trade_price": last_trade_price,
        "last_add_price": last_add_price,
        "pyramid_step": pyramid_step,
        "pyramid_active": pyramid_active,
        "target_reached_once": target_reached_once,
        "clear_step": clear_step,
    }

    # ========== 加仓决策（统一由 strategy.py 决定） ==========
    add_qty = 0.0
    add_reason = ""
    add_qty, add_reason, add_state, cfg_updates, add_events = get_add_trade_decision(
        state_dict, cfg, target_units, double_target, current_price, ma150, zone, position_mode, 100
    )
    if cfg_updates.get("pyramid_add_enabled") in {"yes", "auto"} and cfg.get("pyramid_add_enabled") != cfg_updates.get("pyramid_add_enabled"):
        cfg["pyramid_add_enabled"] = cfg_updates["pyramid_add_enabled"]
        persist_runtime_position_to_config(name, current_units, current_avg_cost)
    for _evt in add_events:
        if _evt == "PYRAMID_AUTO_TRIGGERED":
            logging.info(f"[{now_str}] 倒金字塔加仓已激活（价格跌破MA150）")
        elif _evt == "PYRAMID_SWITCH_TO_AUTO":
            logging.info(f"[{now_str}] 倒金字塔加仓已切回 auto 模式（进入趋势/离场区）")
    if add_qty > 0:
        new_avg_cost = calculate_new_avg_cost(current_units, current_avg_cost, add_qty, current_price)
        after_units = normalize_position_amount(current_units + add_qty, position_mode)
        log_trade(
            dcf_name=name, symbol=symbol, price=current_price, qty=add_qty, side="BUY",
            reason=add_reason, zone=zone,
            pos_before=current_units, pos_after=after_units,
            avg_cost_before=current_avg_cost, avg_cost_after=new_avg_cost,
            last_trade_price_before=last_trade_price, last_trade_price_after=current_price,
            last_trade_side_before=last_trade_side, last_trade_side_after="buy",
            raw_price=raw_price, restor_price=restor_price,
            ma150=ma150, dividend=dividend, split_ratio=split_ratio,
            last_add_price_before=last_add_price,
        )
        current_units = after_units
        current_avg_cost = new_avg_cost
        dcf_state["current_units"] = current_units
        dcf_state["avg_cost"] = current_avg_cost
        dcf_state["last_trade_price"] = current_price
        dcf_state["last_trade_side"] = "buy"
        dcf_state["pyramid_step"] = add_state.get("pyramid_step", pyramid_step)
        dcf_state["last_add_price"] = add_state.get("last_add_price", last_add_price)
        dcf_state["target_reached_once"] = add_state.get("target_reached_once", target_reached_once)
        dcf_state["pyramid_active"] = add_state.get("pyramid_active", pyramid_active)
        persist_runtime_position_to_config(name, current_units, current_avg_cost)
        extra_info = f"🏛{add_reason}: {format_units_for_display(add_qty, position_mode)}\n⏳动态K={dynamic_k150:.3f}，横盘评分={sideways_score:.2f}"
        trade_msg = build_trade_message(
            name=name, symbol=symbol, now_str=now_str, zone=zone,
            trade_action="买入", trade_price=current_price, trade_qty=add_qty,
            last_trade_price=last_trade_price, last_trade_side=last_trade_side,
            position_after=after_units, avg_cost_after=new_avg_cost,
            ma150=ma150, ma150_source=ma150_source,
            target_units=target_units, double_target=double_target,
            sell_price=sell_price, clear_price=clear_price,
            position_mode=position_mode, extra_info=extra_info
        )
        logging.info(trade_msg)
        messages.append(trade_msg)
    state_dict.update(add_state)
    pyramid_step = dcf_state.get("pyramid_step", pyramid_step)
    clear_step = dcf_state.get("clear_step", clear_step)
    last_add_price = dcf_state.get("last_add_price", last_add_price)

    # ========== 卖出决策 ==========
    if zone == "TREND_ZONE":
        sell_qty, new_state = get_trend_sell_decision(
            state_dict, cfg, target_units, position_mode, current_price, ma150, 100
        )
        if sell_qty > 0:
            after_units = normalize_position_amount(current_units - sell_qty, position_mode)
            log_trade(
                dcf_name=name, symbol=symbol, price=current_price, qty=sell_qty, side="SELL",
                reason="TREND_ZONE_SELL", zone="TREND_ZONE",
                pos_before=current_units, pos_after=after_units,
                avg_cost_before=current_avg_cost, avg_cost_after=current_avg_cost,
                last_trade_price_before=last_trade_price, last_trade_price_after=current_price,
                last_trade_side_before=last_trade_side, last_trade_side_after="sell",
                raw_price=raw_price, restor_price=restor_price,
                ma150=ma150, dividend=dividend, split_ratio=split_ratio,
                last_add_price_before=last_add_price,
            )
            current_units = after_units
            dcf_state["current_units"] = current_units
            dcf_state["last_trade_price"] = current_price
            dcf_state["last_trade_side"] = "sell"
            last_trade_price = new_state.get("last_trade_price", last_trade_price)
            persist_runtime_position_to_config(name, current_units, current_avg_cost)
            extra_info = f"🎯趋势区卖出机动仓: {format_units_for_display(sell_qty, position_mode)}\n⏳动态K={dynamic_k150:.3f}，横盘评分={sideways_score:.2f}"
            trade_msg = build_trade_message(
                name=name, symbol=symbol, now_str=now_str, zone="TREND_ZONE",
                trade_action="卖出", trade_price=current_price, trade_qty=sell_qty,
                last_trade_price=last_trade_price, last_trade_side=last_trade_side,
                position_after=after_units, avg_cost_after=current_avg_cost,
                ma150=ma150, ma150_source=ma150_source,
                target_units=target_units, double_target=double_target,
                sell_price=sell_price, clear_price=clear_price,
                position_mode=position_mode, extra_info=extra_info
            )
            logging.info(trade_msg)
            messages.append(trade_msg)
    elif zone == "SELL_ZONE":
        pyramid_weights = cfg.get("pyramid_weights", [0.03, 0.055, 0.08, 0.105, 0.13, 0.155, 0.18, 0.205, 0.23, 0.255])
        sell_plan = calculate_pyramid_sell_plan(target_units, pyramid_weights, position_mode, 100)
        total_steps = len(sell_plan)
        target_step = get_pyramid_sell_target_step(current_price, ma150, cfg, total_steps)
        if target_step > clear_step:
            for step_info in sell_plan[clear_step:target_step]:
                step = step_info["step"]
                sell_units = min(step_info["units"], current_units)
                if sell_units <= POSITION_EPSILON:
                    clear_step = step
                    continue
                after_units = normalize_position_amount(current_units - sell_units, position_mode)
                log_trade(
                    dcf_name=name, symbol=symbol, price=current_price, qty=sell_units, side="SELL",
                    reason=f"SELL_ZONE_PYRAMID_STEP_{step}", zone="SELL_ZONE",
                    pos_before=current_units, pos_after=after_units,
                    avg_cost_before=current_avg_cost, avg_cost_after=current_avg_cost,
                    last_trade_price_before=last_trade_price, last_trade_price_after=current_price,
                    last_trade_side_before=last_trade_side, last_trade_side_after="sell",
                    raw_price=raw_price, restor_price=restor_price,
                    ma150=ma150, dividend=dividend, split_ratio=split_ratio,
                    last_add_price_before=last_add_price,
                )
                current_units = after_units
                dcf_state["current_units"] = current_units
                dcf_state["last_trade_price"] = current_price
                dcf_state["last_trade_side"] = "sell"
                clear_step = step
                dcf_state["clear_step"] = clear_step
                persist_runtime_position_to_config(name, current_units, current_avg_cost)
                extra_info = f"🧹离场区倒金字塔卖出: 第{step}步 ({step_info['weight_percent']:.1f}%)\n{format_units_for_display(sell_units, position_mode)}\n⏳动态K={dynamic_k150:.3f}，横盘评分={sideways_score:.2f}"
                trade_msg = build_trade_message(
                    name=name, symbol=symbol, now_str=now_str, zone="SELL_ZONE",
                    trade_action="卖出", trade_price=current_price, trade_qty=sell_units,
                    last_trade_price=last_trade_price, last_trade_side=last_trade_side,
                    position_after=after_units, avg_cost_after=current_avg_cost,
                    ma150=ma150, ma150_source=ma150_source,
                    target_units=target_units, double_target=double_target,
                    sell_price=sell_price, clear_price=clear_price,
                    position_mode=position_mode, extra_info=extra_info
                )
                logging.info(trade_msg)
                messages.append(trade_msg)
                if current_units <= POSITION_EPSILON:
                    break

    # 保存状态
    dcf_state["pyramid_step"] = pyramid_step
    dcf_state["last_add_price"] = last_add_price
    dcf_state["pyramid_active"] = pyramid_active
    dcf_state["target_reached_once"] = target_reached_once
    dcf_state["clear_step"] = clear_step
    return messages

# ===========================
# 日志清理函数
# ===========================
def clean_old_dcf_logs(log_dir: str, keep: int = 7, filename_pattern: str = r"^dcf\.(\d{8})\.log$"):
    if keep <= 0:
        keep = 0
    if not os.path.isdir(log_dir):
        logging.warning(f"⚠️ 日志目录不存在，跳过清理: {log_dir}")
        return [], []
    rx = re.compile(filename_pattern)
    candidates = []
    for fn in os.listdir(log_dir):
        m = rx.match(fn)
        if not m:
            continue
        date_str = m.group(1)
        try:
            dt = datetime.strptime(date_str, "%Y%m%d")
        except ValueError:
            continue
        full_path = os.path.join(log_dir, fn)
        candidates.append((dt, full_path))
    candidates.sort(key=lambda x: x[0], reverse=True)
    kept = [p for _, p in candidates[:keep]]
    to_remove = [p for _, p in candidates[keep:]]
    removed = []
    for path in to_remove:
        try:
            os.remove(path)
            removed.append(path)
        except Exception as e:
            logging.error(f"❌ 删除旧日志失败: {path} | {e}")
    if candidates:
        logging.info(f"🧹 日志清理完成：共匹配 {len(candidates)} 份，保留 {len(kept)} 份，删除 {len(removed)} 份")
        for p in removed:
            logging.info(f"🗑️ 已删除旧日志: {os.path.basename(p)}")
    else:
        logging.info("🧹 日志清理：未发现符合 dcf.YYYYMMDD.log 格式的日志，无需清理")
    return kept, removed

# ===========================
# 主循环
# ===========================
def main_loop():
    logging.info("=" * 60)
    logging.info("DCF策略启动完成")
    logging.info(f"当前时间: {strategy_now().strftime('%Y-%m-%d %H:%M:%S')}")
    logging.info("=" * 60)
    config_path = os.path.join(BASE_DIR, "dcf.yaml")
    global ETF_CONFIG, STRATEGY, FULL_CONFIG
    ETF_CONFIG, STRATEGY_from_conf, FULL_CONFIG = load_config(config_path)
    STRATEGY.update(STRATEGY_from_conf)
    logging.info(f"策略时区: {get_strategy_timezone_name()}，服务器时区不影响 session_start/session_end/daily_push_time/log_rotate_time")
    logging.info("📌 各标的策略运行状态:")
    for name, cfg in ETF_CONFIG.items():
        if not isinstance(cfg, dict):
            logging.error(f"配置项 {name} 不是字典，类型为 {type(cfg)}，已跳过。请检查 dcf.yaml 格式。")
            continue
        strategy_run = normalize_strategy_run_value(cfg.get("strategy_run", "on"), "on")
        status_icon = "🟢" if strategy_run == "on" else "🔴"
        logging.info(f" {status_icon} {name}: {strategy_run.upper()}")
    logging.info("=" * 60)
    state = load_state()
    if "_meta" not in state:
        state["_meta"] = {
            "last_daily_push_date": None,
            "last_log_rotate_date": None,
        }
    last_config_reload_seq = _safe_int(state.get("_meta", {}).get("config_reload_seq", 0), 0)
    while True:
        ETF_CONFIG, STRATEGY_from_conf, FULL_CONFIG = load_config(config_path)
        STRATEGY.update(STRATEGY_from_conf)
        last_config_reload_seq = apply_runtime_config_reload_if_needed(state, last_config_reload_seq)
        now = strategy_now()
        # 日志轮转
        if should_rotate_logs(state, now):
            logging.info("=" * 60)
            logging.info(f"🔄 开始执行日志轮转 - {now.strftime('%Y-%m-%d %H:%M:%S')}")
            logging.info("=" * 60)
            rotate_success = rotate_and_backup_logs(now)
            if rotate_success:
                log_dir = os.path.join(BASE_DIR, "log")
                try:
                    clean_old_dcf_logs(log_dir=log_dir, keep=7)
                except Exception as e:
                    logging.exception(f"❌ 执行日志清理函数失败: {e}")
                snapshot = build_daily_snapshot(state)
                logging.info("=" * 60)
                logging.info("📌每日快照内容:")
                logging.info("=" * 60)
                logging.info(snapshot)
                logging.info("=" * 60)
                try:
                    send_notification(snapshot)
                    logging.info("✅ 每日快照推送成功")
                except Exception as e:
                    logging.error(f"❌ 推送每日快照失败: {e}")
                state["_meta"]["last_log_rotate_date"] = now.date().isoformat()
                state["_meta"]["last_daily_push_date"] = now.date().isoformat()
                save_state(state)
                logging.info("=" * 60)
                logging.info("✅ 日志轮转与快照推送完成")
                logging.info("🕒 下次轮转时间: 明天 09:00")
                logging.info("=" * 60)
        # 非交易时段
        if not in_trade_session(now):
            time_module.sleep(STRATEGY.get("loop_interval", 60))
            continue
        # 策略执行
        all_trade_msgs = []
        all_market_error_msgs = []
        for name, cfg in ETF_CONFIG.items():
            if not isinstance(cfg, dict):
                logging.error(f"配置项 {name} 不是字典，类型为 {type(cfg)}，已跳过。")
                continue
            try:
                msgs = strategy_for_dcf(name, cfg, state)
                for msg in msgs or []:
                    text = str(msg)
                    if "🎯[TRADE]" in text:
                        all_trade_msgs.append(text)
                    elif "🎯[ERROR]" in text:
                        all_market_error_msgs.append(text)
                    else:
                        logging.info(f"非交易消息未推送: {text[:160]}")
            except Exception as e:
                logging.exception(f"{name} 策略执行出错: {e}")
        save_state(state)
        # 推送交易信号：只有真正 TRADE 才进入买卖推送
        if all_trade_msgs:
            body = (chr(10) * 2).join(all_trade_msgs)
            logging.info("=" * 60)
            logging.info("📨 推送买卖信号:")
            logging.info("=" * 60)
            logging.info(body)
            try:
                send_notification(body)
                logging.info("✅ 买卖信号推送成功")
            except Exception:
                logging.exception("❌ 推送买卖信号失败")
            logging.info("=" * 60)
        # 推送行情错误：仅全部数据源失败且已按当天去重后的 ERROR 会到这里
        if all_market_error_msgs:
            body = (chr(10) * 2).join(all_market_error_msgs)
            logging.info("=" * 60)
            logging.info("📨 推送行情错误:")
            logging.info("=" * 60)
            logging.info(body)
            try:
                send_notification(body)
                logging.info("✅ 行情错误推送成功")
            except Exception:
                logging.exception("❌ 推送行情错误失败")
            logging.info("=" * 60)
        time_module.sleep(STRATEGY.get("loop_interval", 60))

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        logging.info("用户中断程序执行")
    except Exception as e:
        logging.exception(f"程序异常退出: {e}")