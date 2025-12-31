#!/usr/bin/env bash
set -euo pipefail

# 自动给脚本加执行权限（可保留，也可删除）
chmod +x "$0" >/dev/null 2>&1 || true

# ========= 基本配置 =========

# 当前脚本所在目录（你是从 /root 运行，那就是 /root）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 所有运行时文件都放在 dcf 子目录中，避免把 /root 搞乱
DCF_DIR="$SCRIPT_DIR/dcf"

# Python 监控脚本路径
PY_SCRIPT="$DCF_DIR/dcf.py"

# Python 命令（如未来用虚拟环境，再改这里）
PYTHON_CMD="python3"

# PID & 日志文件也放在 dcf 目录
PID_FILE="$DCF_DIR/dcf.pid"
LOG_FILE="$DCF_DIR/dcf.log"

# PushPlus/Telegram 配置文件（必须放在 dcf 目录）
PUSHPLUS_CONF="$DCF_DIR/push.conf"

# venv 目录（依赖安装优先走 venv）
VENV_DIR="$DCF_DIR/.venv"


# ========= 公共函数 =========

ensure_dcf_dir() {
    if [ ! -d "$DCF_DIR" ]; then
        echo "创建目录: $DCF_DIR"
        mkdir -p "$DCF_DIR"
    fi
}

# ============================================
# 依赖安装/更新（系统依赖 + Python依赖）
# 通过 update_rely() 实现
# ============================================
update_rely() {
    ensure_dcf_dir

    echo "================================="
    echo "开始安装/更新依赖..."
    echo "目标目录: $DCF_DIR"
    echo "================================="

    # 1) 系统依赖：python3 / venv / pip
    echo "[1/3] 安装系统依赖（python3-venv / python3-pip 等）"
    sudo apt update -y
    sudo apt install -y python3 python3-venv python3-pip ca-certificates curl wget

    # 2) 创建/更新虚拟环境
    echo "[2/3] 准备虚拟环境: $VENV_DIR"
    if [ ! -d "$VENV_DIR" ]; then
        "$PYTHON_CMD" -m venv "$VENV_DIR"
    fi

    # shellcheck disable=SC1090
    source "$VENV_DIR/bin/activate"

    python -m pip install -U pip setuptools wheel

    # 3) 安装 Python 依赖（根据你的 dcf.py 代码：requests / pyyaml / json5）
    echo "[3/3] 安装 Python 依赖（requests / pyyaml / json5）"
    pip install -U requests pyyaml json5

    echo "================================="
    echo "依赖安装完成 ✅"
    echo "当前 Python: $(python -V)"
    echo "pip: $(pip -V)"
    echo "================================="

    deactivate || true
}

# ============================================
# 写入/更新 Push 配置文件（push.conf）
# 统一写入到 $PUSHPLUS_CONF
# ============================================
ensure_push_conf_file() {
    ensure_dcf_dir
    if [ ! -f "$PUSHPLUS_CONF" ]; then
        {
            echo "# 自动生成的 Push 配置"
            echo "# 创建时间: $(date '+%Y-%m-%d %H:%M:%S')"
            echo ""
        } > "$PUSHPLUS_CONF"
        chmod 600 "$PUSHPLUS_CONF"
    fi
}

add_cron_watchdog() {
    # 每5分钟检查一次 dcf.py 是否在跑
    local cron_line="*/5 * * * * bash $SCRIPT_DIR/dcf.sh --cron-check >/dev/null 2>&1"

    # 先删掉旧的同类行，再追加新的，避免重复
    (crontab -l 2>/dev/null | grep -v "dcf.sh --cron-check" || true; echo "$cron_line") | crontab -

    echo "已在 crontab 中添加每5分钟检查任务。"
}

remove_cron_watchdog() {
    # 删除所有包含 dcf.sh --cron-check 的行
    (crontab -l 2>/dev/null | grep -v "dcf.sh --cron-check" || true) | crontab - 2>/dev/null || true
    echo "已从 crontab 中移除检查任务（如存在）。"
}

