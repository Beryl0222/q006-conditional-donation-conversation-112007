"""按发送顺序重核承诺、条件、授权、交接单与争议。

关键约定（对应需求原文）：
- 逾期含义服从原约定与*有效答复的发送顺序*：补录一条较早发出的答复后，
  重新核对谁在何时获得决定权；判断一律以 sent 为准，ingested 只用于复原
  交接单开出当时协调员能看到什么。
- 代收点只授权确认收货地点，就不能替委托方同意少发一箱。
- 配套只同意一半时，已同意部分能否单独成立取决于原话是否允许拆分。
- 条件可同时被两方满足且原话未说明先后时，只发问询，不替捐出方决定。
- 交接单开出后才出现相互冲突的早期消息：冻结有争议的未交割部分，
  已签收部分转人工追认，不重新派发。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from .model import (
    ACT_ACCEPT,
    ACT_CONDITIONAL_ACCEPT,
    ACT_INSTRUCTION,
    ACT_OFFER,
    ACT_REFUSE,
    ACT_REQUEST_CONFIRM,
    ACT_SUBSTITUTE,
    COND_DEADLINE,
    COND_OTHER_PARTY,
    COND_PROCEDURE,
    STATE_BLOCKED,
    STATE_DISPUTED,
    STATE_EFFECTIVE,
    STATE_PENDING,
    TURNOVER_DISPUTED,
    TURNOVER_PENDING,
    TURNOVER_REJECTED,
    TURNOVER_VALID,
    Commitment,
    Condition,
    Offer,
    OfferLine,
    ParsedMessage,
    Question,
    Statement,
    TurnoverReview,
    parse_dt,
)
from .parser import ItemRegister, parse_message, resolve_references

SCOPE_LOCATION = "指定收货地点"


@dataclass
class Evaluation:
    """整包资料分析后的完整结论。"""

    records: dict
    messages: dict[str, ParsedMessage]
    offers: list[Offer]
    commitments: list[Commitment]
    statements: list[Statement]
    turnover_reviews: list[TurnoverReview]
    questions: list[Question]
    missing_upstream: list[str]
    warnings: list[str] = field(default_factory=list)

    def records_turnovers(self) -> list[dict]:
        return list(self.records.get("turnover") or [])

    def to_dict(self) -> dict:
        return {
            "messages": {mid: msg.to_dict() for mid, msg in self.messages.items()},
            "offers": [offer.to_dict() for offer in self.offers],
            "commitments": [item.to_dict() for item in self.commitments],
            "statements": [item.to_dict() for item in self.statements],
            "turnover_reviews": [item.to_dict() for item in self.turnover_reviews],
            "questions": [item.to_dict() for item in self.questions],
            "missing_upstream": self.missing_upstream,
            "warnings": self.warnings,
        }


class Analyzer:
    def __init__(self, records: dict):
        self.records = records
        self.register = ItemRegister(records["item_register"])
        self.messages: dict[str, ParsedMessage] = {}
        self.grants = list(records.get("grant") or [])
        self.turnovers = list(records.get("turnover") or [])
        self.receipts = list(records.get("receipt") or [])
        self.missing_upstream: list[str] = []
        self.warnings: list[str] = []
        self.questions: list[Question] = []
        self.statements: list[Statement] = []
        self._statement_key: dict[tuple[str, int], Statement] = {}

    # ------------------------------------------------------------------ run
    def run(self) -> Evaluation:
        self._parse_all()
        offers = self._build_offers()
        self._record_statements(offers)
        commitments = self._evaluate_commitments(offers)
        reviews = self._review_turnovers(offers, commitments)
        self._collect_missing()
        return Evaluation(
            records=self.records,
            messages=self.messages,
            offers=offers,
            commitments=commitments,
            statements=self.statements,
            turnover_reviews=reviews,
            questions=self.questions,
            missing_upstream=sorted(set(self.missing_upstream)),
            warnings=self.warnings,
        )

    def _parse_all(self) -> None:
        for row in self.records["inbox"]:
            parsed = parse_message(row, self.register)
            self.messages[parsed.id] = parsed
        resolve_references(self.messages, self.register)

    # --------------------------------------------------------------- offers
    def _build_offers(self) -> list[Offer]:
        offers: list[Offer] = []
        for msg in sorted(self.messages.values(), key=lambda m: m.sent):
            offer_acts = [act for act in msg.acts if act.kind == ACT_OFFER and act.items]
            if not offer_acts:
                continue
            mentions = [m for act in offer_acts for m in act.items]
            extra_conditions = [
                cond
                for act in msg.acts
                if act.kind == ACT_INSTRUCTION
                for cond in act.conditions
            ]
            # 成对/不可拆的物品归为一组；声明可单送的物品各自独立成组
            paired_lines = [m for m in mentions if m.paired and not m.splittable]
            free_lines = [m for m in mentions if not (m.paired and not m.splittable)]
            groups: list[list] = []
            if paired_lines:
                groups.append(paired_lines)
            groups.extend([[line] for line in free_lines])
            primary, fallback = self._parties_from_offer(msg.text)
            for group in groups:
                # 只有成对主约适用“首选点/逾期改给”规则；独立可单送的物品
                # 若在独立小句里另有收件人，按该小句解析；没有则不挂双方，
                # 也不继承主约的截止/改派条件，等待具体接收方答复。
                group_primary, group_fallback = primary, fallback
                group_deadline = msg.deadline
                group_conditions = list(msg.offer_conditions) + extra_conditions
                if not any(m.paired and not m.splittable for m in group):
                    standalone = self._standalone_party(msg.text, group[0])
                    if standalone is not None:
                        group_primary, group_fallback = standalone, None
                    else:
                        group_primary, group_fallback = None, None
                        group_deadline = None
                        group_conditions = extra_conditions
                lines = [
                    OfferLine(
                        sku=m.sku,
                        qty=m.qty,
                        paired=bool(m.paired and not m.splittable),
                        splittable=m.splittable or not m.paired,
                        raw=m.raw,
                    )
                    for m in group
                ]
                offers.append(
                    Offer(
                        thread=msg.thread,
                        source_id=msg.id,
                        sender=msg.sender,
                        lines=lines,
                        primary_party=group_primary,
                        fallback_party=group_fallback,
                        deadline=group_deadline,
                        conditions=group_conditions,
                        pickup=msg.pickup,
                    )
                )
        return offers

    @staticmethod
    def _parties_from_offer(text: str) -> tuple[Optional[str], Optional[str]]:
        first = None
        match = re.search(r"给([一-龥]{2,4})[；。，]?", text)
        if match:
            first = match.group(1)
        fallback = None
        match = re.search(r"改给\s*([一-龥]{2,4})", text)
        if match:
            fallback = match.group(1)
        return first, fallback

    def _standalone_party(self, text: str, mention) -> Optional[str]:
        """在独立/可单送小句里解析专门的收件方，如“B11 一箱独立给盐田”。"""
        row = self.register.rows.get(mention.sku, {})
        needles = [mention.sku, row.get("description", ""), row.get("description", "")[-2:]]
        for clause in re.split(r"[。；;]", text):
            if any(needle and needle in clause for needle in needles) and re.search(
                r"独立|可单送|单独", clause
            ):
                match = re.search(r"给\s*([一-龥]{2,4})", clause)
                if match:
                    return match.group(1)
        return None

    # ----------------------------------------------------------- statements
    def _record_statements(self, offers: list[Offer]) -> None:
        for msg in sorted(self.messages.values(), key=lambda m: m.sent):
            for index, act in enumerate(msg.acts):
                if act.kind == ACT_OFFER and not msg.reply_to and not msg.forwards:
                    continue
                summary = act.option_text or act.text
                self.statements.append(
                    Statement(
                        party=msg.sender,
                        message_id=msg.id,
                        sent=msg.sent,
                        act_kind=act.kind,
                        summary=summary,
                        location=act.location,
                    )
                )
                # 记下行为对象与语句的对应，避免同消息多条同类行为互相串标
                self._statement_key[(msg.id, index)] = self.statements[-1]

    # -------------------------------------------------------- commitments
    def _evaluate_commitments(self, offers: list[Offer]) -> list[Commitment]:
        commitments: list[Commitment] = []
        # 同一 sku+数量只能确认给一个点
        allocated: dict[tuple[str, int], str] = {}
        for offer in offers:
            result = self._evaluate_offer(offer)
            commitments.extend(result)
            for commitment in result:
                if commitment.state == STATE_EFFECTIVE:
                    for sku, qty in commitment.items.items():
                        key = (sku, qty)
                        if key in allocated and allocated[key] != commitment.receiver:
                            self.questions.append(
                                Question(
                                    code="重复确认",
                                    topic=f"{sku} 被两处确认",
                                    to_party=offer.sender,
                                    text=(
                                        f"{sku}×{qty} 已确认给 {allocated[key]}，"
                                        f"又出现确认给 {commitment.receiver} 的有效说法；"
                                        "原话未说明先后，须由捐出方澄清，不得自行改派。"
                                    ),
                                    related=[offer.source_id, commitment.basis[0] if commitment.basis else ""],
                                )
                            )
                        else:
                            allocated[key] = commitment.receiver or ""
        return commitments

    def _evaluate_offer(self, offer: Offer) -> list[Commitment]:
        replies = self._replies_chain(offer)
        candidates: list[tuple[ParsedMessage, object, Optional[str]]] = []
        eligible = {offer.primary_party, offer.fallback_party} - {None}
        # 代理人代表的委托方若在候选名单里，代理人的答复也要纳入评估
        represented = self._represented_principals()
        for msg in replies:
            is_agent_reply = msg.sender in represented and represented[msg.sender] in eligible
            if eligible and msg.sender not in eligible and not is_agent_reply:
                continue
            for index, act in enumerate(msg.acts):
                # 效力标注独立于候选筛选：越权替代、条件未成就等也要留痕
                if act.kind in (ACT_ACCEPT, ACT_CONDITIONAL_ACCEPT, ACT_SUBSTITUTE):
                    self._annotate_statement(msg, index, act, offer)
                if act.kind not in (ACT_ACCEPT, ACT_CONDITIONAL_ACCEPT, ACT_SUBSTITUTE):
                    continue
                # 条件前缀之类的空物品小句不是承诺，不参与候选
                if not act.items:
                    continue
                # 行为只涉及别的物品（如成对主约的答复提到独立物品）时与本要约无关
                if not {m.sku for m in act.items} & offer.sku_set:
                    continue
                reason = self._invalid_reason(offer, msg, act)
                candidates.append((msg, act, reason))

        valid = [(msg, act) for msg, act, reason in candidates if reason is None]
        commitments: list[Commitment] = []
        items = {line.sku: line.qty for line in offer.lines}

        if not valid:
            unmet = self._open_requirements(offer, candidates)
            commitments.append(
                Commitment(
                    offer_source=offer.source_id,
                    thread=offer.thread,
                    receiver=None,
                    items=items,
                    state=STATE_PENDING if not self._chain_broken(offer) else STATE_BLOCKED,
                    basis=[offer.source_id],
                    conditions=list(offer.conditions),
                    unmet=unmet,
                    pickup=offer.pickup,
                    note=("上游来信缺失，无法替双方给出结论。" if self._chain_broken(offer)
                          else "尚无满足全部条件的有效接受。"),
                )
            )
            return commitments

        # 多个有效接受：原话给了先后（首选/逾期改给）按原话；否则交捐出方澄清
        chosen = self._choose_party(offer, valid)
        if chosen is None:
            parties = sorted({msg.sender for msg, _ in valid})
            self.questions.append(
                Question(
                    code="条件同时满足",
                    topic=offer.thread,
                    to_party=offer.sender,
                    text=(
                        "以下各方对同一组物资的接受条件同时成立，原话未规定先后："
                        + "、".join(parties)
                        + "。请捐出方澄清归属，系统不替人决定。"
                    ),
                    related=[offer.source_id] + [msg.id for msg, _ in valid],
                )
            )
            commitments.append(
                Commitment(
                    offer_source=offer.source_id,
                    thread=offer.thread,
                    receiver=None,
                    items=items,
                    state=STATE_BLOCKED,
                    basis=[offer.source_id] + [msg.id for msg, _ in valid],
                    conditions=list(offer.conditions),
                    unmet=["捐出方澄清归属"],
                    note="条件同时满足且原话未说明先后。",
                )
            )
            return commitments

        chosen_msg, chosen_act = chosen
        # 接受行为自身条件 + 要约上的生效条件（截止/改派类是路由规则，不计入未满足项）
        pending: list[str] = []
        for cond in [*offer.conditions, *chosen_act.conditions]:
            if cond.kind == COND_DEADLINE:
                continue
            if not self._condition_met(cond, offer, chosen_msg):
                pending.append(cond.text)
        unmet = pending
        state = STATE_EFFECTIVE if not unmet else STATE_PENDING
        # 取货日：优先接受方原话，其次要约/协调员转述中的周六安排
        pickup = chosen_msg.pickup or offer.pickup
        if pickup is None:
            pickup = self._pickup_along_chain(chosen_msg)
        commitments.append(
            Commitment(
                offer_source=offer.source_id,
                thread=offer.thread,
                receiver=chosen_msg.sender,
                items=items,
                state=state,
                basis=[offer.source_id, chosen_msg.id],
                conditions=list(offer.conditions) + list(chosen_act.conditions),
                unmet=unmet,
                location=chosen_act.location or self._location_along_chain(chosen_msg),
                pickup=pickup,
                note="",
            )
        )
        # 被原话先后规则排除的他方接受，留痕并发问询
        for msg, act in valid:
            if msg.id != chosen_msg.id:
                self.questions.append(
                    Question(
                        code="决定权顺序",
                        topic=offer.thread,
                        to_party=offer.sender,
                        text=(
                            f"{msg.sender} 在 {msg.sent:%Y-%m-%d %H:%M} 的接受"
                            f"（{msg.id}）与 {chosen_msg.sender} 的有效接受冲突；"
                            f"按原约定的发送顺序，决定权已在截止前归于 {chosen_msg.sender}，"
                            "请捐出方确认是否另有安排。"
                        ),
                        related=[offer.source_id, msg.id, chosen_msg.id],
                    )
                )
        return commitments

    # --------------------------------------------------- acceptance checks
    def _invalid_reason(self, offer: Offer, msg: ParsedMessage, act) -> Optional[str]:
        # 1. 授权范围：代理人只能在 grant 权能内行事
        grant = self._grant_for(msg.sender)
        if grant is not None:
            if not self._grant_active(grant, msg.sent):
                return f"授权 {grant['id']} 已过期"
            scopes = set(grant["scopes"])
            if act.kind in (ACT_ACCEPT, ACT_CONDITIONAL_ACCEPT, ACT_SUBSTITUTE):
                # 只拿到“指定收货地点”权能的代收点，不能替委托方接受或改方案
                if scopes == {SCOPE_LOCATION}:
                    return "仅授权指定收货地点，不能替委托方同意接受、少发或拆分"
                if self._reduces_offer(offer, act):
                    return "代理人主张缩减了要约内容，超出授权范围"
            if act.location and SCOPE_LOCATION not in scopes:
                return "代理人无权指定收货地点"
        # 2. 截止时间（按发送时间）
        if offer.deadline is not None and msg.sent > offer.deadline:
            # 逾期答复只有在触发原约定的改派前提时才有意义
            if not self._deadline_triggered(offer):
                return "答复晚于原约定截止时间，且改派前提未成就"
        # 3. 配套完整性：成对物资不允许只接一半，除非原话允许拆分
        wanted = {m.sku: m.qty for m in act.items}
        offer_items = {line.sku: line.qty for line in offer.lines}
        if any(line.paired and not line.splittable for line in offer.lines):
            if set(wanted) != set(offer_items):
                return "原约定要求成对交割，该答复只接受配套的一部分，原话不允许拆分"
        for sku, qty in wanted.items():
            if sku in offer_items and qty < offer_items[sku]:
                return f"{sku} 数量少于要约，构成替代而非接受"
        # 4. 附条件接受：逐条机判，无法机判的条件保留待人工
        for cond in act.conditions:
            met, evaluable = self._condition_met(cond, offer, msg, with_flag=True)
            if evaluable and not met:
                return f"附条件未成就：{cond.text}"
            if not evaluable:
                return f"条件无法机判，需人工核对：{cond.text}"
        return None

    def _reduces_offer(self, offer: Offer, act) -> bool:
        wanted = {m.sku for m in act.items}
        return bool(wanted) and wanted != offer.sku_set

    def _annotate_statement(self, msg: ParsedMessage, index: int, act, offer: Offer) -> None:
        reason = self._invalid_reason(offer, msg, act)
        statement = self._statement_key.get((msg.id, index))
        if statement is None:
            return
        # 无物品的寒暄/条件前缀小句不做效力判断
        statement.valid = None if not act.items else (reason is None)
        statement.invalid_reason = reason if act.items else None

    def _grant_for(self, sender: str) -> Optional[dict]:
        for grant in self.grants:
            if grant["agent"] == sender:
                return grant
        return None

    def _represented_principals(self) -> dict[str, str]:
        """返回 {代理人: 委托方} 映射。"""
        return {grant["agent"]: grant["principal"] for grant in self.grants}

    @staticmethod
    def _grant_active(grant: dict, when: datetime) -> bool:
        expires = datetime.combine(date.fromisoformat(grant["expires"]),
                                   datetime.max.time(), tzinfo=when.tzinfo)
        return when <= expires

    def _deadline_triggered(self, offer: Offer) -> bool:
        """原约定“截止前无人确认合送和收货便改给”是否成就——按发送顺序。"""
        if offer.deadline is None:
            return False
        for msg in self._replies_chain(offer):
            if msg.sent > offer.deadline:
                continue
            for act in msg.acts:
                if act.kind in (ACT_ACCEPT, ACT_CONDITIONAL_ACCEPT) and self._is_full_accept(offer, act):
                    return False
        return True

    def _is_full_accept(self, offer: Offer, act) -> bool:
        wanted = {m.sku: m.qty for m in act.items}
        return wanted == {line.sku: line.qty for line in offer.lines}

    # ---------------------------------------------------------- conditions
    def _condition_met(self, cond: Condition, offer: Offer, msg: ParsedMessage,
                       with_flag: bool = False):
        # 答复链上游缺失：他方状态类条件无法机判，不能默认成就
        if cond.kind == COND_OTHER_PARTY and self._chain_broken(offer):
            return (False, False) if with_flag else False
        if cond.kind == COND_DEADLINE:
            met = self._deadline_triggered(offer)
            return (met, True) if with_flag else met
        if cond.kind == COND_OTHER_PARTY:
            met = self._party_state_matches(cond, offer)
            return (met, True) if with_flag else met
        if cond.kind == COND_PROCEDURE:
            # 补签等手续：资料里没有完成凭证即视为未满足
            return (False, True) if with_flag else False
        return (False, False) if with_flag else False

    def _party_state_matches(self, cond: Condition, offer: Offer) -> bool:
        target = cond.party
        if target in ("对方",):
            target = offer.primary_party
        expected = cond.expected or ""
        if expected.startswith("未"):
            # 期望他方在截止前未确认：截止前存在完整接受则条件不成就
            if offer.deadline is None:
                return False
            for msg in self._replies_chain(offer):
                if msg.sent > offer.deadline or msg.sender != target:
                    continue
                if any(act.kind == ACT_ACCEPT and self._is_full_accept(offer, act)
                       for act in msg.acts):
                    return False
            return True
        if expected == "已签收":
            return any(
                receipt["receiver"] == target
                and any(sku in receipt["items"] for sku in offer.sku_set)
                for receipt in self.receipts
            )
        return False

    # ------------------------------------------------------------- routing
    def _choose_party(self, offer: Offer, valid):
        """按原话规定的先后选择有效接受的归属；原话无先后则返回 None。"""
        if offer.primary_party is not None or offer.fallback_party is not None:
            for msg, act in valid:
                if msg.sender == offer.primary_party:
                    return msg, act
            for msg, act in valid:
                if msg.sender == offer.fallback_party and self._deadline_triggered(offer):
                    return msg, act
            return None
        return None

    def _open_requirements(self, offer, candidates) -> list[str]:
        unmet: list[str] = []
        if offer.deadline is not None:
            unmet.append(f"需在 {offer.deadline:%Y-%m-%d %H:%M} 前确认合送与收货")
        for msg, act, reason in candidates:
            if reason:
                unmet.append(f"{msg.id}（{msg.sender}）无效：{reason}")
        for cond in offer.conditions:
            unmet.append(f"条件待满足：{cond.text}")
        return unmet

    def _chain_broken(self, offer: Offer) -> bool:
        """要约结论依赖的消息链上是否存在缺失的上游引用。

        断链消息无法通过 _roots_to 被发现，因此直接扫描全量消息：
        同 thread 缺引用，或缺失的引用目标本应把该消息接回本要约，都算断链。
        """
        source = self.messages.get(offer.source_id)
        if source is not None and source.references_missing:
            return True
        chain_ids = {msg.id for msg in self._replies_chain(offer)}
        for msg in self.messages.values():
            if not msg.references_missing:
                continue
            if msg.id in chain_ids or msg.thread == offer.thread:
                return True
            # 跨 thread：它回复/转发的目标缺失，而它提到了本要约的物品
            if {m.sku for m in msg.item_mentions} & offer.sku_set:
                return True
        return False

    def _replies_chain(self, offer: Offer) -> list[ParsedMessage]:
        """关联到本要约的答复消息，按发送时间排序。

        一条消息只有在 reply_to / forwards 链上真正指回本要约时才算答复，
        避免把独立单（如 B11 单独给盐田）卷进其他话题。
        """
        linked = [
            msg for msg in self.messages.values()
            if msg.id != offer.source_id
            and self._roots_to(msg, offer.source_id, set())
        ]
        return sorted(linked, key=lambda m: m.sent)

    def _roots_to(self, msg: ParsedMessage, root_id: str, seen: set) -> bool:
        refs = [ref for ref in ([msg.reply_to, *msg.forwards]) if ref]
        for ref in refs:
            if ref == root_id:
                return True
            if ref in seen:
                continue
            seen.add(ref)
            nxt = self.messages.get(ref)
            if nxt is not None and self._roots_to(nxt, root_id, seen):
                return True
        return False

    def _location_along_chain(self, msg: ParsedMessage) -> Optional[str]:
        current = msg
        seen: set[str] = set()
        while current is not None and current.id not in seen:
            seen.add(current.id)
            for act in current.acts:
                if act.location:
                    return act.location
            ref = current.reply_to or (current.forwards[0] if current.forwards else None)
            current = self.messages.get(ref) if ref else None
        return None

    def _pickup_along_chain(self, msg: ParsedMessage) -> Optional[str]:
        current = msg
        seen: set[str] = set()
        while current is not None and current.id not in seen:
            seen.add(current.id)
            if current.pickup:
                return current.pickup
            ref = current.reply_to or (current.forwards[0] if current.forwards else None)
            current = self.messages.get(ref) if ref else None
        return None

    # --------------------------------------------------------- turnovers
    def _review_turnovers(self, offers: list[Offer],
                          commitments: list[Commitment]) -> list[TurnoverReview]:
        return [self._review_one(turnover, commitments) for turnover in self.turnovers]

    def _review_one(self, turnover: dict,
                    commitments: list[Commitment]) -> TurnoverReview:
        tid = turnover["id"]
        items = dict(turnover["items"])
        source_ids = list(turnover.get("source") or [])
        issue_time = parse_dt(turnover["time"])
        review = TurnoverReview(
            turnover_id=tid,
            revision=turnover["revision"],
            state=TURNOVER_PENDING,
            receiver=turnover["receiver"],
            items=items,
            basis=source_ids,
        )

        # 补录：单据开出之后才进入系统、却更早发出的消息
        backfilled = [
            mid for mid in source_ids
            if mid in self.messages and self.messages[mid].ingested > issue_time
        ]
        later_acceptances = [
            mid for mid in backfilled
            if any(act.kind in (ACT_ACCEPT, ACT_CONDITIONAL_ACCEPT)
                   for act in self.messages[mid].acts)
        ]

        # 签收记录
        signed: dict[str, int] = {}
        for receipt in self.receipts:
            if receipt["turnover"] == tid:
                for sku, qty in receipt["items"].items():
                    signed[sku] = signed.get(sku, 0) + qty

        same_receiver = [
            c for c in commitments
            if c.receiver == turnover["receiver"] and set(c.items) == set(items)
        ]
        # 另有有效承诺把同批物资确认给了别的点
        other_owner = next(
            (c for c in commitments
             if c.state == STATE_EFFECTIVE and c.receiver != turnover["receiver"]
             and set(c.items) & set(items)),
            None,
        )

        if same_receiver and same_receiver[0].state == STATE_EFFECTIVE and not later_acceptances:
            review.state = TURNOVER_VALID if not same_receiver[0].unmet else TURNOVER_PENDING
            review.note = "双方对同一组可交割物资一致，条件已核对。"
            return review

        # 还没有任何一方形成有效承诺：保持待确认，不构成争议
        if not other_owner and not later_acceptances:
            unmet = same_receiver[0].unmet if same_receiver else ["尚无有效接受"]
            review.state = TURNOVER_PENDING
            review.note = "仍有待满足条件：" + "；".join(unmet)
            return review

        # 复核不成立 / 争议冻结：补录消息或他方有效承诺与本单据冲突
        review.state = TURNOVER_DISPUTED
        review.conflicts = later_acceptances + (
            [other_owner.basis[-1]] if other_owner and other_owner.basis else []
        )
        review.frozen_lines = {sku: qty for sku, qty in items.items() if sku not in signed}
        review.received_lines = {sku: qty for sku, qty in signed.items() if sku in items}
        parts = [
            f"单据 {turnover['revision']} 基于补录前可见消息开出；"
            f"按发送顺序重核，{turnover['receiver']} 的接受不成立"
        ]
        if later_acceptances:
            parts.append(f"，补录的更早答复 {', '.join(later_acceptances)} 先取得决定权。")
        if other_owner:
            parts.append(f"同批物资已有效确认给 {other_owner.receiver}。")
        if review.frozen_lines:
            parts.append("未交割部分冻结，不得再次派发。")
        if review.received_lines:
            parts.append("已签收部分须人工追认或补办，不能当作余物重新派发。")
        review.note = "".join(parts)
        self.questions.append(
            Question(
                code="争议冻结",
                topic=f"交接单 {turnover['revision']}",
                to_party=self._donor_of(source_ids),
                text=(
                    f"交接单 {turnover['revision']} 的未交割部分 {review.frozen_lines} 已冻结；"
                    f"已签收 {review.received_lines} 需人工追认。请有权补确认的一方处理。"
                ),
                related=source_ids + review.conflicts,
            )
        )
        return review

    def _donor_of(self, source_ids: list[str]) -> Optional[str]:
        for mid in source_ids:
            msg = self.messages.get(mid)
            if msg is not None and any(act.kind == ACT_OFFER for act in msg.acts):
                return msg.sender
        # 退回：资料里第一个提出要约的人
        for msg in self.messages.values():
            if any(act.kind == ACT_OFFER for act in msg.acts):
                return msg.sender
        return None

    # ------------------------------------------------------------ missing
    def _collect_missing(self) -> None:
        for msg in self.messages.values():
            for ref in msg.references_missing:
                self.missing_upstream.append(ref)
                self.warnings.append(
                    f"{msg.id} 引用的上游来信 {ref} 缺失；依赖该前文的结论无法替双方给出。"
                )
        # 独立承诺（无缺失引用的要约）照常受理，无需警告；只汇总受影响的 thread
        broken_threads = {
            msg.thread for msg in self.messages.values() if msg.references_missing
        }
        for thread in sorted(broken_threads):
            self.questions.append(
                Question(
                    code="上游缺失",
                    topic=thread,
                    to_party=None,
                    text=f"话题 {thread} 缺少上游来信，相关部分暂无法结论，其余独立承诺继续受理。",
                )
            )
