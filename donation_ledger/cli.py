"""协调员查看承诺状态、交接单和取货清单的命令行工具。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .ledger import Analysis, analyse
from .models import load_packet


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _summarise(analysis: Analysis) -> dict[str, object]:
    status = analysis.status()
    now = analysis.pickup_view()
    return {
        "提供状况": status["offers"],
        "各方答复": status["stances"],
        "交接单": status["turnovers"],
        "版本记录": status["versions"],
        "签收": status["receipts"],
        "现在可取货": now["now"],
        "待回问题": now["pending_questions"],
        "冻结部分": now["frozen"],
        "需要捐出方澄清": now["clarifications"],
        "目前无法下结论": analysis.unresolved,
        "缺引用": analysis.missing_refs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="核对公益站的消息承诺、交接单与取货资格（仅用合成资料）"
    )
    parser.add_argument("path", type=Path, help="资料文件路径")
    parser.add_argument("--list", action="store_true", help="只列记录类别与数量")
    sub = parser.add_subparsers(dest="command")

    status = sub.add_parser("status", help="全部承诺与单据状态")
    status.add_argument("--json", action="store_true", help="输出 JSON")
    status.add_argument("--party", help="只看某方可以取得的说法与引用")

    pickup = sub.add_parser("pickup", help="现在可以叫谁来取货")
    pickup.add_argument("--json", action="store_true", help="输出 JSON")

    export = sub.add_parser("export", help="导出给某一方看的交接内容")
    export.add_argument("turnover", help="交接单编号或版本，例如 H-67/1")
    export.add_argument("--party", required=True, help="给哪一方看")
    export.add_argument("--json", action="store_true", help="输出 JSON")

    messages = sub.add_parser("messages", help="列出某方在某一话题下有权看到的原话")
    messages.add_argument("party")
    messages.add_argument("--thread")

    args = parser.parse_args(argv)

    if args.list:
        from inspect_records import load_records

        for kind, group in load_records(args.path).items():
            print(f"{kind}　{len(group)} 条")
        return 0

    packet = load_packet(args.path)
    analysis = analyse(packet)

    if args.command == "pickup":
        payload = analysis.pickup_view()
        if args.json:
            _print_json(payload)
            return 0
        print("现在可以取货：")
        if not payload["now"]:
            print("  （没有）")
        for row in payload["now"]:
            items = "、".join(f"{sku} {qty:g}箱" for sku, qty in row["items"].items())
            print(f"  {row['party']}：{items}　凭 {row['turnover']}（{'、'.join(row['basis'])}）")
        print("\n待回问题：")
        for row in payload["pending_questions"]:
            print(f"  [{row['party']}] {row['question']}（{row['reason']}）")
        print("\n冻结的未交割部分：")
        for row in payload["frozen"]:
            items = "、".join(f"{sku} {qty:g}箱" for sku, qty in row["items"].items())
            signed = "、".join(
                f"{sku} {qty:g}箱" for sku, qty in row.get("already_signed", {}).items()
            )
            label = items or "（无未交割数量）"
            if signed:
                label += f"（已签收 {signed}）"
            print(f"  {label}：{row['reason']}")
        if payload["clarifications"]:
            print("\n需要捐出方澄清：")
            for row in payload["clarifications"]:
                print(f"  {row['question']}（{row['reason']}）")
        if analysis.unresolved:
            print("\n目前无法下结论：")
            for line in analysis.unresolved:
                print(f"  {line}")
        return 0

    if args.command == "export":
        payload = analysis.export_for(args.party, args.turnover)
        if payload is None:
            parser.error(f"找不到交接单 {args.turnover}")
        _print_json(payload)
        return 0

    if args.command == "messages":
        for message in sorted(packet.messages.values(), key=lambda row: row.sent):
            if not message.can_be_seen_by(args.party):
                continue
            if args.thread and message.thread != args.thread:
                continue
            print(f"{message.id}　{message.sent.isoformat()}　{message.sender}")
            print(f"  {message.text}")
        return 0

    payload = _summarise(analysis)
    if args.json:
        _print_json(payload)
        return 0
    if getattr(args, "party", None):
        payload["各方答复"] = analysis.statements_by(args.party)
        payload["可见引用"] = analysis.visible_refs(args.party)
    for title, rows in payload.items():
        print(f"## {title}")
        if isinstance(rows, (list, tuple)):
            for row in rows:
                print("  " + json.dumps(row, ensure_ascii=False, default=str))
        elif isinstance(rows, dict):
            for key, value in rows.items():
                print(f"  {key}：{json.dumps(value, ensure_ascii=False, default=str)}")
        else:
            print(f"  {rows}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
