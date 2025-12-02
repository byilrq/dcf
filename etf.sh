#!/usr/bin/env bash

# 自动给脚本加执行权限
chmod +x "$0"

# ========= 基本配置 =========

# 当前脚本所在目录（你是从 /root 运行，那就是 /root）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 所有运行时文件都放在 etf 子目录中，避免把 /root 搞乱
ETF_DIR="$SCRIPT_DIR/etf"

# Python 监控脚本路径
PY_SCRIPT="$ETF_DIR/etf.py"

# Python 命令（如未来用虚拟环境，再改这里）
PYTHON_CMD="python3"

# PID & 日志文件也放在 etf 目录
PID_FILE="$ETF_DIR/etf.pid"
LOG_FILE="$ETF_DIR/etf.log"

# PushPlus 配置也放在 etf 目录
PUSHPLUS_CONF="$ETF_DIR/pushplus.conf"


# ========= 公共函数 =========

ensure_etf_dir() {
    if [ ! -d "$ETF_DIR" ]; then
        echo "创建目录: $ETF_DIR"
        mkdir -p "$ETF_DIR"
    fi
}


add_cron_watchdog() {
    # 每小时整点检查一次 etf.py 是否在跑
    local cron_line="0 * * * * bash $SCRIPT_DIR/etf.sh --cron-check >/dev/null 2>&1"

    # 先删掉旧的同类行，再追加新的，避免重复
    (crontab -l 2>/dev/null | grep -v "etf.sh --cron-check"; echo "$cron_line") | crontab -

    echo "已在 crontab 中添加每小时检查任务。"
}

remove_cron_watchdog() {
    # 删除所有包含 etf.sh --cron-check 的行
    crontab -l 2>/dev/null | grep -v "etf.sh --cron-check" | crontab - 2>/dev/null || true
    echo "已从 crontab 中移除检查任务（如存在）。"
}

cron_check() {
    # 供 cron 调用的检查模式，不进入交互菜单
    ensure_etf_dir

    # 若有 PushPlus 配置，加载
    if [ -f "$PUSHPLUS_CONF" ]; then
        # shellcheck disable=SC1090
        source "$PUSHPLUS_CONF"
    fi

    # 如果有 PID 文件且进程还在，就什么都不做
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ps -p "$PID" > /dev/null 2>&1; then
            # 正常运行
            exit 0
        else
            # PID 文件有，但进程没了，清理掉
            rm -f "$PID_FILE"
        fi
    fi

    # 走到这里说明进程不在运行 → 自动启动一遍
    echo "$(date '+%Y.%m.%d.%H:%M:%S') [cron-check] 检测到 etf.py 未运行，自动重启..." >> "$LOG_FILE"
    nohup "$PYTHON_CMD" "$PY_SCRIPT" >> "$LOG_FILE" 2>&1 &
    NEW_PID=$!
    echo "$NEW_PID" > "$PID_FILE"
    echo "$(date '+%Y.%m.%d.%H:%M:%S') [cron-check] 已重新启动 etf.py，PID=$NEW_PID" >> "$LOG_FILE"
}

start_etf() {
    ensure_etf_dir

    if [ ! -f "$PY_SCRIPT" ]; then
        echo "找不到 $PY_SCRIPT，请先用菜单 3 下载 etf.py。"
        return
    fi

    # 如果有 PushPlus 配置，就加载 Token
    if [ -f "$PUSHPLUS_CONF" ]; then
        # shellcheck disable=SC1090
        source "$PUSHPLUS_CONF"
    else
        echo "提示：未配置 PushPlus Token，脚本只会打印，不会推送。"
    fi

    # 检查是否已有运行中的进程
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ps -p "$PID" > /dev/null 2>&1; then
            echo "etf.py 已在运行中（PID=$PID），如需重启请先选择“停止脚本”。"
            return
        fi
    fi

    echo "启动 etf.py ..."
    nohup "$PYTHON_CMD" "$PY_SCRIPT" >> "$LOG_FILE" 2>&1 &
    NEW_PID=$!
    echo "$NEW_PID" > "$PID_FILE"

    echo "etf.py 已启动，PID=$NEW_PID"
    echo "日志文件：$LOG_FILE"

    # 添加 cron 看门狗
    add_cron_watchdog
}


stop_etf() {
    ensure_etf_dir

    if [ ! -f "$PID_FILE" ]; then
        echo "没有找到 PID 文件，可能 etf.py 未在运行。"
        # 既然都停了，也顺手移除 cron 看门狗
        remove_cron_watchdog
        return
    fi

    PID=$(cat "$PID_FILE")
    if ! ps -p "$PID" > /dev/null 2>&1; then
        echo "PID 文件存在但进程未运行，清理 PID 文件。"
        rm -f "$PID_FILE"
        remove_cron_watchdog
        return
    fi

    echo "正在停止 etf.py (PID=$PID)..."
    kill "$PID"

    sleep 2
    if ps -p "$PID" > /dev/null 2>&1; then
        echo "进程未退出，尝试强制 kill -9..."
        kill -9 "$PID"
    fi

    rm -f "$PID_FILE"
    echo "etf.py 已停止。"

    # 停止时移除 cron 看门狗
    remove_cron_watchdog
}