# ============================================
# 防止重复运行（与 pushplus.sh 一致：pidof）
# ============================================
cron_check() {
    # 供 cron 调用的检查模式，不进入交互菜单
    ensure_dcf_dir

    # 若有 PushPlus 配置，加载
    if [ -f "$PUSHPLUS_CONF" ]; then
        # shellcheck disable=SC1090
        source "$PUSHPLUS_CONF"
    fi

    # 如果有 PID 文件且进程还在，就什么都不做
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
        if [ -n "${PID}" ] && ps -p "$PID" > /dev/null 2>&1; then
            exit 0
        else
            rm -f "$PID_FILE"
        fi
    fi

    echo "$(date '+%Y.%m.%d.%H:%M:%S') [cron-check] 检测到 dcf.py 未运行，自动重启..." >> "$LOG_FILE"

    # 如果有 venv，就用 venv 的 python，否则用系统 python3
    if [ -x "$VENV_DIR/bin/python" ]; then
        nohup "$VENV_DIR/bin/python" "$PY_SCRIPT" >> "$LOG_FILE" 2>&1 &
    else
        nohup "$PYTHON_CMD" "$PY_SCRIPT" >> "$LOG_FILE" 2>&1 &
    fi

    NEW_PID=$!
    echo "$NEW_PID" > "$PID_FILE"
    echo "$(date '+%Y.%m.%d.%H:%M:%S') [cron-check] 已重新启动 dcf.py，PID=$NEW_PID" >> "$LOG_FILE"
}

# ============================================
# 启动脚本（nohup + PID + cron 看门狗）
# ============================================
start_dcf() {
    ensure_dcf_dir

    if [ ! -f "$PY_SCRIPT" ]; then
        echo "找不到 $PY_SCRIPT，请先用菜单 3 安装依赖，并用菜单下载/更新 dcf.py。"
        return
    fi

    # 如果有 Push 配置，就加载 Token
    if [ -f "$PUSHPLUS_CONF" ]; then
        # shellcheck disable=SC1090
        source "$PUSHPLUS_CONF"
    else
        echo "提示：未配置 push.conf，脚本只会写日志，不会推送。"
        echo "你可以用菜单 4 配置推送。"
    fi

    # 检查是否已有运行中的进程
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
        if [ -n "${PID}" ] && ps -p "$PID" > /dev/null 2>&1; then
            echo "dcf.py 已在运行中（PID=$PID），如需重启请先选择“停止脚本”。"
            return
        fi
    fi

    echo "启动 dcf.py ..."
    echo "日志文件：$LOG_FILE"

    # 优先使用 venv python
    if [ -x "$VENV_DIR/bin/python" ]; then
        nohup "$VENV_DIR/bin/python" "$PY_SCRIPT" >> "$LOG_FILE" 2>&1 &
    else
        echo "提示：未检测到虚拟环境 $VENV_DIR，建议先执行菜单 3 安装依赖。"
        nohup "$PYTHON_CMD" "$PY_SCRIPT" >> "$LOG_FILE" 2>&1 &
    fi

    NEW_PID=$!
    echo "$NEW_PID" > "$PID_FILE"

    echo "dcf.py 已启动，PID=$NEW_PID"

    # 添加 cron 看门狗
    add_cron_watchdog
}

# ============================================
# 停止脚本（kill + 清理 PID + 移除 cron）
# ============================================
stop_dcf() {
    ensure_dcf_dir

    if [ ! -f "$PID_FILE" ]; then
        echo "没有找到 PID 文件，可能 dcf.py 未在运行。"
        remove_cron_watchdog
        return
    fi

    PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [ -z "${PID}" ] || ! ps -p "$PID" > /dev/null 2>&1; then
        echo "PID 文件存在但进程未运行，清理 PID 文件。"
        rm -f "$PID_FILE"
        remove_cron_watchdog
        return
    fi

    echo "正在停止 dcf.py (PID=$PID)..."
    kill "$PID" || true

    sleep 2
    if ps -p "$PID" > /dev/null 2>&1; then
        echo "进程未退出，尝试强制 kill -9..."
        kill -9 "$PID" || true
    fi

    rm -f "$PID_FILE"
    echo "dcf.py 已停止。"

    remove_cron_watchdog
}

# ============================================
# 更新 dcf.py（从 GitHub 拉取）
# ============================================
update_script() {
    ensure_dcf_dir

    echo "下载最新 dcf.py 到 $DCF_DIR ..."
    wget -N --no-check-certificate \
      https://raw.githubusercontent.com/byilrq/dcf/main/dcf.py \
      -O "$PY_SCRIPT"

    if [ $? -eq 0 ]; then
        echo "dcf.py 已成功更新到最新版本。"
    else
        echo "更新失败，请检查网络或 GitHub 路径。"
    fi
}

