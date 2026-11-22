"""资料包的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import json


BEIJING = timezone(timedelta(hours=8))


def parse_moment(value: str) -> datetime:
    """读入 ISO 时间；没有时区的按北京时间理解。"""

    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BEIJING)
    return moment


@dataclass(frozen=True)
class Item:
    sku: str
    description: str
    per_carton: int | None = None

    def units(self, cartons: float) -> float:
        if self.per_carton is None:
            return cartons
        return cartons * self.per_carton


@dataclass
class Message:
    id: str
    thread: str
    sender: str
    sent: datetime
    ingested: datetime
    text: str
    viewers: tuple[str, ...] = ()
    reply_to: str | None = None
    forwards: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    def can_be_seen_by(self, party: str) -> bool:
        return party in self.viewers

    @property
    def references(self) -> tuple[str, ...]:
        refs: list[str] = []
        if self.reply_to:
            refs.append(self.reply_to)
        refs.extend(self.forwards)
        return tuple(dict.fromkeys(refs))


@dataclass(frozen=True)
class Grant:
    id: str
    principal: str
    agent: str
    scopes: tuple[str, ...]
    expires: date | None = None

    def active_on(self, day: date) -> bool:
        return self.expires is None or day <= self.expires

    def allows(self, domain: str) -> bool:
        """判断授权是否覆盖某一类动作。

        ``domain`` 是 location / items / quantity / full 之一。
        授权文字只要写出对应范围即可；写“全权”则覆盖全部。
        """

        if not self.scopes:
            return False
        joined = "、".join(self.scopes)
        if any(word in joined for word in ("全权", "一切", "所有事项", "全部事项")):
            return True
        words = {
            "location": ("地点", "收货", "仓库", "接收", "到场"),
            "items": ("品项", "物品", "内容", "赠送", "器材", "物资"),
            "quantity": ("数量", "箱数", "件数", "多少"),
            "withdraw": ("撤回", "取消", "变更", "更改"),
        }
        if domain == "full":
            return all(self.allows(name) for name in ("location", "items", "quantity"))
        return any(word in joined for word in words.get(domain, ()))


@dataclass
class Turnover:
    id: str
    revision: str
    sources: tuple[str, ...]
    declared_state: str
    receiver: str
    items: Mapping[str, float]
    shown_to: tuple[str, ...]
    time: datetime
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def version(self) -> str:
        return self.revision

    @property
    def version_number(self) -> int:
        _, _, tail = self.revision.rpartition("/")
        return int(tail) if tail.isdigit() else 1

    @property
    def sheet_number(self) -> str:
        head, _, _ = self.revision.partition("/")
        return head


@dataclass
class Receipt:
    id: str
    turnover: str
    receiver: str
    items: Mapping[str, float]
    signed_at: datetime
    note: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)


class Packet:
    """一次性读入的合成资料包，保留原始编号以便追溯。"""

    def __init__(
        self,
        items: Iterable[Item],
        messages: Iterable[Message],
        grants: Iterable[Grant] = (),
        turnovers: Iterable[Turnover] = (),
        receipts: Iterable[Receipt] = (),
    ) -> None:
        self.items = {item.sku: item for item in items}
        self.messages = {message.id: message for message in messages}
        self.grants = list(grants)
        self.turnovers = {turnover.id: turnover for turnover in turnovers}
        self.receipts = {receipt.id: receipt for receipt in receipts}

    # -- 常用索引 ---------------------------------------------------------
    @property
    def donors(self) -> set[str]:
        return {m.sender for m in self.messages.values() if m.sender == "捐出方"}

    @property
    def coordinators(self) -> set[str]:
        return {m.sender for m in self.messages.values() if m.sender == "协调员"}

    @property
    def parties(self) -> list[str]:
        """可能作为一方的名称：发言人、可见人、单据接收方与授权双方。"""

        names: list[str] = []
        for message in self.messages.values():
            names.append(message.sender)
            names.extend(message.viewers)
        for turnover in self.turnovers.values():
            names.append(turnover.receiver)
            names.extend(turnover.shown_to)
        for grant in self.grants:
            names.extend((grant.principal, grant.agent))
        special = {"捐出方", "协调员"}
        ordered: list[str] = []
        for name in names:
            if name and name not in special and name not in ordered:
                ordered.append(name)
        return ordered

    def grant_for_agent(self, agent: str, day: date) -> list[Grant]:
        return [
            grant
            for grant in self.grants
            if grant.agent == agent and grant.active_on(day)
        ]

    def role_of(self, sender: str) -> str:
        if sender == "捐出方" or sender in self.donors:
            return "donor"
        if sender == "协调员" or sender in self.coordinators:
            return "coordinator"
        if any(grant.agent == sender for grant in self.grants):
            return "agent"
        if any(turnover.receiver == sender for turnover in self.turnovers.values()):
            return "receiver"
        return "site"

    def party_context(self, message: Message) -> str | None:
        """推断发言人代表哪一方：本人，或授权记录中的委托方。"""

        grants = [g for g in self.grant_for_agent(message.sender, message.sent.date())]
        if len(grants) == 1:
            return grants[0].principal
        if message.sender in self.parties:
            return message.sender
        if message.thread in self.parties:
            return message.thread
        return None


def load_packet(path: str | Path) -> Packet:
    """读取资料文件。缺字段或引用丢失不在读取阶段抛错，交给分析阶段说明。"""

    with Path(path).open(encoding="utf-8") as stream:
        groups = json.load(stream)
    if not isinstance(groups, dict) or not groups:
        raise ValueError("资料应包含非空的记录类别")

    def rows(kind: str) -> list[dict[str, Any]]:
        value = groups.get(kind, [])
        if not isinstance(value, list):
            raise ValueError(f"{kind} 应为记录数组")
        return value

    items: list[Item] = []
    for row in rows("item_register"):
        sku = row.get("sku") or row.get("id")
        if not isinstance(sku, str) or not sku:
            raise ValueError("item_register 中存在没有物品编号的记录")
        items.append(
            Item(
                sku=sku,
                description=str(row.get("description", "")),
                per_carton=row.get("per_carton"),
            )
        )

    messages: list[Message] = []
    for row in rows("inbox"):
        messages.append(
            Message(
                id=str(row["id"]),
                thread=str(row.get("thread", "")),
                sender=str(row.get("sender", "")),
                sent=parse_moment(row["sent"]),
                ingested=parse_moment(row.get("ingested") or row["sent"]),
                text=str(row.get("text", "")),
                viewers=tuple(row.get("viewers") or ()),
                reply_to=row.get("reply_to"),
                forwards=tuple(row.get("forwards") or ()),
                raw=row,
            )
        )

    grants: list[Grant] = []
    for row in rows("grant"):
        expires = row.get("expires")
        grants.append(
            Grant(
                id=str(row["id"]),
                principal=str(row["principal"]),
                agent=str(row["agent"]),
                scopes=tuple(row.get("scopes") or ()),
                expires=date.fromisoformat(expires) if expires else None,
            )
        )

    turnovers: list[Turnover] = []
    for row in rows("turnover"):
        sources = row.get("source") or ()
        if isinstance(sources, str):
            sources = (sources,)
        turnovers.append(
            Turnover(
                id=str(row["id"]),
                revision=str(row.get("revision", row["id"])),
                sources=tuple(str(source) for source in sources),
                declared_state=str(row.get("state", "")),
                receiver=str(row["receiver"]),
                items={str(k): float(v) for k, v in (row.get("items") or {}).items()},
                shown_to=tuple(row.get("shown_to") or ()),
                time=parse_moment(row["time"]),
                raw=row,
            )
        )

    receipts: list[Receipt] = []
    for row in rows("receipt"):
        receipts.append(
            Receipt(
                id=str(row["id"]),
                turnover=str(row["turnover"]),
                receiver=str(row.get("receiver", "")),
                items={str(k): float(v) for k, v in (row.get("items") or {}).items()},
                signed_at=parse_moment(row["signed_at"]),
                note=str(row.get("note", "")),
                raw=row,
            )
        )

    return Packet(items, messages, grants, turnovers, receipts)
