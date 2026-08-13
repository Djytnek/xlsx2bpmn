# -*- coding: utf-8 -*-
"""
Отрисовка схемы в PNG.

Растр получается из того же SVG, что рисует render_svg, поэтому картинка
совпадает с векторной один в один. Требует cairosvg — библиотека
необязательная, без неё остаётся SVG.

Единственная точка входа: to_png(xml: str | bytes, scale: float) -> bytes
"""
from __future__ import annotations

from .core import ConvertError
from .render_svg import to_svg

NO_LIBRARY = (
    "Для PNG нужна библиотека cairosvg, а она не установлена.\n"
    "Поставьте её командой:  pip install cairosvg\n"
    "Либо пользуйтесь SVG — он открывается в браузере и вставляется "
    "в документы не хуже."
)

NO_CAIRO = (
    "Библиотека cairosvg установлена, но не находит системную библиотеку "
    "cairo.\nНа Debian и Ubuntu она ставится так:  apt install libcairo2\n"
    "Если прав на это нет, пользуйтесь SVG — он работает без всяких "
    "дополнений."
)


def to_png(xml: str | bytes, scale: float = 2.0) -> bytes:
    """Схема с координатами -> PNG. scale=2 даёт удвоенное разрешение."""
    svg = to_svg(xml)

    try:
        import cairosvg
    except ImportError:
        raise ConvertError(NO_LIBRARY) from None
    except OSError:                      # cairocffi не нашёл libcairo
        raise ConvertError(NO_CAIRO) from None

    try:
        return cairosvg.svg2png(bytestring=svg.encode("utf-8"), scale=scale)
    except Exception as exc:             # noqa: BLE001
        raise ConvertError(f"Не удалось нарисовать PNG: "
                           f"{type(exc).__name__}: {exc}") from exc


def available() -> bool:
    """Можно ли вообще рисовать PNG в этом окружении."""
    try:
        import cairosvg                              # noqa: F401
    except Exception:                                # noqa: BLE001
        return False
    return True
