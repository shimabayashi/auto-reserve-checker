#!/usr/bin/env python3
"""
mirakosta_watch.py
Hotel MiraCosta (DHM) のキャンセル空きを検出して メール で通知する。

GitHub Actions 想定:
  - secrets: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM, MAIL_TO
  - 監視対象 URL/ラベル: config.json
  - state.json: Actions cache で前回状態を引き継ぐ

ローカル実行:
  python3 mirakosta_watch.py --debug
  python3 mirakosta_watch.py --test-mail
  python3 mirakosta_watch.py --reset
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import random
import re
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
STATE_PATH = SCRIPT_DIR / "state.json"
LOG_PATH = SCRIPT_DIR / "watch.log"
DEBUG_DIR = SCRIPT_DIR / "debug"

AVAILABLE_KEYWORDS = [
    "空室あり",
    "残りわずか",
    "予約可能",
    "ご予約はこちら",
    "空室照会",
]

UNAVAILABLE_KEYWORDS = [
    "満室",
    "空室なし",
    "ご予約いただけません",
    "予約不可",
    "受付停止",
    "受付を終了",
    "お取り扱いできません",
    "該当する宿泊プランはありません",
]


@dataclass
class Config:
    # SMTP 設定 (secrets / env で渡す想定)
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    mail_from: str
    mail_to: list[str]  # カンマ区切り文字列も許容、loadで分割

    target_url: str
    label: str = "ミラコスタ"
    user_agent: str = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
    )
    timeout_sec: int = 20
    notify_on_unchanged_available: bool = False
    smtp_use_ssl: bool = False  # True: SMTPS(465), False: STARTTLS(587)

    @classmethod
    def load(cls, path: Path) -> "Config":
        data: dict = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))

        # 環境変数で上書き
        env_map = {
            "SMTP_HOST": "smtp_host",
            "SMTP_PORT": "smtp_port",
            "SMTP_USER": "smtp_user",
            "SMTP_PASSWORD": "smtp_password",
            "MAIL_FROM": "mail_from",
            "MAIL_TO": "mail_to",
            "TARGET_URL": "target_url",
            "LABEL": "label",
            "SMTP_USE_SSL": "smtp_use_ssl",
        }
        for env_key, attr in env_map.items():
            v = os.environ.get(env_key)
            if v:
                data[attr] = v

        # 型変換
        if "smtp_port" in data:
            data["smtp_port"] = int(data["smtp_port"])
        if "smtp_use_ssl" in data and isinstance(data["smtp_use_ssl"], str):
            data["smtp_use_ssl"] = data["smtp_use_ssl"].lower() in ("1", "true", "yes")
        if "mail_to" in data and isinstance(data["mail_to"], str):
            data["mail_to"] = [s.strip() for s in data["mail_to"].split(",") if s.strip()]

        required = [
            "smtp_host", "smtp_port", "smtp_user", "smtp_password",
            "mail_from", "mail_to", "target_url",
        ]
        missing = [k for k in required if not data.get(k)]
        if missing:
            sys.exit(
                f"[ERROR] 必須項目が未設定: {missing}\n"
                f"  GitHub Actions: secrets に SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD/MAIL_FROM/MAIL_TO を設定\n"
                f"  ローカル: config.json または環境変数で同名キーを設定"
            )

        valid_keys = {f for f in cls.__dataclass_fields__}
        data = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**data)


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("mirakosta")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def fetch_page(cfg: Config, logger: logging.Logger) -> Optional[str]:
    time.sleep(random.uniform(0.5, 2.5))

    headers = {
        "User-Agent": cfg.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://reserve.tokyodisneyresort.jp/sp/hotel/",
    }
    try:
        resp = requests.get(cfg.target_url, headers=headers, timeout=cfg.timeout_sec)
        resp.encoding = resp.apparent_encoding or "utf-8"
        logger.info(f"HTTP {resp.status_code}, len={len(resp.text)}")
        if resp.status_code != 200:
            return None
        return resp.text
    except requests.RequestException as e:
        logger.error(f"fetch failed: {e}")
        return None


def detect_status(html: str) -> tuple[str, list[str]]:
    text = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style\b[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)

    unavailable_hits = [k for k in UNAVAILABLE_KEYWORDS if k in text]
    available_hits = [k for k in AVAILABLE_KEYWORDS if k in text]

    if unavailable_hits and not available_hits:
        return "UNAVAILABLE", unavailable_hits
    if available_hits and not unavailable_hits:
        return "AVAILABLE", available_hits
    if available_hits and unavailable_hits:
        decisive_unavail = any(
            k in text for k in ["該当する宿泊プランはありません", "受付を終了", "予約不可"]
        )
        if decisive_unavail:
            return "UNAVAILABLE", unavailable_hits
        return "AVAILABLE", available_hits
    return "UNKNOWN", []


def send_mail(cfg: Config, subject: str, body: str, logger: logging.Logger) -> bool:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.mail_from
    msg["To"] = ", ".join(cfg.mail_to)
    msg.set_content(body)

    try:
        if cfg.smtp_use_ssl:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ctx, timeout=20) as s:
                s.login(cfg.smtp_user, cfg.smtp_password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20) as s:
                s.ehlo()
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
                s.login(cfg.smtp_user, cfg.smtp_password)
                s.send_message(msg)
        logger.info(f"mail sent to {cfg.mail_to}")
        return True
    except (smtplib.SMTPException, OSError) as e:
        logger.error(f"smtp error: {e}")
        return False


def run_check(cfg: Config, logger: logging.Logger, debug: bool = False) -> None:
    html = fetch_page(cfg, logger)
    if html is None:
        return

    if debug:
        DEBUG_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        snap = DEBUG_DIR / f"snapshot_{ts}.html"
        snap.write_text(html, encoding="utf-8")
        logger.info(f"[debug] saved {snap}")

    status, hits = detect_status(html)
    logger.info(f"status={status} hits={hits}")

    state = load_state()
    prev_status = state.get("status", "INIT")

    if status == "UNKNOWN":
        logger.warning(
            "status UNKNOWN — キーワード未一致。debug実行でHTML確認してください。"
        )
        state["last_unknown_at"] = datetime.now().isoformat(timespec="seconds")
        save_state(state)
        return

    now = datetime.now().isoformat(timespec="seconds")
    should_notify = False
    if status == "AVAILABLE":
        if prev_status != "AVAILABLE":
            should_notify = True
        elif cfg.notify_on_unchanged_available:
            should_notify = True

    if should_notify:
        subject = f"🎉 {cfg.label} に空きが出ました"
        body = (
            f"Hotel MiraCosta のキャンセル空きを検出しました。\n\n"
            f"対象: {cfg.label}\n"
            f"検出キーワード: {', '.join(hits)}\n"
            f"検出時刻: {now}\n\n"
            f"予約ページ:\n{cfg.target_url}\n\n"
            f"※ 既に他者に取られている可能性があります。お急ぎください。\n"
        )
        send_mail(cfg, subject, body, logger)
        state["last_notified_at"] = now

    state["status"] = status
    state["last_checked_at"] = now
    if status != prev_status:
        state["last_changed_at"] = now
        state["previous_status"] = prev_status
    save_state(state)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="HTMLスナップショット保存")
    parser.add_argument("--test-mail", action="store_true", help="メール送信テスト")
    parser.add_argument("--reset", action="store_true", help="状態ファイルをリセット")
    args = parser.parse_args()

    logger = setup_logger()
    cfg = Config.load(CONFIG_PATH)

    if args.reset:
        if STATE_PATH.exists():
            STATE_PATH.unlink()
        logger.info("state reset.")
        return 0

    if args.test_mail:
        ok = send_mail(
            cfg,
            "✅ mirakosta_watch テスト通知",
            f"テスト送信です。\n時刻: {datetime.now().isoformat(timespec='seconds')}\n対象URL: {cfg.target_url}\n",
            logger,
        )
        return 0 if ok else 1

    try:
        run_check(cfg, logger, debug=args.debug)
    except Exception as e:
        logger.exception(f"unhandled error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