# ============================================
# 推送设置入口（PushPlus & Telegram）
# 修复：统一使用 $PUSHPLUS_CONF
# ============================================
config_push() {
    ensure_dcf_dir
    ensure_push_conf_file

    echo "当前 Push 配置文件路径：$PUSHPLUS_CONF"
    echo "----------------------------------------"
    if grep -q "^export PUSHPLUS_TOKEN=" "$PUSHPLUS_CONF"; then
        echo "PUSHPLUS_TOKEN: 已配置"
    else
        echo "PUSHPLUS_TOKEN: (未配置)"
    fi
    if grep -q "^export TELEGRAM_BOT_TOKEN=" "$PUSHPLUS_CONF"; then
        echo "TELEGRAM_BOT_TOKEN: 已配置"
    else
        echo "TELEGRAM_BOT_TOKEN: (未配置)"
    fi
    if grep -q "^export TELEGRAM_CHAT_ID=" "$PUSHPLUS_CONF"; then
        echo "TELEGRAM_CHAT_ID: 已配置"
    else
        echo "TELEGRAM_CHAT_ID: (未配置)"
    fi
    echo "----------------------------------------"
    echo
    echo "请选择要配置/测试的推送方式："
    echo "1) 配置 PushPlus"
    echo "2) 配置 Telegram"
    echo "3) 两者都配置"
    echo "4) 发送测试消息到 PushPlus"
    echo "5) 发送测试消息到 Telegram"
    echo "6) 退出"

    read -r -p "请选择 [1-6]: " choice
    echo

    case "$choice" in
        1) config_pushplus ;;
        2) config_telegram ;;
        3) config_pushplus; echo; config_telegram ;;
        4) test_pushplus ;;
        5) test_telegram ;;
        6) echo "已取消修改。"; return ;;
        *) echo "无效的选择，已取消。"; return ;;
    esac
}

# ============================================
# 配置 PushPlus
# ============================================
config_pushplus() {
    ensure_push_conf_file

    echo "=== 配置 PushPlus ==="
    local current_token=""
    if grep -q "^export PUSHPLUS_TOKEN=" "$PUSHPLUS_CONF"; then
        current_token=$(grep "^export PUSHPLUS_TOKEN=" "$PUSHPLUS_CONF" | head -1 | cut -d'"' -f2)
        echo "当前 PushPlus Token: ${current_token:0:8}****"
    else
        echo "当前 PushPlus Token: (未配置)"
    fi

    read -r -p "是否设置 PushPlus Token？(y/n): " ans
    case "$ans" in
        y|Y)
            read -r -p "请输入 PushPlus Token（注意不要泄露给他人）: " token
            if [ -z "$token" ]; then
                echo "Token 为空，取消设置。"
                return
            fi

            # 删除旧的配置行
            sed -i '/^export PUSHPLUS_TOKEN=/d' "$PUSHPLUS_CONF"
            echo "export PUSHPLUS_TOKEN=\"$token\"" >> "$PUSHPLUS_CONF"
            echo "PushPlus Token 已更新。"
            ;;
        *)
            echo "已跳过 PushPlus 配置。"
            ;;
    esac
}

