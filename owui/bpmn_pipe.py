"""
title: BPMN-схема
author: xlsx2bpmn
version: 1.1.0
description: Описание процесса словами -> BPMN-схема с картинкой. Правится в диалоге. Понимает и обратный ход: пришлите .bpmn — получите разбор.
"""

# Функция-конвейер (Pipe) для Open WebUI.
#
# Модель здесь делает ровно одну вещь — пишет таблицу процесса. Всё остальное
# (проверка, починка, раскладка, отрисовка) выполняет обычный Python, поэтому
# результат не зависит от того, умеет ли локальная модель вызывать инструменты.
#
# Требует установленного в контейнер OWUI пакета xlsx2bpmn.
# С моделями общается по HTTP напрямую, минуя внутренности OWUI, — так функция
# переживает обновления Open WebUI.

from __future__ import annotations

import asyncio
import base64
import json
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from xlsx2bpmn import apply_layout, convert, layout_process, to_table
from xlsx2bpmn.core import ConvertError
from xlsx2bpmn.render_svg import to_svg

try:                                    # промпт лежит внутри пакета
    from importlib.resources import files as _res

    PROMPT = _res("xlsx2bpmn").joinpath("prompt.md").read_text(encoding="utf-8")
except Exception:                       # noqa: BLE001
    PROMPT = ""

HEADER_RE = re.compile(r"^\s*id\s*\|", re.M)
FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*$", re.M)
THINK_RE = re.compile(r"<think>.*?</think>", re.S)
BPMN_RE = re.compile(r"<\s*(\w+:)?definitions", re.I)
TABLE_BLOCK = re.compile(r"<!--таблица\n(.*?)\n-->", re.S)

REPAIR = """Таблица не прошла проверку. Ошибки ниже; номер строки — это номер
строки таблицы, считая заголовок первой строкой.

{report}

Исправь только эти ошибки и выведи таблицу целиком заново. Формат прежний,
ничего кроме таблицы."""

EDIT = """Вот таблица процесса:

{table}

Пользователь просит изменить её так:

{request}

Внеси только эти изменения и выведи таблицу целиком заново. Формат тот же,
все прежние правила в силе. Ничего кроме таблицы не выводи."""


