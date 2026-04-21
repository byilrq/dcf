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


# ========= 终端颜色（支持时启用） =========
if [ -t 1 ]; then
    C_RESET=$'\033[0m'
    C_BOLD=$'\033[1m'
    C_DIM=$'\033[2m'
    C_RED=$'\033[31m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[34m'
    C_MAGENTA=$'\033[35m'
    C_CYAN=$'\033[36m'
else
    C_RESET=""
    C_BOLD=""
    C_DIM=""
    C_RED=""
    C_GREEN=""
    C_YELLOW=""
    C_BLUE=""
    C_MAGENTA=""
    C_CYAN=""
fi

# ========= 公共函数 =========


setup_interactive_input() {
    if [ -t 0 ]; then
        stty sane 2>/dev/null || true
        stty erase '^?' 2>/dev/null || true
        bind "set enable-bracketed-paste off" >/dev/null 2>&1 || true
        bind "set editing-mode emacs" >/dev/null 2>&1 || true
    fi
}

prompt_read() {
    local __var_name="$1"
    local __prompt="$2"
    local __default="${3-}"
    local __value=""

    if [ -t 0 ]; then
        setup_interactive_input
        if [ -n "$__default" ]; then
            read -e -r -p "$__prompt" -i "$__default" __value || return 1
        else
            read -e -r -p "$__prompt" __value || return 1
        fi
    else
        read -r -p "$__prompt" __value || return 1
    fi

    printf -v "$__var_name" '%s' "$__value"
}

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
    echo "虚拟环境: $VENV_DIR"
    echo "================================="

    # ---------- 基本检查 ----------
    if ! command -v sudo >/dev/null 2>&1; then
        echo "❌ 未检测到 sudo，无法安装系统依赖。请用 root 运行或手动安装 python3-venv/python3-pip。"
        return 1
    fi

    # ---------- 1) 系统依赖 ----------
    echo "[1/4] 安装系统依赖（python3-venv / python3-pip 等）"
    if ! sudo apt-get update -y; then
        echo "❌ apt-get update 失败。可能是网络/源/锁占用问题。"
        echo "   你可以先执行：sudo lsof /var/lib/dpkg/lock-frontend 或等待系统自动更新完成。"
        return 1
    fi

    if ! sudo apt-get install -y \
        python3 python3-venv python3-pip \
        ca-certificates curl wget nginx openssl \
        build-essential; then
        echo "❌ apt-get install 失败。"
        return 1
    fi

    # ---------- 2) 创建/更新虚拟环境 ----------
    echo "[2/4] 准备虚拟环境: $VENV_DIR"

    if [ -d "$VENV_DIR" ] && [ ! -x "$VENV_DIR/bin/python" ]; then
        echo "⚠️ 检测到虚拟环境可能损坏（缺少 $VENV_DIR/bin/python），将重建..."
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

    # ---------- 3) 安装 Python 依赖 ----------
    echo "[4/4] 安装 Python 依赖"

    if [ -f "$DCF_DIR/requirements.txt" ]; then
        echo "   检测到 requirements.txt，按其安装/更新依赖..."
        if ! $VPY -m pip install -U -r "$DCF_DIR/requirements.txt"; then
            echo "❌ requirements.txt 安装失败。"
            deactivate || true
            return 1
        fi
    else
        echo "   未检测到 requirements.txt，安装默认依赖（requests / pyyaml / json5 / pandas / yfinance / scipy / flask / gunicorn / ruamel.yaml / werkzeug）"
        if ! $VPY -m pip install -U requests pyyaml json5 pandas yfinance scipy flask gunicorn ruamel.yaml werkzeug; then
            echo "❌ 依赖安装失败。"
            deactivate || true
            return 1
        fi
    fi

    # ---------- 自检：import 测试 ----------
    echo "   进行依赖自检（import requests/yaml/json5/pandas/yfinance/scipy/flask/ruamel）..."
    if ! $VPY - <<'PY'
import sys
ok = True
checks = [("requests","requests"),("yaml","yaml"),("json5","json5"),("pandas","pandas"),("yfinance","yfinance"),("scipy","scipy"),("flask","flask"),("ruamel","ruamel.yaml")]
for label, mod in checks:
    try:
        __import__(mod)
        print(f"✅ import {label} OK")
    except Exception as e:
        ok = False
        print(f"❌ import {label} FAILED: {e}")
