import time as time_module
import json
from pathlib import Path
from datetime import datetime, time
import requests
import logging
import os
import math
import csv
from logging.handlers import TimedRotatingFileHandler
import sys

# ===========================
# 路径配置
# ===========================
BASE_DIR = Path(__file__).parent
config_path = os.path.join(BASE_DIR, "etf.conf")
STATE_FILE = BASE_DIR / "etf_monitor_state.json"
LOG_DIR = BASE_DIR / "log"
TRADE_LOG_FILE = BASE_DIR / "trade_log.csv"

# 创建日志目录
LOG_DIR.mkdir(exist_ok=True)

# ===========================
# 辅助函数
# ===========================
def calculate_new_avg_cost(old_position, old_avg_cost, add_units, add_price):
    """计算加仓后的新平均成本"""
    if old_position == 0:
        return add_price
    total_cost_before = old_position * old_avg_cost
    total_cost_after = total_cost_before + add_units * add_price
    return total_cost_after / (old_position + add_units)

def _parse_hm(s: str):
    """把 '09:30' 解析成 (9, 30)"""
    try:
        h, m = s.split(":")
        return int(h), int(m)
    except Exception:
        return 9, 30

# ===========================
# 日志设置
# ===========================
def setup_logging():
    """配置按天轮转的日志"""
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    if logger.hasHandlers():
        logger.handlers.clear()
    
    log_file = LOG_DIR / "etf.log"
    file_handler = TimedRotatingFileHandler(
        filename=str(log_file),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8"
    )
    file_handler.suffix = "%Y%m%d"
    file_handler.setLevel(logging.INFO)
    
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    
    formatter = logging.Formatter("%(message)s")
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

logger = setup_logging()

# ===========================
# 读取配置文件
# ===========================
def load_config(path):
    if not os.path.exists(path):
        raise Exception(f"配置文件不存在: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        logging.error(f"无法解析配置文件 {path}")
        raise e
    etf_cfg = cfg.get("ETF_CONFIG", {})
    strategy_cfg = cfg.get("STRATEGY", {})
    return etf_cfg, strategy_cfg

# 全局策略参数
STRATEGY = {
    "loop_interval": 60,
    "fetch_history_days": 400,
    "ma_period_short": 150,
    "ma_period_long": 300,
    "session_start": "09:30",
    "session_end": "16:00",
    "daily_push_time": "09:30"
}

# ===========================
# 时间控制函数
# ===========================
def in_trade_session(now: datetime = None) -> bool:
    """是否在交易时段（用于只在 9:30~16:00 内跑策略）。"""
    if now is None:
        now = datetime.now()
    start_str = STRATEGY.get("session_start", "09:30")
    end_str = STRATEGY.get("session_end", "16:00")
    sh, sm = _parse_hm(start_str)
    eh, em = _parse_hm(end_str)
    t = now.time()
    start_t = time(sh, sm)
    end_t = time(eh, em)
    return start_t <= t <= end_t

def should_do_daily_push(state: dict, now: datetime = None) -> bool:
    """
    每天 9:30 推送一次：
    - 只要时间 >= daily_push_time
    - 并且当天还没推送过
    """
    if now is None:
        now = datetime.now()
    push_str = STRATEGY.get("daily_push_time", "09:30")
    ph, pm = _parse_hm(push_str)
    push_t = time(ph, pm)
    today = now.date().isoformat()
    meta = state.get("_meta", {})
    last_date = meta.get("last_daily_push_date")
    if now.time() >= push_t and last_date != today:
        return True
    return False
# =======================================
# 推送功能 (双通道：PushPlus & Telegram)
# =======================================
# 注意：这些环境变量需要从您的 push.conf 文件中加载
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "")
PUSHPLUS_URL = "http://www.pushplus.plus/send"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

