"""LLM-ревью сделки: бык/медведь/судья по advice-файлу.
Анализ-only: ничего не исполняет. Если LLM недоступен — пропуск с пометкой.
Конфиг через env: LLM_BASE (default http://localhost:4000/v1), LLM_MODEL (default primary),
LLM_API_KEY (default sk-dummy). Требует LiteLLM-proxy или совместимый OpenAI-endpoint."""
import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PALACE = Path(os.environ.get("MEMPALACE_PALACE", r"C:\Users\h04x\.mempalace\palace"))
MEM_REPO = Path(r"C:\Users\h04x\Projects\h04xjrn_2.0\mempalace-memory-system\mempalace")
WING = "trading"
ROOM = "hall_events"

LLM_BASE = os.environ.get("LLM_BASE", "http://localhost:4000/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "primary")
LLM_KEY = os.environ.get("LLM_API_KEY", "sk-dummy")
TIMEOUT = 40

BULL = ("Ты — бычий аналитик FX. Проанализируй сделку, найди аргументы ЗА вход "
        "по сигналу, оцени стоп и цель. Максимум 120 слов, только по существу.")
BEAR = ("Ты — медвежий аналитик FX. Проанализируй сделку, найди аргументы ПРОТИВ "
        "входа: что может пойти не так, слабые места стопа/цели, риски. "
        "Максимум 120 слов, только по существу.")
JUDGE = ("Ты — риск-судья. Выслушал быка и медведя. Дай СТРУКТУРИРОВАННЫЙ вердикт:\n"
         "1) AGREED|DISAGREE|NEUTRAL (согласен с сигналом, против, нейтрально)\n"
         "2) Ключевые предупреждения (максимум 3, если есть)\n"
         "3) Итог: максимум 60 слов.")


def llm_call(prompt: str, system: str) -> str:
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 600,
    }).encode()
    req = urllib.request.Request(LLM_BASE + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {LLM_KEY}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"].strip()


def mem_write(text: str, source: str) -> bool:
    try:
        if not PALACE.exists():
            return False
        sys.path.insert(0, str(MEM_REPO))
        import mempalace.palace as mp
        from mempalace.miner import add_drawer
        collection = mp.get_collection(str(PALACE))
        drawer_id = f"drawer_{WING}_{ROOM}_{hashlib.sha256((source + text).encode()).hexdigest()[:24]}"
        metadata = {
            "wing": WING, "room": ROOM, "source_file": source, "chunk_index": 0,
            "added_by": "llm_review", "filed_at": datetime.now().isoformat(),
        }
        collection.upsert(documents=[text], ids=[drawer_id], metadatas=[metadata])
        return True
    except Exception:
        return False


def extract_advice_lines(path: Path) -> str:
    txt = path.read_text(encoding="utf-8", errors="replace")
    lines = []
    for ln in txt.splitlines():
        if any(ln.strip().startswith(p) for p in
               ("# ", "Символ", "Сигнал", "ВХОД-СКОР", "Зона", "Стоп", "Цель",
                "Вердикт", "ВЕТО", "Консенсус", "Newswatch", "Сентимент",
                "Risk", "Mean-reversion", "Session", "Волатильность", "XAU", "GBP",
                "EUR", "DAX", "USDJPY", "BTC", "Итог", "- ")):
            lines.append(ln)
    return "\n".join(lines[:40])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="путь к advice-файлу (по умолчанию последний сегодняшний)")
    ap.add_argument("--date", help="YYYYMMDD — выбрать все advice этого дня")
    ap.add_argument("--no-mem", action="store_true")
    args = ap.parse_args()

    adv_dir = BASE_DIR / "advices"
    if args.file:
        files = [Path(args.file)]
    elif args.date:
        files = sorted(adv_dir.glob(f"advice_*_{args.date}_*.md"))
    else:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        files = sorted(adv_dir.glob(f"advice_*_{today}_*.md"))[-1:]
    if not files:
        print("Нет advice-файлов для ревью")
        return

    try:
        urllib.request.urlopen(LLM_BASE + "/models", timeout=5)
    except Exception:
        print(f"LLM недоступен ({LLM_BASE}) — ревью пропущено. Запусти LiteLLM-роутер "
              f"или задай LLM_BASE/LLM_MODEL/LLM_API_KEY.")
        return

    report_dir = BASE_DIR / "llm_reviews"
    report_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    report = [f"# LLM-ревью сделок ({stamp} UTC)\n"]
    for f in files:
        print(f"Ревью: {f.name}")
        ctx = extract_advice_lines(f)
        try:
            bull = llm_call(ctx, BULL)
            bear = llm_call(ctx, BEAR)
            judge = llm_call(f"СДЕЛКА:\n{ctx}\n\nБЫК:\n{bull}\n\nМЕДВЕДЬ:\n{bear}", JUDGE)
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as e:
            print(f"  ошибка LLM: {e}")
            continue
        report.append(f"\n## {f.stem}\n\n**БЫК:**\n{bull}\n\n**МЕДВЕДЬ:**\n{bear}\n\n**СУДЬЯ:**\n{judge}\n")
        if not args.no_mem:
            mem_write(f"LLM-ревью {f.stem}:\n{judge}", f"llm_review_{f.stem}")
    out = report_dir / f"llm_review_{stamp}.md"
    out.write_text("\n".join(report), encoding="utf-8")
    print(f"Отчёт: {out}")


if __name__ == "__main__":
    main()