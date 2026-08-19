# -*- coding: utf-8 -*-
"""Telegram bot: market/advice/status on demand. Long polling on requests (no deps).

Config: data_source.json -> "telegram": {"token": "...", "chat_id": 123}
Commands: /start /help /market /advice SYM /btc /status
Alerts still go via telegram_alert.py (sentinel).
"""

import html
import json
import subprocess
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).parent
CFG = json.loads((HERE / "data_source.json").read_text(encoding="utf-8"))
TG = CFG.get("telegram") or {}
TOKEN = TG.get("token") or ""
CHAT_ID = TG.get("chat_id")
API = f"https://api.telegram.org/bot{TOKEN}"

if not TOKEN:
    print("No telegram.token in data_source.json. Create bot via @BotFather, add token + chat_id.")
    sys.exit(1)

AUTHORIZED = {int(CHAT_ID)} if CHAT_ID is not None else None
COMMANDS = (
    "/start - приветствие\n"
    "/market - сводка по рынку (последний общий совет)\n"
    "/advice SYM - совет по символу (XAUUSD GBPUSD EURUSD GBPJPY DAX BTCUSD)\n"
    "/btc - статус битка\n"
    "/status - система: активные инструменты, процессы\n"
    "/help - это сообщение"
)


def send(chat_id: int, text: str) -> None:
    for chunk in (text[i:i + 4000] for i in range(0, len(text), 4000)):
        requests.post(f"{API}/sendMessage",
                      json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"},
                      timeout=20)


def authorized(chat_id: int) -> bool:
    return AUTHORIZED is None or chat_id in AUTHORIZED


def run_advice(symbol: str) -> str:
    p = subprocess.run([sys.executable, str(HERE / "advice.py"), "--symbol", symbol, "--no-mem"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=240, cwd=HERE)
    out = (p.stdout or "") + (p.stderr or "")
    out = out.split("DISCLAIMER")[0].strip()
    return out[-3500:] if out else "нет данных"


def cmd_market() -> str:
    m = HERE / "market_now.md"
    if not m.exists():
        return "market_now.md нет — запусти advice.py"
    txt = m.read_text(encoding="utf-8").split("DISCLAIMER")[0].strip()
    return txt[-3500:]


def cmd_status() -> str:
    lines = ["<b>Система</b>", ""]
    from strategy import SYMBOL_PARAMS
    active = [s for s, p in SYMBOL_PARAMS.items() if p]
    lines.append(f"Активные инструменты: {', '.join(active)}")
    lines.append(f"Отключены: {', '.join(s for s, p in SYMBOL_PARAMS.items() if not p)}")
    m = HERE / "market_now.md"
    lines.append(f"market_now: {'обновлён' if m.exists() else 'нет'} "
                 f"({time.strftime('%Y-%m-%d %H:%M', time.localtime(m.stat().st_mtime)) if m.exists() else ''})")
    for name in ("sentinel.py", "webui.py"):
        import subprocess as sp
        r = sp.run(["powershell", "-NoProfile", "-Command",
                    f"Get-Process python3.13 -ErrorAction SilentlyContinue | Select-String -Pattern '{name}'"],
                   capture_output=True, text=True, timeout=30)
        lines.append(f"{name}: {'RUNNING' if r.stdout.strip() else 'stopped'}")
    return "\n".join(lines)


def handle(chat_id: int, text: str) -> None:
    parts = text.strip().split()
    cmd = parts[0].lower()
    if cmd in ("/start", "/help"):
        send(chat_id, f"Торговая система. Команды:\n{COMMANDS}")
    elif cmd == "/market":
        send(chat_id, cmd_market())
    elif cmd == "/status":
        send(chat_id, cmd_status())
    elif cmd in ("/btc",):
        send(chat_id, run_advice("BTCUSD"))
    elif cmd == "/advice":
        sym = (parts[1] if len(parts) > 1 else "").upper()
        if sym not in ("XAUUSD", "GBPUSD", "EURUSD", "GBPJPY", "DAX", "BTCUSD"):
            send(chat_id, "Символ? /advice GBPJPY | BTCUSD | XAUUSD | EURUSD | GBPUSD | DAX")
            return
        send(chat_id, f"Считаю {sym}... (до 4 минут)")
        try:
            send(chat_id, f"<b>{sym}</b>\n" + run_advice(sym))
        except subprocess.TimeoutExpired:
            send(chat_id, f"{sym}: таймаут 4 мин — попробуй позже")
    else:
        send(chat_id, f"Не знаю команду. /help")


def main() -> None:
    offset = 0
    print(f"Bot polling {API} (auth={AUTHORIZED})")
    while True:
        try:
            r = requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=40)
            if r.status_code != 200:
                time.sleep(5)
                continue
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat_id = (msg.get("chat") or {}).get("id")
                text = msg.get("text") or ""
                if not chat_id or not text:
                    continue
                if not authorized(chat_id):
                    print(f"ignored unauthorized chat {chat_id}")
                    continue
                print(f"<- {chat_id}: {text[:80]}")
                try:
                    handle(chat_id, text)
                except Exception as exc:
                    send(chat_id, f"ошибка: {exc}")
                    print(f"error: {exc}")
        except requests.RequestException as exc:
            print(f"poll error: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    main()