sys.exit(0 if ok else 1)
PY
    then
        echo "❌ 依赖自检未通过。请检查网络、pip 源或 Python 版本。"
        deactivate || true
        return 1
    fi

    echo "已安装的关键包版本："
    "$VPY" - <<'PY'
import yaml, json5, requests, pandas, yfinance, scipy, flask, ruamel.yaml
print("requests:", requests.__version__)
print("pyyaml:  ", yaml.__version__)
print("json5:   ", json5.__version__)
print("pandas:  ", pandas.__version__)
print("yfinance:", yfinance.__version__)
print("scipy:   ", scipy.__version__)
print("flask:   ", flask.__version__)
print("ruamel:  ", ruamel.yaml.__version__)
PY

    echo "================================="
    echo "依赖安装完成 ✅"
    echo "Python: $($VPY -V)"
    echo "pip:    $($VPIP -V)"
    echo "================================="

    deactivate || true

    echo
    prompt_read _web_ans "是否现在配置 Web 管理端（nginx 819 + 登录页）？(y/n): " "n"
    if [[ "${_web_ans:-n}" =~ ^[yY]$ ]]; then
        configure_web_portal
    fi

    return 0
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

    prompt_read choice "请选择 [1-6]: "
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

    prompt_read ans "是否设置 PushPlus Token？(y/n): "
    case "$ans" in
        y|Y)
            prompt_read token "请输入 PushPlus Token（注意不要泄露给他人）: "
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

    prompt_read ans "是否设置 Telegram 配置？(y/n): "
    case "$ans" in
        y|Y)
            prompt_read bot_token "请输入 Telegram Bot Token: "
            if [ -z "$bot_token" ]; then
                echo "Bot Token 为空，取消设置。"
                return
            fi

            prompt_read chat_id "请输入 Telegram Chat ID: "
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

# ============================================
# Web 管理端（Flask + gunicorn + nginx:819）
# ============================================
WEB_APP_FILE="$DCF_DIR/dcf_web.py"
WEB_CONF_FILE="$DCF_DIR/web_portal.json"
WEB_SERVICE_FILE="/etc/systemd/system/dcf-web.service"
WEB_NGINX_SITE="/etc/nginx/sites-available/dcf-web-819.conf"
WEB_NGINX_LINK="/etc/nginx/sites-enabled/dcf-web-819.conf"
WEB_INTERNAL_PORT="1819"
WEB_PUBLIC_PORT="819"

configure_web_portal() {
    ensure_dcf_dir

    if [ ! -f "$WEB_APP_FILE" ]; then
        echo "❌ 未找到 $WEB_APP_FILE，请先把 dcf_web.py / web_templates / web_static 放到 $DCF_DIR"
        return 1
    fi

    local domain="sharq.eu.org"
    local admin_user="admin"
    local admin_pass=""
    local admin_pass2=""
    local cert_dir="$DCF_DIR/certs"
    local cert_file="$cert_dir/dcf-web.crt"
    local key_file="$cert_dir/dcf-web.key"
    local le_dir="/etc/letsencrypt/live/$domain"
    local le_fullchain="$le_dir/fullchain.pem"
    local le_privkey="$le_dir/privkey.pem"

    mkdir -p "$cert_dir"

    echo
    echo "${C_CYAN}${C_BOLD}Web 管理端配置${C_RESET}"
    prompt_read domain "请输入访问域名（默认 sharq.eu.org）: " "$domain"
    domain="${domain:-sharq.eu.org}"
    prompt_read admin_user "请输入管理账号（默认 admin）: " "$admin_user"
    admin_user="${admin_user:-admin}"
    prompt_read admin_pass "请输入登录密码: " ""
    prompt_read admin_pass2 "请再次输入登录密码: " ""

    if [ -z "$admin_pass" ] || [ "$admin_pass" != "$admin_pass2" ]; then
        echo "❌ 两次密码不一致或为空。"
        return 1
    fi

    if [ ! -x "$VENV_DIR/bin/python" ]; then
        echo "❌ 未检测到虚拟环境 Python：$VENV_DIR/bin/python，请先执行菜单 3。"
        return 1
    fi

    local password_hash
    password_hash="$($VENV_DIR/bin/python - <<PY
from werkzeug.security import generate_password_hash
print(generate_password_hash(${admin_pass@Q}))
PY
)"

    local secret_key
    secret_key="$($VENV_DIR/bin/python - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)"

    ADMIN_USER="$admin_user" \
    PASSWORD_HASH="$password_hash" \
    SECRET_KEY="$secret_key" \
    DOMAIN_NAME="$domain" \
    WEB_CONF_FILE="$WEB_CONF_FILE" \
    $VENV_DIR/bin/python - <<'PY'
