#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import ast
import json
import re
import secrets
import subprocess
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from typing import Any, Dict, List, Tuple

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from ruamel.yaml import YAML
from ruamel.yaml.constructor import DuplicateKeyError
import yaml as pyyaml
from werkzeug.security import check_password_hash

from push import (
    PUSH_CONFIG_FILE,
    PUSH_LOG_FILE,
    PUSH_LOG_KEEP_LINES,
    PUSH_DEFAULTS,
    PUSH_CHANNEL_VALUES,
    load_push_config as read_push_config,
    write_push_config,
    read_push_logs,
    send_push_test,
)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "dcf.yaml"
STATE_FILE = BASE_DIR / "dcf_monitor_state.json"
WEB_CONFIG_FILE = BASE_DIR / "web_portal.json"
BACKTEST_FILE = BASE_DIR / "backtest_dcf.py"
TRADE_LOG_FILE = BASE_DIR / "trade_log.csv"
BACKTEST_OUT_DIR = BASE_DIR / "backtest_out"
SNAPSHOT_DIR = BASE_DIR / "data" / "snapshots"
STATE_BACKUP_DIR = BASE_DIR / "data" / "state_backups"
STATE_BACKUP_INDEX = STATE_BACKUP_DIR / "index.json"

yaml = YAML()
yaml.preserve_quotes = True
yaml.indent(mapping=2, sequence=4, offset=2)
yaml.width = 4096

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "web_templates"),
    static_folder=str(BASE_DIR / "web_static"),
)

APP_DISPLAY_NAME = "闲云量化"

PAGE_TITLES = {
    "symbols": "标的",
    "status": "状态",
    "params": "参数",
    "backtest": "回测",
    "push": "推送",
}

PARAM_HELP: Dict[str, str] = {
    "k150": "MA150 的动态倍率基准。值越大，MA150 上沿越宽，越不容易进入高估/趋势卖出区。",
    "sideways_window_30": "用最近多少天的 MA30 变化来评估横盘程度。窗口越小越灵敏，越大越平滑。",
    "sideways_window_60": "用最近多少天的 MA60 变化来评估横盘程度。窗口越小越灵敏，越大越平滑。",
    "sideways_weight_60": "横盘评分中，MA60 所占权重。越大越偏向中期横盘判断。",
    "sideways_min_k150": "横盘评分很高时，动态 K150 最低可压到的值。数值越小，箱体上沿越容易下移。",
    "trend_multiple": "箱体区上沿倍数 = MA150 * trend_multiple，超过后进入趋势区。",
    "sell_multiple": "离场区触发倍数 = MA150 * sell_multiple，超过后开始倒金字塔卖出底仓。",
    "add_box_step": "倒金字塔加仓步长。进入机会区并补到目标仓位后，价格每相对 last_add_price 下跌该比例，触发下一步加仓。",
    "add_box_units_percent": "旧版箱体区固定加仓比例，最新策略不再使用 BOX 区独立回补。",
    "trend_zone_step_percent": "趋势区卖出步长。价格相对 last_trade_price 上涨达到该比例时，才检查是否卖出超出目标仓位的部分。",
    "trend_zone_sell_percent": "趋势区单次卖出比例，按目标仓位计算，并且只卖出高于目标仓位的机动仓。",
    "clear_zone_step_percent": "离场区倒金字塔卖出步长。价格相对 sell_multiple 每上移该比例，推进一个清仓步数。",
    "grid_box_percent": "箱体区网格交易步长。当前回测策略不使用该参数，主要保留给实盘/后续网格逻辑。",
    "grid_box_units_percent": "箱体区网格交易比例。当前回测策略不使用该参数，主要保留给实盘/后续网格逻辑。",
    "box_grid_enabled": "箱体区网格开关。当前回测策略不使用该参数，状态栏可用于展示配置。",
    "pyramid_steps": "倒金字塔加仓/离场步数上限，实际步数不会超过 pyramid_weights 长度。",
    "pyramid_weights": "倒金字塔每步权重。机会区加仓按目标仓位乘以对应权重；离场区卖出也按目标仓位拆分。",
    "pyramid_add_enabled": "倒金字塔加仓开关。auto=等待首次进入机会区后自动切到 yes；yes=机会区先补到目标仓位，再按步长和权重加仓。进入趋势区或离场区会自动切回 auto 并重置加仓步数。",
}

BACKTEST_HELP_TEXT = """1) 价格口径：信号和区间使用 Adj Close；成交、估值、持仓成本使用 Close；分红按除息日现金入账，拆股按除权日调整仓位和成本。
2) 区间划分：CHANCE=价格<MA150；BOX=MA150~MA150*trend_multiple；TREND=MA150*trend_multiple~MA150*sell_multiple；SELL=价格≥MA150*sell_multiple。
3) 倒金字塔加仓：历史回测每次都从 pyramid_add_enabled=auto 起步，忽略 dcf.yaml 中实盘监控用的 yes；只有首次进入 CHANCE_ZONE 后才自动切到 yes。
4) 箱体区规则：回测起步在 BOX_ZONE 时不会因实盘 yes 直接补仓；经历趋势区/离场区卖出后，回到 BOX_ZONE 也不直接回补。只有已由 CHANCE_ZONE 激活的倒金字塔模式，才可在 CHANCE/BOX 中继续按步长加仓。
5) 趋势/离场卖出：TREND_ZONE 只卖出高于目标仓位的机动仓；SELL_ZONE 按 clear_zone_step_percent 推进倒金字塔卖出步数。
6) 回测成本：历史初始持仓成本使用回测窗口第一天 Close；current_avg_cost 仅用于实盘监控，不参与回测成本初始化。
7) 收益口径：期末持仓收益率使用“最新价格 / 摊薄后持仓成本 - 1”；摊薄后持仓成本只扣当前持仓周期内的分红现金贡献和已实现交易收益贡献。综合收益率使用累计投入口径。
8) 百分比模式下，qty 表示仓位比例；交易日志保留上一次成交价和上一次加仓价。"""

BACKTEST_METRICS_HELP_TEXT = """期末持仓收益率：按股票软件常见摊薄成本口径计算，只看期末仍持有仓位，公式为 最新价格 / 摊薄后持仓成本 - 1。摊薄后持仓成本只用“账户收益贡献”扣减成本，即当前持仓周期内的分红现金贡献 + 已实现交易收益贡献；不会使用已经除以累计投入后的分红收益率或交易实现收益率，避免重复缩放。若仓位曾完全清空，之前已结束持仓周期的收益不会继续摊到后面新建仓位。

综合收益率：按累计投入口径计算，公式为 (分红收益贡献 + 交易实现收益贡献 + 期末持仓浮盈贡献) / 累计投入仓位。累计投入包括回测起始底仓和回测期间所有买入过的仓位，因此它不是单纯的期末持仓盈亏率，也不是账户总收益贡献。换手越多，累计投入越大，综合收益率会被摊薄。"""


