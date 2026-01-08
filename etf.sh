#!/usr/bin/env bash
set -euo pipefail

chmod +x "$0" >/dev/null 2>&1 || true

# ========= 基本配置 =========
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ETF_DIR="$SCRIPT_DIR/etf"
PY_SCRIPT="$ETF_DIR/etf.py"

PYTHON_CMD="python3"

PID_FILE="$ETF_DIR/etf.pid"
LOG_FILE="$ETF_DIR/etf.log"

PUSH_CONF="$ETF_DIR/push.conf"

VENV_DIR="$ETF_DIR/.venv"

# ========= 公共函数 =========
ensure_etf_dir() {
    if [ ! -d "$ETF_DIR" ]; then
        echo "创建目录: $ETF_DIR"
        mkdir -p "$ETF_DIR"
    fi
}

# ============================================
# 依赖安装/更新（系统依赖 + Python依赖）
# ============================================
update_rely() {
    ensure_etf_dir
    echo "================================="
    echo "开始安装/更新依赖..."
    echo "目标目录: $ETF_DIR"
    echo "虚拟环境: $VENV_DIR"
    echo "================================="

    if ! command -v sudo >/dev/null 2>&1; then
        echo "❌ 未检测到 sudo，无法安装系统依赖。请用 root 运行或手动安装 python3-venv/python3-pip。"
        return 1
    fi

    echo "[1/4] 安装系统依赖（python3-venv / python3-pip 等）"
    if ! sudo apt-get update -y; then
        echo "❌ apt-get update 失败。可能是网络/源/锁占用问题。"
        return 1
    fi

    if ! sudo apt-get install -y \
        python3 python3-venv python3-pip \
        ca-certificates curl wget \
        build-essential; then
        echo "❌ apt-get install 失败。"
        return 1
    fi

    echo "[2/4] 准备虚拟环境: $VENV_DIR"
    if [ -d "$VENV_DIR" ] && [ ! -x "$VENV_DIR/bin/python" ]; then
        echo "⚠️ 检测到虚拟环境可能损坏，将重建..."
        rm -rf "$VENV_DIR"
    fi

    if [ ! -d "$VENV_DIR" ]; then
        if ! python3 -m venv "$VENV_DIR"; then
            echo "❌ 创建虚拟环境失败。"
            return 1
        fi
    fi

    # shellcheck disable=SC1090
    source "$VENV_DIR/bin/activate" || {
        echo "❌ 激活虚拟环境失败。"
        return 1
    }

    local VPY="$VENV_DIR/bin/python"
    local VPIP="$VENV_DIR/bin/pip"

    echo "   使用 Python: $($VPY -V 2>/dev/null)"
    echo "   使用 pip:    $($VPIP -V 2>/dev/null)"

    echo "[3/4] 升级 pip/setuptools/wheel"
    if ! $VPY -m pip install -U pip setuptools wheel; then
        echo "❌ pip 基础组件升级失败。"
        deactivate || true
        return 1
    fi

    echo "[4/4] 安装 Python 依赖"
    if [ -f "$ETF_DIR/requirements.txt" ]; then
        echo "   检测到 requirements.txt，按其安装/更新依赖..."
        if ! $VPY -m pip install -U -r "$ETF_DIR/requirements.txt"; then
            echo "❌ requirements.txt 安装失败。"
            deactivate || true
            return 1
        fi
    else
        echo "   未检测到 requirements.txt，安装默认依赖（requests / pyyaml / json5）"
        if ! $VPY -m pip install -U requests pyyaml json5; then
            echo "❌ 依赖安装失败。"
            deactivate || true
            return 1
        fi
    fi

    echo "   进行依赖自检（import requests/yaml/json5）..."
    if ! $VPY - <<'PY'
import sys
ok = True
for mod in ("requests", "yaml", "json5"):
    try:
        __import__(mod)
        print(f"✅ import {mod} OK")
    except Exception as e:
        ok = False
        print(f"❌ import {mod} FAILED: {e}")
sys.exit(0 if ok else 1)
PY
    then
        echo "❌ 依赖自检未通过。"
        deactivate || true
        return 1
    fi

    echo "已安装的关键包版本："
    "$VPY" - <<'PY'
import yaml, json5, requests
print("requests:", requests.__version__)
print("pyyaml:  ", yaml.__version__)
print("json5:   ", json5.__version__)
PY

    echo "================================="
    echo "依赖安装完成 ✅"
    echo "Python: $($VPY -V)"
    echo "pip:    $($VPIP -V)"
    echo "================================="

    deactivate || true
    return 0
}

