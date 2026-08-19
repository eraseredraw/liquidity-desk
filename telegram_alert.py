"""telegram_alert.py — push-алерты в Telegram (опционально).

Настройка:
1. @BotFather → /newbot → BOT_TOKEN
2. Написать боту любое сообщение
3. https://api.telegram.org/bot<BOT_TOKEN>/getUpdates → chat_id
4. Записать в data_source.json: {"telegram": {"token": "...", "chat_id": "..."}}
"""

import json

import requests
from pathlib import Path

HERE = Path(__file__).parent


def _config() -> dict:
    try:
        data = json.loads((HERE / "data_source.json").read_text(encoding="utf-8"))
        return data.get("telegram", {})
    except Exception:
        return {}


def send(message: str) -> bool:
    cfg = _config()
    token, chat_id = cfg.get("token"), cfg.get("chat_id")
    if not token or not chat_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}, timeout=10)
        return resp.status_code == 200
    except Exception:
        return False


def alert_zone(symbol: str, price: float, zone: str, signal: str, score: int) -> bool:
    emoji = "🚀" if signal == "buy" else "🔻" if signal == "sell" else "👀"
    return send(
        f"{emoji} <b>{symbol}</b> в зоне\n"
        f"Цена: <b>{price:.4f}</b>\n"
        f"Зона: {zone}\n"
        f"Сигнал: <b>{signal.upper()}</b>\n"
        f"Скор: <b>{score}/100</b>"
    )


def alert_signal_change(symbol: str, old_signal: str, new_signal: str, price: float) -> bool:
    emoji = "🚀" if new_signal == "buy" else "🔻" if new_signal == "sell" else "➡️"
    return send(
        f"{emoji} <b>{symbol}</b> сигнал изменился\n"
        f"{str(old_signal).upper()} → <b>{str(new_signal).upper()}</b>\n"
        f"Цена: {price:.4f}"
    )