PUSH_FIELDS: List[Dict[str, str]] = [
    {"key": "PUSH_ENABLED", "label": "启用推送", "type": "switch", "channel": "base", "help": "拨动后立即写入 /root/dcf/push.conf 并生效。"},
    {"key": "PUSH_CHANNEL", "label": "推送通道", "type": "select", "channel": "base", "help": "切换后页面会自动显示对应通道的配置项。"},
    {"key": "TELEGRAM_BOT_TOKEN", "label": "Telegram Bot Token", "type": "password", "channel": "telegram", "help": "BotFather 生成的 bot token。"},
    {"key": "TELEGRAM_CHAT_ID", "label": "Telegram Chat ID", "type": "text", "channel": "telegram", "help": "个人、群组或频道的 chat_id。"},
    {"key": "GOTIFY_URL", "label": "Gotify 服务地址", "type": "text", "channel": "gotify", "help": "本机 Gotify 默认：https://sharq.eu.org:2084，保存时会自动去掉末尾多余空格。"},
    {"key": "GOTIFY_TOKEN", "label": "Gotify Application Token", "type": "password", "channel": "gotify", "help": "Gotify 网页端 Applications 里创建应用后复制的 token。"},
    {"key": "GOTIFY_PRIORITY", "label": "Gotify 优先级", "type": "number", "channel": "gotify", "help": "默认 10。数值越高优先级越高。"},
    {"key": "NTFY_URL", "label": "ntfy 服务地址", "type": "text", "channel": "ntfy", "help": "同一台服务器本机调用推荐：http://127.0.0.1:8083；外部访问默认：https://sharq.eu.org:2085。"},
    {"key": "NTFY_TOPIC", "label": "ntfy Topic", "type": "text", "channel": "ntfy", "help": "ntfy 的 Topic 相当于频道名，客户端订阅同一个 Topic 才能收到推送。"},
    {"key": "NTFY_USERNAME", "label": "ntfy 用户名", "type": "text", "channel": "ntfy", "help": "如果 ntfy 开启登录认证，请填写用户名；未开启认证可留空。"},
    {"key": "NTFY_PASSWORD", "label": "ntfy 密码", "type": "password", "channel": "ntfy", "help": "如果 ntfy 开启登录认证，请填写密码；未开启认证可留空。"},
    {"key": "NTFY_PRIORITY", "label": "ntfy 优先级", "type": "number", "channel": "ntfy", "help": "ntfy 优先级范围 1-5，默认 4。"},
    {"key": "NTFY_TAGS", "label": "ntfy Tags", "type": "text", "channel": "ntfy", "help": "逗号分隔，例如 dcf,chart_with_upwards_trend。"},
    {"key": "PUSHPLUS_TOKEN", "label": "PushPlus Token", "type": "password", "channel": "pushplus", "help": "PushPlus 官网获取的 token。"},
]

PUSH_SELECT_OPTIONS = {
    "PUSH_CHANNEL": [
        ("telegram", "Telegram"),
        ("gotify", "Gotify"),
        ("ntfy", "ntfy"),
        ("pushplus", "PushPlus"),
        ("none", "none"),
    ],
}


FIELD_GROUPS: List[Dict[str, Any]] = [
    {
        "id": "basic",
        "title": "基础信息",
        "items": [
            ("symbol", "代码", "text"),
            ("price_scale", "价格缩放", "number"),
            ("strategy_run", "运行状态", "select_yes_no"),
        ],
    },
    {
        "id": "position",
        "title": "仓位参数",
        "items": [
            ("base_units", "初始仓位", "text"),
            ("target_units", "目标仓位", "text"),
            ("double_target_factor", "仓位上限倍数", "number"),
            ("current_units", "当前持仓", "text"),
            ("current_avg_cost", "当前成本", "number"),
        ],
    },
    {
        "id": "sideways",
        "title": "均线与横盘",
        "items": [
            ("k150", "MA150系数", "number"),
            ("sideways_window_30", "横盘MA30天数", "number"),
            ("sideways_window_60", "横盘MA60天数", "number"),
            ("sideways_weight_60", "横盘MA60权重", "number"),
            ("sideways_min_k150", "动态MA150最小值", "number"),
        ],
    },
    {
        "id": "zones",
        "title": "区间界限",
        "items": [
            ("trend_multiple", "箱体区上沿倍数", "number"),
            ("sell_multiple", "离场区触发倍数", "number"),
        ],
    },
    {
        "id": "box_add",
        "title": "箱体区加仓",
        "items": [
            ("add_box_step", "加仓步长", "number"),
            ("add_box_units_percent", "每次加仓比例", "number"),
        ],
    },
    {
        "id": "box_grid",
        "title": "箱体区网格（底仓已满）",
        "items": [
            ("box_grid_enabled", "开启网格交易", "select_yes_no"),
            ("grid_box_percent", "网格步长", "number"),
            ("grid_box_units_percent", "网格交易比例", "number"),
        ],
    },
    {
        "id": "trend_zone",
        "title": "趋势区减仓",
        "items": [
            ("trend_zone_step_percent", "减仓步长", "number"),
            ("trend_zone_sell_percent", "每次减仓比例", "number"),
        ],
    },
    {
        "id": "clear_zone",
        "title": "离场区减仓（倒金字塔）",
        "items": [
            ("clear_zone_step_percent", "减仓步长", "number"),
            ("pyramid_steps", "倒金字塔步数", "number"),
            ("pyramid_weights", "倒金字塔权重", "text"),
        ],
    },
    {
        "id": "pyramid_add",
        "title": "倒金字塔加仓（机会区+箱体区）",
        "items": [
            ("pyramid_add_enabled", "倒金字塔加仓开关", "select"),
            ("add_box_step", "加仓步长", "number"),
            ("pyramid_weights", "倒金字塔权重", "text"),
            ("pyramid_steps", "倒金字塔步数", "number"),
        ],
    },
]

SELECT_FIELD_OPTIONS = {
    "strategy_run": [("on", "on"), ("off", "off")],
    "box_grid_enabled": [("no", "no"), ("yes", "yes")],
    "pyramid_add_enabled": [("auto", "auto"), ("yes", "yes")],
}

SUMMARY_KEYS = [
    "标的", "模式", "K线数量",
    "期末持仓收益率", "综合收益率",
    "分红收益率", "交易实现收益率", "持仓收益率",
    "最新价格", "首次建仓原始价", "首次建仓复权价", "首次建仓日",
    "最大回撤", "最大回撤(参考)",
    "期末持仓", "摊薄后持仓成本", "期末持仓成本", "期末市值权重", "期末市值",
    "期末现金权重", "期末现金", "期末持仓市值",
    "结束总权益(参考)", "结束价值(参考)",
]

def default_web_config() -> Dict[str, Any]:
    return {
        "app_name": APP_DISPLAY_NAME,
        "admin_username": "admin",
        "password_hash": "",
        "secret_key": secrets.token_hex(32),
        "domain": "sharq.eu.org",
        "public_port": 819,
        "internal_port": 1819,
    }

def load_web_config() -> Dict[str, Any]:
    if not WEB_CONFIG_FILE.exists():
        cfg = default_web_config()
        WEB_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return cfg
    raw = WEB_CONFIG_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        cfg = default_web_config()
        WEB_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return cfg
    try:
        return json.loads(raw)
    except Exception:
        try:
            cfg = ast.literal_eval(raw)
            if isinstance(cfg, dict):
                merged = default_web_config()
                merged.update(cfg)
                WEB_CONFIG_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
                return merged
        except Exception:
            pass
    cfg = default_web_config()
    WEB_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg

def init_app_secret() -> None:
    cfg = load_web_config()
    app.secret_key = cfg.get("secret_key") or secrets.token_hex(32)

init_app_secret()


