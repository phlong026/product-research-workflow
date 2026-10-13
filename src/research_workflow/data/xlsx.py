from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

_ILLEGAL = {c for c in range(32) if c not in (9, 10, 13)}


def _cell(value: Any) -> Any:
    """None 保持为空单元格（未知≠0）；容器类型转 JSON 文本；去除 Excel 不允许的控制字符。"""
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, (list, tuple, dict)):
        value = json.dumps(value, ensure_ascii=False)
    text = str(value)
    return "".join(ch for ch in text if ord(ch) not in _ILLEGAL)


def write_xlsx(path: str | Path, sheets: dict[str, Iterable[dict[str, Any]]]) -> Path:
    """写出可编辑的多工作表 Excel（纯数值/文本，无公式、无宏）。"""
    try:
        from openpyxl import Workbook
    except ImportError as exc:  # pragma: no cover - dependency boundary
        raise RuntimeError("需要 data 可选依赖: pip install -e '.[data]'") from exc
    workbook = Workbook()
    workbook.remove(workbook.active)
    for raw_name, rows in sheets.items():
        name = "".join(ch for ch in raw_name if ch not in "[]:*?/\\")[:31] or "Sheet"
        sheet = workbook.create_sheet(name)
        rows = list(rows)
        headers: list[str] = []
        for row in rows:
            for key in row:
                if key not in headers:
                    headers.append(key)
        if headers:
            sheet.append(headers)
            for row in rows:
                sheet.append([_cell(row.get(h)) for h in headers])
            sheet.freeze_panes = "A2"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path
