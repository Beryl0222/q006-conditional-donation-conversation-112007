"""授权与发言资格。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .models import Grant, Packet
from .text import Reading


@dataclass(frozen=True)
class Authority:
    speaker: str
    principal: str
    can_commit: bool
    can_location: bool
    reason: str
    grant: Grant | None = None


def authority_for(reading: Reading, packet: Packet) -> Authority:
    """判断发言人能否代表某方作出本条所含的承诺。

    记录里的委托人本人发言，当然可以承诺。代理人只有在授权范围内
    才能改变被代理方的承诺；只授权确认收货地点的，不能替捐出方同
    意少发一箱。
    """

    message = reading.message
    day = message.sent.date()
    grants = packet.grant_for_agent(message.sender, day)
    if not grants:
        principal = _self_principal(message.sender, message.thread, packet)
        if principal is None:
            return Authority(
                speaker=message.sender,
                principal=message.sender,
                can_commit=False,
                can_location=False,
                reason="无法确定发言人代表哪一方",
            )
        return Authority(
            speaker=message.sender,
            principal=principal,
            can_commit=True,
            can_location=True,
            reason="本人发言",
        )

    grant = max(grants, key=lambda item: (item.expires or date.max))
    principal = grant.principal
    can_location = grant.allows("location") or grant.allows("full")
    content_domains: set[str] = set()
    for group in reading.groups:
        if group.polarity in {"refuse", "withdraw"}:
            content_domains.add("withdraw")
        if group.items:
            content_domains.add("items")
            content_domains.add("quantity")
    if reading.withdrawal or reading.refusal:
        content_domains.add("withdraw")
    can_commit = all(grant.allows(domain) for domain in content_domains) if content_domains else (
        can_location or grant.allows("full")
    )
    if can_commit:
        reason = f"授权范围 {'、'.join(grant.scopes)} 覆盖本条"
    else:
        missing = [domain for domain in sorted(content_domains) if not grant.allows(domain)]
        label = {"items": "物品", "quantity": "箱数", "withdraw": "撤回／拒绝"}
        reason = "授权只含" + "、".join(grant.scopes) + "，不含" + "、".join(label.get(d, d) for d in missing)
    return Authority(
        speaker=message.sender,
        principal=principal,
        can_commit=can_commit,
        can_location=can_location,
        reason=reason,
        grant=grant,
    )


def _self_principal(sender: str, thread: str, packet: Packet) -> str | None:
    if sender in packet.parties and sender != "捐出方":
        return sender
    if thread in packet.parties:
        return thread
    if sender == "捐出方" or sender in packet.donors:
        return "捐出方"
    return None