# ============================================
# Push 配置文件
# ============================================
ensure_push_conf_file() {
    ensure_etf_dir
    if [ ! -f "$PUSH_CONF" ]; then
        {
            echo "# 自动生成的 Push 配置"
            echo "# 创建时间: $(date '+%Y-%m-%d %H:%M:%S')"
            echo ""
        } > "$PUSH_CONF"
        chmod 600 "$PUSH_CONF"
    fi
}

# ============================================
# cron 看门狗（每5分钟检查一次 etf.py）
# ============================================
add_cron_watchdog() {
    local cron_line="*/5 * * * * bash $SCRIPT_DIR/etf.sh --cron-check >/dev/null 2>&1"
    (crontab -l 2>/dev/null | grep -v "etf.sh --cron-check" || true; echo "$cron_line") | crontab -
    echo "已在 crontab 中添加每5分钟检查任务。"
}

remove_cron_watchdog() {
    (crontab -l 2>/dev/null | grep -v "etf.sh --cron-check" || true) | crontab - 2>/dev/null || true
    echo "已从 crontab 中移除检查任务（如存在）。"
}

cron_check() {
    ensure_etf_dir

    if [ -f "$PUSH_CONF" ]; then
        # shellcheck disable=SC1090
        source "$PUSH_CONF"
    fi

    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
        if [ -n "${PID}" ] && ps -p "$PID" >/dev/null 2>&1; then
            exit 0
        else
            rm -f "$PID_FILE"
        fi
    fi

    echo "$(date '+%Y.%m.%d.%H:%M:%S') [cron-check] 检测到 etf.py 未运行，自动重启..." >> "$LOG_FILE"

    if [ -x "$VENV_DIR/bin/python" ]; then
        nohup "$VENV_DIR/bin/python" "$PY_SCRIPT" >> "$LOG_FILE" 2>&1 &
    else
        nohup "$PYTHON_CMD" "$PY_SCRIPT" >> "$LOG_FILE" 2>&1 &
    fi

    NEW_PID=$!
    echo "$NEW_PID" > "$PID_FILE"
    echo "$(date '+%Y.%m.%d.%H:%M:%S') [cron-check] 已重新启动 etf.py，PID=$NEW_PID" >> "$LOG_FILE"
}

# ============================================
# 启动/停止
# ============================================
start_etf() {
    ensure_etf_dir

    if [ ! -f "$PY_SCRIPT" ]; then
        echo "找不到 $PY_SCRIPT，请先把 etf.py 放到：$ETF_DIR"
        return
    fi

    if [ -f "$PUSH_CONF" ]; then
        # shellcheck disable=SC1090
        source "$PUSH_CONF"
    else
        echo "提示：未配置 push.conf，脚本只会写日志，不会推送。"
        echo "你可以用菜单 4 配置推送。"
    fi

    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
        if [ -n "${PID}" ] && ps -p "$PID" >/dev/null 2>&1; then
            echo "etf.py 已在运行中（PID=$PID），如需重启请先停止。"
            return
        fi
    fi

    echo "启动 etf.py ..."
    echo "日志文件：$LOG_FILE"

    if [ -x "$VENV_DIR/bin/python" ]; then
        nohup "$VENV_DIR/bin/python" "$PY_SCRIPT" >> "$LOG_FILE" 2>&1 &
    else
        echo "提示：未检测到虚拟环境 $VENV_DIR，建议先执行菜单 3 安装依赖。"
        nohup "$PYTHON_CMD" "$PY_SCRIPT" >> "$LOG_FILE" 2>&1 &
    fi

    NEW_PID=$!
    echo "$NEW_PID" > "$PID_FILE"
    echo "etf.py 已启动，PID=$NEW_PID"

    add_cron_watchdog
}

