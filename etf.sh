#!/usr/bin/env bash

# 自动为脚本加执行权限（即使你是 bash etf.sh，也不会出问题）
chmod +x "$0"

# ========= 基本配置 =========

# 当前脚本所在目录（你是 wget 到 /root 下，那就是 /root）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Python 脚本（监控程序）
PY_SCRIPT="$SCRIPT_DIR/etf.py"

# Python 命令（如果以后用虚拟环境，再改这里）
PYTHON_CMD="python3"

# 进程 PID 文件 & 日志文件
PID_FILE="$SCRIPT_DIR/etf.pid"
LOG_FILE="$SCRIPT_DIR/etf.log"

# PushPlus 配置文件（不会被提交到 GitHub）
PUSHPLUS_CONF="$HOME/.etf_pushplus.conf"


# ========= 公共函数 =========

start_etf() {
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
}

stop_etf() {
    if [ ! -f "$PID_FILE" ]; then
        echo "没有找到 PID 文件，可能 etf.py 未在运行。"
        return
    fi

    PID=$(cat "$PID_FILE")
    if ! ps -p "$PID" > /dev/null 2>&1; then
        echo "PID 文件存在但进程未运行，清理 PID 文件。"
        rm -f "$PID_FILE"
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
}

update_script() {
    echo "下载最新 etf.py ..."
    wget -N --no-check-certificate \
      https://raw.githubusercontent.com/byilrq/etf/main/etf.py \
      -O "$SCRIPT_DIR/etf.py"

    if [ $? -eq 0 ]; then
        echo "etf.py 已成功更新到最新版本。"
    else
        echo "更新失败，请检查网络或 GitHub 路径。"
    fi
}

config_pushplus() {
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

show_status() {
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
}


show_menu() {
    echo "==============================="
    echo "  ETF 网格监控 管理菜单"
    echo " （脚本目录：$SCRIPT_DIR）"
    echo "==============================="
    echo "1) 启动脚本"
    echo "2) 停止脚本"
    echo "3) 更新脚本 (下载最新 etf.py)"
    echo "4) PushPlus 推送设置"
    echo "5) 查看运行状态"
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