# ============================================
# 配置 Telegram
# ============================================
config_telegram() {
    ensure_push_conf_file

    echo "=== 配置 Telegram ==="
    local current_bot_token=""
    local current_chat_id=""

    if grep -q "^export TELEGRAM_BOT_TOKEN=" "$PUSHPLUS_CONF"; then
        current_bot_token=$(grep "^export TELEGRAM_BOT_TOKEN=" "$PUSHPLUS_CONF" | head -1 | cut -d'"' -f2)
        echo "当前 Telegram Bot Token: ${current_bot_token:0:8}****"
    else
        echo "当前 Telegram Bot Token: (未配置)"
    fi

    if grep -q "^export TELEGRAM_CHAT_ID=" "$PUSHPLUS_CONF"; then
        current_chat_id=$(grep "^export TELEGRAM_CHAT_ID=" "$PUSHPLUS_CONF" | head -1 | cut -d'"' -f2)
        echo "当前 Telegram Chat ID: $current_chat_id"
    else
        echo "当前 Telegram Chat ID: (未配置)"
    fi

    read -r -p "是否设置 Telegram 配置？(y/n): " ans
    case "$ans" in
        y|Y)
            read -r -p "请输入 Telegram Bot Token: " bot_token
            if [ -z "$bot_token" ]; then
                echo "Bot Token 为空，取消设置。"
                return
            fi

            read -r -p "请输入 Telegram Chat ID: " chat_id
            if [ -z "$chat_id" ]; then
                echo "Chat ID 为空，取消设置。"
                return
            fi

            sed -i '/^export TELEGRAM_BOT_TOKEN=/d' "$PUSHPLUS_CONF"
            sed -i '/^export TELEGRAM_CHAT_ID=/d' "$PUSHPLUS_CONF"

            echo "export TELEGRAM_BOT_TOKEN=\"$bot_token\"" >> "$PUSHPLUS_CONF"
            echo "export TELEGRAM_CHAT_ID=\"$chat_id\"" >> "$PUSHPLUS_CONF"

            echo "Telegram 配置已更新。"
            echo "提示：请确保你已经给 Bot 发过消息/或把 Bot 加入群组并说过话，否则可能收不到推送。"
            ;;
        *)
            echo "已跳过 Telegram 配置。"
            ;;
    esac
}
# ============================================
# 发送测试消息到 PushPlus
# ============================================
test_pushplus() {
    ensure_dcf_dir
    ensure_push_conf_file

    if ! command -v curl >/dev/null 2>&1; then
        echo "错误：未安装 curl，无法发送测试消息。"
        return 1
    fi

    # shellcheck disable=SC1090
    source "$PUSHPLUS_CONF" 2>/dev/null || true

    if [[ -z "${PUSHPLUS_TOKEN:-}" ]]; then
        echo "PUSHPLUS_TOKEN 未配置，请先在菜单中配置 PushPlus。"
        return 1
    fi

    local title="DCF 测试 PushPlus"
    local content="PushPlus 测试消息发送成功 ✅\n时间：$(date '+%Y-%m-%d %H:%M:%S')\n主机：$(hostname)\n"

    echo "正在发送 PushPlus 测试消息..."
    local resp
    resp="$(curl -sS --max-time 10 \
        -X POST "http://www.pushplus.plus/send" \
        -d "token=${PUSHPLUS_TOKEN}" \
        --data-urlencode "title=${title}" \
        --data-urlencode "content=${content}" \
        -d "template=txt" || true)"

    # 不依赖 jq，用 grep 判断是否成功（兼容 code=0 或 code=200）
    if echo "$resp" | grep -Eq '"code"[[:space:]]*:[[:space:]]*(0|200)'; then
        echo "PushPlus 测试消息发送成功。"
        return 0
    fi

    echo "PushPlus 测试消息可能发送失败，返回："
    echo "$resp"
    return 1
}

# ============================================
# 发送测试消息到 Telegram
# ============================================
test_telegram() {
    ensure_dcf_dir
    ensure_push_conf_file

    if ! command -v curl >/dev/null 2>&1; then
        echo "错误：未安装 curl，无法发送测试消息。"
        return 1
    fi

    # shellcheck disable=SC1090
    source "$PUSHPLUS_CONF" 2>/dev/null || true

    if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
        echo "Telegram 未配置完整：需要 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID"
        return 1
    fi

    local text
    text=$'DCF Telegram 测试消息发送成功 ✅\n'
    text+="时间：$(date '+%Y-%m-%d %H:%M:%S')"$'\n'
    text+="主机：$(hostname)"$'\n'

    echo "正在发送 Telegram 测试消息..."
    local url="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"

    local resp
    resp="$(curl -sS --max-time 10 \
        -X POST "$url" \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${text}" \
        -d "disable_web_page_preview=true" || true)"

    # 成功：{"ok":true,...}
    if echo "$resp" | grep -Eq '"ok"[[:space:]]*:[[:space:]]*true'; then
        echo "Telegram 测试消息发送成功。"
        return 0
    fi

    echo "Telegram 测试消息可能发送失败，返回："
    echo "$resp"
    return 1
}



