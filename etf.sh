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

# ===============================================================
#                          利润计算函数
# ===============================================================
etf_profit() {
    local log_file="${ETF_DIR}/trade_log.csv"
    local state_file="${ETF_DIR}/etf_monitor_state.json"
    local config_file="${ETF_DIR}/etf.conf"

    echo "================================="
    echo "  ETF 策略收益分析（修复版）"
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
    required_cols = {"date", "etf_name", "symbol", "price", "qty", "side", "reason", 
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
class ETFStat:
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
etf_stats = {}
for row in ordered_rows:
    try:
        name = row.get("etf_name", "").strip() or "UNKNOWN"
        symbol = row.get("symbol", "").strip() or ""
        
        key = (name, symbol)
        if key not in etf_stats:
            etf_stats[key] = ETFStat(name, symbol)
        
        etf_stats[key].process_trade(row)
        
    except Exception as e:
        print(f"跳过无法处理的记录: {row}, 错误: {e}")

# ====== 输出分析结果 ======
print(f"\n{'='*60}")
print("ETF 策略收益分析（基于完整交易记录）")
print(f"{'='*60}")

if not etf_stats:
    print("未找到有效的交易记录")
    sys.exit(0)

total_realized = 0.0
total_investment = 0.0
total_current_value = 0.0
total_profit_by_type = defaultdict(float)

# 获取当前价格信息
current_prices = {}
if state:
    for etf_name, etf_state in state.items():
        if etf_name == "_meta":
            continue
        current_prices[etf_name] = etf_state.get('last_price', 0)

for (name, symbol), stat in sorted(etf_stats.items(), key=lambda x: x[0][0]):
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
first_date = min(s.first_trade_date for s in etf_stats.values() if s.first_trade_date)
last_date = max(s.last_trade_date for s in etf_stats.values() if s.last_trade_date)

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

# =================脚本cron没小时触发自动重启部分 =============

if [ "$1" = "--cron-check" ]; then
    cron_check
    exit 0
fi
# ========= 若以 --cron-check 启动，则只做检查后退出 =========

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