import json
import os
from pathlib import Path

cfg = {
    "app_name": "资产管理系统",
    "admin_username": os.environ["ADMIN_USER"],
    "password_hash": os.environ["PASSWORD_HASH"],
    "secret_key": os.environ["SECRET_KEY"],
    "domain": os.environ["DOMAIN_NAME"],
    "public_port": 819,
    "internal_port": 1819,
}
Path(os.environ["WEB_CONF_FILE"]).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
PY


    if [ -f "$le_fullchain" ] && [ -f "$le_privkey" ]; then
        echo "检测到 Let's Encrypt 正式证书，优先使用：$le_dir"
        cert_file="$le_fullchain"
        key_file="$le_privkey"
    else
        if [ ! -f "$cert_file" ] || [ ! -f "$key_file" ]; then
            echo "未检测到正式证书，生成自签名证书（可后续替换为正式证书）..."
            openssl req -x509 -nodes -newkey rsa:2048 \
                -keyout "$key_file" \
                -out "$cert_file" \
                -days 3650 \
                -subj "/CN=$domain" >/dev/null 2>&1
        fi
    fi

    echo "检查 Web 程序与模板目录..."
    if [ ! -d "$DCF_DIR/web_templates" ] || [ ! -d "$DCF_DIR/web_static" ]; then
        echo "❌ 缺少 web_templates 或 web_static 目录，请确认已复制到 $DCF_DIR"
        return 1
    fi
    if ! (cd "$DCF_DIR" && "$VENV_DIR/bin/python" -c "import dcf_web; print('dcf_web import ok')") ; then
        echo "❌ dcf_web.py 导入失败，请检查依赖或文件内容。"
        return 1
    fi

    sudo tee "$WEB_SERVICE_FILE" >/dev/null <<SERVICE
[Unit]
Description=DCF Web Portal
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$DCF_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/gunicorn -w 2 -b 127.0.0.1:$WEB_INTERNAL_PORT dcf_web:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SERVICE

    sudo tee "$WEB_NGINX_SITE" >/dev/null <<NGINX
server {
    listen $WEB_PUBLIC_PORT ssl;
    listen [::]:$WEB_PUBLIC_PORT ssl;
    server_name $domain;

    ssl_certificate     $cert_file;
    ssl_certificate_key $key_file;
    ssl_session_timeout 1d;
    ssl_session_cache shared:SSL:10m;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;

    client_max_body_size 10m;

    location / {
        proxy_pass http://127.0.0.1:$WEB_INTERNAL_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 60;
    }
}
NGINX

    sudo ln -sf "$WEB_NGINX_SITE" "$WEB_NGINX_LINK"
    sudo nginx -t || return 1
    sudo systemctl daemon-reload
    sudo systemctl enable dcf-web >/dev/null 2>&1 || true
    sudo systemctl restart dcf-web

    sleep 2
    if ! sudo systemctl is-active --quiet dcf-web; then
        echo "❌ dcf-web 服务启动失败，最近日志如下："
        sudo systemctl --no-pager --full status dcf-web || true
        echo "----------------------------------------"
        sudo journalctl -u dcf-web -n 50 --no-pager || true
        return 1
    fi

    if ! curl -ksS "http://127.0.0.1:$WEB_INTERNAL_PORT/login" >/dev/null 2>&1; then
        echo "❌ 后端服务未正常响应 http://127.0.0.1:$WEB_INTERNAL_PORT/login"
        sudo journalctl -u dcf-web -n 50 --no-pager || true
        return 1
    fi

    sudo systemctl reload nginx

    echo
    echo "✅ Web 管理端已配置完成"
    echo "访问地址: https://$domain:$WEB_PUBLIC_PORT/login"
    if [ "$cert_file" = "$le_fullchain" ]; then
        echo "证书来源: Let's Encrypt ($le_dir)"
    else
        echo "证书来源: 自签名证书 ($cert_file)"
    fi
}


