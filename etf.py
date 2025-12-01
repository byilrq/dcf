import time
import json
from pathlib import Path
from datetime import datetime
import requests
import os
import logging
import sys

# ========= 路径 & 日志配置 =========

BASE_DIR = Path(__file__).parent

# 状态文件 & 配置文件 & 日志文件都放在脚本同目录
STATE_FILE = BASE_DIR / "etf_monitor_state.json"
CONFIG_FILE = BASE_DIR / "etf.conf"
LOG_FILE = BASE_DIR / "etf.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y.%m.%d.%H:%M:%S",  # 日志时间格式，例如 2025.12.01.13:01:05
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# ========= 加载配置 =========

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if "ETF_CONFIG" not in cfg:
                logging.error(f"配置文件 {CONFIG_FILE} 中缺少 'ETF_CONFIG' 字段。")
                sys.exit(1)
            return cfg
        except Exception:
            logging.exception(f"读取配置文件 {CONFIG_FILE} 失败")
            sys.exit(1)
    else:
        logging.error(f"配置文件 {CONFIG_FILE} 不存在，请先创建 etf.conf。")
        sys.exit(1)


full_config = load_config()
ETF_CONFIG = full_config["ETF_CONFIG"]

# ========= PushPlus 设置 =========

PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "")
PUSHPLUS_URL = "http://www.pushplus.plus/send"

# 轮询间隔（秒）
POLL_INTERVAL_SECONDS = 10  # 调试用，实盘可改为 600（10 分钟）

# ========= 状态读写 =========

def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            # 如果配置里新增了 ETF，状态里可能没有，补上
            for name in ETF_CONFIG.keys():
                if name not in state:
                    state[name] = {"last_price": None, "tick": 0}
            return state
        except Exception:
            logging.exception(f"读取状态文件 {STATE_FILE} 失败，重新初始化状态")
    # 初始 state
    return {name: {"last_price": None, "tick": 0} for name in ETF_CONFIG.keys()}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        logging.exception(f"保存状态到 {STATE_FILE} 失败")


# ========= 获取价格函数 =========

# 某些 ETF 在东财接口里价格放大 10 倍，需要缩放
PRICE_SCALE = {
    "SH520890": 0.1,
    "SH515080": 0.1,
}


def get_price_from_api(symbol: str, tick: int = 0) -> float:
    """
    使用东方财富 push2 接口获取实时价格。
    symbol 格式建议：'SH515080' 或 'SZ159920' 等。
    """
    raw_symbol = symbol.upper().strip()
    symbol = raw_symbol

    if symbol.startswith("SH"):
        market = "1"   # 1 = 上证
        code = symbol[2:]
    elif symbol.startswith("SZ"):
        market = "0"   # 0 = 深证
        code = symbol[2:]
    else:
        market = "1"
        code = symbol

    secid = f"{market}.{code}"
    url = (
        "https://push2.eastmoney.com/api/qt/stock/get"
        f"?secid={secid}&fields=f43"
    )

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/"
    }

    resp = requests.get(url, headers=headers, timeout=5)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("data") or data["data"].get("f43") in (None, 0):
        raise ValueError(f"行情数据为空: {symbol}, 返回: {data}")

    price_raw = data["data"]["f43"]   # 一般是“分”，有些品种再放大 10 倍
    price = price_raw / 100.0        # 先按常规 /100

    # 特定 ETF 价格再缩放（比如 520890 / 515080）
    scale = PRICE_SCALE.get(raw_symbol, 1.0)
    price = price * scale

    return round(float(price), 3)


# ========= 通知函数 =========