stop_etf() {
    ensure_etf_dir

    if [ ! -f "$PID_FILE" ]; then
        echo "没有找到 PID 文件，可能 etf.py 未在运行。"
        remove_cron_watchdog
        return
    fi

    PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [ -z "${PID}" ] || ! ps -p "$PID" >/dev/null 2>&1; then
        echo "PID 文件存在但进程未运行，清理 PID 文件。"
        rm -f "$PID_FILE"
        remove_cron_watchdog
        return
    fi

    echo "正在停止 etf.py (PID=$PID)..."
    kill "$PID" || true

    sleep 2
    if ps -p "$PID" >/dev/null 2>&1; then
        echo "进程未退出，尝试强制 kill -9..."
        kill -9 "$PID" || true
    fi

    rm -f "$PID_FILE"
    echo "etf.py 已停止。"

    remove_cron_watchdog
}

# ============================================
# Push 设置入口（PushPlus & Telegram + 测试）
# ============================================
config_push() {
    ensure_etf_dir
    ensure_push_conf_file

    echo "当前 Push 配置文件路径：$PUSH_CONF"
    echo "----------------------------------------"
    if grep -q "^export PUSHPLUS_TOKEN=" "$PUSH_CONF"; then echo "PUSHPLUS_TOKEN: 已配置"; else echo "PUSHPLUS_TOKEN: (未配置)"; fi
    if grep -q "^export TELEGRAM_BOT_TOKEN=" "$PUSH_CONF"; then echo "TELEGRAM_BOT_TOKEN: 已配置"; else echo "TELEGRAM_BOT_TOKEN: (未配置)"; fi
    if grep -q "^export TELEGRAM_CHAT_ID=" "$PUSH_CONF"; then echo "TELEGRAM_CHAT_ID: 已配置"; else echo "TELEGRAM_CHAT_ID: (未配置)"; fi
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

config_pushplus() {
    ensure_push_conf_file

    echo "=== 配置 PushPlus ==="
    local current_token=""
    if grep -q "^export PUSHPLUS_TOKEN=" "$PUSH_CONF"; then
        current_token=$(grep "^export PUSHPLUS_TOKEN=" "$PUSH_CONF" | head -1 | cut -d'"' -f2)
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
            sed -i '/^export PUSHPLUS_TOKEN=/d' "$PUSH_CONF"
            echo "export PUSHPLUS_TOKEN=\"$token\"" >> "$PUSH_CONF"
            echo "PushPlus Token 已更新。"
            ;;
        *) echo "已跳过 PushPlus 配置。" ;;
    esac
}

config_telegram() {
    ensure_push_conf_file

    echo "=== 配置 Telegram ==="
    local current_bot_token=""
    local current_chat_id=""

    if grep -q "^export TELEGRAM_BOT_TOKEN=" "$PUSH_CONF"; then
        current_bot_token=$(grep "^export TELEGRAM_BOT_TOKEN=" "$PUSH_CONF" | head -1 | cut -d'"' -f2)
        echo "当前 Telegram Bot Token: ${current_bot_token:0:8}****"
    else
        echo "当前 Telegram Bot Token: (未配置)"
    fi

    if grep -q "^export TELEGRAM_CHAT_ID=" "$PUSH_CONF"; then
        current_chat_id=$(grep "^export TELEGRAM_CHAT_ID=" "$PUSH_CONF" | head -1 | cut -d'"' -f2)
        echo "当前 Telegram Chat ID: $current_chat_id"
    else
        echo "当前 Telegram Chat ID: (未配置)"
    fi

    read -r -p "是否设置 Telegram 配置？(y/n): " ans
    case "$ans" in
        y|Y)
            read -r -p "请输入 Telegram Bot Token: " bot_token
            [ -z "$bot_token" ] && { echo "Bot Token 为空，取消设置。"; return; }

            read -r -p "请输入 Telegram Chat ID: " chat_id
            [ -z "$chat_id" ] && { echo "Chat ID 为空，取消设置。"; return; }

            sed -i '/^export TELEGRAM_BOT_TOKEN=/d' "$PUSH_CONF"
            sed -i '/^export TELEGRAM_CHAT_ID=/d' "$PUSH_CONF"
            echo "export TELEGRAM_BOT_TOKEN=\"$bot_token\"" >> "$PUSH_CONF"
            echo "export TELEGRAM_CHAT_ID=\"$chat_id\"" >> "$PUSH_CONF"

            echo "Telegram 配置已更新。"
            echo "提示：请确保你已经给 Bot 发过消息/或把 Bot 加入群组并说过话，否则可能收不到推送。"
            ;;
        *) echo "已跳过 Telegram 配置。" ;;
    esac
}

