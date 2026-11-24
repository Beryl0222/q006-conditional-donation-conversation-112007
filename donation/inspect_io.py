"""资料读取与编号校验（与 inspect_records.py 的规则保持一致）。"""

from __future__ import annotations

import json
from pathlib import Path


def load_records(path: str | Path) -> dict:
    """读取 UTF-8 资料并检查记录编号唯一、引用字段类型正确。"""
    with Path(path).open(encoding="utf-8") as stream:
        groups = json.load(stream)
    if not isinstance(groups, dict) or not groups:
        raise ValueError("资料应包含非空的记录类别")
    for kind, records in groups.items():
        if not isinstance(records, list):
            raise ValueError(f"{kind} 应为记录数组")
        ids = set()
        for row in records:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                raise ValueError(f"{kind} 中存在没有编号的记录")
            if row["id"] in ids:
                raise ValueError(f"{kind} 中编号重复 {row['id']}")
            ids.add(row["id"])
    return groups
