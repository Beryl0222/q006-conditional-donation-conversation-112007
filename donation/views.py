"""按各方权限组织对外视图：只带出它有权看到的消息引用。

可见性来源：
- inbox 各消息的 viewers：能取得原话的人；
- turnover 的 shown_to：实际看到该单据的人；
- grant：只说明能代表谁办理何事，不额外授予阅读权。
"""

from __future__ import annotations

from typing import Optional

from .analyzer import Evaluation
from .model import (
    STATE_BLOCKED,
    STATE_DISPUTED,
    STATE_EFFECTIVE,
    STATE_PENDING,
    TURNOVER_DISPUTED,
)


class PartyView:
    def __init__(self, evaluation: Evaluation, party: str):
        self.ev = evaluation
        self.party = party

    def _can_see(self, message_id: str) -> bool:
        msg = self.ev.messages.get(message_id)
        return msg is not None and self.party in msg.viewers

    def _filter_refs(self, ids: list[str]) -> list[str]:
        return [mid for mid in ids if mid and self._can_see(mid)]

    def _visible_principals(self) -> set[str]:
        """本方作为委托方时，其代理人发出的消息也归入本方视角。"""
        result = {self.party}
        for grant in self.ev.records.get("grant") or []:
            if grant.get("principal") == self.party:
                result.add(grant.get("agent"))
        return result

    def export(self) -> dict:
        """导给某一方看的交接内容：只带它有权看到的原话引用。"""
        visible_senders = self._visible_principals()
        commitments = []
        for commitment in self.ev.commitments:
            visible_refs = self._filter_refs(commitment.basis)
            # 一条依据都看不到的承诺（如别的站点独立单）整组不导出
            if not visible_refs:
                continue
            if commitment.receiver not in (None, self.party) and not self._is_counterparty(commitment):
                continue
            commitments.append(
                {
                    "话题": commitment.thread,
                    "对应方": commitment.receiver,
                    "物资": commitment.items,
                    "状态": commitment.state,
                    "收货地点": commitment.location,
                    "取货日": commitment.pickup,
                    "未满足条件": commitment.unmet,
                    "可见依据": visible_refs,
                    "说明": commitment.note,
                }
            )
        turnovers = []
        review_ids = {r.turnover_id for r in self.ev.turnover_reviews}
        for turnover in self.ev.records_turnovers():
            if self.party not in turnover.get("shown_to", []):
                continue
            review = next(r for r in self.ev.turnover_reviews
                          if r.turnover_id == turnover["id"])
            turnovers.append(
                {
                    "单据": review.revision,
                    "状态": review.state,
                    "收货方": review.receiver,
                    "物资": review.items,
                    "冻结部分": review.frozen_lines,
                    "已签收待追认": review.received_lines,
                    "可见依据": self._filter_refs(review.basis),
                    "冲突消息": self._filter_refs(review.conflicts),
                    "说明": review.note,
                }
            )
        statements = [
            {
                "发言人": s.party,
                "消息": s.message_id,
                "发送时间": s.sent.isoformat(),
                "行为": s.act_kind,
                "说法": s.summary,
                "效力": ("有效" if s.valid else "无效" if s.valid is False else "—"),
                "无效原因": s.invalid_reason,
            }
            for s in self.ev.statements
            if s.party in visible_senders and self._can_see(s.message_id)
        ]
        questions = [
            {
                "编号": q.code,
                "事项": q.text,
                "关联消息": self._filter_refs(q.related),
            }
            for q in self.ev.questions
            if q.to_party in (None, self.party)
        ]
        return {
            "查看方": self.party,
            "承诺": commitments,
            "交接单": turnovers,
            "本方曾确认的说法": statements,
            "待办与问询": questions,
            "缺失的上游来信": self.ev.missing_upstream,
        }

    def _is_counterparty(self, commitment) -> bool:
        # 捐出方是所有承诺的相对方；其余站点只见与自己有关的承诺
        if self.party == self._donor_name():
            return True
        return False

    def _donor_name(self) -> Optional[str]:
        for offer in self.ev.offers:
            return offer.sender
        return None


def pickup_report(evaluation: Evaluation) -> dict:
    """“现在可以叫谁来取货”：有效承诺、待回问题、每方曾确认的说法。"""
    callable_now = []
    blocked = []
    for commitment in evaluation.commitments:
        entry = {
            "话题": commitment.thread,
            "收货方": commitment.receiver,
            "物资": commitment.items,
            "地点": commitment.location,
            "取货日": commitment.pickup,
            "依据": commitment.basis,
        }
        if commitment.state == STATE_EFFECTIVE and not commitment.unmet:
            callable_now.append(entry)
        else:
            blocked.append({**entry, "状态": commitment.state, "待满足": commitment.unmet,
                            "说明": commitment.note})

    frozen_stock = {}
    for review in evaluation.turnover_reviews:
        if review.state == TURNOVER_DISPUTED:
            for sku, qty in review.frozen_lines.items():
                frozen_stock[sku] = frozen_stock.get(sku, 0) + qty

    return {
        "现在可取货": callable_now,
        "暂时不能取货": blocked,
        "冻结库存": frozen_stock,
        "待回问题": [q.to_dict() for q in evaluation.questions],
        "各方说法": [s.to_dict() for s in evaluation.statements],
        "缺失上游": evaluation.missing_upstream,
    }
