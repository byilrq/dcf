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
STATE_FILE = BASE_DIR / "etf_monitor.json"
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
ROTATION_CONFIG = full_config.get("ROTATION_CONFIG", None)


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
    
# ===============轮动计算函数====================
def compute_rotation_suggestions():
    """
    根据 ROTATION_CONFIG，对一组 ETF（目前两只）给出轮动建议。
    返回消息字符串列表。
    """
    if not ROTATION_CONFIG:
        return []
    if not ROTATION_CONFIG.get("enabled", False):
        return []

    members = ROTATION_CONFIG.get("members", [])
    if len(members) < 2:
        return []

    # 只考虑配置里存在的标的
    etfs = [m for m in members if m in ETF_CONFIG]
    if len(etfs) < 2:
        return []

    total_base_units = ROTATION_CONFIG.get("total_base_units", 0)
    if total_base_units <= 0:
        # 若未设置，则用各自 base_units 之和
        total_base_units = sum(ETF_CONFIG[n].get("base_units", 0) for n in etfs)
    if total_base_units <= 0:
        return []

    min_w = ROTATION_CONFIG.get("min_weight", 0.3)
    max_w = ROTATION_CONFIG.get("max_weight", 0.7)
    rebalance_threshold = ROTATION_CONFIG.get("rebalance_threshold", 0.10)  # 权重偏离目标的阈值

    # 1. 根据股息率计算 score & 初始权重
    scores = {}
    for name in etfs:
        dy = ETF_CONFIG[name].get("dividend_yield", 0.0)
        scores[name] = max(dy, 0.0)

    total_score = sum(scores.values())
    if total_score <= 0:
        return []

    raw_weights = {name: scores[name] / total_score for name in etfs}

    # 2. 限制在 [min_w, max_w] 范围内，然后归一化
    clipped_weights = {}
    for name, w in raw_weights.items():
        clipped_weights[name] = min(max(w, min_w), max_w)

    clipped_sum = sum(clipped_weights.values())
    weights = {name: clipped_weights[name] / clipped_sum for name in etfs}

    # 3. 根据目标权重 → 目标份额
    target_units = {name: int(round(total_base_units * weights[name])) for name in etfs}
    current_units = {name: ETF_CONFIG[name].get("base_units", 0) for name in etfs}

    # 当前真实权重（基于 base_units 估算）
    current_total_units = sum(current_units.values())
    if current_total_units <= 0:
        return []

    current_weights = {name: current_units[name] / current_total_units for name in etfs}

    # 4. 判断是否需要轮动
    #   若两只都接近目标权重（偏差 < rebalance_threshold/2），则不提示
    need_rebalance = False
    for name in etfs:
        diff_w = abs(current_weights[name] - weights[name])
        if diff_w >= rebalance_threshold / 2:
            need_rebalance = True
            break

    if not need_rebalance:
        return []

    # 5. 计算建议调整份额：
    #   从相对贵（权重>目标）的那只减仓，挪到相对便宜的那只
    #   按 “一进一出” 取最小可行调整
    diffs = {name: target_units[name] - current_units[name] for name in etfs}
    # 正数 = 需要增加, 负数 = 需要减少
    cheap = [name for name in etfs if diffs[name] > 0]
    expensive = [name for name in etfs if diffs[name] < 0]

    if not cheap or not expensive:
        return []

    # 为简单起见，只考虑一对（两只）
    cheap_name = cheap[0]
    expensive_name = expensive[0]

    # 建议调整份额 = 需要加的份额与需要减的份额绝对值的较小值
    suggested_units = min(diffs[cheap_name], -diffs[expensive_name])

    # 为了避免非常小的调整，这里要求至少等于“较大一方 step_units”
    step_units_cheap = int(ETF_CONFIG[cheap_name].get("base_units", 0) * ETF_CONFIG[cheap_name].get("step_pct", 0.01))
    step_units_expensive = int(ETF_CONFIG[expensive_name].get("base_units", 0) * ETF_CONFIG[expensive_name].get("step_pct", 0.01))
    min_units = max(step_units_cheap, step_units_expensive, 1)

    if suggested_units < min_units:
        return []

    now_str = datetime.now().strftime("%Y.%m.%d.%H:%M")

    msg = (
        f"{ROTATION_CONFIG.get('group_name', '轮动策略')} 触发【轮动建议】:\n"
        f"- 运行时间: {now_str}\n"
        f"- 成员ETF: {', '.join(etfs)}\n"
        f"- 当前份额: {expensive_name}={current_units[expensive_name]}，"
        f"{cheap_name}={current_units[cheap_name]}\n"
        f"- 目标份额: {expensive_name}={target_units[expensive_name]}，"
        f"{cheap_name}={target_units[cheap_name]}\n"
        f"- 当前权重: {expensive_name}={current_weights[expensive_name]*100:.1f}%，"
        f"{cheap_name}={current_weights[cheap_name]*100:.1f}%\n"
        f"- 目标权重: {expensive_name}={weights[expensive_name]*100:.1f}%，"
        f"{cheap_name}={weights[cheap_name]*100:.1f}%\n"
        f"- 建议操作: 从减仓约 {suggested_units} 份，"
        f"轮换到加仓 {suggested_units} 份。\n"
        f"  说明：基于当前股息率，{cheap_name} 相对更便宜，"
        f"建议组合向其倾斜。"
    )

    logging.info(
        f"轮动建议生成：从 {expensive_name} 减 {suggested_units} 份，"
        f"加到 {cheap_name}。当前权重: "
        f"{expensive_name}={current_weights[expensive_name]*100:.1f}%，"
        f"{cheap_name}={current_weights[cheap_name]*100:.1f}%；"
        f"目标权重: {expensive_name}={weights[expensive_name]*100:.1f}%，"
        f"{cheap_name}={weights[cheap_name]*100:.1f}%"
    )

    return [msg]

# ========= 主循环 =========

def main_loop():
    state = load_state()
    logging.info("ETF 网格监控脚本启动完成。")

    while True:
        all_messages = []

        # 1. 单标的网格信号
        for name, cfg in ETF_CONFIG.items():
            try:
                msgs = check_signals_for_etf(name, cfg, state)
                all_messages.extend(msgs)
            except Exception:
                logging.exception(f"{name} 检查信号时出错")

        # 2. 轮动策略信号（双 ETF 之间）
        try:
            rotation_msgs = compute_rotation_suggestions()
            all_messages.extend(rotation_msgs)
        except Exception:
            logging.exception("轮动策略计算时出错")

        # 3. 如有任何信号 → 推送
        if all_messages:
            full_msg = "\n\n".join(all_messages)
            send_notification(full_msg)

        save_state(state)
        time.sleep(POLL_INTERVAL_SECONDS)



if __name__ == "__main__":
    main_loop()