update_script() {
    ensure_etf_dir

    echo "下载最新 etf.py 到 $ETF_DIR ..."
    wget -N --no-check-certificate \
      https://raw.githubusercontent.com/byilrq/etf/main/etf.py \
      -O "$PY_SCRIPT"

    if [ $? -eq 0 ]; then
        echo "etf.py 已成功更新到最新版本。"
    else
        echo "更新失败，请检查网络或 GitHub 路径。"
    fi
}

config_pushplus() {
    ensure_etf_dir

    echo "当前 PushPlus 配置文件路径：$PUSHPLUS_CONF"

    if [ -f "$PUSHPLUS_CONF" ]; then
        echo "已存在配置文件，当前内容为："
        grep "PUSHPLUS_TOKEN" "$PUSHPLUS_CONF" || echo "(未找到 PUSHPLUS_TOKEN 行)"
    else
        echo "尚未创建 PushPlus 配置文件。"
    fi

    echo
    read -r -p "是否重新设置 PushPlus Token？(y/n): " ans
    case "$ans" in
        y|Y)
            read -r -p "请输入 PushPlus Token（注意不要泄露给他人）: " token
            if [ -z "$token" ]; then
                echo "Token 为空，取消设置。"
                return
            fi

            {
                echo "# 自动生成的 PushPlus 配置"
                echo "export PUSHPLUS_TOKEN=\"$token\""
            } > "$PUSHPLUS_CONF"

            chmod 600 "$PUSHPLUS_CONF"
            echo "已写入 Token 到 $PUSHPLUS_CONF，并设置权限为 600。"
            echo "下次使用菜单 1 启动时，会自动加载该 Token。"
            ;;
        *)
            echo "已取消修改。"
            ;;
    esac
}

# 状态查询含cron 
show_status() {
    ensure_etf_dir
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ps -p "$PID" > /dev/null 2>&1; then
            echo "etf.py 正在运行（PID=$PID）。"
        else
            echo "PID 文件存在，但进程未运行。"
        fi
    else
        echo "etf.py 当前未在运行。"
    fi
    echo "当前cron任务："
    crontab -l 2>/dev/null | grep "etf.sh --cron-check" || echo "无相关cron任务。"
}