def send_notification(message: str):
    """
    使用 PushPlus 推送通知到你的微信/手机。
    """
    if not PUSHPLUS_TOKEN:
        # 没填 token 就直接写日志
        logging.warning(f"通知（未配置 PUSHPLUS_TOKEN，仅写日志，不推送）：\n{message}\n")
        return

    payload = {
        "token": PUSHPLUS_TOKEN,
        "title": "ETF 网格信号提醒",
        "content": message,
        "template": "txt"  # 纯文本即可
    }

    try:
        resp = requests.post(PUSHPLUS_URL, json=payload, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 200:
            logging.error(f"[PushPlus] 推送失败: {data}")
        else:
            logging.info(f"[PushPlus] 推送成功：{datetime.now().strftime('%Y.%m.%d.%H:%M')}")
    except Exception:
        logging.exception("[PushPlus] 推送异常")
        logging.error(f"消息内容如下：\n{message}")


# ========= 根据股息率决定网格和买卖权限 =========

def decide_grid_and_mode(cfg: dict):
    """
    根据当前配置里的股息率，决定：
    - 当前网格间距 grid_pct
    - 是否允许买入 can_buy
    - 是否允许卖出 can_sell（目前恒为 True，可扩展）
    - 当前估值档位 label（用于提醒）
    """
    dy = cfg.get("dividend_yield", None)
    if dy is None:
        # 如果你懒得填股息率，就用默认 grid_pct，不限制买入卖出
        return cfg.get("grid_pct", 0.04), True, True, "未知"

    # A 档：超级低估（>= 7%）
    if dy >= 0.07:
        return cfg.get("grid_low", 0.03), True, True, f"A档极低估 (DY={dy:.1%})"

    # B 档：低估（6% ~ 7%）
    if 0.06 <= dy < 0.07:
        return cfg.get("grid_mid", 0.04), True, True, f"B档低估 (DY={dy:.1%})"

    # C 档：合理（5% ~ 6%）
    if 0.05 <= dy < 0.06:
        # 可以适当放宽一点网格
        return cfg.get("grid_high", 0.05), True, True, f"C档合理 (DY={dy:.1%})"

    # D 档：偏贵（< 5%）→ 不建议再买，只允许卖
    # 网格用更宽一点，避免频繁交易
    return cfg.get("grid_expensive", 0.06), False, True, f"D档偏贵 (DY={dy:.1%})"


# ========= 核心：单只 ETF 网格检查 =========

def check_signals_for_etf(name: str, cfg: dict, state: dict):
    """
    对单只 ETF 进行网格检查。
    返回要发的消息字符串列表。
    """
    symbol = cfg["symbol"]
    base_price = cfg["base_price"]

    # 根据股息率决定当前网格和买卖模式
    grid_pct, can_buy, can_sell, dy_label = decide_grid_and_mode(cfg)

    # 仓位参数
    step_pct = cfg.get("step_pct", 0.01)          # 每格占总资金比例
    base_units = cfg.get("base_units", 0)         # 底仓份额（如 10000 份）
    step_units = int(base_units * step_pct) if base_units else 0  # 每格建议买/卖份额

    # tick 用来让价格查询有个“时间推进”的标记（目前只用于状态）
    tick = state.get(name, {}).get("tick", 0) + 1
    state[name]["tick"] = tick

    current_price = get_price_from_api(symbol, tick)
    last_price = state.get(name, {}).get("last_price")

    if name not in state:
        state[name] = {}

    # 第一次运行：只记录价格，不发信号
    if last_price is None:
        logging.info(
            f"{name} 首次价格记录: {current_price}，股息率档位：{dy_label}，"
            f"底仓份额: {base_units}，每格建议份额: {step_units}"
        )
        state[name]["last_price"] = current_price
        return []

    messages = []

    price_ratio_now = current_price / base_price
    price_ratio_last = last_price / base_price

    # 格子编号 n: price = base_price * (1 + n * grid_pct)
    current_grid = int((price_ratio_now - 1) / grid_pct)
    last_grid = int((price_ratio_last - 1) / grid_pct)

    logging.info(
        f"{name} 当前价格: {current_price}，股息率档位：{dy_label}，"
        f"当前格子: {current_grid}，上次格子: {last_grid}，"
        f"底仓份额: {base_units}，每格建议份额: {step_units}"
    )

    # 时间字符串，例如 2025.12.01.13:01
    now_str = datetime.now().strftime("%Y.%m.%d.%H:%M")

    # 向上穿越，触发卖出网格
    if current_grid > last_grid and can_sell:
        for g in range(last_grid + 1, current_grid + 1):
            level_price = base_price * (1 + g * grid_pct)
            msg = (
                f"{name} ({symbol}) 触发【卖出网格】:\n"
                f"- 运行时间: {now_str}\n"
                f"- 股息率档位: {dy_label}\n"
                f"- 网格编号: {g}\n"
                f"- 当前网格间距: {grid_pct:.2%}\n"
                f"- 参考卖出价: {level_price:.4f}\n"
                f"- 当前价: {current_price:.4f}\n"
                f"- 建议：减一档网格仓（约减 {step_units} 份，占总资金 {step_pct*100:.1f}%），不动底仓。"
            )
            messages.append(msg)

    # 向下穿越，触发买入网格（仅当允许买入时）
    elif current_grid < last_grid and can_buy:
        for g in range(last_grid - 1, current_grid - 1, -1):
            level_price = base_price * (1 + g * grid_pct)
            msg = (
                f"{name} ({symbol}) 触发【买入网格】:\n"
                f"- 运行时间: {now_str}\n"
                f"- 股息率档位: {dy_label}\n"
                f"- 网格编号: {g}\n"
                f"- 当前网格间距: {grid_pct:.2%}\n"
                f"- 参考买入价: {level_price:.4f}\n"
                f"- 当前价: {current_price:.4f}\n"
                f"- 建议：加一档网格仓（约加 {step_units} 份，占总资金 {step_pct*100:.1f}%），不动底仓。"
            )
            messages.append(msg)

    # 如果 current_grid < last_grid 但 can_buy=False（偏贵区域），则不会发买入信号

    state[name]["last_price"] = current_price
    return messages


# ========= 主循环 =========

def main_loop():
    state = load_state()
    logging.info("ETF 网格监控脚本启动完成。")

    while True:
        all_messages = []

        for name, cfg in ETF_CONFIG.items():
            try:
                msgs = check_signals_for_etf(name, cfg, state)
                all_messages.extend(msgs)
            except Exception:
                logging.exception(f"{name} 检查信号时出错")

        if all_messages:
            full_msg = "\n\n".join(all_messages)
            send_notification(full_msg)

        save_state(state)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main_loop()
