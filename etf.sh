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

# 利润计算函数
etf_profit() {
    local log_file="${ETF_DIR}/trade_log.csv"
    local state_file="${ETF_DIR}/etf_monitor_state.json"
    local config_file="${ETF_DIR}/etf.conf"

    echo "================================="
    echo "  ETF 策略收益分析"
    echo "  交易流水文件: ${log_file}"
    echo "  状态文件: ${state_file}"
    echo "================================="

    if [[ ! -f "$log_file" ]]; then
        echo "错误：未找到交易流水文件：${log_file}"
        echo "请确认策略已产生日志（trade_log.csv）后再执行分析。"
        return 1
    fi

    if [[ ! -f "$state_file" ]]; then
        echo "警告：未找到状态文件：${state_file}"
        echo "将仅基于交易流水进行分析。"
    fi

    if [[ ! -f "$config_file" ]]; then
        echo "警告：未找到配置文件：${config_file}"
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

# ====== 读取配置文件 ======
config = {}
if os.path.exists(config_file_path):
    try:
        with open(config_file_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except Exception as e:
        print(f"警告：读取配置文件失败: {e}")

# ====== 读取状态文件 ======
state = {}
if os.path.exists(state_file_path):
    try:
        with open(state_file_path, 'r', encoding='utf-8') as f:
            state = json.load(f)
    except Exception as e:
        print(f"警告：读取状态文件失败: {e}")

# ====== 读取 CSV，并尽量按日期排序 ======
rows = []
with open(trade_log_path, newline='', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    required_cols = {"date", "etf_name", "symbol", "price", "qty", "side", "reason", "zone", "pos_after"}
    if not required_cols.issubset(reader.fieldnames or []):
        print(f"CSV 字段缺失，至少需要字段：{', '.join(sorted(required_cols))}")
        print(f"当前字段：{reader.fieldnames}")
        sys.exit(1)

    for r in reader:
        rows.append(r)

def parse_dt(s):
    # 尝试按常见格式解析日期
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y.%m.%d.%H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None

# 尽可能按日期排序
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
        
        # 交易统计
        self.trade_count = 0
        self.buy_count = 0
        self.sell_count = 0
        
        # 数量统计
        self.buy_qty = 0
        self.sell_qty = 0
        
        # 资金统计
        self.buy_amount = 0.0
        self.sell_amount = 0.0
        
        # 持仓和成本
        self.position = 0
        self.position_history = []  # (datetime, position)
        self.cost_history = []  # (datetime, avg_cost)
        self.avg_cost = 0.0
        self.total_cost = 0.0  # 持仓总成本
        
        # 收益统计
        self.realized_pnl = 0.0
        self.realized_by_reason = defaultdict(float)
        self.realized_by_zone = defaultdict(float)
        
        # 交易记录
        self.trades = []
        
        # 时间段统计
        self.first_trade_date = None
        self.last_trade_date = None
    
    def on_trade(self, date_str, price, qty, side, reason, zone):
        # 解析日期
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
            self.buy_amount += price * qty
            
            # 更新持仓成本（加权平均法）
            total_cost_before = self.total_cost
            new_cost = price * qty
            self.total_cost = total_cost_before + new_cost
            self.position += qty
            
            if self.position > 0:
                self.avg_cost = self.total_cost / self.position
            
            # 记录交易
            self.trades.append({
                'date': dt or date_str,
                'type': 'BUY',
                'price': price,
                'qty': qty,
                'amount': price * qty,
                'reason': reason,
                'zone': zone,
                'position_after': self.position,
                'avg_cost_after': self.avg_cost
            })
            
        elif side == "SELL":
            self.sell_count += 1
            self.sell_qty += qty
            sell_amount = price * qty
            self.sell_amount += sell_amount
            
            # 计算已实现收益
            realized_pnl = (price - self.avg_cost) * qty
            self.realized_pnl += realized_pnl
            self.realized_by_reason[reason or "UNKNOWN"] += realized_pnl
            self.realized_by_zone[zone or "UNKNOWN"] += realized_pnl
            
            # 更新持仓
            self.position -= qty
            if self.position <= 0:
                self.position = max(self.position, 0)
                self.avg_cost = 0.0
                self.total_cost = 0.0
            else:
                self.total_cost = self.avg_cost * self.position
            
            # 记录交易
            self.trades.append({
                'date': dt or date_str,
                'type': 'SELL',
                'price': price,
                'qty': qty,
                'amount': price * qty,
                'realized_pnl': realized_pnl,
                'reason': reason,
                'zone': zone,
                'position_after': self.position,
                'avg_cost_after': self.avg_cost
            })
    
    def get_trading_days(self):
        if self.first_trade_date and self.last_trade_date:
            return (self.last_trade_date - self.first_trade_date).days + 1
        return 0
    
    def get_annualized_return(self, initial_capital=100000):
        """计算年化收益率"""
        if self.get_trading_days() == 0:
            return 0.0
        
        total_return = self.realized_pnl / initial_capital if initial_capital > 0 else 0
        years = self.get_trading_days() / 365.0
        
        if years > 0 and total_return > -1:
            return ((1 + total_return) ** (1 / years) - 1) * 100
        return 0.0
    
    def analyze_profit_breakdown(self):
        """分解不同类型的收益"""
        breakdown = {
            '网格收益': 0.0,
            '趋势收益': 0.0,
            '底仓收益': 0.0,
            '其他收益': 0.0
        }
        
        for reason, pnl in self.realized_by_reason.items():
            reason_upper = str(reason).upper()
            if 'BOX_GRID' in reason_upper:
                breakdown['网格收益'] += pnl
            elif 'ABOVE_120' in reason_upper:
                breakdown['趋势收益'] += pnl
            elif 'BETWEEN_300_150' in reason_upper:
                breakdown['底仓收益'] += pnl
            else:
                breakdown['其他收益'] += pnl
        
        return breakdown
    
    def get_sharpe_ratio(self, risk_free_rate=0.02):
        """计算夏普比率（简化版）"""
        if len(self.trades) < 2:
            return 0.0
        
        # 计算每笔交易的收益率
        returns = []
        cash_flow = 0
        
        for trade in self.trades:
            if trade['type'] == 'BUY':
                cash_flow -= trade['amount']
            else:  # SELL
                cash_flow += trade['amount']
                # 当有卖出时，计算该时间段的收益率
                if cash_flow > 0:
                    # 简化：使用交易金额作为权重
                    returns.append(trade.get('realized_pnl', 0) / abs(cash_flow))
        
        if len(returns) < 2:
            return 0.0
        
        avg_return = sum(returns) / len(returns)
        std_return = (sum((r - avg_return) ** 2 for r in returns) / len(returns)) ** 0.5
        
        if std_return > 0:
            return (avg_return * 252 - risk_free_rate) / (std_return * (252 ** 0.5))
        return 0.0
    
    def get_max_drawdown(self):
        """计算最大回撤（基于持仓价值变化）"""
        if len(self.trades) == 0:
            return 0.0
        
        # 计算每个时间点的持仓价值
        values = []
        current_position = 0
        current_avg_cost = 0.0
        
        for trade in sorted(self.trades, key=lambda x: x['date'] if isinstance(x['date'], datetime) else datetime.min):
            if trade['type'] == 'BUY':
                current_position = trade['position_after']
                current_avg_cost = trade['avg_cost_after']
            elif trade['type'] == 'SELL':
                current_position = trade['position_after']
                current_avg_cost = trade['avg_cost_after']
            
            # 假设市场价等于成本价（简化）
            market_price = current_avg_cost if current_avg_cost > 0 else trade['price']
            values.append(current_position * market_price)
        
        if len(values) < 2:
            return 0.0
        
        peak = values[0]
        max_dd = 0.0
        
        for value in values:
            if value > peak:
                peak = value
            dd = (peak - value) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        
        return max_dd * 100  # 返回百分比

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
        zone = (r.get("zone") or "").strip()
        date_str = r.get("date", "")
    except Exception as e:
        print(f"跳过无法解析的行：{r}，错误：{e}", file=sys.stderr)
        continue

    key = (name, symbol)
    if key not in etf_stats:
        etf_stats[key] = ETFStat(name, symbol)
    stat = etf_stats[key]

    stat.on_trade(date_str, price, qty, side, reason, zone)

# ====== 获取当前持仓信息 ======
current_positions = {}
if state:
    for etf_name, etf_state in state.items():
        if etf_name == "_meta":
            continue
        current_positions[etf_name] = {
            'position': etf_state.get('current_units', 0),
            'avg_cost': etf_state.get('last_price', 0),  # 使用最后价格作为近似
            'last_price': etf_state.get('last_price', 0)
        }

# ====== 汇总输出 ======
print(f"\n{'='*60}")
print("ETF 策略收益详细分析")
print(f"{'='*60}")
print(f"分析时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"交易记录总数: {len(ordered_rows)}")
print()

total_realized = 0.0
total_grid_realized = 0.0
total_trend_realized = 0.0
total_base_realized = 0.0
total_other_realized = 0.0
total_current_value = 0.0
total_investment = 0.0

# 计算每只ETF的详细指标
for (name, symbol), stat in sorted(etf_stats.items(), key=lambda x: x[0][0]):
    print(f"{'='*50}")
    print(f"标的: {name} ({symbol})")
    print(f"{'-'*50}")
    
    # 基本信息
    print(f"交易统计:")
    print(f"  总交易笔数: {stat.trade_count:6d}  (买入: {stat.buy_count:3d} / 卖出: {stat.sell_count:3d})")
    print(f"  交易数量: 买入 {stat.buy_qty:10,d} 股 / 卖出 {stat.sell_qty:10,d} 股")
    print(f"  交易金额: 买入 ¥{stat.buy_amount:12,.2f} / 卖出 ¥{stat.sell_amount:12,.2f}")
    
    if stat.first_trade_date and stat.last_trade_date:
        trading_days = stat.get_trading_days()
        print(f"  交易期间: {stat.first_trade_date.strftime('%Y-%m-%d')} 至 {stat.last_trade_date.strftime('%Y-%m-%d')} ({trading_days} 天)")
    
    # 持仓信息
    print(f"\n持仓信息:")
    print(f"  当前持仓: {stat.position:10,d} 股")
    print(f"  持仓成本: ¥{stat.avg_cost:10.4f} / 股")
    print(f"  持仓总成本: ¥{stat.total_cost:12,.2f}")
    
    # 从状态文件获取最新价格计算浮动盈亏
    current_info = current_positions.get(name, {})
    current_price = current_info.get('last_price', stat.avg_cost)
    if current_price > 0 and stat.position > 0:
        current_value = stat.position * current_price
        floating_pnl = (current_price - stat.avg_cost) * stat.position
        floating_pnl_pct = (current_price / stat.avg_cost - 1) * 100 if stat.avg_cost > 0 else 0
        
        print(f"  当前价格: ¥{current_price:10.4f} / 股")
        print(f"  持仓市值: ¥{current_value:12,.2f}")
        print(f"  浮动盈亏: ¥{floating_pnl:12,.2f} ({floating_pnl_pct:+.2f}%)")
        
        total_current_value += current_value
        total_investment += stat.total_cost
    
    # 收益分析
    print(f"\n收益分析:")
    print(f"  已实现收益: ¥{stat.realized_pnl:12,.2f}")
    
    # 收益分解
    profit_breakdown = stat.analyze_profit_breakdown()
    print(f"  收益分解:")
    print(f"    底仓收益 (BETWEEN_300_150): ¥{profit_breakdown['底仓收益']:12,.2f}")
    print(f"    网格收益 (BOX_GRID):         ¥{profit_breakdown['网格收益']:12,.2f}")
    print(f"    趋势收益 (ABOVE_120):        ¥{profit_breakdown['趋势收益']:12,.2f}")
    print(f"    其他收益:                    ¥{profit_breakdown['其他收益']:12,.2f}")
    
    # 性能指标
    print(f"\n性能指标:")
    if stat.buy_amount > 0:
        total_return_pct = (stat.realized_pnl / stat.buy_amount) * 100
        print(f"  总收益率: {total_return_pct:+.2f}%")
    
    annualized_return = stat.get_annualized_return(stat.buy_amount if stat.buy_amount > 0 else 100000)
    print(f"  年化收益率: {annualized_return:+.2f}%")
    
    sharpe_ratio = stat.get_sharpe_ratio()
    print(f"  夏普比率: {sharpe_ratio:+.3f}")
    
    max_dd = stat.get_max_drawdown()
    print(f"  最大回撤: {max_dd:+.2f}%")
    
    # 交易质量指标
    if stat.sell_count > 0:
        avg_profit_per_trade = stat.realized_pnl / stat.sell_count
        win_trades = sum(1 for trade in stat.trades if trade.get('realized_pnl', 0) > 0)
        win_rate = win_trades / stat.sell_count * 100 if stat.sell_count > 0 else 0
        
        print(f"  平均每笔盈利: ¥{avg_profit_per_trade:10,.2f}")
        print(f"  胜率: {win_rate:6.1f}% ({win_trades}/{stat.sell_count})")
    
    # 按区间统计
    if stat.realized_by_zone:
        print(f"\n按区间收益统计:")
        for zone, pnl in sorted(stat.realized_by_zone.items()):
            if pnl != 0:
                print(f"  {zone:<20}: ¥{pnl:12,.2f}")
    
    # 累计到总计
    total_realized += stat.realized_pnl
    total_grid_realized += profit_breakdown['网格收益']
    total_trend_realized += profit_breakdown['趋势收益']
    total_base_realized += profit_breakdown['底仓收益']
    total_other_realized += profit_breakdown['其他收益']

    print()

# 总体汇总
print(f"{'='*60}")
print("总体汇总")
print(f"{'='*60}")

print(f"总已实现收益: ¥{total_realized:,.2f}")
print(f"收益构成:")
print(f"  底仓收益: ¥{total_base_realized:12,.2f} ({total_base_realized/total_realized*100:.1f}%)" if total_realized != 0 else "  底仓收益: ¥0.00")
print(f"  网格收益: ¥{total_grid_realized:12,.2f} ({total_grid_realized/total_realized*100:.1f}%)" if total_realized != 0 else "  网格收益: ¥0.00")
print(f"  趋势收益: ¥{total_trend_realized:12,.2f} ({total_trend_realized/total_realized*100:.1f}%)" if total_realized != 0 else "  趋势收益: ¥0.00")
print(f"  其他收益: ¥{total_other_realized:12,.2f} ({total_other_realized/total_realized*100:.1f}%)" if total_realized != 0 else "  其他收益: ¥0.00")

if total_investment > 0:
    total_return_pct = total_realized / total_investment * 100
    print(f"总收益率: {total_return_pct:+.2f}%")

if total_current_value > 0 and total_investment > 0:
    total_assets = total_current_value + total_realized
    total_return_all = (total_assets / total_investment - 1) * 100
    print(f"总资产: ¥{total_assets:,.2f} (市值: ¥{total_current_value:,.2f} + 已实现: ¥{total_realized:,.2f})")
    print(f"综合收益率: {total_return_all:+.2f}%")

# 计算总体年化收益率（简化）
if len(etf_stats) > 0:
    first_date = min(s.first_trade_date for s in etf_stats.values() if s.first_trade_date)
    last_date = max(s.last_trade_date for s in etf_stats.values() if s.last_trade_date)
    
    if first_date and last_date:
        total_days = (last_date - first_date).days + 1
        total_years = total_days / 365.0
        
        if total_years > 0 and total_investment > 0:
            total_return = total_realized / total_investment
            annualized_return = ((1 + total_return) ** (1 / total_years) - 1) * 100
            print(f"总体年化收益率: {annualized_return:+.2f}%")
            print(f"运行时间: {total_days} 天 ({total_years:.1f} 年)")

print(f"{'='*60}")
print("说明:")
print("1. 底仓收益: 在MA300-MA150区间建立底仓和加仓的收益")
print("2. 网格收益: 在箱体区(MA150-MA150*1.2)网格交易的收益")
print("3. 趋势收益: 在强势区(>MA150*1.2)减仓的收益")
print("4. 年化收益率: 假设收益复投计算的年化回报率")
print("5. 夏普比率: 每单位风险获得的超额回报，>1为良好")
print("6. 最大回撤: 策略运行期间的最大亏损幅度")
print(f"{'='*60}")

PYCODE
}

#=============== 脚本--cron-check自动重启程序==============================
if [ "$1" = "--cron-check" ]; then
    cron_check
    exit 0
fi
# ========= 若以 --cron-check 启动，则只做检查后退出 ======================
show_menu() {
    echo "==============================="
    echo "  ETF 策略监控 管理菜单"
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