# 利润计算
etf_profit() {
    local log_file="${ETF_DIR}/trade_log.csv"

    echo "================================"
    echo "  ETF 网格策略 收益分析"
    echo "  交易流水文件: ${log_file}"
    echo "================================"

    if [[ ! -f "$log_file" ]]; then
        echo "错误：未找到交易流水文件：${log_file}"
        echo "请确认策略已产生日志（trade_log.csv）后再执行分析。"
        return 1
    fi

    python3 - "$log_file" << 'PYCODE'
import csv
import sys
import os
from collections import defaultdict
from datetime import datetime

trade_log_path = sys.argv[1]

# ====== 读取 CSV，并尽量按日期排序 ======
rows = []
with open(trade_log_path, newline='', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    required_cols = {"date", "etf_name", "symbol", "price", "qty", "side", "reason"}
    if not required_cols.issubset(reader.fieldnames or []):
        raise SystemExit(
            f"CSV 字段缺失，至少需要字段：{', '.join(sorted(required_cols))}\n"
            f"当前字段：{reader.fieldnames}"
        )

    for r in reader:
        rows.append(r)

def parse_dt(s):
    # 尝试按常见格式解析日期；失败则返回 None，保持原顺序
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y.%m.%d.%H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None

# 尽可能按日期排序（无效日期的保持原顺序放在前面）
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
class ETFStat:
    def __init__(self, name, symbol):
        self.name = name
        self.symbol = symbol

        self.trade_count = 0
        self.buy_count = 0
        self.sell_count = 0

        self.buy_qty = 0
        self.sell_qty = 0

        self.position = 0      # 当前持仓份额
        self.avg_cost = 0.0    # 当前持仓的加权平均成本

        self.realized_pnl = 0.0
        self.realized_by_reason = defaultdict(float)

    def on_buy(self, price, qty, reason):
        self.trade_count += 1
        self.buy_count += 1
        self.buy_qty += qty

        # 加权平均成本更新
        total_cost_before = self.avg_cost * self.position
        total_cost_after = total_cost_before + price * qty
        self.position += qty
        if self.position > 0:
            self.avg_cost = total_cost_after / self.position
        else:
            self.avg_cost = 0.0

    def on_sell(self, price, qty, reason):
        self.trade_count += 1
        self.sell_count += 1
        self.sell_qty += qty

        # 用当前 avg_cost 估算已实现收益（简化：不做逐笔 FIFO）
        realized = (price - self.avg_cost) * qty
        self.realized_pnl += realized
        self.realized_by_reason[reason or "UNKNOWN"] += realized

        self.position -= qty
        if self.position <= 0:
            self.position = max(self.position, 0)
            self.avg_cost = 0.0

# 每只 ETF 的统计
etf_stats = {}

# ====== 逐行处理交易 ======
for r in ordered_rows:
    try:
        name = r.get("etf_name", "").strip() or "UNKNOWN"
        symbol = r.get("symbol", "").strip() or ""
        price = float(r.get("price", 0.0))
        qty = int(float(r.get("qty", 0)))
        side = (r.get("side") or "").strip().upper()
        reason = (r.get("reason") or "").strip().upper()
    except Exception as e:
        print(f"跳过无法解析的行：{r}，错误：{e}", file=sys.stderr)
        continue

    key = (name, symbol)
    if key not in etf_stats:
        etf_stats[key] = ETFStat(name, symbol)
    stat = etf_stats[key]

    if side == "BUY":
        stat.on_buy(price, qty, reason)
    elif side == "SELL":
        stat.on_sell(price, qty, reason)
    else:
        print(f"警告：未知 side={side}，行数据：{r}", file=sys.stderr)

# ====== 汇总输出 ======
print(f"======== ETF 交易流水分析 ========")
print(f"文件：{os.path.abspath(trade_log_path)}")
print(f"总记录数：{len(ordered_rows)}")
print()

# 总体统计（按“网格 vs 非网格”）
total_realized = 0.0
total_grid_realized = 0.0
total_non_grid_realized = 0.0

def is_grid_reason(reason: str) -> bool:
    \"\"\"网格收益的定义：reason 中包含 BOX_GRID（比如 BOX_GRID_SELL / BOX_GRID_BUY）\"\"\"
    return "BOX_GRID" in reason

for (name, symbol), stat in sorted(etf_stats.items(), key=lambda x: x[0][0]):
    print(f"--- 标的：{name} ({symbol}) ---")
    print(f"成交笔数：{stat.trade_count}  （买入 {stat.buy_count} 笔 / 卖出 {stat.sell_count} 笔）")
    print(f"成交数量：买入 {stat.buy_qty} 份 / 卖出 {stat.sell_qty} 份")
    print(f"当前持仓：{stat.position} 份")

    if stat.position > 0:
        print(f"当前持仓成本（加权平均）：{stat.avg_cost:.4f}")
    else:
        print(f"当前无持仓（或持仓为 0），成本记为 0")

    print(f"已实现收益合计（所有 reason）：{stat.realized_pnl:.2f}")

    # 按 reason 细分
    if stat.realized_by_reason:
        print("已实现收益按原因拆分：")
        for reason, pnl in sorted(stat.realized_by_reason.items(), key=lambda x: -x[1]):
            print(f"  {reason:<20} : {pnl:>10.2f}")
    else:
        print("尚无任何卖出成交记录（未产生已实现收益）")

    # 网格 vs 非网格拆分（仅对 SELL 的 realized 有意义）
    grid_pnl = sum(p for r, p in stat.realized_by_reason.items() if is_grid_reason(r))
    non_grid_pnl = stat.realized_pnl - grid_pnl

    print(f"  其中：")
    print(f"    网格相关已实现收益（reason 含 BOX_GRID）：{grid_pnl:.2f}")
    print(f"    非网格已实现收益：{non_grid_pnl:.2f}")
    print()

    total_realized += stat.realized_pnl
    total_grid_realized += grid_pnl
    total_non_grid_realized += non_grid_pnl

print("======== 全部 ETF 汇总 ========")
print(f"所有标的合计已实现收益：{total_realized:.2f}")
print(f"  其中：网格相关已实现收益：{total_grid_realized:.2f}")
print(f"        非网格已实现收益：{total_non_grid_realized:.2f}")

if abs(total_realized) > 1e-8:
    grid_ratio = total_grid_realized / total_realized * 100
    print(f"  网格收益占全部已实现收益比例：{grid_ratio:.2f}%")
else:
    print("  目前总已实现收益为 0，无法计算网格收益占比。")

print("================================")

PYCODE
}

# ========= 若以 --cron-check 启动，则只做检查后退出 =========

if [ "$1" = "--cron-check" ]; then
    cron_check
    exit 0
fi


show_menu() {
    echo "==============================="
    echo "  ETF 网格监控 管理菜单"
    echo " （管理脚本目录：$SCRIPT_DIR）"
    echo " （运行文件目录：$ETF_DIR）"
    echo "==============================="
    echo "1) 启动脚本"
    echo "2) 停止脚本"
    echo "3) 更新脚本"
    echo "4) PushPlus设置"
    echo "5) 查看运行状态"
    echo "6) 分析收益"
    echo "0) 退出"
    echo "==============================="
}

# ========= 主循环 =========

while true; do
    show_menu
    read -r -p "请选择操作: " choice
    case "$choice" in
        1) start_etf ;;
        2) stop_etf ;;
        3) update_script ;;
        4) config_pushplus ;;
        5) show_status ;;
        6) etf_profit ;;
        0)
            echo "退出管理脚本。"
            exit 0
            ;;
        *)
            echo "无效选项，请重新输入。"
            ;;
    esac

    echo
    read -r -p "按回车键继续..." _
done
