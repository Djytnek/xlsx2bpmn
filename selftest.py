# -*- coding: utf-8 -*-
"""Проверка на встроенном шаблоне. Запуск: python selftest.py"""
import xml.etree.ElementTree as ET
from pathlib import Path

from xlsx2bpmn import apply_layout, convert, layout_process, to_table, to_workbook
from xlsx2bpmn.layout import DI, B

TEMPLATE = Path(__file__).parent / "xlsx2bpmn" / "template.xlsx"


def main() -> int:
    result = convert(TEMPLATE.read_bytes())
    assert result.ok, [str(i) for i in result.issues]
    print(f"конвертация: {result.stats}")

    xml, warnings = apply_layout(result.xml, layout_process)
    root = ET.fromstring(xml)
    plane = root.find(f".//{DI}BPMNPlane")
    shapes = [e for e in plane if e.tag == f"{DI}BPMNShape"]
    edges = [e for e in plane if e.tag == f"{DI}BPMNEdge"]
    print(f"раскладка: фигур {len(shapes)}, стрелок {len(edges)}")

    semantic = {n.get("id") for p in root.findall(f"{B}process") for n in p
                if n.tag.split("}")[1] not in ("laneSet", "dataObject")}
    drawn = {e.get("bpmnElement") for e in shapes + edges}
    missing = semantic - drawn
    assert not missing, f"без координат: {missing}"
    print("все элементы получили координаты")

    objects = {n.get("id") for p in root.findall(f"{B}process")
               for n in p.findall(f"{B}dataObject")}
    bad = [e for e in shapes if e.get("bpmnElement") in objects]
    assert not bad, "у dataObject не должно быть фигуры"
    print("документы отрисованы корректно")

    again, _ = apply_layout(result.xml, layout_process)
    assert again == xml, "результат недетерминирован"
    print("результат детерминирован")

    # --- обратный ход: круг должен замыкаться ------------------------------
    back = to_table(xml)
    assert back.ok, [str(i) for i in back.issues]
    assert not back.errors, [str(i) for i in back.errors]
    print(f"разбор обратно: строк {back.stats['nodes']}, "
          f"колонок {back.stats['columns']}, потерь {len(back.warnings)}")

    rebuilt = convert(back.table.encode("utf-8"))
    assert rebuilt.ok, [str(i) for i in rebuilt.issues]

    def semantic(text: str) -> str:
        node = ET.fromstring(text)
        for diagram in node.findall(f"{DI}BPMNDiagram"):
            node.remove(diagram)
        ET.indent(node, space="  ")
        return ET.tostring(node, encoding="unicode")

    assert semantic(result.xml) == semantic(rebuilt.xml), "круг исказил схему"
    print("таблица -> схема -> таблица -> схема: смысл сохранён")

    twice, _ = apply_layout(rebuilt.xml, layout_process)
    assert to_table(twice).table == back.table, "таблица не является неподвижной точкой"
    print("таблица — неподвижная точка")

    book = to_workbook(back, TEMPLATE.read_bytes())
    from_book = convert(book)
    assert from_book.ok, [str(i) for i in from_book.issues]
    assert from_book.stats["nodes"] == result.stats["nodes"], "в xlsx потерялись строки"
    print("выгрузка в xlsx читается обратно")

    for w in warnings:
        print(f"  предупреждение: {w}")
    print("\nВСЁ ХОРОШО")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
