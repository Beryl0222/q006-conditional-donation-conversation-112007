"""把消息、授权、交接单和签收记录核对成承诺状态。

核心判断只依赖两件事：原话说了什么（:mod:`text`）和谁在什么时间有权
说什么（:mod:`authority`）。所有先后比较一律用原发送时间 ``sent``，
``ingested`` 只用来发现“补录的早期答复”，并据此要求重新核对交接单。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Sequence

from .authority import Authority, authority_for
from .models import Item, Packet, Receipt
from .text import Condition, ItemGroup, Reading, clean_number, read_packet


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------


@dataclass
class OfferBundle:
    id: str
    root_id: str
    items: dict[str, float]
    split_allowed: bool | None
    target: str | None
    location: str | None
    deadline: datetime | None
    deadline_text: str | None
    fallback: str | None
    text: str
    generic: bool = False
    superseded_by: str | None = None
    missing_refs: tuple[str, ...] = ()
    conditions: list[Condition] = field(default_factory=list)

    def describe(self) -> str:
        items = "、".join(f"{sku} {clean_number(qty)}箱" for sku, qty in self.items.items())
        if self.target:
            return f"{items} → {self.target}"
        return items


@dataclass
class ConditionCheck:
    kind: str
    text: str
    satisfied: bool | None
    detail: str


@dataclass
class Stance:
    message_id: str
    speaker: str
    party: str
    kind: str
    bundle_id: str | None
    positive_items: dict[str, float]
    refused_items: dict[str, float]
    sent: datetime
    ingested: datetime
    authority: Authority
    status: str
    reasons: list[str] = field(default_factory=list)
    conditions: list[ConditionCheck] = field(default_factory=list)
    withdrawn_by: str | None = None
    is_relay: bool = False
    missing_refs: tuple[str, ...] = ()
    location: str | None = None

    @property
    def effective(self) -> bool:
        """有效且未被撤回的承诺；部分撤回后剩下的物品仍算有效。"""

        return self.status == "accepted" or (
            self.status == "withdrawn" and bool(self.positive_items)
        )

    @property
    def ref(self) -> str:
        return self.message_id


@dataclass
class TurnoverCheck:
    id: str
    revision: str
    receiver: str
    items: dict[str, float]  # 折算成箱
    raw_items: dict[str, float]
    time: datetime
    sources: tuple[str, ...]
    shown_to: tuple[str, ...]
    declared_state: str
    status: str  # valid / pending_confirmation / invalid / frozen
    unmet: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    missing_refs: tuple[str, ...] = ()
    frozen_items: dict[str, float] = field(default_factory=dict)
    signed_items: dict[str, float] = field(default_factory=dict)
    superseded_by: str | None = None

    @property
    def version(self) -> str:
        return self.revision


@dataclass
class PendingQuestion:
    party: str
    about: str
    question: str
    reason: str
    refs: tuple[str, ...] = ()
    version: str | None = None


@dataclass
class Pickup:
    party: str
    items: dict[str, float]
    location: str | None
    basis: tuple[str, ...]
    turnover: str | None
    conditions: tuple[str, ...] = ()


@dataclass
class FrozenPart:
    items: dict[str, float]
    reason: str
    refs: tuple[str, ...] = ()
    needed_from: tuple[str, ...] = ()
    # 已实际签收、不能当余物再派的部分。
    already_signed: dict[str, float] = field(default_factory=dict)


@dataclass
class ReceiptCheck:
    receipt: Receipt
    turnover_status: str
    status: str  # consistent / manual_ratification
    detail: str


@dataclass
class Analysis:
    packet: Packet
    readings: dict[str, Reading]
    authorities: dict[str, Authority]
    offers: list[OfferBundle]
    stances: list[Stance]
    turnovers: list[TurnoverCheck]
    receipts: list[ReceiptCheck]
    pendings: list[PendingQuestion]
    pickups: list[Pickup]
    frozen: list[FrozenPart]
    unresolved: list[str]
    clarifications: list[PendingQuestion]
    missing_refs: dict[str, list[str]]

    # -- 查询 -------------------------------------------------------------
    def offer(self, bundle_id: str) -> OfferBundle | None:
        return next((offer for offer in self.offers if offer.id == bundle_id), None)

    def turnover(self, key: str) -> TurnoverCheck | None:
        return next(
            (check for check in self.turnovers if key in {check.id, check.revision}),
            None,
        )

    def effective_acceptances(self, bundle_id: str | None = None) -> list[Stance]:
        return [
            stance
            for stance in self.stances
            if stance.effective and (bundle_id is None or stance.bundle_id == bundle_id)
        ]

    def statements_by(self, party: str) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for stance in sorted(self.stances, key=lambda item: item.sent):
            if stance.party != party:
                continue
            rows.append(
                {
                    "message": stance.message_id,
                    "sent": stance.sent.isoformat(),
                    "kind": stance.kind,
                    "status": stance.status,
                    "speaker": stance.speaker,
                    "items": stance.positive_items,
                    "refused": stance.refused_items,
                    "reasons": stance.reasons,
                    "conditions": [
                        {"text": check.text, "satisfied": check.satisfied, "detail": check.detail}
                        for check in stance.conditions
                    ],
                }
            )
        return rows

    def visible_refs(self, party: str) -> list[str]:
        if party == "协调员":
            return sorted(self.packet.messages)
        return sorted(
            message.id
            for message in self.packet.messages.values()
            if message.can_be_seen_by(party)
        )

    def export_for(self, party: str, key: str) -> dict[str, object] | None:
        """给某一方看的交接内容：版本、物品、未满足条件和它有权看到的引用。"""

        check = self.turnover(key)
        if check is None:
            return None
        visible = set(self.visible_refs(party))
        all_refs = set(check.sources)
        for stance in self.stances:
            if stance.bundle_id and any(
                sku in self._bundle_items(stance.bundle_id) for sku in check.raw_items
            ):
                all_refs.add(stance.message_id)
        cited = sorted(all_refs & visible)
        omitted = sorted(all_refs - visible)
        return {
            "party": party,
            "turnover": check.id,
            "revision": check.revision,
            "version": check.version,
            "receiver": check.receiver,
            "items": check.raw_items,
            "cartons": check.items,
            "status": check.status,
            "declared_state": check.declared_state,
            "conditions_unmet": check.unmet,
            "version_seen": {
                "revision": check.revision,
                "shown_to": list(check.shown_to),
                "party_saw_version": party in check.shown_to,
            },
            "accepted_contents": [
                {
                    "message": stance.message_id,
                    "party": stance.party,
                    "items": stance.positive_items,
                }
                for stance in self.stances
                if stance.effective and set(stance.positive_items) & set(check.items)
            ],
            "cited_messages": cited,
            "omitted_messages_count": len(omitted),
            "note": "只列出该方有权取得的原话；未列出的引用需由协调员另行转达。",
        }

    def _bundle_items(self, bundle_id: str) -> dict[str, float]:
        offer = self.offer(bundle_id)
        return offer.items if offer else {}

    def entity_versions(self) -> list[dict[str, object]]:
        """双方各自看见过的交接单版本与对方明确接受过的内容。"""

        rows: list[dict[str, object]] = []
        for check in self.turnovers:
            accepted = [
                {
                    "message": stance.message_id,
                    "party": stance.party,
                    "items": stance.positive_items,
                }
                for stance in self.stances
                if stance.effective and set(stance.positive_items) & set(check.items)
            ]
            rows.append(
                {
                    "turnover": check.id,
                    "revision": check.revision,
                    "status": check.status,
                    "superseded_by": check.superseded_by,
                    "shown_to": list(check.shown_to),
                    "items": check.raw_items,
                    "unmet": list(check.unmet),
                    "accepted_by_parties": accepted,
                }
            )
        return rows

    def pickup_view(self) -> dict[str, object]:
        return {
            "now": [
                {
                    "party": pickup.party,
                    "items": pickup.items,
                    "location": pickup.location,
                    "basis": list(pickup.basis),
                    "turnover": pickup.turnover,
                    "conditions": list(pickup.conditions),
                }
                for pickup in self.pickups
            ],
            "pending_questions": [
                {
                    "party": pending.party,
                    "about": pending.about,
                    "question": pending.question,
                    "reason": pending.reason,
                    "version": pending.version,
                    "refs": list(pending.refs),
                }
                for pending in self.pendings
            ],
            "frozen": [
                {
                    "items": part.items,
                    "already_signed": part.already_signed,
                    "reason": part.reason,
                    "refs": list(part.refs),
                    "needed_from": list(part.needed_from),
                }
                for part in self.frozen
            ],
            "unresolved": list(self.unresolved),
            "clarifications": [
                {
                    "party": question.party,
                    "about": question.about,
                    "question": question.question,
                    "reason": question.reason,
                }
                for question in self.clarifications
            ],
        }

    def status(self) -> dict[str, object]:
        return {
            "offers": [
                {
                    "id": offer.id,
                    "source": offer.root_id,
                    "items": offer.items,
                    "split_allowed": offer.split_allowed,
                    "target": offer.target,
                    "deadline": offer.deadline.isoformat() if offer.deadline else None,
                    "fallback": offer.fallback,
                    "generic": offer.generic,
                    "superseded_by": offer.superseded_by,
                    "missing_refs": list(offer.missing_refs),
                }
                for offer in self.offers
            ],
            "stances": [
                {
                    "message": stance.message_id,
                    "party": stance.party,
                    "speaker": stance.speaker,
                    "kind": stance.kind,
                    "bundle": stance.bundle_id,
                    "status": stance.status,
                    "items": stance.positive_items,
                    "refused": stance.refused_items,
                    "sent": stance.sent.isoformat(),
                    "ingested": stance.ingested.isoformat(),
                    "authority": {
                        "can_commit": stance.authority.can_commit,
                        "can_location": stance.authority.can_location,
                        "reason": stance.authority.reason,
                    },
                    "conditions": [
                        {"text": check.text, "satisfied": check.satisfied, "detail": check.detail}
                        for check in stance.conditions
                    ],
                    "reasons": stance.reasons,
                    "withdrawn_by": stance.withdrawn_by,
                }
                for stance in sorted(self.stances, key=lambda item: item.sent)
            ],
            "turnovers": [
                {
                    "id": check.id,
                    "revision": check.revision,
                    "receiver": check.receiver,
                    "items": check.raw_items,
                    "cartons": check.items,
                    "state": check.status,
                    "declared_state": check.declared_state,
                    "time": check.time.isoformat(),
                    "shown_to": list(check.shown_to),
                    "unmet": check.unmet,
                    "conflicts": check.conflicts,
                    "reasons": check.reasons,
                    "missing_refs": list(check.missing_refs),
                    "frozen_items": check.frozen_items,
                    "signed_items": check.signed_items,
                    "superseded_by": check.superseded_by,
                }
                for check in self.turnovers
            ],
            "receipts": [
                {
                    "id": row.receipt.id,
                    "turnover": row.receipt.turnover,
                    "status": row.status,
                    "detail": row.detail,
                    "items": row.receipt.items,
                }
                for row in self.receipts
            ],
            "pickup": self.pickup_view(),
            "statements": {party: self.statements_by(party) for party in self._statement_parties()},
            "versions": self.entity_versions(),
            "missing_refs": self.missing_refs,
        }

    def _statement_parties(self) -> list[str]:
        parties = {stance.party for stance in self.stances}
        parties.update(self.packet.parties)
        order = [party for party in self.packet.parties if party in parties]
        if "捐出方" in parties:
            order.insert(0, "捐出方")
        return order


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def analyse(packet: Packet) -> Analysis:
    readings = read_packet(packet)
    authorities = {mid: authority_for(reading, packet) for mid, reading in readings.items()}
    missing = _missing_refs(packet)
    root_map = _root_map(packet, readings)

    offers, generic_notes = _build_offers(packet, readings, root_map, missing)

    # 先算每条答复自身是否具备承诺资格（权限、完整性、期限），再算条件。
    stances = _build_stances(packet, readings, authorities, offers, root_map, missing)
    _mark_backfilled(stances, offers)

    # 附条件撤回要在已有有效接受之后判断，才能体现“先接受、后撤回”。
    _apply_withdrawals(packet, stances, readings, offers)

    # 依据条件结果补正接受状态。
    _finalise_acceptance_conditions(stances)

    turnover_checks, receipt_checks = _check_turnovers(packet, offers, stances, missing)
    frozen = _freeze_parts(packet, offers, stances, turnover_checks, receipt_checks)
    pendings, clarifications = _pending_questions(
        packet, offers, stances, turnover_checks, receipt_checks, frozen, missing
    )
    pickups = _pickups(offers, stances, turnover_checks, frozen)

    # 只列“还不能替双方下结论”的部分；已经能定性为无效、逾期、越权的
    # 结论放在各自的 status/reasons 里，不混进未决事项。
    unresolved = list(dict.fromkeys(generic_notes))
    for message_id, refs in missing.items():
        label = "交接单" if message_id in packet.turnovers else "消息"
        unresolved.append(
            f"{label} {message_id} 缺少引用 {'、'.join(refs)}，相关物品和条件无法与上游核对"
        )
    for stance in stances:
        if stance.status in {"needs_clarification", "unverifiable", "unmatched"}:
            unresolved.append(
                f"{stance.message_id}：{stance.status}（{'；'.join(stance.reasons)}）"
            )

    return Analysis(
        packet=packet,
        readings=readings,
        authorities=authorities,
        offers=offers,
        stances=stances,
        turnovers=turnover_checks,
        receipts=receipt_checks,
        pendings=pendings,
        pickups=pickups,
        frozen=frozen,
        unresolved=list(dict.fromkeys(unresolved)),
        clarifications=clarifications,
        missing_refs=missing,
    )


def _missing_refs(packet: Packet) -> dict[str, list[str]]:
    """本记录直接引用或经由转述链缺了哪封上游来信。"""

    direct: dict[str, list[str]] = {}
    for message in packet.messages.values():
        gaps = [ref for ref in message.references if ref not in packet.messages]
        if gaps:
            direct[message.id] = gaps
    for turnover in packet.turnovers.values():
        gaps = [ref for ref in turnover.sources if ref not in packet.messages]
        if gaps:
            direct[turnover.id] = gaps

    def upstream(message_id: str, trail: set[str]) -> list[str]:
        if message_id in trail or message_id not in packet.messages:
            return []
        trail.add(message_id)
        found = list(direct.get(message_id, []))
        for ref in packet.messages[message_id].references:
            found.extend(upstream(ref, trail))
        return list(dict.fromkeys(found))

    missing: dict[str, list[str]] = {}
    for key in direct:
        if key in packet.messages:
            gathered = upstream(key, set())
            if gathered:
                missing[key] = gathered
        else:
            missing[key] = direct[key]
    # 交接单引用了一条本身缺上游的答复时，也继承这个缺口。
    for turnover in packet.turnovers.values():
        gaps: list[str] = []
        for ref in turnover.sources:
            gaps.extend(upstream(ref, set()))
        if gaps:
            missing.setdefault(turnover.id, list(dict.fromkeys(gaps)))
    return missing


def _root_map(packet: Packet, readings: Mapping[str, Reading]) -> dict[str, str | None]:
    """沿回复／转述链找最初那条捐出方消息。"""

    roots: dict[str, str | None] = {}

    def resolve(message_id: str, trail: set[str]) -> str | None:
        if message_id in roots:
            return roots[message_id]
        if message_id in trail or message_id not in packet.messages:
            return None
        trail.add(message_id)
        message = packet.messages[message_id]
        if message.sender == "捐出方" or message.sender in packet.donors:
            roots[message_id] = message_id
            return message_id
        for ref in message.references:
            found = resolve(ref, trail)
            if found:
                roots[message_id] = found
                return found
        roots[message_id] = None
        return None

    for message_id in packet.messages:
        resolve(message_id, set())
    return roots


def _merge_groups(groups: Sequence[ItemGroup]) -> list[dict[str, object]]:
    """把同一分句里逐件列出的物品合并成一组，保留可否拆分与时限。"""

    merged: list[dict[str, object]] = []
    index: dict[tuple[str, str | None, bool | None, object], dict[str, object]] = {}
    for group in groups:
        key = (group.text, group.target, group.split_allowed, group.deadline)
        entry = index.get(key)
        if entry is None:
            entry = {
                "text": group.text,
                "target": group.target,
                "split_allowed": group.split_allowed,
                "deadline": group.deadline,
                "fallback": group.fallback,
                "items": {},
                "polarity": group.polarity,
            }
            index[key] = entry
            merged.append(entry)
        items: dict[str, float] = entry["items"]  # type: ignore[assignment]
        for item in group.items:
            items[item.sku] = max(items.get(item.sku, 0.0), item.cartons)
        if group.polarity in {"refuse", "withdraw"}:
            entry["polarity"] = group.polarity
        if entry.get("fallback") is None and group.fallback:
            entry["fallback"] = group.fallback
    return merged


def _deadline_binds(reading: Reading, entry: Mapping[str, object]) -> bool:
    """期限属于“到某时无人确认便改送”的报价，而不适用于无关物品。"""

    fallback = entry.get("fallback") or reading.fallback
    if fallback:
        return True
    return any(condition.kind == "no_acceptance_by_deadline" for condition in reading.conditions)


def _build_offers(
    packet: Packet,
    readings: Mapping[str, Reading],
    root_map: Mapping[str, str | None],
    missing: Mapping[str, list[str]],
) -> tuple[list[OfferBundle], list[str]]:
    offers: list[OfferBundle] = []
    notes: list[str] = []
    for message in sorted(packet.messages.values(), key=lambda row: (row.sent, row.id)):
        reading = readings[message.id]
        if message.sender not in {"捐出方", *packet.donors}:
            continue
        if reading.kind not in {"offer", "accept"}:
            continue
        if not reading.groups:
            continue
        merged = _merge_groups(reading.groups)
        for position, entry in enumerate(merged, start=1):
            items = dict(entry["items"])  # type: ignore[arg-type]
            if not items:
                continue
            split = entry["split_allowed"]  # type: ignore[assignment]
            # 同一句里会分开答复不同物品；只有一条物品组时才把整条的
            # 指定对象套到它身上，避免把“备用手腕可单送”也算给前一句的
            # 受赠方。
            target = entry["target"] or (reading.target if len(merged) == 1 else None)
            if target is None and entry.get("fallback") is None and len(merged) > 1:
                # “给岭东……便改给盐田”的候选归属只跟写明接受方的那组走。
                pass
            deadline = entry["deadline"]  # type: ignore[assignment]
            if deadline is not None and not _deadline_binds(reading, entry):
                deadline = None
            offer = OfferBundle(
                id=f"{message.id}:g{position}",
                root_id=message.id,
                items=items,
                split_allowed=split,
                target=target,
                location=reading.location,
                deadline=deadline.at if deadline else None,
                deadline_text=deadline.text if deadline else None,
                fallback=(entry.get("fallback") if deadline is not None else None),
                text=str(entry["text"]),
                generic=target is None,
                missing_refs=tuple(missing.get(message.id, ())),
                conditions=list(reading.conditions),
            )
            offers.append(offer)
    # 泛指的同一物品后来被指定给某方时，以具体那条为准。
    for offer in offers:
        if not offer.generic:
            continue
        specific = [
            other
            for other in offers
            if other is not offer
            and not other.generic
            and other.target
            and set(other.items) & set(offer.items)
            and other.root_id != offer.root_id
            and packet.messages[other.root_id].sent >= packet.messages[offer.root_id].sent
        ]
        if specific:
            offer.superseded_by = specific[-1].id
    return offers, notes


def _item_positions(reading: Reading) -> tuple[dict[str, float], dict[str, float], str | None]:
    positive: dict[str, float] = {}
    refused: dict[str, float] = {}
    overall = None
    for group in reading.groups:
        for item in group.items:
            if group.polarity in {"refuse", "withdraw", "no_split"}:
                refused[item.sku] = max(refused.get(item.sku, 0.0), item.cartons)
            elif group.polarity == "defer":
                continue
            elif group.polarity in {"accept", "counter", "request", "neutral"}:
                positive[item.sku] = max(positive.get(item.sku, 0.0), item.cartons)
        if overall is None and group.polarity in {"accept", "refuse", "counter", "no_split"}:
            overall = group.polarity
    return positive, refused, overall


def _match_offer(
    packet: Packet,
    reading: Reading,
    party: str,
    offers: Sequence[OfferBundle],
    root_map: Mapping[str, str | None],
) -> OfferBundle | None:
    positive, refused, _ = _item_positions(reading)
    touches = set(positive) | set(refused)
    if not touches:
        return None
    root = root_map.get(reading.message.id)
    scored: list[tuple[int, datetime, OfferBundle]] = []
    for offer in offers:
        if offer.superseded_by:
            continue
        overlap = touches & set(offer.items)
        if not overlap:
            continue
        score = len(overlap) * 2
        if root and offer.root_id == root:
            score += 10
        if offer.target and offer.target == party:
            score += 4
        root_message = packet.messages.get(offer.root_id)
        if root_message and root_message.thread and root_message.thread == reading.message.thread:
            score += 1
        # 指名给某一方的报价比泛指报价更具体，优先匹配；否则“B11 给
        # 盐田”会被前面泛指的“备用护腕可单送”抢走。
        if offer.target:
            score += 2
        scored.append((score, reading.message.sent, offer))
    if not scored:
        return None
    scored.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    return scored[0][2]


def _build_stances(
    packet: Packet,
    readings: Mapping[str, Reading],
    authorities: Mapping[str, Authority],
    offers: Sequence[OfferBundle],
    root_map: Mapping[str, str | None],
    missing: Mapping[str, list[str]],
) -> list[Stance]:
    stances: list[Stance] = []
    for message in sorted(packet.messages.values(), key=lambda row: (row.sent, row.id)):
        reading = readings[message.id]
        authority = authorities[message.id]
        if message.sender in {"捐出方", *packet.donors}:
            continue
        if message.sender == "协调员":
            stances.append(
                Stance(
                    message_id=message.id,
                    speaker=message.sender,
                    party="协调员",
                    kind=reading.kind,
                    bundle_id=None,
                    positive_items={},
                    refused_items={},
                    sent=message.sent,
                    ingested=message.ingested,
                    authority=authority,
                    status="relay" if reading.kind in {"relay", "relay_request", "request_confirmation"} else "not_a_commitment",
                    reasons=["协调员转述或询问，不构成承诺"],
                    is_relay=True,
                    missing_refs=tuple(missing.get(message.id, ())),
                )
            )
            continue
        party = authority.principal
        offer = _match_offer(packet, reading, party, offers, root_map)
        positive, refused, polarity = _item_positions(reading)
        if offer is None:
            # 报价本身缺上游说明时，答复仍可登记为该报价下的一条记录，
            # 但结论标为“无法核对”，不阻塞其余独立承诺。
            dangling = _dangling_offer(reading, party, offers, missing)
            if dangling is not None:
                stance = Stance(
                    message_id=message.id,
                    speaker=message.sender,
                    party=party,
                    kind=reading.kind,
                    bundle_id=dangling.id,
                    positive_items=positive,
                    refused_items=refused,
                    sent=message.sent,
                    ingested=message.ingested,
                    authority=authority,
                    status="unverifiable",
                    reasons=[
                        f"针对的上游来信 {'、'.join(dangling.missing_refs)} 缺失，"
                        "这条答复只能先受理，物品和条件无法与双方核对"
                    ],
                    missing_refs=tuple(missing.get(message.id, ())) or dangling.missing_refs,
                )
                stances.append(stance)
                continue
            if reading.groups or reading.missing_refs:
                if reading.missing_refs or missing.get(message.id):
                    gaps = reading.missing_refs or missing.get(message.id, [])
                    status = "unverifiable"
                    reasons = [
                        f"引用的上游来信 {'、'.join(gaps)} 缺失，这条答复只能先受理，"
                        "物品和条件无法与双方核对"
                    ]
                else:
                    status = "unmatched"
                    reasons = ["找不到本条针对的原始报价"]
                stances.append(
                    Stance(
                        message_id=message.id,
                        speaker=message.sender,
                        party=party,
                        kind=reading.kind,
                        bundle_id=None,
                        positive_items=positive,
                        refused_items=refused,
                        sent=message.sent,
                        ingested=message.ingested,
                        authority=authority,
                        status=status,
                        reasons=reasons,
                        missing_refs=tuple(reading.missing_refs or missing.get(message.id, ())),
                    )
                )
            continue
        stance = Stance(
            message_id=message.id,
            speaker=message.sender,
            party=party,
            kind=reading.kind,
            bundle_id=offer.id,
            positive_items=positive,
            refused_items=refused,
            sent=message.sent,
            ingested=message.ingested,
            authority=authority,
            status="recorded",
            is_relay=reading.kind in {"relay", "relay_request"},
            missing_refs=tuple(missing.get(message.id, ())),
            location=reading.location,
        )
        _classify_stance(stance, reading, offer, packet, readings, authorities, root_map)
        stances.append(stance)
    return stances


def _dangling_offer(
    reading: Reading,
    party: str,
    offers: Sequence[OfferBundle],
    missing: Mapping[str, list[str]],
) -> OfferBundle | None:
    """答复指向的来信缺失时，找那条不完整报价，供登记与说明用。"""

    if not reading.missing_refs and not missing.get(reading.message.id):
        return None
    for offer in offers:
        if not offer.missing_refs:
            continue
        if offer.target in {None, party}:
            return offer
    return None


def _deadline_applies(offer: OfferBundle, stance: Stance) -> bool:
    """改送期限只管指定受赠方；原话没点名时（泛指报价）按期限处理。"""

    if offer.deadline is None:
        return False
    return offer.target is None or stance.party == offer.target


def _late_against_offer(offer: OfferBundle, stance: Stance) -> bool:
    if not _deadline_applies(offer, stance):
        return False
    return stance.sent > offer.deadline  # type: ignore[operator]


def _classify_stance(
    stance: Stance,
    reading: Reading,
    offer: OfferBundle,
    packet: Packet,
    readings: Mapping[str, Reading],
    authorities: Mapping[str, Authority],
    root_map: Mapping[str, str | None],
) -> None:
    # 1) 权限。
    if not stance.authority.can_commit:
        stance.status = "unauthorized"
        stance.reasons.append(stance.authority.reason)
        if stance.authority.can_location and reading.location:
            stance.reasons.append(
                f"只能在授权范围内确认收货地点“{reading.location}”，不能替委托方改变物品或箱数"
            )
        return

    # 2) 转述／询问不是承诺。
    if stance.is_relay:
        stance.status = "relay"
        stance.reasons.append("转述或询问，不构成承诺")
        return

    # 3) 只同意配套的一半：原话是否允许拆分决定能否单独成立。
    if stance.kind in {"accept", "conditional_accept", "info", "counter"} and stance.positive_items:
        if offer.split_allowed is False:
            missing_items = set(offer.items) - set(stance.positive_items)
            extra = set(stance.positive_items) - set(offer.items)
            short = any(
                stance.positive_items.get(sku, 0) < qty
                for sku, qty in offer.items.items()
                if sku in stance.positive_items
            )
            if missing_items or extra or short:
                stance.status = "incomplete"
                stance.reasons.append(
                    "本条只同意配套的一部分；原话写明成套／成对，不能拆分，"
                    "已同意的部分不能单独成立"
                )
                return
        elif offer.split_allowed is True:
            if set(stance.positive_items) - set(offer.items):
                stance.status = "incomplete"
                stance.reasons.append("答复的物品不在原报价内")
                return
        else:
            exact = set(stance.positive_items) == set(offer.items) and all(
                stance.positive_items.get(sku, 0) >= qty
                for sku, qty in offer.items.items()
            )
            if not exact:
                stance.status = "needs_clarification"
                stance.reasons.append("原话未说明能否拆分，部分同意不能直接生效")
                return

    # 4) 改价／改量是还价，需要捐出方另行接受。
    if stance.kind == "counter" and stance.positive_items:
        if set(stance.positive_items) != set(offer.items) or any(
            stance.positive_items.get(sku, 0) < qty for sku, qty in offer.items.items()
        ):
            stance.status = "counter"
            stance.reasons.append("提出替代数量或品项，属于还价；需捐出方接受后才成立")
            return

    # 5) 期限与改送权。期限只约束原报价指定的受赠方，而且只管“要不要
    # 接”的答复；已经接了的人之后撤回自己的承诺，不受这个期限限制。
    # 补录的早期答复按原发送时间判断，不看系统何时收到。
    if stance.kind not in {"withdraw", "conditional_withdrawal", "refusal"} and _late_against_offer(offer, stance):
        stance.status = "late"
        stance.reasons.append(
            f"原约定 {offer.deadline_text or offer.deadline.isoformat()} 前答复，"
            f"本条原发送时间为 {stance.sent.isoformat()}，在其后，原报价已失效"
        )
        return
    if offer.fallback and stance.party == offer.fallback:
        primary_accept = [
            other
            for other in _acceptance_candidates(packet, readings, authorities, offer, root_map)
            if other.party == offer.target
            and (offer.deadline is None or other.sent <= offer.deadline)
        ]
        if primary_accept:
            stance.status = "condition_unmet"
            first = min(primary_accept, key=lambda item: item.sent)
            stance.reasons.append(
                f"改送以“{offer.target}逾期未接”为前提；{offer.target} 已于 {first.sent.isoformat()} 有效答复"
            )
            stance.conditions.append(
                ConditionCheck(
                    "no_acceptance_by_deadline",
                    f"{offer.target} 在期限前未接",
                    False,
                    f"{first.message_id} 在期限前已答复",
                )
            )
            return

    # 5b) 补录由 _mark_backfilled 在所有答复排好序后统一核对。

    # 6) 条件检查。
    condition_results = _check_conditions(reading, stance, packet)
    stance.conditions.extend(condition_results)
    if any(check.satisfied is None for check in condition_results):
        stance.status = "needs_clarification"
        stance.reasons.append("存在无法核对的条件，不能替双方下结论")
        return
    if stance.kind in {"conditional_withdrawal", "withdraw", "refusal"}:
        # 撤回在 _apply_withdrawals 里处理。
        stance.status = "withdrawal_claim" if stance.kind != "refusal" else "refused"
        if stance.kind == "refusal" and not reading.conditions:
            stance.status = "refused"
            stance.reasons.append("明确拒绝")
        return

    if stance.kind not in {"accept", "conditional_accept", "info", "offer"}:
        stance.status = "recorded"
        return
    if stance.kind == "offer":
        stance.status = "recorded"
        return
    if not stance.positive_items:
        stance.status = "recorded"
        if stance.refused_items:
            stance.status = "refused"
            stance.reasons.append("只作拒绝，未同意任何物品")
        return

    if any(check.satisfied is False for check in condition_results):
        stance.status = "condition_unmet"
        reasons = [check.detail for check in condition_results if check.satisfied is False]
        stance.reasons.extend(reasons)
        return

    stance.status = "accepted"
    stance.reasons.append("在有效期限内、授权范围内，对同一组物品作出接受")


def _mark_backfilled(stances: Sequence[Stance], offers: Sequence[OfferBundle]) -> None:
    """补录的早期答复按原发送时间重排，并标出是否改变了决定权归属。"""

    by_id = {offer.id: offer for offer in offers}
    for stance in stances:
        if stance.bundle_id is None:
            continue
        later = [
            other
            for other in stances
            if other.bundle_id == stance.bundle_id
            and other.party == stance.party
            and other.sent > stance.sent
            and other.ingested < stance.ingested
        ]
        if not later:
            continue
        offer = by_id.get(stance.bundle_id)
        stance.reasons.append(
            "补录的较早答复："
            + "、".join(other.message_id for other in later)
            + " 按原发送时间重排，不再按系统收到顺序处理"
        )
        if offer and offer.deadline:
            if stance.sent <= offer.deadline < min(other.sent for other in later):
                stance.reasons.append(
                    f"本条在期限 {offer.deadline_text or offer.deadline.isoformat()} 前作出，"
                    "决定权应重新核给本条所在的一方"
                )


def _acceptance_candidates(
    packet: Packet,
    readings: Mapping[str, Reading],
    authorities: Mapping[str, Authority],
    offer: OfferBundle,
    root_map: Mapping[str, str | None],
) -> list[Stance]:
    """不带条件的初步接受，用于判断“有没有人在期限前接了”。"""

    candidates: list[Stance] = []
    for message in packet.messages.values():
        reading = readings[message.id]
        authority = authorities[message.id]
        if message.sender in {"捐出方", *packet.donors} or message.sender == "协调员":
            continue
        if not authority.can_commit:
            continue
        if reading.kind not in {"accept", "conditional_accept", "info"}:
            continue
        if root_map.get(message.id) != offer.root_id:
            continue
        positive, _, _ = _item_positions(reading)
        if not positive:
            continue
        if offer.split_allowed is False:
            if set(positive) != set(offer.items):
                continue
        elif offer.split_allowed is True:
            if not set(positive) <= set(offer.items):
                continue
        else:
            if set(positive) != set(offer.items):
                continue
        if any(reading.conditions):
            continue
        candidates.append(
            Stance(
                message_id=message.id,
                speaker=message.sender,
                party=authority.principal,
                kind=reading.kind,
                bundle_id=offer.id,
                positive_items=positive,
                refused_items={},
                sent=message.sent,
                ingested=message.ingested,
                authority=authority,
                status="candidate",
            )
        )
    return candidates


def _check_conditions(
    reading: Reading, stance: Stance, packet: Packet
) -> list[ConditionCheck]:
    checks: list[ConditionCheck] = []
    for condition in reading.conditions:
        if condition.kind == "no_acceptance_by_deadline":
            deadline = condition.deadline
            accepted = _any_prior_acceptance(packet, reading, condition.party, deadline)
            checks.append(
                ConditionCheck(
                    condition.kind,
                    condition.text,
                    accepted is None,
                    (
                        f"{accepted} 已在期限前有效答复"
                        if accepted
                        else "期限内没有有效答复"
                    ),
                )
            )
        elif condition.kind == "item_signed":
            receipt = _receipt_for_condition(packet, condition.item, reading.message.sent, stance.party)
            checks.append(
                ConditionCheck(
                    condition.kind,
                    condition.text,
                    receipt is not None,
                    (
                        f"{receipt.id} 显示 {receipt.items} 已由 {receipt.receiver} 签收"
                        if receipt
                        else "没有签收记录支持该条件"
                    ),
                )
            )
        else:
            checks.append(
                ConditionCheck(condition.kind, condition.text, None, "条件表述无法核对，需要人工确认")
            )
    return checks


def _any_prior_acceptance(
    packet: Packet, reading: Reading, party: str | None, deadline: datetime | None
) -> str | None:
    """在期限前，是否有该方（或任何有资格方）对同一组物品的有效接受。"""

    for other in sorted(packet.messages.values(), key=lambda row: row.sent):
        if other.id == reading.message.id or other.sent > reading.message.sent:
            continue
        if deadline and other.sent > deadline:
            continue
        # 只把明确接受、且没有条件的答复当作“有人接了”。
        text = other.text
        if not any(word in text for word in ("确认", "同意", "接受", "提走", "来接", "接走", "各一箱")):
            continue
        if any(word in text for word in ("如果", "若", "假如", "倘若", "要是", "一旦")):
            continue
        if party and party not in other.viewers and party not in other.thread and party != other.sender:
            # 期限条件问到的是某方有没有接；发言人不一定在 viewers 里。
            pass
        return other.id
    return None


def _receipt_for_condition(
    packet: Packet, sku: str | None, before: datetime, party: str
) -> Receipt | None:
    for receipt in sorted(packet.receipts.values(), key=lambda row: row.signed_at):
        if receipt.signed_at > before:
            continue
        if receipt.receiver == party:
            continue
        if sku and sku not in receipt.items:
            continue
        return receipt
    return None


def _apply_withdrawals(
    packet: Packet,
    stances: list[Stance],
    readings: Mapping[str, Reading],
    offers: Sequence[OfferBundle],
) -> None:
    for withdrawal in sorted(stances, key=lambda item: item.sent):
        # 单纯拒绝只是不接受；只有撤回才去改动自己先前的有效接受。
        if withdrawal.kind not in {"conditional_withdrawal", "withdraw"}:
            continue
        condition_failed = any(check.satisfied is False for check in withdrawal.conditions)
        condition_unknown = any(check.satisfied is None for check in withdrawal.conditions)
        if condition_unknown:
            withdrawal.status = "needs_clarification"
            continue
        if withdrawal.kind in {"conditional_withdrawal", "withdraw"} and condition_failed:
            withdrawal.status = "condition_unmet"
            continue
        targets = set(withdrawal.refused_items)
        if not targets:
            targets = set(withdrawal.positive_items)
        if withdrawal.kind == "withdraw":
            targets = set(withdrawal.positive_items) | set(withdrawal.refused_items)
        if not targets:
            continue
        for stance in stances:
            if stance is withdrawal or stance.bundle_id != withdrawal.bundle_id:
                continue
            if stance.party != withdrawal.party or not stance.effective:
                continue
            if stance.sent > withdrawal.sent:
                # 后发的答复优先；撤回不能倒过来取消之后的新答复。
                continue
            hit = targets & set(stance.positive_items)
            if not hit:
                continue
            for sku in hit:
                stance.positive_items.pop(sku, None)
            stance.status = "withdrawn"
            stance.withdrawn_by = withdrawal.message_id
            target_items = sorted(hit)
            if target_items:
                stance.reasons.append(
                    f"{withdrawal.message_id} 撤回了 {'、'.join(target_items)}；"
                    + (
                        "其余物品 "
                        + "、".join(sorted(stance.positive_items))
                        + " 仍按原答复有效"
                        if stance.positive_items
                        else "本条已无剩余有效物品"
                    )
                )
        withdrawal.status = "withdrawal_effective"
        withdrawal.reasons.append(
            f"条件已成立，撤回 {'、'.join(sorted(targets))}；按原答复顺序处理"
        )


def _finalise_acceptance_conditions(stances: list[Stance]) -> None:
    for stance in stances:
        if stance.status != "accepted":
            continue
        if any(check.satisfied is False for check in stance.conditions):
            stance.status = "condition_unmet"
        elif any(check.satisfied is None for check in stance.conditions):
            stance.status = "needs_clarification"


# ---------------------------------------------------------------------------
# 交接单
# ---------------------------------------------------------------------------


def _to_cartons(sku: str, units: float, packet: Packet) -> float:
    item: Item | None = packet.items.get(sku)
    per_carton = item.per_carton if item else None
    if not per_carton:
        return units
    return units / per_carton


def _bundle_for_items(
    items: Mapping[str, float], offers: Sequence[OfferBundle], receiver: str
) -> OfferBundle | None:
    """按物资集合找对应报价；优先接受方自己的报价。"""

    candidates: list[tuple[int, OfferBundle]] = []
    target_set = set(items)
    for offer in offers:
        if offer.superseded_by:
            continue
        overlap = target_set & set(offer.items)
        if not overlap:
            continue
        score = len(overlap) * 2
        if set(target_set) == set(offer.items):
            score += 5
        if offer.target == receiver:
            score += 3
        candidates.append((score, offer))
    if not candidates:
        return None
    candidates.sort(key=lambda entry: entry[0], reverse=True)
    return candidates[0][1]


def _check_turnovers(
    packet: Packet,
    offers: Sequence[OfferBundle],
    stances: Sequence[Stance],
    missing: Mapping[str, list[str]],
) -> tuple[list[TurnoverCheck], list[ReceiptCheck]]:
    """按单据发出时点和之后补录的消息核对每份交接单。"""

    checks: list[TurnoverCheck] = []
    for turnover in sorted(packet.turnovers.values(), key=lambda row: row.time):
        cartons = {sku: _to_cartons(sku, units, packet) for sku, units in turnover.items.items()}
        check = TurnoverCheck(
            id=turnover.id,
            revision=turnover.revision,
            receiver=turnover.receiver,
            items=cartons,
            raw_items=dict(turnover.items),
            time=turnover.time,
            sources=turnover.sources,
            shown_to=turnover.shown_to,
            declared_state=turnover.declared_state,
            status="pending_confirmation",
            missing_refs=tuple(missing.get(turnover.id, ())),
        )
        checks.append(check)

        if check.missing_refs:
            check.status = "incomplete"
            check.unmet.append(
                "缺少上游来信 " + "、".join(check.missing_refs) + "，无法核对物品与条件"
            )
            check.reasons.append("引用不完整，只能先受理其余独立承诺，这份不能替双方下结论")
            continue

        offer = _bundle_for_items(cartons, offers, turnover.receiver)
        if offer is None:
            check.status = "invalid"
            check.reasons.append("找不到覆盖该组物资的捐出方原报价")
            continue
        check.reasons.append(f"对应报价 {offer.root_id}：{offer.describe()}")
        if not check.unmet:
            check.status = "valid"
        if set(cartons) - set(offer.items):
            check.status = "invalid"
            check.reasons.append("交接单物品超出原报价范围")
        if offer.target and turnover.receiver not in {offer.target, offer.fallback}:
            check.status = "invalid"
            check.reasons.append(
                f"原报价指定 {offer.target}，{turnover.receiver} 既非受赠方也非约定改送方"
            )

        receive_stances = [
            stance
            for stance in stances
            if stance.party == turnover.receiver and stance.bundle_id == offer.id
        ]

        # 单据发出前该方是否形成有效接受（按原发送时间）。
        accepted_before = [
            stance
            for stance in receive_stances
            if stance.effective and stance.sent <= turnover.time
        ]
        # 发出后补录、但原发送时间早于发出的答复也会改变结论。
        backfilled = [
            stance
            for stance in stances
            if stance.sent <= turnover.time < stance.ingested
            and stance.bundle_id == offer.id
        ]

        if not accepted_before:
            failed = [
                stance
                for stance in receive_stances
                if stance.sent <= turnover.time and not stance.effective
            ]
            if turnover.declared_state == "待确认" and not failed:
                check.status = "pending_confirmation"
                check.unmet.append(f"{turnover.receiver} 尚未确认 {turnover.revision} 的物品与箱数")
            else:
                check.status = "invalid"
                check.unmet.append(f"{turnover.receiver} 没有在单据发出前形成有效接受")
                for stance in failed:
                    check.reasons.append(
                        f"{stance.message_id}：{stance.status}（{'；'.join(stance.reasons)}）"
                    )
        else:
            check.reasons.append(
                "接受方有效答复：" + "、".join(stance.message_id for stance in accepted_before)
            )
            accepted_items: dict[str, float] = {}
            for stance in accepted_before:
                for sku, qty in stance.positive_items.items():
                    accepted_items[sku] = max(accepted_items.get(sku, 0.0), qty)
            for sku, qty in cartons.items():
                if sku not in accepted_items:
                    check.unmet.append(f"交接单包含接收方未确认的 {sku}")
                elif accepted_items[sku] < qty:
                    check.unmet.append(
                        f"交接单的 {sku} 数量 {clean_number(qty)}箱 超出接收方确认的"
                        f" {clean_number(accepted_items[sku])}箱"
                    )
            if offer.split_allowed is False and set(cartons) != set(offer.items):
                check.unmet.append("原报价成套，交接单不能只列其中一部分")

        # 接受方答复里的条件必须全部满足。
        conditions_ok = True
        for stance in receive_stances:
            for condition in stance.conditions:
                if condition.satisfied is False:
                    conditions_ok = False
                    check.unmet.append(f"{stance.message_id} 的条件未满足：{condition.detail}")
                elif condition.satisfied is None:
                    conditions_ok = False
                    check.unmet.append(f"{stance.message_id} 的条件无法核对：{condition.text}")
        if not conditions_ok and accepted_before:
            check.status = "invalid"
            check.reasons.append("接受方的前提条件没有全部满足，不能形成有效交接")

        # 同一组物资被另一方在先有效接受：不能再确认给本接收方。
        other_claims = [
            stance
            for stance in stances
            if stance.effective
            and stance.bundle_id == offer.id
            and stance.party != turnover.receiver
            and set(stance.positive_items) & set(cartons)
        ]
        for claim in other_claims:
            if claim.sent <= turnover.time:
                check.conflicts.append(
                    f"{claim.message_id}：{claim.party} 已于 {claim.sent.isoformat()} 有效接受，早于本单据"
                )
                check.status = "invalid"
                check.unmet.append("同一组物资已有另一方的在先有效承诺，不能重复确认")
            elif claim.message_id in {stance.message_id for stance in backfilled}:
                check.conflicts.append(
                    f"{claim.message_id}：{claim.party} 的原发送时间 {claim.sent.isoformat()} "
                    f"早于本单据，但 {claim.ingested.isoformat()} 才补录"
                )

        # 单据发出后才补录的、相互冲突的早期消息：冻结有争议的未交割部分。
        disputed: set[str] = set()
        for stance in backfilled:
            if stance is None:
                continue
            if stance.party == turnover.receiver:
                if not stance.effective:
                    disputed |= set(cartons) & (set(stance.refused_items) | set(stance.positive_items))
            else:
                if stance.effective and set(stance.positive_items) & set(cartons):
                    disputed |= set(cartons) & set(stance.positive_items)
                elif stance.refused_items:
                    disputed |= set(cartons) & set(stance.refused_items)
        if disputed and check.status in {"valid", "pending_confirmation"}:
            check.status = "frozen"
            for sku in disputed:
                check.frozen_items[sku] = cartons[sku]
            check.reasons.append(
                "单据发出后才收到相互冲突的早期消息（"
                + "、".join(
                    f"{stance.message_id} 发送于 {stance.sent.isoformat()}、{stance.ingested.isoformat()} 补录"
                    for stance in backfilled
                )
                + "），有争议的未交割部分冻结，待有权补确认的一方答复"
            )

        # 展示与版本：双方各自看到的版本要保留。宣称“双方已确认”而
        # 有一方没看过，这份单据不能成立。
        if turnover.receiver not in turnover.shown_to:
            check.unmet.append("受赠方未看到该版本")
        donor_names = {"捐出方", *packet.donors}
        if not donor_names & set(turnover.shown_to):
            check.unmet.append("捐出方未看到该版本")
        if check.unmet and check.status in {"valid", "pending_confirmation"}:
            # “等待接收方确认”本身不算缺陷；只有实质缺口才判无效。
            substantive = [
                item for item in check.unmet if "尚未确认" not in item
            ]
            if substantive:
                check.status = "invalid"

        # 同一组物资已经被另一份单据（或本单据的其它版本）确认给别的
        # 接收方时，后形成的这份不能同时成立。
        for other in checks:
            if other is check or other.receiver == check.receiver:
                continue
            if other.status not in {
                "valid",
                "pending_confirmation",
                "frozen",
                "invalid",
            }:
                continue
            overlap = set(cartons) & set(other.items)
            if not overlap:
                continue
            # 只有另一份单据本身已有对方有效答复支持时，才算重复分配；
            # 两份都只是草稿时，交给待确认问题，不互相判死。
            other_offer = _bundle_for_items(other.items, offers, other.receiver)
            other_has_basis = bool(other_offer) and any(
                stance.effective and stance.bundle_id == other_offer.id
                for stance in stances
            )
            if not other_has_basis:
                continue
            earlier = other.time <= check.time or (
                other.status == "valid" and check.status != "valid"
            )
            if earlier:
                check.status = "invalid"
                check.conflicts.append(
                    f"{other.revision} 已把 {'、'.join(sorted(overlap))} 确认给 {other.receiver}"
                )
                check.unmet.append("同一物资不能同时确认给两个接收方")
            else:
                other.status = "invalid"
                other.conflicts.append(
                    f"{check.revision} 已把 {'、'.join(sorted(overlap))} 确认给 {check.receiver}"
                )
                other.unmet.append("同一物资不能同时确认给两个接收方")

    # 同一单据的新版本取代旧版本：旧版本保留原样，标为已被替代。
    by_sheet: dict[str, list[TurnoverCheck]] = {}
    for check in checks:
        by_sheet.setdefault(check.revision.partition("/")[0], []).append(check)
    for versions in by_sheet.values():
        if len(versions) < 2:
            continue
        latest = max(versions, key=lambda row: row.time)
        for old_version in versions:
            if old_version is latest:
                continue
            old_version.reasons.append(
                f"已有更新版本 {latest.revision}；旧版本保留双方当时看见的内容"
            )
            old_version.superseded_by = latest.revision

    # 有效单据：双方一致、条件满足、没有重复分配、没有后续冲突。
    for check in checks:
        if check.status == "pending_confirmation" and check.unmet and any(
            "没有在单据发出前" in item or "未看到" in item for item in check.unmet
        ):
            check.status = "invalid"
        if (
            check.status == "pending_confirmation"
            and not check.unmet
            and check.conflicts == []
        ):
            # 待确认的单据本身没有缺陷，但接收方还没确认；保持待确认。
            pass
        if (
            check.status == "invalid"
            and not check.unmet
        ):
            check.unmet.append("单据存在无法并立的冲突")

    receipt_checks = _check_receipts(packet, checks)
    return checks, receipt_checks


def _check_receipts(packet: Packet, checks: Sequence[TurnoverCheck]) -> list[ReceiptCheck]:
    receipt_checks: list[ReceiptCheck] = []
    for receipt in sorted(packet.receipts.values(), key=lambda row: row.signed_at):
        check = next((row for row in checks if row.id == receipt.turnover), None)
        if check is None:
            receipt_checks.append(
                ReceiptCheck(
                    receipt,
                    "unknown",
                    "manual_ratification",
                    "签收单指向不存在的交接单，需人工追认",
                )
            )
            continue
        covered = set(receipt.items) <= set(check.raw_items)
        if check.status == "valid" and covered:
            status = "consistent"
            detail = f"{check.revision} 有效，签收数量在单据范围内"
        else:
            status = "manual_ratification"
            detail = (
                f"{check.revision} 在签收时为 {check.status}，"
                f"{'、'.join(sorted(receipt.items))} 已实际签收；需人工追认或补办，不能当余物再派"
            )
            signed = {
                sku: _to_cartons(sku, units, packet) for sku, units in receipt.items.items()
            }
            check.signed_items.update(signed)
        receipt_checks.append(ReceiptCheck(receipt, check.status, status, detail))
    return receipt_checks


def _freeze_parts(
    packet: Packet,
    offers: Sequence[OfferBundle],
    stances: Sequence[Stance],
    turnovers: Sequence[TurnoverCheck],
    receipts: Sequence[ReceiptCheck],
) -> list[FrozenPart]:
    """冻结有争议的未交割部分；已实际签收的物品转人工追认，不当余物再派。"""

    frozen: list[FrozenPart] = []
    signed_by_sku: dict[str, float] = {}
    for row in receipts:
        for sku, units in row.receipt.items.items():
            signed_by_sku[sku] = signed_by_sku.get(sku, 0.0) + _to_cartons(sku, units, packet)

    disputed_by_sku: dict[str, list[TurnoverCheck]] = {}
    for check in turnovers:
        if check.status not in {"invalid", "frozen", "incomplete"}:
            continue
        for sku in check.items:
            disputed_by_sku.setdefault(sku, []).append(check)

    for offer in offers:
        if offer.superseded_by:
            continue
        for sku, qty in offer.items.items():
            checks = disputed_by_sku.get(sku)
            if not checks:
                continue
            closed = [
                stance
                for stance in stances
                if stance.bundle_id == offer.id
                and sku in stance.refused_items
                and stance.status == "withdrawal_effective"
            ]
            signed = signed_by_sku.get(sku, 0.0)
            if signed and not closed and not any(
                check.revision in {c.revision for c in checks} for check in checks
            ):
                continue
            remaining = max(qty - signed, 0.0)
            if remaining == 0 and signed:
                reason = (
                    f"{'、'.join(check.revision for check in checks)} 对该物品存在冲突，"
                    "但已实际签收；需人工追认或补办，不能当成可再次派发的余物"
                )
                frozen.append(
                    FrozenPart(
                        items={},
                        already_signed={sku: signed},
                        reason=reason,
                        refs=tuple(
                            dict.fromkeys(
                                [check.revision for check in checks]
                                + [row.receipt.id for row in receipts if sku in row.receipt.items]
                            )
                        ),
                        needed_from=("捐出方",),
                    )
                )
                continue
            reasons = [f"{'、'.join(check.revision for check in checks)} 对该物品存在冲突"]
            if closed:
                reasons.append(f"{closed[0].message_id} 撤回了该物品")
            if signed:
                reasons.append(f"其中 {clean_number(signed)}箱已实际签收，需人工追认或补办")
            reasons.append("未交割部分冻结，不另行派发")
            if remaining <= 0:
                continue
            frozen.append(
                FrozenPart(
                    items={sku: remaining},
                    already_signed={sku: signed} if signed else {},
                    reason="；".join(reasons),
                    refs=tuple(
                        dict.fromkeys(
                            [check.revision for check in checks]
                            + [stance.message_id for stance in closed]
                            + [row.receipt.id for row in receipts if sku in row.receipt.items]
                        )
                    ),
                    needed_from=tuple(
                        dict.fromkeys(
                            ["捐出方"]
                            + [
                                stance.party
                                for stance in stances
                                if stance.bundle_id == offer.id
                                and stance.status in {"withdrawal_effective", "needs_clarification"}
                            ]
                        )
                    ),
                )
            )
    return frozen


def _pending_questions(
    packet: Packet,
    offers: Sequence[OfferBundle],
    stances: Sequence[Stance],
    turnovers: Sequence[TurnoverCheck],
    receipts: Sequence[ReceiptCheck],
    frozen: Sequence[FrozenPart],
    missing: Mapping[str, list[str]],
) -> tuple[list[PendingQuestion], list[PendingQuestion]]:
    pendings: list[PendingQuestion] = []
    clarifications: list[PendingQuestion] = []

    for check in turnovers:
        if check.status in {"pending_confirmation", "invalid", "incomplete", "frozen"}:
            if check.missing_refs:
                pendings.append(
                    PendingQuestion(
                        party="协调员",
                        about=check.revision,
                        question=f"补充上游来信 {'、'.join(check.missing_refs)} 后再核对 {check.revision}",
                        reason="缺少上游引用，暂不能替双方给出结论",
                        refs=check.sources,
                        version=check.revision,
                    )
                )
                continue
            offer = _bundle_for_items(check.items, offers, check.receiver)
            for unmet in check.unmet:
                if "尚未确认" in unmet:
                    pendings.append(
                        PendingQuestion(
                            party=check.receiver,
                            about=check.revision,
                            question=f"请确认 {check.revision} 的版本内容："
                            + "、".join(f"{sku} {clean_number(qty)}箱" for sku, qty in check.items.items()),
                            reason="该版本已展示给该方，但尚无有效答复",
                            refs=tuple(ref for ref in check.sources if ref in packet.messages),
                            version=check.revision,
                        )
                    )
                elif "条件" in unmet:
                    if check.conflicts:
                        # 条件不成立是因为另一方的在先承诺，不是让接收方
                        # 重新答应一次就能解决。
                        pendings.append(
                            PendingQuestion(
                                party="协调员",
                                about=check.revision,
                                question=(
                                    f"{check.revision} 不能成立：{check.conflicts[0]}；"
                                    "请把版本退回双方并说明下一步"
                                ),
                                reason=unmet,
                                refs=tuple(ref for ref in check.sources if ref in packet.messages),
                                version=check.revision,
                            )
                        )
                    else:
                        pendings.append(
                            PendingQuestion(
                                party=check.receiver,
                                about=check.revision,
                                question=f"请重新确认 {check.revision}：原附条件未满足",
                                reason=unmet,
                                refs=tuple(ref for ref in check.sources if ref in packet.messages),
                                version=check.revision,
                            )
                        )
            if check.status == "frozen":
                parties = {
                    stance.party
                    for stance in stances
                    if stance.bundle_id
                    and stance.message_id
                    in {ref for ref in check.sources} | {
                        other.message_id
                        for other in stances
                        if other.bundle_id
                        and set(other.positive_items) & set(check.items)
                    }
                }
                for party in sorted(parties - {"捐出方", "协调员"}) or sorted(parties):
                    pendings.append(
                        PendingQuestion(
                            party=party,
                            about=check.revision,
                            question=(
                                f"{check.revision} 有争议的未交割部分已冻结："
                                + "、".join(
                                    f"{sku} {clean_number(qty)}箱"
                                    for sku, qty in check.frozen_items.items()
                                )
                                + "；请有权补确认的一方补充确认"
                            ),
                            reason="单据发出后收到相互冲突的早期消息",
                            refs=tuple(sorted(check.sources)),
                            version=check.revision,
                        )
                    )
        # 已签收但要人工追认的，仍要向协调员/捐出方提问。
        for receipt_check in receipts:
            if receipt_check.receipt.turnover != check.id or receipt_check.status != "manual_ratification":
                continue
            pendings.append(
                PendingQuestion(
                    party="捐出方",
                    about=receipt_check.receipt.id,
                    question=(
                        f"{check.revision} 发生实际签收（{receipt_check.receipt.items}），"
                        "请人工追认或补办，不能按余物再次派发"
                    ),
                    reason=receipt_check.detail,
                    refs=tuple(ref for ref in check.sources if ref in packet.messages),
                    version=check.revision,
                )
            )

    # 未经有效单据的接受，也需要单据确认。
    for offer in offers:
        if offer.superseded_by:
            continue
        accepted = [
            stance
            for stance in stances
            if stance.bundle_id == offer.id and stance.effective
        ]
        # 报价自身缺上游引用，或答复引用的上游说明缺失：只登记受理，
        # 明确告诉协调员哪部分还不能替双方下结论。
        if offer.missing_refs:
            victims = [
                stance
                for stance in stances
                if stance.bundle_id == offer.id and stance.status == "unverifiable"
            ]
            if victims:
                pendings.append(
                    PendingQuestion(
                        party="协调员",
                        about=offer.id,
                        question=(
                            "补充上游来信 " + "、".join(offer.missing_refs)
                            + "；其余独立承诺可照常处理，这部分暂不能替双方给出结论"
                        ),
                        reason="缺上游引用，本条只能先受理",
                        refs=(offer.root_id,),
                    )
                )
            continue
        if not accepted and not any(
            stance.bundle_id == offer.id and stance.status == "unverifiable"
            for stance in stances
        ):
            # 还没人接受：如果是针对某方的报价且没有其他问题时，提醒该方或在途问题。
            if offer.target and offer.root_id in packet.messages:
                if not any(
                    pending.party == offer.target and offer.root_id in pending.refs
                    for pending in pendings
                ):
                    covered = any(
                        check.status in {"pending_confirmation", "valid"}
                        and set(offer.items) & set(check.items)
                        for check in turnovers
                    )
                    if not covered:
                        fates = [
                            stance
                            for stance in stances
                            if stance.bundle_id == offer.id
                        ]
                        if fates:
                            pendings.append(
                                PendingQuestion(
                                    party=offer.target,
                                    about=offer.id,
                                    question=f"请答复 {offer.describe()} 是否接受",
                                    reason="；".join(
                                        f"{stance.message_id}：{stance.status}"
                                        for stance in fates
                                    )
                                    or "尚未答复",
                                    refs=(offer.root_id,),
                                )
                            )
            continue
        covered = any(
            check.status in {"pending_confirmation", "valid", "frozen", "invalid"}
            and set(offer.items) & set(check.items)
            for check in turnovers
        )
        if not covered:
            recipients = {stance.party for stance in accepted}
            if len(recipients) > 1:
                clarifications.append(
                    PendingQuestion(
                        party="捐出方",
                        about=offer.id,
                        question=(
                            f"{offer.describe()} 被 {('、'.join(sorted(recipients)))} "
                            "同时接受，请明确归属后再起草交接单"
                        ),
                        reason="同一组物资不能同时确认给两方，协调员不替捐出方决定",
                        refs=(offer.root_id,),
                    )
                )
                continue
            for stance in accepted:
                pendings.append(
                    PendingQuestion(
                        party="协调员",
                        about=offer.id,
                        question=f"为 {stance.party} 起草交接单：{offer.describe()}",
                        reason=f"{stance.message_id} 已有效接受，但还没有对应单据",
                        refs=tuple(dict.fromkeys([offer.root_id, stance.message_id])),
                    )
                )

    # 两方各自提出的条件都可能成立、原话又没有说明先后：必须由捐出方
    # 澄清，协调员不替人决定。也包括同一组物资被两方在相近时点接受。
    for offer in offers:
        if offer.superseded_by or offer.missing_refs:
            continue
        parties: dict[str, list[Stance]] = {}
        for stance in stances:
            if stance.bundle_id != offer.id or stance.party in {"协调员", "捐出方"}:
                continue
            if stance.status in {
                "accepted",
                "condition_unmet",
                "needs_clarification",
                "withdrawn",
            }:
                parties.setdefault(stance.party, []).append(stance)
        if len(parties) < 2:
            continue
        # 已经有明确先后（一方在另一方之前有效接受，或期限已给出改送顺
        # 序）时，不要求澄清。
        resolved = bool(offer.deadline or offer.fallback)
        if resolved:
            continue
        names = "、".join(parties)
        clarifications.append(
            PendingQuestion(
                party="捐出方",
                about=offer.id,
                question=f"{offer.describe()} 同时涉及 {names}，请明确先后或各自可取的部分",
                reason="两方的条件都可能成立，原话没有说明先后；协调员不替捐出方决定",
                refs=(offer.root_id,),
            )
        )

    return pendings, clarifications


def _pickups(
    offers: Sequence[OfferBundle],
    stances: Sequence[Stance],
    turnovers: Sequence[TurnoverCheck],
    frozen: Sequence[FrozenPart],
) -> list[Pickup]:
    pickups: list[Pickup] = []
    frozen_ids = {sku for part in frozen for sku in part.items}
    for check in turnovers:
        if check.status != "valid":
            continue
        available = {
            sku: qty for sku, qty in check.items.items() if sku not in frozen_ids
        }
        # 已实际签收、又受争议的部分只能人工追认，不再算可取。
        for part in frozen:
            for sku in part.already_signed:
                available.pop(sku, None)
        if not available:
            continue
        basis = [check.revision]
        accepting = [
            stance
            for stance in stances
            if stance.effective
            and stance.party == check.receiver
            and set(stance.positive_items) & set(available)
        ]
        basis.extend(stance.message_id for stance in accepting)
        location = None
        for stance in sorted(accepting, key=lambda row: row.sent, reverse=True):
            location = stance.location or location
        pickups.append(
            Pickup(
                party=check.receiver,
                items=available,
                location=location,
                basis=tuple(dict.fromkeys(basis)),
                turnover=check.revision,
                conditions=tuple(
                    f"{check_row.message_id}：{condition.text}"
                    for check_row in accepting
                    for condition in check_row.conditions
                    if condition.satisfied
                ),
            )
        )
    return pickups


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------


def analyse_file(path: str) -> Analysis:
    from .models import load_packet

    return analyse(load_packet(path))