DCF_NAV_STYLE = """
<style id="dcf-responsive-nav-style">
.nav {
  display: grid !important;
  grid-template-columns: repeat(5, minmax(0, 1fr)) !important;
  gap: 10px !important;
  align-items: stretch !important;
}
.nav a {
  min-height: 42px !important;
  padding: 10px 12px !important;
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  text-align: center !important;
  white-space: nowrap !important;
}
@media (max-width: 640px) {
  .nav {
    display: flex !important;
    flex-wrap: nowrap !important;
    overflow-x: auto !important;
    -webkit-overflow-scrolling: touch !important;
    padding-bottom: 4px !important;
    scrollbar-width: none !important;
  }
  .nav::-webkit-scrollbar { display: none !important; }
  .nav a {
    flex: 0 0 auto !important;
    min-width: 68px !important;
    padding: 9px 12px !important;
  }
}
</style>
"""

@app.after_request
def inject_responsive_nav_style(response):
    """Ensure the 5 main navigation buttons stay in one row on desktop and work well on mobile."""
    try:
        content_type = response.headers.get("Content-Type", "")
        if "text/html" not in content_type.lower():
            return response
        html = response.get_data(as_text=True)
        if "dcf-responsive-nav-style" in html or "</head>" not in html:
            return response
        html = html.replace("</head>", DCF_NAV_STYLE + "\n</head>", 1)
        response.set_data(html)
        response.headers["Content-Length"] = str(len(response.get_data()))
    except Exception:
        return response
    return response

def normalize_strategy_run_value(value: Any, default: str = "on") -> str:
    """运行开关只允许 on/off；无效或缺失值按 default 处理，默认 on。"""
    s = str(value if value is not None else "").strip().lower()
    if s in {"on", "off"}:
        return s
    return default
def normalize_config(data: Dict[str, Any]) -> bool:
    changed = False
    common = data.get("COMMON_BACKTEST_CONFIG")
    if isinstance(common, dict):
        # 移除废弃字段（MA300相关及旧字段）
        for k in ["k300", "ma300_min_coef", "pyramid_enabled", "fee_rate", "slippage_bp", "stop_add_above_percent",
                  "core_zone_upper_multiple", "core_sell_start_multiple", "core_sell_step_percent",
                  "sell_percent", "sell_trigger_up_percent"]:
            if k in common:
                common.pop(k, None)
                changed = True
        new_run = normalize_strategy_run_value(common.get("strategy_run", "on"), "on")
        if common.get("strategy_run") != new_run:
            common["strategy_run"] = new_run
            changed = True
        if common.get("box_grid_enabled") not in {"yes", "no"}:
            common["box_grid_enabled"] = "no"
            changed = True
        if common.get("pyramid_add_enabled") not in {"yes", "auto"}:
            common["pyramid_add_enabled"] = "auto"
            changed = True
        # 确保新字段有默认值
        common.setdefault("trend_multiple", 1.2)
        common.setdefault("sell_multiple", 1.5)
        common.setdefault("add_box_step", 0.05)
        common.setdefault("add_box_units_percent", 0.1)
        common.setdefault("trend_zone_step_percent", 0.01)
        common.setdefault("trend_zone_sell_percent", 0.05)
        common.setdefault("clear_zone_step_percent", 0.08)
        common.setdefault("pyramid_weights", [0.03, 0.055, 0.08, 0.105, 0.13, 0.155, 0.18, 0.205, 0.23, 0.255])
        common.setdefault("pyramid_steps", 10)
        common.setdefault("pyramid_add_enabled", "auto")

    etf_cfg = data.get("ETF_CONFIG")
    if isinstance(etf_cfg, dict):
        for _, section in etf_cfg.items():
            if not isinstance(section, dict):
                continue
            for k in ["k300", "ma300_min_coef", "pyramid_enabled", "fee_rate", "slippage_bp", "stop_add_above_percent",
                      "core_zone_upper_multiple", "core_sell_start_multiple", "core_sell_step_percent",
                      "sell_percent", "sell_trigger_up_percent"]:
                if k in section:
                    section.pop(k, None)
                    changed = True
            new_run = normalize_strategy_run_value(section.get("strategy_run", "on"), "on")
            if section.get("strategy_run") != new_run:
                section["strategy_run"] = new_run
                changed = True
            if section.get("box_grid_enabled") not in {"yes", "no"}:
                section["box_grid_enabled"] = "no"
                changed = True
            if section.get("pyramid_add_enabled") not in {"yes", "auto"}:
                section["pyramid_add_enabled"] = "auto"
                changed = True
            section.setdefault("trend_multiple", 1.2)
            section.setdefault("sell_multiple", 1.5)
            section.setdefault("add_box_step", 0.05)
            section.setdefault("add_box_units_percent", 0.1)
            section.setdefault("trend_zone_step_percent", 0.01)
            section.setdefault("trend_zone_sell_percent", 0.05)
            section.setdefault("clear_zone_step_percent", 0.08)
            section.setdefault("pyramid_weights", [0.03, 0.055, 0.08, 0.105, 0.13, 0.155, 0.18, 0.205, 0.23, 0.255])
            section.setdefault("pyramid_steps", 10)
            section.setdefault("pyramid_add_enabled", "auto")
    return changed

def read_yaml() -> Dict[str, Any]:
    if not CONFIG_FILE.exists():
        return {}
    raw = CONFIG_FILE.read_text(encoding="utf-8")
    try:
        data = yaml.load(raw) or {}
    except DuplicateKeyError:
        data = pyyaml.safe_load(raw) or {}
        if isinstance(data, dict):
            write_yaml(data)
    if isinstance(data, dict) and normalize_config(data):
        write_yaml(data)
    return data

def write_yaml(data: Dict[str, Any]) -> None:
    normalize_config(data)
    with CONFIG_FILE.open("w", encoding="utf-8") as f:
        yaml.dump(data, f)

def read_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def snapshot_dates(limit: int = 30) -> List[str]:
    if not SNAPSHOT_DIR.exists():
        return []
    dates = []
    for p in SNAPSHOT_DIR.glob("*.jsonl"):
        if re.match(r"^\d{4}-\d{2}-\d{2}$", p.stem):
            dates.append(p.stem)
    return sorted(dates, reverse=True)[:limit]


def read_snapshot_records(day: str = "", limit: int = 2000) -> List[Dict[str, Any]]:
    day = (day or datetime.now().strftime("%Y-%m-%d")).strip()
    path = SNAPSHOT_DIR / f"{day}.jsonl"
    if not path.exists():
        return []
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]
        if limit and len(lines) > limit:
            lines = lines[-limit:]
        out = []
        for line in lines:
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    out.append(item)
            except Exception:
                continue
        return out
    except Exception:
        return []


def _snapshot_matches(record: Dict[str, Any], selected: str, symbol_code: str) -> bool:
    if not isinstance(record, dict):
        return False
    if selected == "COMMON_BACKTEST_CONFIG":
        return True
    name = str(record.get("name", "")).strip()
    symbol = str(record.get("symbol", "")).strip().upper()
    return name == selected or (symbol_code and symbol == symbol_code.upper())


