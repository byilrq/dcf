#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DCF push notification helper.

Keep push delivery isolated from strategy/web code. The ntfy sender intentionally
keeps the old working dcf.py behavior: body bytes + Title/Priority/Markdown
headers through requests.post.
"""

from __future__ import annotations

import base64
import logging
from email.header import Header
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests

BASE_DIR = Path(__file__).resolve().parent
PUSH_CONFIG_FILE = Path("/root/dcf/push.conf")
PUSH_LOG_FILE = Path("/root/dcf/push.log")
PUSH_LOG_KEEP_LINES = 30
PUSHPLUS_URL = "http://www.pushplus.plus/send"

PUSH_DEFAULTS: Dict[str, str] = {
    "PUSH_ENABLED": "yes",
    "PUSH_CHANNEL": "gotify",
    "PUSHPLUS_TOKEN": "",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "GOTIFY_URL": "https://sharq.eu.org:2084",
    "GOTIFY_TOKEN": "",
    "GOTIFY_PRIORITY": "10",
    "NTFY_URL": "http://127.0.0.1:8083",
    "NTFY_TOPIC": "let-rss",
    "NTFY_USERNAME": "",
    "NTFY_PASSWORD": "",
    "NTFY_PRIORITY": "4",
}

PUSH_CHANNEL_VALUES = {"telegram", "gotify", "ntfy", "pushplus", "none"}


def _strip_shell_quotes(value: Any) -> str:
    """Parse values from push.conf as literal credentials.

    push.conf is edited by the Web UI and read by Python directly; it is not
    sourced by the shell.  Keep credentials as the user entered them.  This
    parser also repairs older over-escaped values such as:
        export NTFY_PASSWORD="Plex0819\\\\$"
    so the runtime value becomes exactly:
        Plex0819$
    """
    import re

    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]

    # Repair old shell-escaped dollar forms, including repeated saves that
    # produced multiple backslashes before $.
    text = re.sub(r"\\+\$", "$", text)
    return (
        text
        .replace('\\"', '"')
        .replace('\\`', '`')
        .replace('\\\\', '\\')
    )


def _normalize_config(cfg: Dict[str, Any]) -> Dict[str, str]:
    merged = dict(PUSH_DEFAULTS)
    merged.update({k: "" if v is None else str(v).strip() for k, v in (cfg or {}).items()})

    enabled = str(merged.get("PUSH_ENABLED", "yes")).strip().lower()
    merged["PUSH_ENABLED"] = "no" if enabled in {"no", "false", "0", "off"} else "yes"

    channel = str(merged.get("PUSH_CHANNEL", "gotify")).strip().lower()
    if channel == "both":
        channel = "pushplus"
    elif channel == "all":
        channel = "gotify"
    if channel not in PUSH_CHANNEL_VALUES:
        channel = "gotify"
    merged["PUSH_CHANNEL"] = channel

    try:
        merged["GOTIFY_PRIORITY"] = str(int(float(str(merged.get("GOTIFY_PRIORITY", "10") or "10"))))
    except Exception:
        merged["GOTIFY_PRIORITY"] = "10"

    try:
        ntfy_priority = int(float(str(merged.get("NTFY_PRIORITY", "4") or "4")))
        merged["NTFY_PRIORITY"] = str(max(1, min(5, ntfy_priority)))
    except Exception:
        merged["NTFY_PRIORITY"] = "4"

    merged["NTFY_URL"] = str(merged.get("NTFY_URL", "")).strip().rstrip("/") or PUSH_DEFAULTS["NTFY_URL"]
    merged["NTFY_TOPIC"] = str(merged.get("NTFY_TOPIC", "")).strip().strip("/") or PUSH_DEFAULTS["NTFY_TOPIC"]
    return merged


def load_push_config() -> Dict[str, str]:
    """Read /root/dcf/push.conf, falling back to environment variables."""
    cfg = dict(PUSH_DEFAULTS)
    for key in cfg:
        if os.getenv(key) is not None:
            cfg[key] = os.getenv(key, cfg[key]) or ""

    for path in (PUSH_CONFIG_FILE, BASE_DIR / "push.conf"):
        if not path.exists():
            continue
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key in cfg:
                    cfg[key] = _strip_shell_quotes(value)
            break
        except Exception as exc:
            logging.error(f"读取推送配置失败 {path}: {exc}")
    return _normalize_config(cfg)


def shell_quote_export(value: Any) -> str:
    """Write values in a human-readable quoted form.

    The file is parsed by Python, not sourced by bash, so do not escape `$`.
    This keeps passwords like Plex0819$ stored exactly as entered.
    """
    text = "" if value is None else str(value)
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


def build_push_config_text(cfg: Dict[str, Any]) -> str:
    merged = _normalize_config(cfg)
    ordered_keys = [
        "PUSH_ENABLED",
        "PUSH_CHANNEL",
        "PUSHPLUS_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "GOTIFY_URL",
        "GOTIFY_TOKEN",
        "GOTIFY_PRIORITY",
        "NTFY_URL",
        "NTFY_TOPIC",
        "NTFY_USERNAME",
        "NTFY_PASSWORD",
        "NTFY_PRIORITY",
    ]
    lines = [
        "# 自动生成的 Push 配置",
        f"# 更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    for key in ordered_keys:
        lines.append(f"export {key}={shell_quote_export(merged.get(key, ''))}")
    lines.append("")
    return "\n".join(lines)


def write_push_config(cfg: Dict[str, Any]) -> None:
    PUSH_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    PUSH_CONFIG_FILE.write_text(build_push_config_text(cfg), encoding="utf-8")


def prune_push_log_lines(keep_lines: int = PUSH_LOG_KEEP_LINES) -> None:
    try:
        if not PUSH_LOG_FILE.exists():
            return
        keep_lines = max(1, int(keep_lines or PUSH_LOG_KEEP_LINES))
        lines = PUSH_LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
        if len(lines) > keep_lines:
            PUSH_LOG_FILE.write_text("\n".join(lines[-keep_lines:]) + "\n", encoding="utf-8")
    except Exception as exc:
        logging.debug(f"清理推送日志失败: {exc}")


def append_push_log(channel: str, success: bool, detail: str) -> None:
    try:
        PUSH_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {channel} | {'成功' if success else '失败'} | {str(detail).replace(chr(10), ' ')[:500]}\n"
        with PUSH_LOG_FILE.open("a", encoding="utf-8") as file_obj:
            file_obj.write(line)
        prune_push_log_lines(PUSH_LOG_KEEP_LINES)
    except Exception as exc:
        logging.error(f"写入推送日志失败: {exc}")


def read_push_logs(limit: int = PUSH_LOG_KEEP_LINES) -> List[str]:
    if not PUSH_LOG_FILE.exists():
        return []
    try:
        lines = [line.strip() for line in PUSH_LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]
        return lines[-int(limit or PUSH_LOG_KEEP_LINES):][::-1]
    except Exception as exc:
        return [f"读取推送日志失败：{exc}"]


def escape_markdown_v2(text: str) -> str:
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(('\\' + ch) if ch in escape_chars else ch for ch in str(text or ""))


def _truncate_msg(text: Any, limit: int = 4000) -> str:
    body = str(text or "")
    if len(body) > limit:
        return body[:limit] + "\n...\n(消息过长，已截断)"
    return body




def _is_symbol_status_block(block: str) -> bool:
    """Return True if a paragraph looks like one symbol status/trade block."""
    text = str(block or "").strip()
    if not text:
        return False
    patterns = (
        "🟢[INFO]【",
        "🎯[TRADE]【",
        "🎯[ERROR]【",
        "🟡[WARN]【",
        "🎯[STOP]【",
        "[仅监控] 🟢[INFO]【",
        "[仅监控] 🎯[ERROR]【",
        "[仅监控] 🟡[WARN]【",
    )
    if text.startswith(patterns):
        return True
    return bool(re.match(r"^\[仅监控\]\s*[^:：\n]+[:：]\s*暂无状态记录", text)) or bool(re.match(r"^[^:：\n]+[:：]\s*暂无状态记录", text))


def _split_ntfy_symbol_batches(text: Any, batch_size: int = 8) -> List[str]:
    """Split long DCF snapshot messages by symbol blocks.

    ntfy may display oversized bodies as attachment.txt.  Instead of truncating
    and losing information, split daily/status snapshots into multiple ntfy
    messages, with up to `batch_size` symbols per message.  Non-snapshot
    messages are sent unchanged.
    """
    body = str(text or "").strip()
    if not body:
        return [""]
    batch_size = max(1, int(batch_size or 8))
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", body) if part.strip()]
    if len(paragraphs) <= 1:
        return [body]

    first_idx = None
    for idx, part in enumerate(paragraphs):
        if _is_symbol_status_block(part):
            first_idx = idx
            break
    if first_idx is None:
        return [body]

    header = "\n\n".join(paragraphs[:first_idx]).strip()
    blocks = paragraphs[first_idx:]
    if len(blocks) <= batch_size:
        return [body]

    chunks: List[str] = []
    total = (len(blocks) + batch_size - 1) // batch_size
    for part_no, start in enumerate(range(0, len(blocks), batch_size), start=1):
        batch = blocks[start:start + batch_size]
        prefix = header
        if header and total > 1:
            prefix = f"{header} ({part_no}/{total})"
        elif total > 1:
            prefix = f"DCF 推送 ({part_no}/{total})"
        chunk_parts = [prefix] if prefix else []
        chunk_parts.extend(batch)
        chunks.append("\n\n".join(chunk_parts))
    return chunks or [body]


def _send_pushplus(msg: str, cfg: Dict[str, str]) -> bool:
    token = str(cfg.get("PUSHPLUS_TOKEN", "")).strip()
    if not token:
        logging.info("未配置 PushPlus Token，跳过该通道推送。")
        return False
    payload = {
        "token": token,
        "title": "",
        "content": _truncate_msg(msg, 4000),
        "template": "txt",
    }
    try:
        resp = requests.post(PUSHPLUS_URL, json=payload, timeout=10)
        try:
            resp_data = resp.json()
        except Exception:
            resp_data = {}
        if resp.status_code != 200 or (resp_data and resp_data.get("code") not in {200, "200", None}):
            logging.error(f"PushPlus 推送失败: {resp.text[:300]}")
            return False
        logging.info("✅ PushPlus 推送成功。")
        return True
    except Exception as exc:
        logging.error(f"PushPlus 推送异常: {exc}")
        return False


def _send_telegram(msg: str, cfg: Dict[str, str]) -> bool:
    token = str(cfg.get("TELEGRAM_BOT_TOKEN", "")).strip()
    chat_id = str(cfg.get("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not chat_id:
        missing = []
        if not token:
            missing.append("Bot Token")
        if not chat_id:
            missing.append("Chat ID")
        logging.info(f"未配置 Telegram {', '.join(missing)}，跳过该通道推送。")
        return False
    try:
        lines = str(msg or "").split("\n")
        filtered_lines = [line for line in lines if "🚦策略运行状态:" not in line]
        plain_msg = "\n".join(filtered_lines)
        payload = {
            "chat_id": chat_id,
            "text": _truncate_msg(escape_markdown_v2(plain_msg), 4000),
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
            "disable_notification": False,
        }
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json=payload, timeout=30)
        if resp.status_code == 200:
            logging.info("✅ Telegram 推送成功。")
            return True
        try:
            error_msg = resp.json().get("description", "未知错误")
        except Exception:
            error_msg = resp.text or "无返回信息"
        logging.error(f"❌ Telegram 推送失败 (状态码{resp.status_code}): {error_msg}")
        if resp.status_code == 400 and "can't parse entities" in error_msg:
            payload.pop("parse_mode", None)
            payload["text"] = _truncate_msg(plain_msg, 4000)
            retry = requests.post(url, json=payload, timeout=30)
            if retry.status_code == 200:
                logging.info("✅ Telegram 纯文本推送成功。")
                return True
        return False
    except Exception as exc:
        logging.error(f"❌ Telegram 推送异常: {exc}")
        return False


def _send_gotify(msg: str, cfg: Dict[str, str]) -> bool:
    gotify_url = str(cfg.get("GOTIFY_URL", "")).strip().rstrip("/")
    gotify_token = str(cfg.get("GOTIFY_TOKEN", "")).strip()
    if not gotify_url or not gotify_token:
        missing = []
        if not gotify_url:
            missing.append("URL")
        if not gotify_token:
            missing.append("Token")
        logging.info(f"未配置 Gotify {', '.join(missing)}，跳过该通道推送。")
        return False
    try:
        priority = int(float(str(cfg.get("GOTIFY_PRIORITY", "10") or "10")))
    except Exception:
        priority = 10
    payload = {
        "message": _truncate_msg(msg, 6000),
        "priority": priority,
    }
    try:
        resp = requests.post(f"{gotify_url}/message", params={"token": gotify_token}, json=payload, timeout=15)
        if 200 <= resp.status_code < 300:
            logging.info("✅ Gotify 推送成功。")
            return True
        logging.error(f"❌ Gotify 推送失败 (状态码{resp.status_code}): {resp.text[:300]}")
        return False
    except Exception as exc:
        logging.error(f"❌ Gotify 推送异常: {exc}")
        return False


def _encode_http_header_value(value: Any) -> str:
    """Encode non-ASCII HTTP header values exactly like the old dcf.py implementation."""
    text = str(value or "")
    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        return Header(text, "utf-8").encode()


def _send_ntfy_detail(msg: str, cfg: Dict[str, str], title: str = "DCF 推送") -> Tuple[bool, str]:
    """Send ntfy using the old working dcf.py behavior, but return details.

    This intentionally keeps the old request shape from dcf_old.py:
    Title + Priority + Markdown, posted with
    requests.post(data=utf8_bytes). No X-* variants and no explicit
    Content-Type are added here.
    """
    ntfy_url = str(cfg.get("NTFY_URL", "")).strip().rstrip("/")
    topic = str(cfg.get("NTFY_TOPIC", "")).strip().strip("/")
    if not ntfy_url or not topic:
        missing = []
        if not ntfy_url:
            missing.append("URL")
        if not topic:
            missing.append("Topic")
        detail = f"未配置 ntfy {', '.join(missing)}"
        logging.info(detail + "，跳过该通道推送。")
        return False, detail
    try:
        priority = int(float(str(cfg.get("NTFY_PRIORITY", "4") or "4")))
    except Exception:
        priority = 4
    priority = max(1, min(5, priority))
    headers = {
        "Title": _encode_http_header_value(title),
        "Priority": str(priority),
        "Markdown": "yes",
    }
    auth = None
    username = str(cfg.get("NTFY_USERNAME", "")).strip()
    password = str(cfg.get("NTFY_PASSWORD", ""))
    if username:
        auth = (username, password)
    safe_url = f"{ntfy_url}/{topic}"
    parts = _split_ntfy_symbol_batches(msg, batch_size=8)
    sent = 0
    failed_details: List[str] = []
    total_bytes = 0
    for idx, part in enumerate(parts, start=1):
        part_headers = dict(headers)
        if len(parts) > 1:
            part_headers["Title"] = _encode_http_header_value(f"{title} {idx}/{len(parts)}")
        payload = str(part or "").encode("utf-8")
        total_bytes += len(payload)
        try:
            resp = requests.post(
                safe_url,
                data=payload,
                headers=part_headers,
                auth=auth,
                timeout=15,
            )
            body = (resp.text or "")[:500].replace("\n", " ")
            if 200 <= resp.status_code < 300:
                sent += 1
                if len(parts) > 1 and idx < len(parts):
                    time.sleep(1)
                continue
            failed_details.append(f"第{idx}/{len(parts)}条 HTTP {resp.status_code}: {body}")
        except Exception as exc:
            failed_details.append(f"第{idx}/{len(parts)}条异常: {exc}")

    if sent == len(parts):
        detail = f"ntfy成功 {sent}/{len(parts)}, priority={priority}, topic={topic}, bytes={total_bytes}"
        logging.info(f"✅ {detail}")
        return True, detail
    detail = f"ntfy失败 {sent}/{len(parts)}, priority={priority}, topic={topic}, " + " | ".join(failed_details)
    logging.error(f"❌ {detail}")
    return False, detail


def _send_ntfy(msg: str, cfg: Dict[str, str]) -> bool:
    ok, _detail = _send_ntfy_detail(msg, cfg)
    return ok


def send_notification(msg: str) -> bool:
    cfg = load_push_config()
    enabled = str(cfg.get("PUSH_ENABLED", "yes")).strip().lower()
    channel = str(cfg.get("PUSH_CHANNEL", "gotify")).strip().lower()
    if enabled in {"no", "false", "0", "off"} or channel == "none":
        logging.info("推送已关闭，跳过通知。")
        append_push_log(channel, True, "推送已关闭，跳过通知")
        return True

    if channel == "telegram":
        ok = _send_telegram(msg, cfg)
        result = f"Telegram:{'成功' if ok else '失败'}"
    elif channel == "gotify":
        ok = _send_gotify(msg, cfg)
        result = f"Gotify:{'成功' if ok else '失败'}"
    elif channel == "ntfy":
        ok, detail = _send_ntfy_detail(msg, cfg)
        result = f"ntfy:{'成功' if ok else '失败'} | {detail}"
    elif channel == "pushplus":
        ok = _send_pushplus(msg, cfg)
        result = f"PushPlus:{'成功' if ok else '失败'}"
    else:
        ok = False
        result = "未执行任何推送通道"
    append_push_log(channel, ok, result)
    return ok


def send_push_test(cfg: Dict[str, Any] | None = None) -> Tuple[bool, str]:
    test_cfg = _normalize_config(cfg or load_push_config())
    if test_cfg.get("PUSH_ENABLED") != "yes" or test_cfg.get("PUSH_CHANNEL") == "none":
        append_push_log(test_cfg.get("PUSH_CHANNEL", "none"), False, "测试推送：推送已关闭")
        return False, "推送已关闭，请先启用推送并选择通道。"
    title = "DCF 推送测试"
    message = f"闲云量化 Web 推送配置测试成功。\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    channel = test_cfg.get("PUSH_CHANNEL", "gotify")
    if channel == "telegram":
        ok = _send_telegram(message, test_cfg)
        detail = f"Telegram：{'成功' if ok else '失败'}"
    elif channel == "gotify":
        ok = _send_gotify(message, test_cfg)
        detail = f"Gotify：{'成功' if ok else '失败'}"
    elif channel == "ntfy":
        ok, ntfy_detail = _send_ntfy_detail(message, test_cfg, title=title)
        detail = f"ntfy：{'成功' if ok else '失败'} | {ntfy_detail}"
    elif channel == "pushplus":
        ok = _send_pushplus(message, test_cfg)
        detail = f"PushPlus：{'成功' if ok else '失败'}"
    else:
        ok = False
        detail = "未执行任何推送通道"
    append_push_log(channel, ok, f"测试推送：{detail}")
    return ok, detail
