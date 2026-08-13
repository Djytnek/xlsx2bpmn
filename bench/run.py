# -*- coding: utf-8 -*-
"""
Замер локальных моделей на задаче «описание процесса -> таблица».

Судья — сам валидатор xlsx2bpmn, поэтому оценка объективная, а не на глаз.
Каждой модели дают промпт и описание процесса; ответ прогоняют через
convert(). Если не сошлось — отчёт валидатора возвращают модели на починку,
и так до --rounds раз.

    python bench/run.py --url http://ЛОКАЛЬНЫЙ_ХОСТ:8080 --key sk-... \\
        --models qwen3.6:35b qwen3.6:27b-110k gemma4:31b

Зависимостей нет: только стандартная библиотека и сам xlsx2bpmn.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xlsx2bpmn import apply_layout, convert, layout_process   # noqa: E402
from xlsx2bpmn.core import ConvertError                       # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROMPT = ROOT / "xlsx2bpmn" / "prompt.md"
CASES = Path(__file__).resolve().parent / "cases"

FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*$", re.M)
HEADER_RE = re.compile(r"^\s*id\s*\|", re.M)


def expected_pipes() -> int:
    """Сколько черт в строке ждём — берём из заголовка в самом промпте."""
    found = HEADER_RE.search(PROMPT.read_text(encoding="utf-8"))
    line = found.group(0) if found else ""
    header = next((l for l in PROMPT.read_text(encoding="utf-8").split("\n")
                   if HEADER_RE.match(l)), line)
    return header.count("|")


PIPES = expected_pipes()


# --------------------------------------------------------------------------- #
# Промпт
# --------------------------------------------------------------------------- #

def build_prompt(description: str) -> str:
    """prompt.md, переделанный с «прочитай файл» на «вот текст»."""
    text = PROMPT.read_text(encoding="utf-8")
    text = re.sub(
        r"^Прочитай описание.*?\n.*?\n",
        "Составь таблицу по описанию процесса, приведённому в конце этого\n"
        "сообщения. Выведи только таблицу.\n",
        text, count=1, flags=re.S | re.M,
    )
    return f"{text}\n\n## Описание процесса\n\n{description.strip()}\n"


REPAIR = """Таблица не прошла проверку. Вот список ошибок; номер строки — это
номер строки таблицы, считая заголовок первой строкой.

{report}