restart_web_portal() {
    echo "=============== 重启 Web 管理端 ==============="
    if [ ! -f "$DCF_DIR/dcf_web.py" ]; then
        echo "❌ 未找到 $DCF_DIR/dcf_web.py"
        return 1
    fi
    if [ ! -x "$VENV_DIR/bin/python" ]; then
        echo "❌ 未找到虚拟环境 Python：$VENV_DIR/bin/python"
        echo "   请先执行：菜单 3) 安装/更新依赖"
        return 1
    fi

    echo "检查 nginx 配置..."
    if ! sudo nginx -t; then
        echo "❌ nginx 配置检查失败，已取消重启网页端。"
        return 1
    fi

    echo "重启 dcf-web 服务..."
    sudo systemctl restart dcf-web
    sleep 1

    if ! sudo systemctl is-active --quiet dcf-web; then
        echo "❌ dcf-web 服务启动失败，最近日志如下："
        sudo systemctl --no-pager --full status dcf-web || true
        echo "--------------------------------------------"
        sudo journalctl -u dcf-web -n 80 --no-pager || true
        return 1
    fi

    echo "重新加载 nginx..."
    sudo systemctl reload nginx || sudo systemctl restart nginx
    echo "✅ Web 管理端已重启。"
    echo "--------------------------------------------"
    web_portal_status
}

web_portal_status() {
    echo "=============== Web 管理端状态 ==============="
    if [ -f "$WEB_CONF_FILE" ]; then
        cat "$WEB_CONF_FILE"
    else
        echo "web_portal.json 未配置"
    fi
    echo "--------------------------------------------"
    sudo systemctl status dcf-web --no-pager -n 5 2>/dev/null || echo "dcf-web 服务未安装"
    echo "--------------------------------------------"
    sudo nginx -t 2>/dev/null || true
}




show_menu() {
    echo -e "${C_CYAN}===============================${C_RESET}"
    echo -e "${C_BOLD}${C_GREEN}  DCF 网格监控 管理菜单${C_RESET}"
    echo -e "${C_DIM} （管理脚本目录：$SCRIPT_DIR）${C_RESET}"
    echo -e "${C_DIM} （运行文件目录：$DCF_DIR）${C_RESET}"
    echo -e "${C_CYAN}===============================${C_RESET}"
    echo -e "${C_YELLOW}1)${C_RESET} 安装/更新依赖"	
    echo -e "${C_GREEN}2)${C_RESET} 启动脚本"
    echo -e "${C_RED}3)${C_RESET} 停止脚本"
    echo -e "${C_BLUE}4)${C_RESET} Push设置"
    echo -e "${C_CYAN}5)${C_RESET} 查看运行状态"
    echo -e "${C_YELLOW}6)${C_RESET} 设置上海时区"
    echo -e "${C_YELLOW}7)${C_RESET} 重启网页端"
    echo -e "${C_CYAN}8)${C_RESET} 查看网页端状态"
    echo -e "${C_RED}0)${C_RESET} 退出"
    echo -e "${C_CYAN}===============================${C_RESET}"
}

# ========= 主循环 =========
while true; do
    show_menu
    echo -ne "${C_BOLD}请选择操作: ${C_RESET}"
    read -r choice
    case "$choice" in
	    1) update_rely ;;
        2) start_dcf ;;
        3) stop_dcf ;;
        4) config_push ;;
        5) show_status ;;
        6) change_tz ;;
        7) restart_web_portal ;;
        8) web_portal_status ;;
        0)
            echo "退出管理脚本。"
            exit 0
            ;;
        *)
            echo "无效选项，请重新输入。"
            ;;
    esac
done

