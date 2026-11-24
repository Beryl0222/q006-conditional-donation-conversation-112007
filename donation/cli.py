"""命令行入口：分析资料并输出承诺、取货指引、按方视图。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .analyzer import Analyzer
from .inspect_io import load_records
from .views import PartyView, pickup_report


def _print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _print_pickup(report: dict) -> None:
    print("== 现在可以叫谁来取货 ==")
    if not report["现在可取货"]:
        print("（暂无满足全部条件、可立即交割的承诺）")
    for item in report["现在可取货"]:
        print(f"- {item['收货方']}：{item['物资']}，地点 {item['地点'] or '未定'}，"
              f"取货日 {item['取货日'] or '未定'}（依据 {'、'.join(item['依据'])}）")
    print("\n== 暂时不能取货 ==")
    for item in report["暂时不能取货"]:
        print(f"- {item['话题']} / {item['收货方'] or '未定归属'}：{item['状态']}")
        for reason in item["待满足"]:
            print(f"    · {reason}")
        if item["说明"]:
            print(f"    说明：{item['说明']}")
    if report["冻结库存"]:
        print("\n== 争议冻结库存（不得再次派发）==")
        for sku, qty in report["冻结库存"].items():
            print(f"- {sku}：{qty}")
    print("\n== 待回问题 / 澄清 ==")
    for question in report["待回问题"]:
        target = question["to_party"] or "协调员"
        print(f"- [{question['code']}] 致 {target}：{question['text']}")
    if report["缺失上游"]:
        print("\n== 缺失的上游来信 ==")
        for ref in report["缺失上游"]:
            print(f"- {ref}")


def _print_party(view: dict) -> None:
    print(f"== 导给 {view['查看方']} 的交接视图 ==")
    print("\n[承诺]")
    for item in view["承诺"]:
        print(f"- {item['话题']} → {item['对应方']}：{item['物资']}，{item['状态']}"
              + (f"，待满足：{'；'.join(item['未满足条件'])}" if item["未满足条件"] else ""))
        print(f"    可见依据：{'、'.join(item['可见依据']) or '（无可见原话）'}")
    print("\n[交接单]")
    for item in view["交接单"]:
        print(f"- {item['单据']}（{item['状态']}）→ {item['收货方']}：{item['物资']}")
        if item["冻结部分"]:
            print(f"    冻结：{item['冻结部分']}；已签收待追认：{item['已签收待追认']}")
        print(f"    可见依据：{'、'.join(item['可见依据']) or '（无可见原话）'}")
    print("\n[本方曾确认的说法]")
    for item in view["本方曾确认的说法"]:
        flag = f"（{item['效力']}{('：' + item['无效原因']) if item['无效原因'] else ''}）"
        print(f"- {item['消息']} {item['行为']}：{item['说法']} {flag}")
    print("\n[待办与问询]")
    for item in view["待办与问询"]:
        print(f"- [{item['编号']}] {item['事项']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="有条件捐赠承诺的会话确认与交接分析")
    parser.add_argument("path", type=Path, help="资料文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("analyze", help="输出完整结构化分析（JSON）")
    sub.add_parser("pickup", help="现在可以叫谁来取货的人工报告")

    export = sub.add_parser("export", help="导给某一方看的交接内容（按 viewers 过滤）")
    export.add_argument("party", help="查看方名称，如 岭东 / 盐田 / 捐出方")
    export.add_argument("--json", action="store_true", help="输出 JSON")

    missing = sub.add_parser("missing", help="列出缺失上游来信及其影响范围")
    missing.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    records = load_records(args.path)
    evaluation = Analyzer(records).run()

    if args.command == "analyze":
        _print_json(evaluation.to_dict())
    elif args.command == "pickup":
        _print_pickup(pickup_report(evaluation))
    elif args.command == "export":
        view = PartyView(evaluation, args.party).export()
        if args.json:
            _print_json(view)
        else:
            _print_party(view)
    elif args.command == "missing":
        payload = {
            "缺失上游": evaluation.missing_upstream,
            "警告": evaluation.warnings,
            "相关问询": [q.to_dict() for q in evaluation.questions if q.code == "上游缺失"],
        }
        if args.json:
            _print_json(payload)
        else:
            for warning in evaluation.warnings:
                print("- " + warning)
            if not evaluation.warnings:
                print("上游来信齐全，所有话题均可独立结论。")


if __name__ == "__main__":
    main()