class Pipe:
    class Valves(BaseModel):
        MODEL: str = Field(
            default="bpmn-worker",
            description="Модель, которая составляет таблицу")
        BASE_URL: str = Field(
            default="http://localhost:8080",
            description="Адрес Open WebUI")
        API_PATH: str = Field(
            default="/api/chat/completions",
            description="Путь к API Open WebUI")
        API_KEY: str = Field(
            default="", description="Токен, если конечная точка его требует")
        REPAIRS: int = Field(
            default=2, description="Сколько раз возвращать модели отчёт об ошибках")
        TIMEOUT: int = Field(
            default=600, description="Сколько секунд ждать ответ модели")
        OUTPUT_DIR: str = Field(
            default="/app/backend/data/bpmn",
            description="Куда складывать готовые .bpmn и .svg")
        SHOW_TABLE: bool = Field(
            default=False,
            description="Показывать таблицу в ответе. Обычно менеджеру не нужна")

    def __init__(self) -> None:
        self.valves = self.Valves()

    def pipes(self) -> list[dict]:
        return [{"id": "bpmn", "name": "BPMN-схема"}]

    # ----------------------------------------------------------------- обмен

    def _call(self, messages: list[dict]) -> str:
        url = self.valves.BASE_URL.rstrip("/") + self.valves.API_PATH
        payload = json.dumps({
            "model": self.valves.MODEL,
            "messages": messages,
            "temperature": 0,
            "stream": False,
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.valves.API_KEY:
            headers["Authorization"] = f"Bearer {self.valves.API_KEY}"

        request = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.valves.TIMEOUT) as resp:
                answer = json.load(resp)
        except urllib.error.HTTPError as exc:
            body = exc.read()[:300].decode("utf-8", "replace")
            raise RuntimeError(f"модель ответила ошибкой {exc.code}: {body}")
        except Exception as exc:                                  # noqa: BLE001
            raise RuntimeError(f"не удалось обратиться к модели по адресу {url} "
                               f"({type(exc).__name__}: {exc})")
        try:
            return answer["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            raise RuntimeError(f"неожиданный ответ модели: {json.dumps(answer)[:300]}")

    async def _ask(self, messages: list[dict]) -> str:
        return await asyncio.to_thread(self._call, messages)

    # ----------------------------------------------------------------- разбор

    @staticmethod
    def _table_of(raw: str) -> str:
        """Вынимает таблицу из ответа модели, отбрасывая всё вокруг."""
        text = FENCE_RE.sub("", THINK_RE.sub("", raw))
        lines = text.split("\n")
        start = next((i for i, l in enumerate(lines) if HEADER_RE.match(l)), None)
        if start is None:
            return ""
        out = []
        for line in lines[start:]:
            if not line.strip():
                continue
            if "|" not in line:
                break
            out.append(line.rstrip())
        return "\n".join(out)

    @staticmethod
    def _stashed(messages: list[dict]) -> str:
        """Достаёт таблицу, спрятанную в прошлом ответе, — для правок словами."""
        for message in reversed(messages):
            if message.get("role") != "assistant":
                continue
            found = TABLE_BLOCK.search(message.get("content") or "")
            if found:
                return found.group(1).strip()
        return ""

    @staticmethod
    def _attached(body: dict, text: str) -> str:
        """Содержимое приложенного файла или вставленного в чат XML."""
        if BPMN_RE.search(text or ""):
            return text
        for item in body.get("files") or []:
            holder = item.get("file") if isinstance(item.get("file"), dict) else item
            data = holder.get("data") if isinstance(holder.get("data"), dict) else {}
            content = data.get("content") or holder.get("content") or ""
            if content:
                return content
        return ""

    # ----------------------------------------------------------------- вывод

    @staticmethod
    def _summary(table: str) -> str:
        """Пересказ таблицы по-человечески. Считается на месте, без модели."""
        rows = [r.split("|") for r in table.split("\n")]
        head = [c.strip() for c in rows[0]]
        index = {name: i for i, name in enumerate(head)}

        def cell(row: list[str], name: str) -> str:
            position = index.get(name)
            return row[position].strip() if position is not None and position < len(row) else ""

        groups: dict[str, list[str]] = {}
        docs: list[str] = []
        for row in rows[1:]:
            kind, name = cell(row, "type"), cell(row, "name") or cell(row, "id")
            if kind == "dataObject":
                docs.append(name)
                continue
            if kind in ("startEvent", "endEvent"):
                continue
            where = cell(row, "lane") or cell(row, "pool") or "Процесс"
            mark = " (развилка)" if kind.endswith("Gateway") else ""
            groups.setdefault(where, []).append(f"{name}{mark}")

        blocks = [f"**{where}**\n" + "\n".join(f"- {step}" for step in steps)
                  for where, steps in groups.items()]
        if docs:
            blocks.append(f"**Документы:** {', '.join(docs)}")
        return "\n\n".join(blocks)

    def _save(self, xml: str, svg: str, table: str) -> list[str]:
        folder = Path(self.valves.OUTPUT_DIR)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            names = []
            for suffix, content in ((".bpmn", xml), (".svg", svg), (".txt", table)):
                path = folder / f"схема-{stamp}{suffix}"
                path.write_text(content, encoding="utf-8")
                names.append(path.name)
            return names
        except OSError:
            return []

    def _reply(self, table: str, xml: str, note: str = "") -> str:
        picture = ""
        try:
            svg = to_svg(xml)
            encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
            picture = f"![Схема процесса](data:image/svg+xml;base64,{encoded})\n\n"
        except Exception:                                          # noqa: BLE001
            svg = ""

        saved = self._save(xml, svg, table) if svg else []
        parts = [note] if note else []
        parts.append(picture + self._summary(table))
        if saved:
            parts.append("Файлы на сервере: " + ", ".join(f"`{n}`" for n in saved))
        if self.valves.SHOW_TABLE:
            parts.append(f"```\n{table}\n```")
        parts.append("_Что-то поправить? Напишите словами — например «убери "
                     "проверку юриста» или «добавь согласование у директора»._")
        parts.append(f"<!--таблица\n{table}\n-->")
        return "\n\n".join(parts)

    # ----------------------------------------------------------------- работа

    async def pipe(self, body: dict, __event_emitter__=None, **kwargs):
        async def say(text: str, done: bool = False) -> None:
            if __event_emitter__:
                await __event_emitter__({"type": "status",
                                         "data": {"description": text, "done": done}})

        messages = body.get("messages") or []
        request = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, list):                      # мультимодальный вид
                    content = " ".join(part.get("text", "") for part in content
                                       if isinstance(part, dict))
                request = (content or "").strip()
                break
        if not request and not (body.get("files") or []):
            return ("Опишите процесс словами — или пришлите файл .bpmn, "
                    "чтобы я его разобрал.")

        if not PROMPT:
            return ("Не найден файл prompt.md внутри пакета xlsx2bpmn. "
                    "Похоже, пакет установлен не полностью — переустановите колесо.")

        # --- обратный ход: прислали готовую диаграмму ----------------------
        diagram = self._attached(body, request)
        if diagram:
            await say("Разбираю присланную схему…")
            try:
                parsed = to_table(diagram)
            except ConvertError as exc:
                await say("Не вышло", True)
                return f"Не удалось прочитать схему: {exc}"
            losses = "\n".join(f"- {i.message}" for i in parsed.warnings[:8])
            await say("Готово", True)
            note = "Разобрал присланную схему."
            if losses:
                note += f"\n\nЧто не уложилось в таблицу:\n{losses}"
            try:
                ready, _ = apply_layout(convert(parsed.table.encode()).xml,
                                        layout_process)
            except Exception:                                      # noqa: BLE001
                ready = diagram
            return self._reply(parsed.table, ready, note)

        # --- прямой ход: построить или поправить ---------------------------
        previous = self._stashed(messages)
        if previous:
            await say("Вношу правку…")
            dialogue = [{"role": "user",
                         "content": PROMPT + "\n\n" + EDIT.format(table=previous,
                                                                  request=request)}]
        else:
            await say("Читаю описание процесса…")
            task = re.sub(r"^Прочитай описание.*?\n.*?\n",
                          "Составь таблицу по описанию процесса в конце "
                          "сообщения. Выведи только таблицу.\n",
                          PROMPT, count=1, flags=re.S | re.M)
            dialogue = [{"role": "user",
                         "content": f"{task}\n\n## Описание процесса\n\n{request}"}]

        problems: list[str] = []
        for attempt in range(self.valves.REPAIRS + 1):
            try:
                answer = await self._ask(dialogue)
            except RuntimeError as exc:
                await say("Сбой", True)
                return (f"Не получилось обратиться к модели `{self.valves.MODEL}`.\n\n"
                        f"{exc}\n\nПроверьте настройки функции: адрес Ollama "
                        f"и имя модели.")

            table = self._table_of(answer)
            if not table:
                problems = ["модель не вернула таблицу"]
                dialogue += [{"role": "assistant", "content": answer},
                             {"role": "user", "content": "Выведи только таблицу, "
                                                         "без пояснений и без ```."}]
                continue

            try:
                built = convert(table.encode("utf-8"))
            except ConvertError as exc:
                problems = [str(exc)]
                built = None

            if built is not None and built.ok:
                await say("Рисую схему…")
                try:
                    xml, _ = apply_layout(built.xml, layout_process)
                except Exception as exc:                           # noqa: BLE001
                    await say("Сбой раскладки", True)
                    return f"Схема собралась, но не разложилась: {exc}"
                await say("Готово", True)
                note = ("Готово." if not attempt else
                        f"Готово (потребовалось исправлений: {attempt}).")
                return self._reply(table, xml, note)

            problems = [str(i) for i in built.errors] if built else problems
            if attempt == self.valves.REPAIRS:
                break
            await say(f"Нашлось несоответствий: {len(problems)}, исправляю…")
            dialogue += [
                {"role": "assistant", "content": answer},
                {"role": "user", "content": REPAIR.format(report="\n".join(problems))},
            ]

        await say("Не сошлось", True)
        return ("Не получилось собрать схему по этому описанию — в нём остались "
                "места, которые можно понять по-разному.\n\nЧто мешает:\n"
                + "\n".join(f"- {p}" for p in problems[:6])
                + "\n\nПопробуйте дописать, что происходит после каждого шага "
                  "и чем процесс заканчивается.")