def send_notification(msg):
    """
    双通道推送消息
    1. 推送至PushPlus
    2. 推送至Telegram Bot
    """
    all_success = True
    # 通道1: 推送至PushPlus
    if PUSHPLUS_TOKEN:
        pushplus_payload = {
            "token": PUSHPLUS_TOKEN,
            "title": "闲云量化策略通知",
            "content": msg,
            "template": "txt"
        }
        try:
            resp = requests.post(PUSHPLUS_URL, json=pushplus_payload, timeout=10)
            if resp.json().get("code") != 200:
                logging.error(f"PushPlus 推送失败: {resp.text}")
                all_success = False
            else:
                logging.info("PushPlus 推送成功。")
        except Exception as e:
            logging.error(f"PushPlus 推送异常: {e}")
            all_success = False
    else:
        logging.info("未配置 PushPlus Token，跳过该通道推送。")
    
    # 通道2: 推送至Telegram
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        telegram_payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown",  # 支持Markdown基本语法，让消息更清晰
            "disable_web_page_preview": True
        }
        try:
            resp = requests.post(TELEGRAM_API_URL, json=telegram_payload, timeout=10)
            if resp.status_code != 200:
                logging.error(f"Telegram 推送失败 (状态码{resp.status_code}): {resp.text}")
                all_success = False
            else:
                logging.info("Telegram 推送成功。")
        except Exception as e:
            logging.error(f"Telegram 推送异常: {e}")
            all_success = False
    else:
        missing = []
        if not TELEGRAM_BOT_TOKEN:
            missing.append("Bot Token")
        if not TELEGRAM_CHAT_ID:
            missing.append("Chat ID")
        logging.info(f"未配置 Telegram {', '.join(missing)}，跳过该通道推送。")
    
    return all_success


# ===========================
# 日志消息生成函数（带图标版本）
# ===========================


# ===========================
# 核心策略逻辑
# ===========================

本地维护

# ===========================
# 主循环
# ===========================
def main_loop():
    logging.info("=" * 60)
    logging.info("ETF 策略脚本启动完成")
    logging.info(f"当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logging.info("=" * 60)
      
    config_path = os.path.join(BASE_DIR, "etf.conf")
    global ETF_CONFIG, STRATEGY
    ETF_CONFIG, STRATEGY_from_conf = load_config(config_path)
    STRATEGY.update(STRATEGY_from_conf)
    
    state = load_state()
    
    if "_meta" not in state:
        state["_meta"] = {"last_daily_push_date": None}
    
    while True:
        now = datetime.now()
        
        if not in_trade_session(now):
            if should_do_daily_push(state, now):
                snapshot = build_daily_snapshot(state)
                logging.info("每日 9:30 状态快照推送:\n%s", snapshot)
                try:
                    send_notification(snapshot)
                except Exception:
                    logging.exception("推送每日快照失败")
                state["_meta"]["last_daily_push_date"] = now.date().isoformat()
                save_state(state)
            
            time_module.sleep(STRATEGY.get("loop_interval", 60))
            continue
        
        all_trade_msgs = []
        for name, cfg in ETF_CONFIG.items():
            try:
                msgs = strategy_for_etf(name, cfg, state)
                if msgs:
                    all_trade_msgs.extend(msgs)
            except Exception as e:
                logging.exception(f"{name} 策略执行出错: {e}")
        
        save_state(state)
        
        if all_trade_msgs:
            body = "\n\n".join(all_trade_msgs)
            logging.info("推送买卖信号:\n%s", body)
            try:
                send_notification(body)
            except Exception:
                logging.exception("推送买卖信号失败")
        
        if should_do_daily_push(state, now):
            snapshot = build_daily_snapshot(state)
            logging.info("每日 9:30 状态快照推送:\n%s", snapshot)
            try:
                send_notification(snapshot)
            except Exception:
                logging.exception("推送每日快照失败")
            state["_meta"]["last_daily_push_date"] = now.date().isoformat()
            save_state(state)
        
        time_module.sleep(STRATEGY.get("loop_interval", 60))

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        logging.info("用户中断程序执行")
    except Exception as e:
        logging.exception(f"程序异常退出: {e}")