test_pushplus() {
    ensure_etf_dir
    ensure_push_conf_file

    if ! command -v curl >/dev/null 2>&1; then
        echo "错误：未安装 curl，无法发送测试消息。"
        return 1
    fi

    # shellcheck disable=SC1090
    source "$PUSH_CONF" 2>/dev/null || true

    if [[ -z "${PUSHPLUS_TOKEN:-}" ]]; then
        echo "PUSHPLUS_TOKEN 未配置，请先配置 PushPlus。"
        return 1
    fi

    local title="ETF 测试 PushPlus"
    local content="PushPlus 测试消息发送成功 ✅\n时间：$(date '+%Y-%m-%d %H:%M:%S')\n主机：$(hostname)\n"

    echo "正在发送 PushPlus 测试消息..."
    local resp
    resp="$(curl -sS --max-time 10 \
        -X POST "http://www.pushplus.plus/send" \
        -d "token=${PUSHPLUS_TOKEN}" \
        --data-urlencode "title=${title}" \
        --data-urlencode "content=${content}" \
        -d "template=txt" || true)"

    if echo "$resp" | grep -Eq '"code"[[:space:]]*:[[:space:]]*(0|200)'; then
        echo "PushPlus 测试消息发送成功。"
        return 0
    fi

    echo "PushPlus 测试消息可能发送失败，返回："
    echo "$resp"
    return 1
}

test_telegram() {
    ensure_etf_dir
    ensure_push_conf_file

    if ! command -v curl >/dev/null 2>&1; then
        echo "错误：未安装 curl，无法发送测试消息。"
        return 1
    fi

    # shellcheck disable=SC1090
    source "$PUSH_CONF" 2>/dev/null || true

    if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
        echo "Telegram 未配置完整：需要 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID"
        return 1
    fi

    local text
    text=$'ETF Telegram 测试消息发送成功 ✅\n'
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
    ensure_etf_dir
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
        if [ -n "${PID}" ] && ps -p "$PID" >/dev/null 2>&1; then
            echo "etf.py 正在运行（PID=$PID）。"
        else
            echo "PID 文件存在，但进程未运行。"
        fi
    else
        echo "etf.py 当前未在运行。"
    fi
    echo "当前cron任务："
    crontab -l 2>/dev/null | grep "etf.sh --cron-check" || echo "无相关cron任务。"
    echo "日志文件：$LOG_FILE"
}

# ========= 若以 --cron-check 启动，则只做检查后退出 =========
if [ "${1:-}" = "--cron-check" ]; then
    cron_check
    exit 0
fi

show_menu() {
    echo "==============================="
    echo "  ETF 监测 管理菜单"
    echo " （管理脚本目录：$SCRIPT_DIR）"
    echo " （运行文件目录：$ETF_DIR）"
    echo "==============================="
    echo "1) 启动脚本"
    echo "2) 停止脚本"
    echo "3) 安装/更新依赖"
    echo "4) Push设置"
    echo "5) 查看运行状态"
    echo "0) 退出"
    echo "==============================="
}

while true; do
    show_menu
    read -r -p "请选择操作: " choice
    case "$choice" in
        1) start_etf ;;
        2) stop_etf ;;
        3) update_rely ;;
        4) config_push ;;
        5) show_status ;;
        0) echo "退出管理脚本。"; exit 0 ;;
        *) echo "无效选项，请重新输入。" ;;
    esac
done