def get_strategy_snapshots(selected: str, symbol_code: str, day: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    records = [r for r in read_snapshot_records(day, limit=5000) if _snapshot_matches(r, selected, symbol_code)]
    recent = list(reversed(records[-10:]))
    latest = recent[0] if recent else {}
    return latest, recent


def build_market_source_stats(selected: str, symbol_code: str, day: str) -> List[Dict[str, Any]]:
    records = [r for r in read_snapshot_records(day, limit=100000) if _snapshot_matches(r, selected, symbol_code)]
    stats: Dict[str, Dict[str, Any]] = {}
    for r in records:
        source = str(r.get("market_source") or r.get("source") or "unknown").strip() or "unknown"
        row = stats.setdefault(source, {"source": source, "total": 0, "ok": 0, "warn": 0, "error": 0, "skip": 0})
        row["total"] += 1
        status = str(r.get("market_status") or "").strip().lower()
        level = str(r.get("level") or "").strip().upper()
        action = str(r.get("action") or r.get("decision") or "").strip().upper()
        if status == "ok" or level == "INFO":
            row["ok"] += 1
        elif status == "warn" or level == "WARN":
            row["warn"] += 1
        elif status == "error" or level == "ERROR":
            row["error"] += 1
        if action in {"SKIP_TRADE", "MONITOR_ONLY"}:
            row["skip"] += 1
    result = []
    for row in stats.values():
        total = row["total"] or 1
        row = dict(row)
        row["success_rate"] = f"{row['ok'] / total * 100:.1f}%"
        result.append(row)
    return sorted(result, key=lambda x: (-x["total"], x["source"]))




def read_trade_state_backup_index() -> List[Dict[str, Any]]:
    if not STATE_BACKUP_INDEX.exists():
        return []
    try:
        data = json.loads(STATE_BACKUP_INDEX.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def get_trade_state_backups(selected: str, symbol_code: str, limit: int = 10) -> List[Dict[str, Any]]:
    if selected == "COMMON_BACKTEST_CONFIG":
        return []
    symbol_code = (symbol_code or "").strip().upper()
    rows = []
    for item in read_trade_state_backup_index():
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip().upper()
        name = str(item.get("name", "")).strip()
        if name != selected and (not symbol_code or symbol != symbol_code):
            continue
        rel = str(item.get("file", "")).strip()
        if rel:
            try:
                path = (BASE_DIR / rel).resolve()
                if not str(path).startswith(str(STATE_BACKUP_DIR.resolve())) or not path.exists():
                    continue
            except Exception:
                continue
        rows.append(item)
    rows.sort(key=lambda x: str(x.get("time", "")), reverse=True)
    return rows[:limit]


def _load_trade_state_backup(backup_id: str, selected: str, symbol_code: str) -> Dict[str, Any]:
    backup_id = (backup_id or "").strip()
    symbol_code = (symbol_code or "").strip().upper()
    if not backup_id:
        raise ValueError("缺少回滚点 ID")
    for item in read_trade_state_backup_index():
        if str(item.get("id", "")) != backup_id:
            continue
        name = str(item.get("name", "")).strip()
        symbol = str(item.get("symbol", "")).strip().upper()
        if name != selected and (not symbol_code or symbol != symbol_code):
            raise ValueError("回滚点不属于当前标的")
        rel = str(item.get("file", "")).strip()
        path = (BASE_DIR / rel).resolve()
        if not str(path).startswith(str(STATE_BACKUP_DIR.resolve())):
            raise ValueError("回滚点路径非法")
        if not path.exists():
            raise ValueError("回滚点文件不存在")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("state_before"), dict):
            raise ValueError("回滚点内容无效")
        return data
    raise ValueError("未找到回滚点")


def _format_units_for_config(value: Any, mode: str) -> Any:
    val = safe_float(value, 0.0)
    if mode == "percent":
        pct = val * 100.0
        text = f"{pct:.6f}".rstrip("0").rstrip(".")
        return f"{text or '0'}%"
    try:
        return int(round(val))
    except Exception:
        return val


def request_runtime_state_restore(selected: str, backup_id: str, state_entry: Dict[str, Any]) -> None:
    state = read_state()
    if not isinstance(state, dict):
        state = {}
    meta = state.setdefault("_meta", {})
    try:
        seq = int(meta.get("config_reload_seq", 0) or 0) + 1
    except Exception:
        seq = 1
    meta["config_reload_seq"] = seq
    meta["config_reload_requested_at"] = current_time_text()
    for _k in ["state_restore_kind", "state_restore_symbol_key", "state_restore_backup_id", "state_restore_entry"]:
        meta.pop(_k, None)
    meta["config_reload_symbol_key"] = selected
    meta["config_reload_symbols"] = [selected]
    meta["state_restore_kind"] = "trade_backup"
    meta["state_restore_symbol_key"] = selected
    meta["state_restore_backup_id"] = backup_id
    meta["state_restore_entry"] = state_entry
    state[selected] = state_entry
    write_state(state)


def restore_trade_state_backup(backup_id: str, selected: str) -> str:
    if selected == "COMMON_BACKTEST_CONFIG":
        raise ValueError("通用回测参数没有交易状态可回滚")
    config = read_yaml()
    section = get_section(config, selected)
    if not section:
        raise ValueError("未找到当前标的配置")
    symbol_code = str(section.get("symbol", "")).strip().upper()
    backup = _load_trade_state_backup(backup_id, selected, symbol_code)
    state_entry = deepcopy(backup.get("state_before") or {})
    mode = str(backup.get("position_mode") or get_position_mode_from_section(section)).strip() or get_position_mode_from_section(section)
    restored_units = state_entry.get("current_units")
    restored_avg_cost = state_entry.get("avg_cost")
    if restored_units is not None:
        section["current_units"] = _format_units_for_config(restored_units, mode)
    if restored_avg_cost is not None:
        section["current_avg_cost"] = round(safe_float(restored_avg_cost, 0.0), 6)
    set_section(config, selected, section)
    write_yaml(config)
    state_entry["last_status_msg"] = f"已恢复到交易前状态回滚点：{backup_id}"
    request_runtime_state_restore(selected, backup_id, state_entry)
    trade = backup.get("trade") or {}
    return f"已恢复到 {backup.get('time', '')} 的交易前状态：{trade.get('side', '')} {trade.get('qty', '')} @ {trade.get('price', '')}"


def get_position_mode_from_section(section: Dict[str, Any]) -> str:
    base = section.get("base_units", 0)
    target = section.get("target_units", 0)
    if isinstance(base, str) and base.strip().endswith("%"):
        return "percent"
    if isinstance(target, str) and target.strip().endswith("%"):
        return "percent"
    try:
        if 0 <= float(base) <= 1 and 0 <= float(target) <= 1:
            return "percent"
    except Exception:
        pass
    return "absolute"

def fmt_snapshot_value(value: Any, digits: int = 3) -> str:
    if value is None or value == "":
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return str(value)

def write_state(data: Dict[str, Any]) -> None:
    try:
        STATE_FILE.write_text(json.dumps(data or {}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def request_runtime_config_reload(selected: str, section: Dict[str, Any]) -> None:
    """Notify the running dcf.py process to reload config and reset runtime state for the edited scope."""
    state = read_state()
    if not isinstance(state, dict):
        state = {}
    meta = state.setdefault("_meta", {})
    try:
        seq = int(meta.get("config_reload_seq", 0) or 0) + 1
    except Exception:
        seq = 1
    if selected == "COMMON_BACKTEST_CONFIG":
        reload_symbols = ["__ALL__"]
    else:
        reload_symbols = [selected]
    meta["config_reload_seq"] = seq
    meta["config_reload_requested_at"] = current_time_text()
    for _k in ["state_restore_kind", "state_restore_symbol_key", "state_restore_backup_id", "state_restore_entry"]:
        meta.pop(_k, None)
    meta["config_reload_symbol_key"] = selected
    meta["config_reload_symbol_code"] = str((section or {}).get("symbol", "")).strip().upper()
    meta["config_reload_symbols"] = reload_symbols
    write_state(state)

def delete_symbol_state(selected: str, symbol_code: str = "") -> None:
    state = read_state()
    if not isinstance(state, dict):
        return
    changed = False
    if selected in state:
        state.pop(selected, None)
        changed = True
    code = (symbol_code or "").strip().upper()
    if code and code in state:
        state.pop(code, None)
        changed = True
    if code:
        to_remove = []
        for key, val in state.items():
            if isinstance(val, dict) and str(val.get("symbol", "")).strip().upper() == code:
                to_remove.append(key)
        for key in to_remove:
            state.pop(key, None)
            changed = True
    if changed:
        write_state(state)


def save_push_config_from_form() -> Dict[str, str]:
    cfg = read_push_config()
    for item in PUSH_FIELDS:
        key = item["key"]
        if key in request.form:
            cfg[key] = (request.form.get(key, "") or "").strip()
    if cfg.get("PUSH_ENABLED") not in {"yes", "no"}:
        cfg["PUSH_ENABLED"] = "yes"
    if str(cfg.get("PUSH_CHANNEL", "")).strip().lower() == "both":
        cfg["PUSH_CHANNEL"] = "pushplus"
    if str(cfg.get("PUSH_CHANNEL", "")).strip().lower() == "all":
        cfg["PUSH_CHANNEL"] = "gotify"
    if cfg.get("PUSH_CHANNEL") not in PUSH_CHANNEL_VALUES:
        cfg["PUSH_CHANNEL"] = "gotify"
    try:
        priority = int(float(str(cfg.get("GOTIFY_PRIORITY", "10") or "10")))
    except Exception:
        priority = 10
    cfg["GOTIFY_PRIORITY"] = str(priority)
    try:
        ntfy_priority = int(float(str(cfg.get("NTFY_PRIORITY", "4") or "4")))
    except Exception:
        ntfy_priority = 4
    cfg["NTFY_PRIORITY"] = str(max(1, min(5, ntfy_priority)))
    cfg["NTFY_URL"] = str(cfg.get("NTFY_URL", "")).strip().rstrip("/") or PUSH_DEFAULTS["NTFY_URL"]
    cfg["NTFY_TOPIC"] = str(cfg.get("NTFY_TOPIC", "")).strip().strip("/") or PUSH_DEFAULTS["NTFY_TOPIC"]
    write_push_config(cfg)
    return cfg

def current_time_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                return default
        return float(value)
    except Exception:
        return default

def normalize_symbol_input(symbol: str) -> str:
    s = (symbol or "").strip().upper().replace(" ", "")
    if not s:
        return s
    if s.startswith(("SH", "SZ", "HK")):
        return s
    if s.isdigit():
        if len(s) == 6:
            return f"SH{s}" if s.startswith("6") else f"SZ{s}"
        if len(s) <= 5:
            return f"HK{s.zfill(5)}"
    return s

def symbol_options(config: Dict[str, Any]) -> List[Tuple[str, str]]:
    result: List[Tuple[str, str]] = [("COMMON_BACKTEST_CONFIG", "通用回测参数")]
    etf_cfg = config.get("ETF_CONFIG", {}) or {}
    for name, item in etf_cfg.items():
        symbol = str(item.get("symbol", "")).strip()
        result.append((name, f"{name} ({symbol})" if symbol else name))
    return result

def get_section(config: Dict[str, Any], selected: str) -> Dict[str, Any]:
    if selected == "COMMON_BACKTEST_CONFIG":
        return config.get("COMMON_BACKTEST_CONFIG", {}) or {}
    return (config.get("ETF_CONFIG", {}) or {}).get(selected, {}) or {}

def set_section(config: Dict[str, Any], selected: str, section: Dict[str, Any]) -> None:
    if selected == "COMMON_BACKTEST_CONFIG":
        config["COMMON_BACKTEST_CONFIG"] = section
    else:
        config.setdefault("ETF_CONFIG", {})
        config["ETF_CONFIG"][selected] = section

def latest_status_for(selected: str, state: Dict[str, Any]) -> str:
    if selected == "COMMON_BACKTEST_CONFIG":
        return "通用回测参数无实时状态。"
    item = state.get(selected, {}) if isinstance(state, dict) else {}
    txt = str(item.get("last_status_msg", "暂无最新状态。")).strip()
    if txt.lower().startswith("last_status_msg"):
        txt = txt.split(":", 1)[-1].strip()
    return txt or "暂无最新状态。"

def convert_form_value(key: str, value: str) -> Any:
    value = value.strip()
    if key in {"symbol", "strategy_run", "box_grid_enabled", "base_units", "target_units", "current_units", "pyramid_add_enabled"}:
        return value
    if key == "pyramid_weights":
        if not value:
            return []
        return [float(x.strip()) for x in value.split(",") if x.strip()]
    if key in {"sideways_window_30", "sideways_window_60", "pyramid_steps"}:
        return int(float(value or 0))
    if value == "":
        return ""
    try:
        return float(value)
    except Exception:
        return value

def get_meta_from_state(selected: str, state: Dict[str, Any]) -> Dict[str, Any]:
    if selected == "COMMON_BACKTEST_CONFIG":
        return {"current_price": "", "last_time": ""}
    node = state.get(selected, {}) if isinstance(state, dict) else {}
    return {
        "current_price": node.get("last_price", ""),
        "last_time": node.get("last_time", ""),
    }

def build_grouped_fields(section: Dict[str, Any]) -> List[Dict[str, Any]]:
    grouped = []
    for group in FIELD_GROUPS:
        item_rows = []
        for key, label, field_type in group["items"]:
            raw = section.get(key, "")
            if isinstance(raw, list):
                display = ", ".join(str(x) for x in raw)
            elif isinstance(raw, bool):
                display = "true" if raw else "false"
            else:
                display = "" if raw is None else str(raw)
            item_rows.append(
                {
                    "key": key,
                    "label": label,
                    "type": field_type,
                    "value": display,
                    "readonly": True,
                    "options": SELECT_FIELD_OPTIONS.get(key, []),
                    "help": PARAM_HELP.get(key, ""),
                }
            )
        grouped.append({"id": group["id"], "title": group["title"], "items": item_rows})
    return grouped

def build_new_symbol_section(config: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    common = deepcopy(config.get("COMMON_BACKTEST_CONFIG", {}) or {})
    normalize_config({"COMMON_BACKTEST_CONFIG": common})
    common["symbol"] = symbol
    common["strategy_run"] = "on"
    common["box_grid_enabled"] = "no"
    common["pyramid_add_enabled"] = "auto"
    common.setdefault("current_units", common.get("base_units", ""))
    common.setdefault("current_avg_cost", 0.0)
    return common

def _artifact_url(path: Path) -> str:
    return url_for("download_artifact", path=str(path))

def _strip_backtest_explanation(text: str) -> str:
    if not text:
        return text
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith('$ /root/dcf/.venv/bin/python '):
            continue
        lines.append(line)
    text = "\n".join(lines).strip()
    marker = "\n说明："
    if marker in text:
        text = text.split(marker, 1)[0].rstrip()
    return text

def parse_backtest_output(text: str, symbol: str) -> Dict[str, Any]:
    summary_cards: List[Tuple[str, str]] = []
    files: Dict[str, Dict[str, str]] = {}
    if not text:
        return {"summary_cards": summary_cards, "files": files}
    kv: Dict[str, str] = {}
    artifact_paths: Dict[str, Path] = {}
    for line in text.splitlines():
        line_s = line.strip()
        m = re.match(r"^(日志|交易明细|事件明细|每日详情):\s*(.+)$", line_s)
        if m:
            label = m.group(1)
            path = Path(m.group(2).strip())
            if path.exists():
                artifact_paths[label] = path
            continue
        if ":" in line_s:
            key, value = line_s.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key in SUMMARY_KEYS or key in {"买入次数", "卖出次数", "分红事件", "拆股事件"}:
                kv[key] = value
    # 交易/报告/配置/打包文件
    if symbol:
        outdir = BACKTEST_OUT_DIR / symbol
        report_path = outdir / f"backtest_report_{symbol}.txt"
        trades_path = outdir / f"trades_{symbol}.csv"
        daily_details_path = outdir / f"daily_details_{symbol}.csv"
        if report_path.exists():
            files["回测摘要"] = {"name": "回测摘要", "path": str(report_path), "url": _artifact_url(report_path)}
        if trades_path.exists():
            files["交易日志"] = {"name": "交易日志", "path": str(trades_path), "url": _artifact_url(trades_path)}
        if daily_details_path.exists():
            files["价格行情"] = {"name": "价格行情", "path": str(daily_details_path), "url": _artifact_url(daily_details_path)}

    def add(label: str, value: str):
        if value != "":
            summary_cards.append((label, value))

    def add_key(key: str, display_label: str = None):
        if key in kv:
            add(display_label or key, kv[key])

    # Web 摘要卡片展示顺序
    add_key("期末持仓收益率")
    add_key("综合收益率")
    if kv.get("买入次数") or kv.get("卖出次数"):
        add("买入/卖出次数", f"{kv.get('买入次数', '0')} | {kv.get('卖出次数', '0')}")

    add_key("分红收益率")
    add_key("交易实现收益率")
    add_key("持仓收益率")

    add_key("最新价格")
    add_key("首次建仓原始价")
    add_key("首次建仓复权价")

    add_key("首次建仓日")
    if kv.get("分红事件") or kv.get("拆股事件"):
        add("分红/拆股事件", f"{kv.get('分红事件', '0')} | {kv.get('拆股事件', '0')}")
    if "最大回撤" in kv:
        add("最大回撤", kv["最大回撤"])
    else:
        add_key("最大回撤(参考)", "最大回撤")

    add_key("期末持仓")
    # 兼容旧报告
    add_key("期末持仓(成本口径)", "期末持仓")
    add_key("摊薄后持仓成本")
    # 兼容旧报告
    add_key("期末持仓成本", "摊薄后持仓成本")
    if "期末市值权重" in kv:
        add("期末市值权重", kv["期末市值权重"])
    elif "期末估算市值权重" in kv:
        add("期末市值权重", kv["期末估算市值权重"])
    elif "期末市值" in kv:
        add("期末市值", kv["期末市值"])
    return {"summary_cards": summary_cards, "files": files}

def run_backtest(symbol: str, days: int, base_units: str, target_units: str) -> Dict[str, Any]:
    if not BACKTEST_FILE.exists():
        return {"output": "❌ 未找到 backtest_dcf.py", "summary_cards": [], "files": {}}
    symbol = normalize_symbol_input(symbol)
    cmd = [
        sys.executable,
        str(BACKTEST_FILE),
        "--config",
        str(CONFIG_FILE),
        "--symbol",
        symbol,
        "--days",
        str(days),
    ]
    if base_units.strip():
        cmd.extend(["--base-units", base_units.strip()])
    if target_units.strip():
        cmd.extend(["--target-units", target_units.strip()])
    try:
        result = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"output": "❌ 回测超时，请缩短天数或检查网络。", "summary_cards": [], "files": {}}
    clean_stdout = _strip_backtest_explanation((result.stdout or "").strip())
    parts = []
    if clean_stdout:
        parts.append(clean_stdout)
    if result.stderr:
        parts.append("[stderr]\n" + result.stderr.strip())
    if result.returncode != 0 and not result.stderr and not clean_stdout:
        parts.append(f"❌ 回测失败，退出码={result.returncode}")
    output = "\n\n".join(x for x in parts if x)
    parsed = parse_backtest_output(clean_stdout, symbol)
    return {"output": output, **parsed}

def analyze_total_profit() -> str:
    import csv
    config = read_yaml()
    state = read_state()
    etf_config = config.get("ETF_CONFIG", {}) if isinstance(config, dict) else {}
    def get_mode_by_name(name: str) -> str:
        cfg = etf_config.get(name, {}) if isinstance(etf_config, dict) else {}
        base = cfg.get("base_units", 0)
        target = cfg.get("target_units", 0)
        if isinstance(base, str) and base.strip().endswith("%"):
            return "percent"
        if isinstance(target, str) and target.strip().endswith("%"):
            return "percent"
        try:
            if 0 <= float(base) <= 1 and 0 <= float(target) <= 1:
                return "percent"
        except Exception:
            pass
        return "absolute"
    def parse_dt(s: str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y.%m.%d.%H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                pass
        return None
    if not TRADE_LOG_FILE.exists():
        return "未找到 trade_log.csv，暂无收益分析数据。"
    with TRADE_LOG_FILE.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    rows.sort(key=lambda r: parse_dt(r.get("date", "")) or datetime.min)
    if not rows:
        return "trade_log.csv 为空。"
    class DCFStat:
        def __init__(self, name: str, symbol: str, mode: str):
            self.name = name
            self.symbol = symbol
            self.mode = mode
            self.trade_count = 0
            self.position = 0.0
            self.avg_cost = 0.0
            self.realized_total = 0.0
            self.total_investment = 0.0
        def process_trade(self, row: Dict[str, str]) -> None:
            price = safe_float(row.get("price", 0))
            qty = safe_float(row.get("qty", 0))
            side = (row.get("side", "") or "").upper()
            pos_after = safe_float(row.get("pos_after", 0))
            avg_cost_before = safe_float(row.get("avg_cost_before", 0))
            avg_cost_after = safe_float(row.get("avg_cost_after", 0))
            self.trade_count += 1
            self.position = pos_after
            if avg_cost_after > 0:
                self.avg_cost = avg_cost_after
            if side == "BUY" and self.mode == "absolute":
                self.total_investment += price * qty
            elif side == "SELL":
                if self.mode == "absolute":
                    self.realized_total += (price - avg_cost_before) * qty if avg_cost_before > 0 else 0.0
                else:
                    self.realized_total += qty * (price / avg_cost_before - 1.0) if avg_cost_before > 0 else 0.0
        def get_current_price(self) -> float:
            d = state.get(self.name, {}) if isinstance(state, dict) else {}
            return safe_float(d.get("last_price", 0))
        def get_current_value_or_weight(self) -> Tuple[float, float]:
            price = self.get_current_price()
            if self.mode == "absolute":
                current_value = self.position * price if price > 0 else self.position * self.avg_cost
                floating = (price - self.avg_cost) * self.position if self.position > 0 and self.avg_cost > 0 and price > 0 else 0.0
                return current_value, floating
            if self.position <= 0:
                return 0.0, 0.0
            if price > 0 and self.avg_cost > 0:
                market_weight = self.position * price / self.avg_cost
                floating = self.position * (price / self.avg_cost - 1.0)
            else:
                market_weight = self.position
                floating = 0.0
            return market_weight, floating
    dcf_stats: Dict[Tuple[str, str], Any] = {}
    for row in rows:
        name = (row.get("dcf_name", "") or "UNKNOWN").strip() or "UNKNOWN"
        symbol = (row.get("symbol", "") or "").strip()
        key = (name, symbol)
        if key not in dcf_stats:
            dcf_stats[key] = DCFStat(name, symbol, get_mode_by_name(name))
        dcf_stats[key].process_trade(row)
    out: List[str] = ["=" * 60, "DCF 策略收益分析", "=" * 60]
    abs_total_realized = 0.0
    abs_total_investment = 0.0
    abs_total_current_value = 0.0
    pct_total_realized = 0.0
    pct_total_floating = 0.0
    pct_total_market_weight = 0.0
    for (name, symbol), stat in sorted(dcf_stats.items(), key=lambda x: x[0][0]):
        out.extend(["", "=" * 50, f"标的: {name} ({symbol})", "-" * 50])
        current_price = stat.get_current_price()
        current_metric, floating_metric = stat.get_current_value_or_weight()
        if stat.mode == "absolute":
            out.append(f"总投入资金: {stat.total_investment:,.2f}")
            out.append(f"已实现收益: {stat.realized_total:,.2f}")
            out.append(f"当前持仓市值: {current_metric:,.2f}")
            abs_total_realized += stat.realized_total
            abs_total_investment += stat.total_investment
            abs_total_current_value += current_metric
        else:
            out.append(f"当前持仓(成本口径): {stat.position * 100:.2f}%")
            out.append(f"当前价格: {current_price:.4f}" if current_price > 0 else "当前价格: -")
            out.append(f"已实现收益贡献: {stat.realized_total * 100:.2f}%")
            out.append(f"浮动收益贡献: {floating_metric * 100:.2f}%")
            out.append(f"综合收益贡献: {(stat.realized_total + floating_metric) * 100:.2f}%")
            pct_total_realized += stat.realized_total
            pct_total_floating += floating_metric
            pct_total_market_weight += current_metric
    out.extend(["", "=" * 60, "总体汇总", "=" * 60])
    if abs_total_investment > 0:
        total_assets = abs_total_current_value + abs_total_realized
        out.append("[股数模式]")
        out.append(f"总投入资金: {abs_total_investment:,.2f}")
        out.append(f"总已实现收益: {abs_total_realized:,.2f}")
        out.append(f"当前持仓市值: {abs_total_current_value:,.2f}")
        out.append(f"总资产: {total_assets:,.2f}")
    if abs(pct_total_market_weight) > 1e-12 or abs(pct_total_realized) > 1e-12 or abs(pct_total_floating) > 1e-12:
        out.append("[百分比模式]")
        out.append(f"当前估算市值权重合计: {pct_total_market_weight * 100:.2f}%")
        out.append(f"已实现收益贡献合计: {pct_total_realized * 100:.2f}%")
        out.append(f"浮动收益贡献合计: {pct_total_floating * 100:.2f}%")
        out.append(f"综合收益贡献合计: {(pct_total_realized + pct_total_floating) * 100:.2f}%")
    return "\n".join(out)

@app.before_request
def require_login() -> Any:
    if request.endpoint in {"login", "static", "download_artifact", "download_backtest_bundle"}:
        return None
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    return None

@app.route("/login", methods=["GET", "POST"])
def login():
    cfg = load_web_config()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        stored_user = cfg.get("admin_username", "admin")
        stored_hash = cfg.get("password_hash", "")
        if username == stored_user and stored_hash and check_password_hash(stored_hash, password):
            session["logged_in"] = True
            session["username"] = username
            return redirect(url_for("status_page"))
        flash("用户名或密码错误", "error")
    return render_template("login.html", app_name=APP_DISPLAY_NAME, domain=cfg.get("domain", ""))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/download-artifact")
def download_artifact():
    path_text = request.args.get("path", "").strip()
    if not path_text:
        abort(404)
    p = Path(path_text).expanduser().resolve()
    allowed_roots = [BACKTEST_OUT_DIR.resolve(), BASE_DIR.resolve()]
    if not any(str(p).startswith(str(root) + "/") or p == root for root in allowed_roots):
        abort(403)
    if not p.exists() or not p.is_file():
        abort(404)
    return send_file(p, as_attachment=True, download_name=p.name)


@app.route("/download-backtest-bundle")
def download_backtest_bundle():
    symbol = normalize_symbol_input(request.args.get("symbol", ""))
    if not symbol:
        abort(404)
    outdir = BACKTEST_OUT_DIR / symbol
    files = [
        outdir / f"backtest_report_{symbol}.txt",
        outdir / f"trades_{symbol}.csv",
        outdir / f"daily_details_{symbol}.csv",
    ]
    existing = [p for p in files if p.exists() and p.is_file()]
    if not existing:
        abort(404)
    bio = BytesIO()
    with ZipFile(bio, "w", ZIP_DEFLATED) as zf:
        for p in existing:
            zf.write(p, arcname=p.name)
    bio.seek(0)
    return send_file(bio, as_attachment=True, download_name=f"backtest_bundle_{symbol}.zip", mimetype="application/zip")

def _selected_key(config: Dict[str, Any]) -> str:
    options = symbol_options(config)
    allowed = {key for key, _ in options}
    requested = (request.args.get("symbol_key", "") or "").strip()
    if requested and requested in allowed:
        session["selected_symbol_key"] = requested
        return requested
    stored = str(session.get("selected_symbol_key", "")).strip()
    if stored in allowed:
        return stored
    fallback = options[0][0] if options else "COMMON_BACKTEST_CONFIG"
    session["selected_symbol_key"] = fallback
    return fallback

def build_symbol_cards(config: Dict[str, Any], selected: str) -> List[Dict[str, str]]:
    etf_cfg = config.get("ETF_CONFIG", {}) or {}
    cards = [{"key": "COMMON_BACKTEST_CONFIG", "label": "通用回测参数", "symbol": "", "active": selected == "COMMON_BACKTEST_CONFIG"}]
    for name, item in etf_cfg.items():
        cards.append({"key": name, "label": name, "symbol": str(item.get("symbol", "")).strip(), "active": selected == name})
    return cards

def _base_context(config: Dict[str, Any], selected: str) -> Dict[str, Any]:
    state = read_state()
    section = get_section(config, selected)
    meta = get_meta_from_state(selected, state)
    grouped_fields = build_grouped_fields(section)
    bt_symbol_default = normalize_symbol_input(section.get("symbol", "")) if selected != "COMMON_BACKTEST_CONFIG" else ""
    common = config.get("COMMON_BACKTEST_CONFIG", {}) or {}
    bt_base_default = str(section.get("base_units", common.get("base_units", "2.5%")))
    bt_target_default = str(section.get("target_units", common.get("target_units", "5%")))
    current_symbol = str(section.get("symbol", "")).strip()
    available_snapshot_dates = snapshot_dates(30)
    requested_snapshot_date = (request.args.get("snapshot_date", "") or "").strip()
    default_snapshot_date = datetime.now().strftime("%Y-%m-%d")
    selected_snapshot_date = requested_snapshot_date if requested_snapshot_date in available_snapshot_dates else (available_snapshot_dates[0] if available_snapshot_dates else default_snapshot_date)
    latest_snapshot, recent_snapshots = get_strategy_snapshots(selected, current_symbol, selected_snapshot_date)
    market_source_stats = build_market_source_stats(selected, current_symbol, selected_snapshot_date)
    trade_state_backups = get_trade_state_backups(selected, current_symbol, 10)
    return {
        "app_name": APP_DISPLAY_NAME,
        "selected": selected,
        "selected_label": "通用回测参数" if selected == "COMMON_BACKTEST_CONFIG" else selected,
        "selected_symbol_code": current_symbol,
        "section": section,
        "status_text": latest_status_for(selected, state),
        "grouped_fields": grouped_fields,
        "current_time": current_time_text(),
        "current_price": meta.get("current_price", ""),
        "last_time": meta.get("last_time", ""),
        "bt_symbol_default": bt_symbol_default,
        "bt_base_default": bt_base_default,
        "bt_target_default": bt_target_default,
        "is_common": selected == "COMMON_BACKTEST_CONFIG",
        "nav_items": PAGE_TITLES,
        "param_help": PARAM_HELP,
        "backtest_help_text": BACKTEST_HELP_TEXT,
        "backtest_metrics_help_text": BACKTEST_METRICS_HELP_TEXT,
        "symbol_cards": build_symbol_cards(config, selected),
        "snapshot_dates": available_snapshot_dates,
        "selected_snapshot_date": selected_snapshot_date,
        "latest_snapshot": latest_snapshot,
        "recent_snapshots": recent_snapshots,
        "market_source_stats": market_source_stats,
        "trade_state_backups": trade_state_backups,
        "fmt_snapshot_value": fmt_snapshot_value,
    }

def _save_all_params(config: Dict[str, Any], selected: str) -> None:
    section = get_section(config, selected).copy()
    for group in FIELD_GROUPS:
        for key, _, _ in group["items"]:
            if key in request.form:
                section[key] = convert_form_value(key, request.form.get(key, ""))
    # 移除已废弃字段
    section.pop("fee_rate", None)
    section.pop("slippage_bp", None)
    section.pop("pyramid_enabled", None)
    section.pop("k300", None)
    section.pop("ma300_min_coef", None)
    if section.get("box_grid_enabled") not in {"yes", "no"}:
        section["box_grid_enabled"] = "no"
    section["strategy_run"] = normalize_strategy_run_value(section.get("strategy_run", "on"), "on")
    if section.get("pyramid_add_enabled") not in {"yes", "auto"}:
        section["pyramid_add_enabled"] = "auto"
    set_section(config, selected, section)
    write_yaml(config)
    request_runtime_config_reload(selected, section)

def _handle_symbol_actions(config: Dict[str, Any], selected: str):
    action = request.form.get("action", "")
    if action == "set_symbol":
        new_selected = (request.form.get("symbol_key", "") or "").strip()
        session["selected_symbol_key"] = new_selected or selected
        return redirect(url_for("symbols_page"))
    if action == "add_symbol":
        symbol_name = (request.form.get("new_name", "") or "").strip()
        symbol_code = normalize_symbol_input(request.form.get("new_symbol", ""))
        if not symbol_name or not symbol_code:
            flash("请填写标的名称和代码。", "error")
        else:
            etf_cfg = config.setdefault("ETF_CONFIG", {}) or {}
            if symbol_name in etf_cfg:
                flash("该名称已存在，请换一个。", "error")
            else:
                config.setdefault("ETF_CONFIG", {})[symbol_name] = build_new_symbol_section(config, symbol_code)
                write_yaml(config)
                flash(f"已新增标的：{symbol_name} ({symbol_code})", "success")
                return redirect(url_for("symbols_page", symbol_key=symbol_name))
    elif action == "delete_symbol":
        if selected == "COMMON_BACKTEST_CONFIG":
            flash("通用回测参数不可删除。", "error")
        else:
            etf_cfg = config.get("ETF_CONFIG", {}) or {}
            if selected in etf_cfg:
                symbol_code = str((etf_cfg.get(selected) or {}).get("symbol", "")).strip().upper()
                del etf_cfg[selected]
                write_yaml(config)
                delete_symbol_state(selected, symbol_code)
                flash(f"已删除标的：{selected}", "success")
                return redirect(url_for("symbols_page", symbol_key="COMMON_BACKTEST_CONFIG"))
            flash("未找到要删除的标的。", "error")
    return None

@app.route("/")
def home_redirect():
    return redirect(url_for("status_page", symbol_key=request.args.get("symbol_key", "")))

@app.route("/symbols", methods=["GET", "POST"])
def symbols_page():
    config = read_yaml()
    selected = _selected_key(config)
    if request.method == "POST":
        redirect_resp = _handle_symbol_actions(config, selected)
        if redirect_resp is not None:
            return redirect_resp
        config = read_yaml()
        selected = _selected_key(config)
    ctx = _base_context(config, selected)
    ctx.update({"page_name": "symbols"})
    return render_template("dashboard.html", **ctx)

@app.route("/status", methods=["GET"])
def status_page():
    config = read_yaml()
    selected = _selected_key(config)
    ctx = _base_context(config, selected)
    ctx.update({"page_name": "status"})
    return render_template("dashboard.html", **ctx)


@app.route("/restore-trade-state", methods=["POST"])
def restore_trade_state_page():
    config = read_yaml()
    selected = (request.form.get("symbol_key", "") or "").strip()
    allowed = {key for key, _ in symbol_options(config)}
    if selected not in allowed:
        selected = _selected_key(config)
    backup_id = (request.form.get("backup_id", "") or "").strip()
    try:
        detail = restore_trade_state_backup(backup_id, selected)
        flash(detail, "success")
    except Exception as e:
        flash(f"回滚失败：{e}", "error")
    return redirect(url_for("status_page", symbol_key=selected))

@app.route("/params", methods=["GET", "POST"])
def params_page():
    config = read_yaml()
    selected = _selected_key(config)
    if request.method == "POST" and request.form.get("action") == "save_params":
        _save_all_params(config, selected)
        flash("参数已保存到 dcf.yaml", "success")
        return redirect(url_for("params_page"))
    ctx = _base_context(config, selected)
    ctx.update({"page_name": "params"})
    return render_template("dashboard.html", **ctx)

@app.route("/backtest", methods=["GET", "POST"])
def backtest_page():
    config = read_yaml()
    selected = _selected_key(config)
    backtest_output = ""
    backtest_cards = []
    backtest_files = {}
    if request.method == "POST" and request.form.get("action") == "run_backtest":
        bt_symbol = normalize_symbol_input(request.form.get("bt_symbol", "") or _base_context(config, selected)["bt_symbol_default"])
        bt_base = (request.form.get("bt_base_units", "") or "").strip()
        bt_target = (request.form.get("bt_target_units", "") or "").strip()
        bt_days = int(request.form.get("bt_days", "800") or "800")
        if not bt_symbol:
            flash("请填写回测代码。", "error")
        else:
            result = run_backtest(bt_symbol, bt_days, bt_base, bt_target)
            backtest_output = result.get("output", "")
            backtest_cards = result.get("summary_cards", [])
            backtest_files = result.get("files", {})
            flash("回测执行完成。", "success")
    ctx = _base_context(config, selected)
    ctx.update({"page_name": "backtest", "backtest_output": backtest_output, "backtest_cards": backtest_cards, "backtest_files": backtest_files})
    return render_template("dashboard.html", **ctx)


@app.route("/push", methods=["GET", "POST"])
def push_page():
    cfg = read_push_config()
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "save_push":
            cfg = save_push_config_from_form()
            flash("推送配置已保存到 /root/dcf/push.conf", "success")
            return redirect(url_for("push_page"))
        if action == "test_push":
            ok, detail = send_push_test(cfg)
            flash(detail, "success" if ok else "error")
            return redirect(url_for("push_page"))
    config = read_yaml()
    selected = _selected_key(config)
    ctx = _base_context(config, selected)
    ctx.update({
        "page_name": "push",
        "push_config_path": str(PUSH_CONFIG_FILE),
        "push_log_path": str(PUSH_LOG_FILE),
        "push_config": cfg,
        "push_fields": PUSH_FIELDS,
        "push_select_options": PUSH_SELECT_OPTIONS,
        "push_logs": read_push_logs(PUSH_LOG_KEEP_LINES),
    })
    return render_template("dashboard.html", **ctx)

if __name__ == "__main__":
    cfg = load_web_config()
    app.run(host="127.0.0.1", port=int(cfg.get("internal_port", 1819)), debug=False)