# ============================================
# 状态查询（含 cron）
# ============================================
show_status() {
    ensure_dcf_dir
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
        if [ -n "${PID}" ] && ps -p "$PID" > /dev/null 2>&1; then
            echo "dcf.py 正在运行（PID=$PID）。"
        else
            echo "PID 文件存在，但进程未运行。"
        fi
    else
        echo "dcf.py 当前未在运行。"
    fi
    echo "当前cron任务："
    crontab -l 2>/dev/null | grep "dcf.sh --cron-check" || echo "无相关cron任务。"
}

# ===============================================================
#                          利润计算函数
# ===============================================================
dcf_profit() {
    local log_file="${DCF_DIR}/trade_log.csv"
    local state_file="${DCF_DIR}/dcf_monitor_state.json"
    local config_file="${DCF_DIR}/dcf.conf"

    echo "================================="
    echo "  策略收益分析"
    echo "  交易流水文件: ${log_file}"
    echo "================================="

    if [[ ! -f "$log_file" ]]; then
        echo "错误：未找到交易流水文件：${log_file}"
        return 1
    fi

    python3 - "$log_file" "$state_file" "$config_file" << 'PYCODE'
import csv
import sys
import os
import json
from collections import defaultdict
from datetime import datetime, timedelta
import math

trade_log_path = sys.argv[1]
state_file_path = sys.argv[2]
config_file_path = sys.argv[3]

# ====== 读取数据 ======
config = {}
if os.path.exists(config_file_path):
    try:
        with open(config_file_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except Exception as e:
        print(f"警告：读取配置文件失败: {e}")

state = {}
if os.path.exists(state_file_path):
    try:
        with open(state_file_path, 'r', encoding='utf-8') as f:
            state = json.load(f)
    except Exception as e:
        print(f"警告：读取状态文件失败: {e}")

# ====== 读取交易记录 ======
rows = []
with open(trade_log_path, newline='', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    required_cols = {"date", "dcf_name", "symbol", "price", "qty", "side", "reason", 
                     "zone", "pos_before", "pos_after", "avg_cost_before", "avg_cost_after"}
    
    if not required_cols.issubset(reader.fieldnames or []):
        print(f"错误：交易记录文件缺少必要字段")
        print(f"需要的字段：{', '.join(sorted(required_cols))}")
        print(f"当前字段：{reader.fieldnames}")
        print("请先运行修复后的策略程序生成完整的交易记录")
        sys.exit(1)
    
    for r in reader:
        rows.append(r)

def parse_dt(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y.%m.%d.%H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None

# 按日期排序
rows_with_dt = []
rows_no_dt = []
for r in rows:
    dt_obj = parse_dt(r.get("date", ""))
    if dt_obj is None:
        rows_no_dt.append(r)
    else:
        rows_with_dt.append((dt_obj, r))

rows_with_dt.sort(key=lambda x: x[0])
ordered_rows = rows_no_dt + [r for _, r in rows_with_dt]

# ====== 分析逻辑 ======
class DCFStat:
    def __init__(self, name, symbol):
        self.name = name
        self.symbol = symbol
        
        self.trade_count = 0
        self.buy_count = 0
        self.sell_count = 0
        
        self.buy_qty = 0
        self.sell_qty = 0
        self.buy_amount = 0.0
        self.sell_amount = 0.0
        
        self.position = 0
        self.avg_cost = 0.0
        self.total_investment = 0.0  # 总投入资金
        
        self.realized_pnl = 0.0
        self.realized_by_type = defaultdict(float)
        self.realized_by_zone = defaultdict(float)
        
        self.trades = []
        self.first_trade_date = None
        self.last_trade_date = None
    
    def process_trade(self, row):
        try:
            date_str = row.get("date", "")
            price = float(row.get("price", 0))
            qty = int(float(row.get("qty", 0)))
            side = row.get("side", "").upper()
            reason = row.get("reason", "").upper()
            zone = row.get("zone", "")
            pos_before = int(float(row.get("pos_before", 0))) if row.get("pos_before") else 0
            pos_after = int(float(row.get("pos_after", 0))) if row.get("pos_after") else 0
            avg_cost_before = float(row.get("avg_cost_before", 0)) if row.get("avg_cost_before") else 0
            avg_cost_after = float(row.get("avg_cost_after", 0)) if row.get("avg_cost_after") else 0
            
            dt = parse_dt(date_str)
            if dt:
                if self.first_trade_date is None or dt < self.first_trade_date:
                    self.first_trade_date = dt
                if self.last_trade_date is None or dt > self.last_trade_date:
                    self.last_trade_date = dt
            
            self.trade_count += 1
            
            if side == "BUY":
                self.buy_count += 1
                self.buy_qty += qty
                amount = price * qty
                self.buy_amount += amount
                self.total_investment += amount
                
                # 更新持仓
                self.position = pos_after
                self.avg_cost = avg_cost_after if avg_cost_after > 0 else self.avg_cost
                
                self.trades.append({
                    'date': dt or date_str,
                    'type': 'BUY',
                    'price': price,
                    'qty': qty,
                    'amount': amount,
                    'reason': reason,
                    'zone': zone,
                    'position_after': pos_after,
                    'avg_cost_after': avg_cost_after
                })
                
            elif side == "SELL":
                self.sell_count += 1
                self.sell_qty += qty
                amount = price * qty
                self.sell_amount += amount
                
                # 计算已实现收益
                realized_pnl = 0
                if avg_cost_before > 0:
                    realized_pnl = (price - avg_cost_before) * qty
                
                self.realized_pnl += realized_pnl
                
                # 按交易类型分类
                if "BOX_GRID" in reason:
                    self.realized_by_type['网格收益'] += realized_pnl
                elif "ABOVE_120" in reason:
                    self.realized_by_type['趋势收益'] += realized_pnl
                elif "BETWEEN_300_150" in reason or "PYRAMID" in reason:
                    self.realized_by_type['底仓收益'] += realized_pnl
                else:
                    self.realized_by_type['其他收益'] += realized_pnl
                
                self.realized_by_zone[zone] += realized_pnl
                
                # 更新持仓
                self.position = pos_after
                self.avg_cost = avg_cost_after if avg_cost_after > 0 else self.avg_cost
                
                self.trades.append({
                    'date': dt or date_str,
                    'type': 'SELL',
                    'price': price,
                    'qty': qty,
                    'amount': amount,
                    'realized_pnl': realized_pnl,
                    'reason': reason,
                    'zone': zone,
                    'position_after': pos_after,
                    'avg_cost_after': avg_cost_after
                })
                
        except Exception as e:
            print(f"处理交易记录失败: {row}, 错误: {e}")
    
    def get_trading_days(self):
        if self.first_trade_date and self.last_trade_date:
            return (self.last_trade_date - self.first_trade_date).days + 1
        return 0
    
    def get_annualized_return(self):
        """计算年化收益率（修正版）"""
        if self.total_investment == 0 or self.get_trading_days() < 1:
            return 0.0
        
        total_return = self.realized_pnl / self.total_investment
        years = self.get_trading_days() / 365.0
        
        if years > 0 and total_return > -1:
            return ((1 + total_return) ** (1 / years) - 1) * 100
        return 0.0
    
    def get_current_value(self, current_price=0):
        """计算当前持仓价值"""
        if current_price > 0:
            return self.position * current_price
        elif self.avg_cost > 0:
            return self.position * self.avg_cost
        return 0.0
    
    def get_floating_pnl(self, current_price=0):
        """计算浮动盈亏"""
        if current_price > 0 and self.position > 0 and self.avg_cost > 0:
            return (current_price - self.avg_cost) * self.position
        return 0.0

# ====== 处理所有交易 ======
dcf_stats = {}
for row in ordered_rows:
    try:
        name = row.get("dcf_name", "").strip() or "UNKNOWN"
        symbol = row.get("symbol", "").strip() or ""
        
        key = (name, symbol)
        if key not in dcf_stats:
            dcf_stats[key] = DCFStat(name, symbol)
        
        dcf_stats[key].process_trade(row)
        
    except Exception as e:
        print(f"跳过无法处理的记录: {row}, 错误: {e}")

# ====== 输出分析结果 ======
print(f"\n{'='*60}")
print("DCF 策略收益分析（基于完整交易记录）")
print(f"{'='*60}")

if not dcf_stats:
    print("未找到有效的交易记录")
    sys.exit(0)

total_realized = 0.0
total_investment = 0.0
total_current_value = 0.0
total_profit_by_type = defaultdict(float)

# 获取当前价格信息
current_prices = {}
if state:
    for dcf_name, dcf_state in state.items():
        if dcf_name == "_meta":
            continue
        current_prices[dcf_name] = dcf_state.get('last_price', 0)

for (name, symbol), stat in sorted(dcf_stats.items(), key=lambda x: x[0][0]):
    print(f"\n{'='*50}")
    print(f"标的: {name} ({symbol})")
    print(f"{'-'*50}")
    
    # 基本信息
    print(f"交易统计:")
    print(f"  总交易笔数: {stat.trade_count:6d}  (买入: {stat.buy_count:3d} / 卖出: {stat.sell_count:3d})")
    print(f"  交易数量: 买入 {stat.buy_qty:10,d} 股 / 卖出 {stat.sell_qty:10,d} 股")
    print(f"  交易金额: 买入 ¥{stat.buy_amount:12,.2f} / 卖出 ¥{stat.sell_amount:12,.2f}")
    print(f"  总投入资金: ¥{stat.total_investment:12,.2f}")
    
    if stat.first_trade_date:
        days = stat.get_trading_days()
        print(f"  首笔交易: {stat.first_trade_date.strftime('%Y-%m-%d')}  (运行 {days} 天)")
    
    # 持仓信息
    print(f"\n持仓信息:")
    print(f"  当前持仓: {stat.position:10,d} 股")
    print(f"  持仓成本: ¥{stat.avg_cost:10.4f} / 股")
    
    # 当前价格和浮动盈亏
    current_price = current_prices.get(name, stat.avg_cost)
    current_value = stat.get_current_value(current_price)
    floating_pnl = stat.get_floating_pnl(current_price)
    
    if current_price > 0:
        print(f"  当前价格: ¥{current_price:10.4f} / 股")
        print(f"  持仓市值: ¥{current_value:12,.2f}")
        if stat.avg_cost > 0:
            pnl_pct = (current_price / stat.avg_cost - 1) * 100
            print(f"  浮动盈亏: ¥{floating_pnl:12,.2f} ({pnl_pct:+.2f}%)")
    
    # 收益分析
    print(f"\n收益分析:")
    print(f"  已实现收益: ¥{stat.realized_pnl:12,.2f}")
    
    if stat.total_investment > 0:
        return_pct = (stat.realized_pnl / stat.total_investment) * 100
        print(f"  收益率: {return_pct:+.2f}%")
    
    # 按类型分解收益
    if stat.realized_by_type:
        print(f"  收益分解:")
        for profit_type, amount in sorted(stat.realized_by_type.items()):
            if amount != 0:
                pct = (amount / stat.realized_pnl * 100) if stat.realized_pnl != 0 else 0
                print(f"    {profit_type:<8}: ¥{amount:12,.2f} ({pct:5.1f}%)")
    
    # 年化收益率
    annualized_return = stat.get_annualized_return()
    if abs(annualized_return) < 100000:  # 过滤异常值
        print(f"  年化收益率: {annualized_return:+.2f}%")
    
    # 交易质量
    if stat.sell_count > 0:
        avg_profit = stat.realized_pnl / stat.sell_count
        print(f"  平均每笔卖出盈利: ¥{avg_profit:10,.2f}")
    
    # 累计到总计
    total_realized += stat.realized_pnl
    total_investment += stat.total_investment
    total_current_value += current_value
    
    for profit_type, amount in stat.realized_by_type.items():
        total_profit_by_type[profit_type] += amount

# 总体汇总
print(f"\n{'='*60}")
print("总体汇总")
print(f"{'='*60}")

print(f"总投入资金: ¥{total_investment:,.2f}")
print(f"总已实现收益: ¥{total_realized:,.2f}")

if total_investment > 0:
    total_return_pct = (total_realized / total_investment) * 100
    print(f"总收益率: {total_return_pct:+.2f}%")

# 收益构成
if total_realized != 0:
    print(f"收益构成:")
    for profit_type in ['底仓收益', '网格收益', '趋势收益', '其他收益']:
        amount = total_profit_by_type.get(profit_type, 0)
        pct = (amount / total_realized) * 100
        print(f"  {profit_type:<6}: ¥{amount:12,.2f} ({pct:5.1f}%)")

# 总资产
total_assets = total_current_value + total_realized
print(f"\n总资产状况:")
print(f"  当前持仓市值: ¥{total_current_value:,.2f}")
print(f"  已实现收益: ¥{total_realized:,.2f}")
print(f"  总资产: ¥{total_assets:,.2f}")

if total_investment > 0:
    total_return_all = (total_assets / total_investment - 1) * 100
    print(f"  综合收益率: {total_return_all:+.2f}%")

# 计算总体年化收益率（简化）
first_date = min(s.first_trade_date for s in dcf_stats.values() if s.first_trade_date)
last_date = max(s.last_trade_date for s in dcf_stats.values() if s.last_trade_date)

if first_date and last_date:
    total_days = (last_date - first_date).days + 1
    total_years = total_days / 365.0
    
    if total_years > 0.01 and total_investment > 0:  # 至少运行3.65天
        total_return = total_realized / total_investment
        annualized_return = ((1 + total_return) ** (1 / total_years) - 1) * 100
        if abs(annualized_return) < 1000:  # 过滤异常值
            print(f"\n总体年化收益率: {annualized_return:+.2f}%")
            print(f"运行时间: {total_days} 天 ({total_years:.2f} 年)")

print(f"\n{'='*60}")
print("说明:")
print("1. 底仓收益: 在MA300-MA150区间建立底仓和加仓的收益")
print("2. 网格收益: 在箱体区(MA150-MA150*1.2)网格交易的收益")
print("3. 趋势收益: 在强势区(>MA150*1.2)减仓的收益")
print("4. 收益率基于完整交易记录计算，考虑了持仓成本")
print("5. 年化收益率已过滤异常值，避免数据失真")
print(f"{'='*60}")

PYCODE
}

# =================设置时区 =============
change_tz(){
    sudo timedatectl set-timezone Asia/Shanghai
    echo "系统时区已经改为Asia/Shanghai"
    timedatectl
}

# ========= 若以 --cron-check 启动，则只做检查后退出 =========
if [ "${1:-}" = "--cron-check" ]; then
    cron_check
    exit 0
fi
# ================历史回测 =============
dcf_backtest() {
    echo "=== 单标的回测 ==="
    read -r -p "请输入股票代码（如 SH000001 / SZ000001 / HK00700）: " symbol
    symbol="$(echo "$symbol" | tr -d ' ' | tr '[:lower:]' '[:upper:]')"

    read -r -p "请输入回测天数（例如 800；直接回车默认 800）: " days
    if [[ -z "$days" ]]; then
        days=800
    fi

    if ! [[ "$days" =~ ^[0-9]+$ ]]; then
        echo "❌ 回测天数必须是整数"
        return 1
    fi

    cd "$DCF_DIR" || return 1

    if [[ ! -f "dcf.yaml" ]]; then
        echo "❌ 未找到 dcf.yaml（目录：$DCF_DIR）"
        return 1
    fi

    if [[ ! -f "backtest_dcf.py" ]]; then
        echo "❌ 未找到 backtest_dcf.py（请把我给你的回测脚本放到 $DCF_DIR）"
        return 1
    fi

    echo "🚀 开始回测：$symbol，天数：$days"
    python3 backtest_dcf.py --config "./dcf.yaml" --symbol "$symbol" --days "$days"
}

show_menu() {
    echo "==============================="
    echo "  DCF 网格监控 管理菜单"
    echo " （管理脚本目录：$SCRIPT_DIR）"
    echo " （运行文件目录：$DCF_DIR）"
    echo "==============================="
    echo "1) 启动脚本"
    echo "2) 停止脚本"
    echo "3) 安装/更新依赖"
    echo "4) Push设置"
    echo "5) 查看运行状态"
    echo "6) 分析收益"
    echo "7) 设置上海时区"
    echo "8) 更新 dcf.py（从GitHub）"
    echo "9) 单标的回测（输入代码+天数）"
    echo "0) 退出"
    echo "==============================="
}

# ========= 主循环 =========
while true; do
    show_menu
    read -r -p "请选择操作: " choice
    case "$choice" in
        1) start_dcf ;;
        2) stop_dcf ;;
        3) update_rely ;;
        4) config_push ;;
        5) show_status ;;
        6) dcf_profit ;;
        7) change_tz ;;
        8) update_script ;;
        9) dcf_backtest ;;
        0)
            echo "退出管理脚本。"
            exit 0
            ;;
        *)
            echo "无效选项，请重新输入。"
            ;;
    esac
done