Исправь только эти ошибки и выведи таблицу целиком заново. Формат прежний:
ровно {pipes} вертикальных черт в строке, ничего кроме таблицы."""


# --------------------------------------------------------------------------- #
# Клиент
# --------------------------------------------------------------------------- #

def ask(url: str, key: str, model: str, messages: list[dict],
        timeout: int) -> tuple[str, float]:
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0,
        "stream": False,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    started = time.monotonic()
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read()[:300].decode('utf-8', 'replace')}")
    except Exception as exc:                                   # noqa: BLE001
        raise RuntimeError(f"{type(exc).__name__}: {exc}")
    elapsed = time.monotonic() - started

    try:
        return payload["choices"][0]["message"]["content"], elapsed
    except (KeyError, IndexError):
        raise RuntimeError(f"неожиданный ответ: {json.dumps(payload)[:300]}")


# --------------------------------------------------------------------------- #
# Оценка
# --------------------------------------------------------------------------- #

def strip_noise(raw: str) -> tuple[str, list[str]]:
    """Вырезает мусор вокруг таблицы и рассказывает, что вырезал."""
    noise: list[str] = []
    text = raw

    thought = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if thought != text:
        noise.append("рассуждения в <think>")
        text = thought

    if FENCE_RE.search(text):
        noise.append("markdown-заборы ```")
        text = FENCE_RE.sub("", text)

    lines = text.split("\n")
    start = next((i for i, l in enumerate(lines) if HEADER_RE.match(l)), None)
    if start is None:
        return "", noise + ["заголовок таблицы не найден"]
    if any(l.strip() for l in lines[:start]):
        noise.append("текст до таблицы")

    body = []
    for line in lines[start:]:
        if not line.strip():
            continue
        if "|" not in line:
            noise.append("текст после таблицы")
            break
        body.append(line)
    return "\n".join(body), noise


def score(raw: str) -> dict:
    table, noise = strip_noise(raw)
    result = {"noise": noise, "table": table, "bad_pipes": 0,
              "rows": 0, "ok": False, "errors": [], "warnings": 0}
    if not table:
        result["errors"] = ["таблица не найдена в ответе"]
        return result

    rows = table.split("\n")
    result["rows"] = len(rows) - 1
    result["bad_pipes"] = sum(1 for l in rows if l.count("|") != PIPES)

    try:
        conv = convert(table.encode("utf-8"))
    except ConvertError as exc:
        result["errors"] = [f"не прочиталось: {exc}"]
        return result

    result["ok"] = conv.ok
    result["errors"] = [str(i) for i in conv.errors]
    result["warnings"] = len(conv.warnings)
    if conv.ok:                                # раскладка тоже должна пройти
        try:
            apply_layout(conv.xml, layout_process)
        except Exception as exc:               # noqa: BLE001
            result["ok"] = False
            result["errors"] = [f"раскладка упала: {exc}"]
    return result


# --------------------------------------------------------------------------- #
# Прогон
# --------------------------------------------------------------------------- #

def run_case(args, model: str, case: Path) -> dict:
    messages = [{"role": "user", "content": build_prompt(case.read_text(encoding="utf-8"))}]
    total = 0.0
    first: dict | None = None

    for attempt in range(args.rounds + 1):
        try:
            reply, elapsed = ask(args.url, args.key, model, messages, args.timeout)
        except RuntimeError as exc:
            return {"case": case.stem, "ok": False, "rounds": attempt,
                    "errors": [str(exc)], "noise": [], "warnings": 0,
                    "bad_pipes": 0, "rows": 0, "seconds": total, "first": first}
        total += elapsed
        current = score(reply)
        if first is None:
            first = dict(current)
        if current["ok"] or attempt == args.rounds:
            return {"case": case.stem, "rounds": attempt, "seconds": round(total, 1),
                    "first": first, **{k: current[k] for k in
                                       ("ok", "errors", "warnings", "bad_pipes", "rows")},
                    "noise": current["noise"], "table": current["table"]}
        messages += [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": REPAIR.format(report="\n".join(current["errors"]), pipes=PIPES)},
        ]
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Замер локальных моделей на xlsx2bpmn")
    ap.add_argument("--url", required=True,
                    help="базовый URL, напр. http://ХОСТ:8080 (Open WebUI) "
                         "или http://ХОСТ:11434 (Ollama)")
    ap.add_argument("--key", default="", help="токен Open WebUI, для Ollama не нужен")
    ap.add_argument("--api", choices=["owui", "ollama"], default="owui",
                    help="owui -> /api/chat/completions, ollama -> /v1/chat/completions")
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--rounds", type=int, default=2,
                    help="сколько раз возвращать отчёт валидатора на починку (0 — без)")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--out", default="bench/results.json")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    if not base.endswith("chat/completions"):
        suffix = "/api/chat/completions" if args.api == "owui" else "/v1/chat/completions"
        base += suffix
    args.url = base

    cases = sorted(CASES.glob("*.md"))
    if not cases:
        print(f"нет описаний в {CASES}", file=sys.stderr)
        return 2
    print(f"эндпоинт: {args.url}\nсценариев: {len(cases)}, "
          f"раундов починки: {args.rounds}\n")

    results: dict[str, list[dict]] = {}
    for model in args.models:
        print(f"=== {model}")
        rows = []
        for case in cases:
            print(f"  {case.stem} ... ", end="", flush=True)
            row = run_case(args, model, case)
            rows.append(row)
            mark = "OK " if row["ok"] else "СБОЙ"
            extra = f", починок {row['rounds']}" if row["rounds"] else ""
            print(f"{mark} {row['seconds']}с{extra}")
            for line in row["errors"][:3]:
                print(f"      {line}")
            for n in row["noise"]:
                print(f"      мусор: {n}")
        results[model] = rows
        print()

    print("=" * 74)
    print(f"{'модель':<24}{'с 1 раза':>10}{'с починкой':>12}"
          f"{'черты':>8}{'мусор':>8}{'сек/шт':>10}")
    print("-" * 74)
    for model, rows in results.items():
        n = len(rows)
        clean = sum(1 for r in rows if r["first"] and r["first"]["ok"])
        fixed = sum(1 for r in rows if r["ok"])
        pipes = sum(r["first"]["bad_pipes"] for r in rows if r["first"])
        noise = sum(1 for r in rows if r["first"] and r["first"]["noise"])
        secs = sum(r["seconds"] for r in rows) / n
        print(f"{model:<24}{f'{clean}/{n}':>10}{f'{fixed}/{n}':>12}"
              f"{pipes:>8}{noise:>8}{secs:>10.0f}")
    print("=" * 74)
    print("с 1 раза — прошло валидатор без починки; черты — строк с неверным")
    print("числом черт; мусор — ответов с текстом/заборами вокруг таблицы")

    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"\nподробности: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
