from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from .db import connect


class NotificationError(RuntimeError):
    pass


def wecom_configured() -> bool:
    return bool(os.getenv("WECOM_WEBHOOK_URL"))


def validate_wecom_webhook(webhook: str) -> tuple[bool, str | None]:
    """只允许企业微信官方群机器人 Webhook，防止误把通知发到其他地址。"""
    try:
        parsed = urlparse(webhook.strip())
    except ValueError:
        return False, "Webhook 地址格式无效"
    if parsed.scheme != "https" or parsed.hostname != "qyapi.weixin.qq.com":
        return False, "必须使用 https://qyapi.weixin.qq.com 的企业微信机器人地址"
    if parsed.path != "/cgi-bin/webhook/send":
        return False, "这不是企业微信群机器人的 Webhook 路径"
    key = parse_qs(parsed.query).get("key", [])
    if len(key) != 1 or not key[0].strip():
        return False, "Webhook 缺少机器人 key"
    return True, None


def send_wecom_markdown(content: str) -> None:
    webhook = os.getenv("WECOM_WEBHOOK_URL")
    if not webhook:
        raise NotificationError("企业微信 Webhook 尚未配置")
    valid, reason = validate_wecom_webhook(webhook)
    if not valid:
        raise NotificationError(reason or "企业微信 Webhook 无效")
    payload = json.dumps({"msgtype": "markdown", "markdown": {"content": content}}).encode("utf-8")
    request = Request(webhook, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise NotificationError(f"企业微信通知失败: {type(exc).__name__}") from exc
    if body.get("errcode") not in (0, None):
        raise NotificationError(f"企业微信通知被拒绝: {body.get('errmsg', 'unknown')}")


def notification_allowed(database: str, fingerprint: str, cooldown_seconds: int = 900, now: datetime | None = None) -> bool:
    """仅判断是否超过冷却期；成功发送后才由 record_notification 写入记录。"""
    now = now or datetime.now(timezone.utc)
    with connect(database) as connection:
        row = connection.execute(
            "SELECT sent_at FROM notification_log WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
    if row is None:
        return True
    try:
        last_sent = datetime.fromisoformat(row["sent_at"])
    except ValueError:
        return True
    if last_sent.tzinfo is None:
        last_sent = last_sent.replace(tzinfo=timezone.utc)
    return now - last_sent >= timedelta(seconds=cooldown_seconds)


def record_notification(database: str, fingerprint: str, channel: str, now: datetime | None = None) -> None:
    sent_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO notification_log(fingerprint, sent_at, channel) VALUES (?, ?, ?) "
            "ON CONFLICT(fingerprint) DO UPDATE SET sent_at=excluded.sent_at, channel=excluded.channel",
            (fingerprint, sent_at, channel),
        )


def send_wecom_once(database: str, fingerprint: str, content: str, cooldown_seconds: int = 900) -> bool:
    """成功发送才记账，避免接口失败时静默丢失告警。返回 False 表示仍在冷却期。"""
    if not notification_allowed(database, fingerprint, cooldown_seconds):
        return False
    send_wecom_markdown(content)
    record_notification(database, fingerprint, "wecom")
    return True
