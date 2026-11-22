"""中文消息的语义提取：物品、箱数、地点、期限、条件、指代与语气。

这里只回答“这句话字面上承诺／询问／拒绝了什么”。是否有权、是否逾
期、条件是否成立、能否与其他消息并立，由 :mod:`donation_ledger.ledger`
结合授权、发送顺序和签收记录判断。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Sequence

from .models import BEIJING, Message, Packet

_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_NUMBER = r"[零〇一二两三四五六七八九十百\d]+"
_CARTON_RE = re.compile(rf"({_NUMBER}|半)\s*箱")
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_WEEKDAY_RE = re.compile(
    r"(?:周|星期)\s*([一二三四五六日天])\s*(?:(\d{1,2})\s*[时点](?:\s*(\d{1,2})\s*分?)?)?"
)
_DATE_RE = re.compile(
    r"(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(?:(\d{1,2})\s*[时点](?:\s*(\d{1,2})\s*分?)?)?"
)
_WAREHOUSE_RE = re.compile(r"([一-鿿]{1,4})仓")
# “在／到／去／送”之后紧跟的才是地点名。
_WAREHOUSE_LEAD_RE = re.compile(r"(?:在|到|去|往|送|交给)([一-鿿]{1,4})仓")
_TARGET_RE = re.compile(r"(?:送给|给)([一-鿿A-Za-z0-9]{1,8})")
_FALLBACK_RE = re.compile(r"(?:便|就|则|再)?改给\s*([一-鿿A-Za-z0-9]{1,8})")
_QUOTE_RE = re.compile(r"他说|她说|对方说|据[^，。；]{0,8}说|转告|转达|转述|引用|原文|听说")
_WITHDRAW_RE = re.compile(r"撤回|取消|作废|不算|不作数|收回|撤销")
_REFUSE_RE = re.compile(
    r"不接受|不同意|不要|请勿|请不要|拒绝|不接|别送|不必|不派|不单独|先不要|暂不要|别发|先别"
)
# “请勿单独派来”是拒绝“拆开单送”，不是拒绝物品本身；先单独识别。
_NO_SPLIT_RE = re.compile(r"(?:不要|请勿|别)\s*单独|不必单独|不可单独|不能单独")
_REQUEST_RE = re.compile(
    r"请[^。！？]{0,16}(?:确认|回复|答复|说|选|定)|请说|能否|可否|是否|要不要|"
    r"哪一个|哪一种|哪个|选哪|确认一下|[?？]"
)
_COUNTER_RE = re.compile(r"改成|改为|换成|换作|要不|只能|少发|只送|只给|先来|先送|先发|减为|减到")
_ACCEPT_RE = re.compile(r"确认|同意|接受|可以|可行|就照|就按|按上次|没问题|就这么|要了|好的|行")
_COMMIT_RE = re.compile(r"提走|来取|来接|接走|收下|签收|领走")
_OFFER_RE = re.compile(r"送给|送给|给|送|可单送|可分开|提供|捐")
_CONDITION_RE = re.compile(r"若|如果|假如|倘若|要是|一旦")
_NO_ACCEPT_RE = re.compile(
    r"无人确认|没人确认|没人接|无人接|没接|还未接|未接|还没有人|没人要|"
    r"过了?点|过了?期|逾期|到点"
)
_SIGNED_RE = re.compile(r"签收|签了|已收|领走|提走|收下")
_SEQUENCE_RE = re.compile(r"后续|以后|随后|再送|下一批|第二批|之后")
_JOINT_RE = re.compile(r"成对|成套|整套|一起|合送|配套|同时|一并|共同|两样都要|两样一起|都")
_SEVERABLE_RE = re.compile(r"单送|单独|分开|分别|独立|任选|也可以|可单|各自")
_DEADLINE_CUE_RE = re.compile(r"前|之前|以前|截止|到期|过期|过了?点|逾期|到点")
_NO_ACCEPT_CUE_RE = re.compile(r"无人|没人|还未接|未接|没有人|没人要")

_STOP_TARGET = {"改", "便", "就", "则", "再", "把", "将", "往", "到", "去", "在", "是", "从", "和", "与"}


def parse_number(value: str) -> float | None:
    """把“一”“两”“十二”“半”等写法换成数字。"""

    value = value.strip()
    if not value:
        return None
    if value == "半":
        return 0.5
    if value.isdigit():
        return int(value)
    if "百" in value:
        left, _, right = value.partition("百")
        hundreds = parse_number(left) if left else 1
        rest = parse_number(right) if right else 0
        if hundreds is None or rest is None:
            return None
        return hundreds * 100 + rest
    if "十" in value:
        left, _, right = value.partition("十")
        tens = parse_number(left) if left else 1
        ones = parse_number(right) if right else 0
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    digits = [_CN_DIGITS.get(char) for char in value]
    if any(digit is None for digit in digits):
        return None
    number = 0
    for digit in digits:
        number = number * 10 + digit
    return number


def clean_number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


@dataclass(frozen=True)
class Deadline:
    at: datetime
    text: str
    weekday: str | None = None
    inherited: bool = False


@dataclass(frozen=True)
class Condition:
    kind: str
    text: str
    party: str | None = None
    item: str | None = None
    deadline: datetime | None = None


@dataclass
class ItemQuantity:
    sku: str
    cartons: float
    source: str = "explicit"  # explicit / inherited / implied / total
    batch: str | None = None

    def as_text(self) -> str:
        return f"{self.sku} {clean_number(self.cartons)}箱"


@dataclass
class ItemGroup:
    items: list[ItemQuantity]
    split_allowed: bool | None = None
    target: str | None = None
    text: str = ""
    polarity: str = "neutral"
    batch: str | None = None
    deadline: Deadline | None = None
    fallback: str | None = None

    @property
    def quantity_map(self) -> dict[str, float]:
        return {item.sku: item.cartons for item in self.items}


@dataclass
class Alternative:
    label: str
    text: str
    groups: list[ItemGroup] = field(default_factory=list)
    day: date | None = None

    @property
    def items(self) -> list[ItemQuantity]:
        seen: dict[str, ItemQuantity] = {}
        for group in self.groups:
            for item in group.items:
                seen.setdefault(item.sku, item)
        return list(seen.values())

    @property
    def split_allowed(self) -> bool | None:
        policies = {group.split_allowed for group in self.groups if group.split_allowed is not None}
        if len(policies) == 1:
            return policies.pop()
        return None

    @property
    def batches(self) -> list[list[ItemQuantity]]:
        batches: dict[str, list[ItemQuantity]] = {}
        for group in self.groups:
            for item in group.items:
                if item.batch:
                    batches.setdefault(item.batch, []).append(item)
        return list(batches.values())


@dataclass
class Reading:
    message: Message
    kind: str
    is_relay: bool
    binding: bool
    groups: list[ItemGroup] = field(default_factory=list)
    alternatives: list[Alternative] = field(default_factory=list)
    selected_alternative: str | None = None
    location: str | None = None
    target: str | None = None
    deadline: Deadline | None = None
    fallback: str | None = None
    conditions: list[Condition] = field(default_factory=list)
    references: list[tuple[str, str, str]] = field(default_factory=list)
    missing_refs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    request: str | None = None
    withdrawal: bool = False
    refusal: bool = False
    reduction: bool = False

    @property
    def items(self) -> list[ItemQuantity]:
        seen: dict[str, ItemQuantity] = {}
        for group in self.groups:
            for item in group.items:
                seen.setdefault(item.sku, item)
        return list(seen.values())

    @property
    def quantity_map(self) -> dict[str, float]:
        return {item.sku: item.cartons for item in self.items}

    @property
    def joint(self) -> bool | None:
        policies = {group.split_allowed for group in self.groups if group.split_allowed is not None}
        if len(policies) == 1:
            return not policies.pop()  # 兼容旧字段：joint=True 表示不可拆
        return None


class Resolver:
    """在一段会话里定位“上次”“备选”“两种”等所指的前文。"""

    def __init__(self, packet: Packet) -> None:
        self.packet = packet
        self.readings: dict[str, Reading] = {}

    def remember(self, reading: Reading) -> None:
        self.readings[reading.message.id] = reading

    def history(self, message: Message) -> list[Message]:
        return sorted(
            (
                other
                for other in self.packet.messages.values()
                if other.id != message.id and other.sent <= message.sent
            ),
            key=lambda other: (other.sent, other.id),
        )

    def prior_in_thread(self, message: Message) -> Message | None:
        candidates = [
            other
            for other in self.history(message)
            if other.thread == message.thread and other.sender != message.sender
        ]
        return candidates[-1] if candidates else None

    def latest_visible(self, message: Message) -> Message | None:
        candidates = [
            other
            for other in self.history(message)
            if other.can_be_seen_by(message.sender) or message.sender in {"协调员", "捐出方"}
        ]
        return candidates[-1] if candidates else None

    def latest_with_alternatives(self, message: Message) -> Message | None:
        for other in reversed(self.history(message)):
            reading = self.readings.get(other.id)
            if reading and reading.alternatives:
                return other
        return None

    def prior_for(self, message: Message) -> Message | None:
        if message.reply_to and message.reply_to in self.packet.messages:
            return self.packet.messages[message.reply_to]
        for ref in message.forwards:
            if ref in self.packet.messages:
                return self.packet.messages[ref]
        if re.search(r"上次|上回|前述|上面|刚才|前面", message.text):
            return self.prior_in_thread(message) or self.latest_visible(message)
        if re.search(r"备选|两套|两种说法|选哪个|选哪", message.text):
            return self.latest_with_alternatives(message)
        return None

    def offer_root(self, message: Message) -> Message | None:
        """沿回复／转述链找到最初那条捐出方的话。"""

        seen: set[str] = set()
        current: Message | None = message
        while current and current.id not in seen:
            seen.add(current.id)
            if current.sender == "捐出方" or current.sender in self.packet.donors:
                return current
            next_ref = current.reply_to or (current.forwards[0] if current.forwards else None)
            if next_ref and next_ref in self.packet.messages:
                current = self.packet.messages[next_ref]
            else:
                break
        candidates = [
            other
            for other in self.history(message)
            if other.sender in {"捐出方", *self.packet.donors} and other.thread == message.thread
        ]
        return candidates[-1] if candidates else None


def _aliases(packet: Packet) -> list[tuple[str, str]]:
    aliases: list[tuple[str, str]] = []
    for item in packet.items.values():
        description = item.description.strip()
        if description:
            aliases.append((description, item.sku))
        for marker in ("备用", "手作", "手工", "专用"):
            if description.startswith(marker) and len(description) > len(marker):
                aliases.append((description[len(marker) :], item.sku))
        aliases.append((item.sku, item.sku))
    aliases.sort(key=lambda pair: len(pair[0]), reverse=True)
    return aliases


def _mentions(text: str, aliases: Sequence[tuple[str, str]]) -> list[tuple[int, str, str]]:
    found: list[tuple[int, int, str, str]] = []
    for alias, sku in aliases:
        for match in re.finditer(re.escape(alias), text, flags=re.IGNORECASE):
            found.append((match.start(), match.end(), match.group(0), sku))
    found.sort(key=lambda entry: (entry[0], -(entry[1] - entry[0])))
    chosen: list[tuple[int, int, str, str]] = []
    for start, end, word, sku in found:
        if any(start < right and end > left for left, right in ((c[0], c[1]) for c in chosen)):
            continue
        chosen.append((start, end, word, sku))
    chosen.sort(key=lambda entry: entry[0])
    results: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    for start, _, word, sku in chosen:
        if sku in seen:
            continue
        seen.add(sku)
        results.append((start, word, sku))
    return results


def _warehouse(text: str) -> str | None:
    # 匹配到“仓”前面的 1–3 个汉字；去掉“送”“改送”等动词后取地名。
    for match in reversed(list(_WAREHOUSE_LEAD_RE.finditer(text))):
        if match.group(1):
            return match.group(1) + "仓"
    for match in reversed(list(_WAREHOUSE_RE.finditer(text))):
        candidate = match.group(1)
        for prefix in ("改送", "再送", "先送", "送到", "送往", "送", "去", "到", "往", "在", "于"):
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix) :]
                break
        if candidate:
            return candidate + "仓"
    return None


def _party_in(candidate: str, packet: Packet) -> str | None:
    candidate = candidate.strip("，,。；;：: 　")
    for party in sorted(packet.parties, key=len, reverse=True):
        if candidate.startswith(party):
            return party
    return None


def _target(text: str, packet: Packet) -> str | None:
    for match in _TARGET_RE.finditer(text):
        prefix = text[max(0, match.start() - 1) : match.start()]
        if prefix == "改":
            continue
        found = _party_in(match.group(1), packet)
        if found:
            return found
    return None


def _clause_target(clause: str, packet: Packet) -> str | None:
    """按分句取接受方；“一箱给岭东，一箱给盐田”要分开落地。"""

    return _target(clause, packet)


def _fallback(text: str, packet: Packet) -> str | None:
    match = _FALLBACK_RE.search(text)
    if not match:
        return None
    return _party_in(match.group(1), packet)


def _weekday_deadline(text: str, sent: datetime) -> tuple[datetime, str, str] | None:
    match = _WEEKDAY_RE.search(text)
    if not match:
        return None
    weekday_char = match.group(1)
    target_index = _WEEKDAY[weekday_char]
    hour = int(match.group(2)) if match.group(2) else 23
    minute = int(match.group(3)) if match.group(3) else (59 if hour == 23 else 0)
    at_time = time(hour, minute)
    base = sent.date()
    delta = (target_index - base.weekday()) % 7
    if delta == 0 and datetime.combine(base, at_time, tzinfo=BEIJING) <= sent:
        delta = 7
    target = base + timedelta(days=delta)
    return datetime.combine(target, at_time, tzinfo=BEIJING), match.group(0), weekday_char


def parse_deadline(
    text: str, sent: datetime, inherited: Deadline | None = None, *, require_cue: bool = True
) -> Deadline | None:
    """只在出现“前／截止／逾期”等时限语气时把时间当期限，避免把提货日误判。"""

    has_cue = bool(_DEADLINE_CUE_RE.search(text) or _NO_ACCEPT_CUE_RE.search(text))
    if require_cue and not has_cue:
        return None
    date_match = _DATE_RE.search(text)
    if date_match:
        month, day = int(date_match.group(1)), int(date_match.group(2))
        hour = int(date_match.group(3)) if date_match.group(3) else 23
        minute = int(date_match.group(4)) if date_match.group(4) else (59 if hour == 23 else 0)
        try:
            at = datetime(sent.year, month, day, hour, minute, tzinfo=BEIJING)
        except ValueError:
            return None
        return Deadline(at, date_match.group(0))
    weekday = _weekday_deadline(text, sent)
    if weekday:
        at, word, weekday_char = weekday
        return Deadline(at, word, weekday_char)
    if inherited is not None:
        return Deadline(inherited.at, inherited.text, inherited.weekday, inherited=True)
    return None


def _split_policy(text: str, polarity: str) -> bool | None:
    if polarity == "no_split":
        return False
    if _JOINT_RE.search(text) and not _SEVERABLE_RE.search(text):
        return False
    if _JOINT_RE.search(text) and _SEVERABLE_RE.search(text):
        # “成套但可分开”之类没有明确态度，留给原话歧义。
        return None
    if _SEVERABLE_RE.search(text):
        return True
    return None


_DEFER_RE = re.compile(r"再说|以后再|暂不(?!要)|先不(?!要)|随后再|待定|回头")


def _polarity(text: str) -> str:
    if _WITHDRAW_RE.search(text):
        return "withdraw"
    if _DEFER_RE.search(text):
        return "defer"
    if _NO_SPLIT_RE.search(text):
        return "no_split"
    if _REFUSE_RE.search(text):
        return "refuse"
    if _REQUEST_RE.search(text) and not re.search(r"确认|同意|接受|就照|就按", text):
        return "request"
    if _COUNTER_RE.search(text):
        return "counter"
    if _ACCEPT_RE.search(text) or _COMMIT_RE.search(text):
        return "accept"
    return "neutral"


def _quantity_for(
    clause: str, word: str, position: int, mention_count: int, aliases: Sequence[tuple[str, str]]
) -> tuple[float | None, str]:
    per_each = re.search(rf"(?:每件|每样|每种)\s*各?\s*({_NUMBER}|半)\s*箱", clause)
    if per_each:
        return parse_number(per_each.group(1)), "explicit"
    each = re.search(rf"各\s*({_NUMBER}|半)\s*箱", clause)
    if each and mention_count >= 2:
        return parse_number(each.group(1)), "explicit"
    after = re.search(rf"{re.escape(word)}\s*(?:各)?\s*({_NUMBER}|半)\s*箱", clause, re.IGNORECASE)
    if after:
        return parse_number(after.group(1)), "explicit"
    before = list(re.finditer(rf"({_NUMBER}|半)\s*箱\s*(?:的)?\s*{re.escape(word)}", clause, re.IGNORECASE))
    if before:
        return parse_number(before[-1].group(1)), "explicit"
    cartons = list(_CARTON_RE.finditer(clause))
    if mention_count == 1 and len(cartons) == 1:
        return parse_number(cartons[0].group(1)), "explicit"
    return None, "explicit"


def _sentence_parts(text: str) -> list[list[str]]:
    """按句号分句，分号／逗号当同一句内的分句。

    “给岭东；周五前无人确认便改给盐田”是同一句里的约定，分号不能
    拆断期限和它约束的物品。
    """

    sentences = re.split(r"[。！？\n]+", text)
    return [
        [part.strip() for part in re.split(r"[，,；;]", sentence) if part.strip()]
        for sentence in sentences
    ]


def _sequence_batches(text: str, items: list[ItemQuantity]) -> list[ItemQuantity]:
    """“先送X后续送Y”按物品在分句里的先后分两批；“成套/一起”同批。"""

    cut_match = _SEQUENCE_RE.search(text)
    if not cut_match or len({item.sku for item in items}) < 2:
        return items
    aliases = _aliases_from_items(items)
    positions: dict[str, int] = {}
    for position, word, sku in _mentions(text, aliases):
        positions.setdefault(sku, position)
    ordered = sorted(items, key=lambda item: positions.get(item.sku, text.find(item.sku)))
    if not ordered:
        return items
    ordered[0].batch = "第一批"
    for item in ordered[1:]:
        item.batch = "第二批"
    return items


def _aliases_from_items(items: Sequence[ItemQuantity]) -> list[tuple[str, str]]:
    return [(item.sku, item.sku) for item in items]


def _stem_words(packet: Packet, text: str, aliases: Sequence[tuple[str, str]]) -> list[tuple[int, str, str]]:
    """除完整简称外，允许“护具”“工具”这类词根在上下文已唯一时落到 SKU。"""

    found = _mentions(text, aliases)
    if found:
        return found
    stems = [(item.description[-2:], item.sku) for item in packet.items.values() if len(item.description) >= 2]
    results: list[tuple[int, str, str]] = []
    for offset, (stem, sku) in enumerate(stems):
        position = text.find(stem)
        if position >= 0:
            results.append((position, stem, sku))
    results.sort(key=lambda entry: entry[0])
    return results


def _parse_alternatives(
    text: str, sent: datetime, packet: Packet, aliases: Sequence[tuple[str, str]], inherited_map: dict[str, float]
) -> list[Alternative]:
    markers = list(
        re.finditer(
            r"备选之一是|备选之[二两]是|备选[一二两]是|另一种是|另一种为|一是|二是|要么是|或者是",
            text,
        )
    )
    if len(markers) < 2:
        return []
    options: list[Alternative] = []
    for index, marker in enumerate(markers):
        start = marker.end()
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        fragment = text[start:end].strip(" ，,。；;")
        if not fragment:
            continue
        label = "1" if index == 0 else "2"
        groups: list[ItemGroup] = []
        mentioned = _mentions(fragment, aliases)
        if mentioned:
            polarity = _polarity(fragment)
            policy = _split_policy(fragment, polarity)
            sequential = bool(_SEQUENCE_RE.search(fragment))
            batch = "第一批" if sequential else None
            items: list[ItemQuantity] = []
            for position, word, sku in mentioned:
                quantity, source = _quantity_for(fragment, word, position, len(mentioned), aliases)
                if quantity is None:
                    quantity = inherited_map.get(sku, 1.0)
                    source = "inherited"
                items.append(ItemQuantity(sku, quantity, source))
            _sequence_batches(fragment, items)
            batch = items[0].batch if items else None
            groups.append(
                ItemGroup(
                    items,
                    split_allowed=policy if policy is not None else (True if sequential else None),
                    text=fragment,
                    polarity=polarity,
                    batch=batch,
                )
            )
        else:
            # “成套周六来取”这类省略物品的备选：沿用上游的成对物品。
            policy = _split_policy(fragment, "neutral")
            chosen = [(sku, qty) for sku, qty in inherited_map.items()]
            if re.search(r"成套|成对|整套|两种|两样", fragment) and len(chosen) > 2:
                chosen = chosen[:2]
            groups.append(
                ItemGroup(
                    [ItemQuantity(sku, qty, "inherited") for sku, qty in chosen],
                    split_allowed=policy if policy is not None else False,
                    text=fragment,
                    polarity=_polarity(fragment),
                )
            )
        day = None
        weekday = _weekday_deadline(fragment, sent)
        if weekday:
            day = weekday[0].date()
        options.append(Alternative(label=label, text=fragment, groups=groups, day=day))
    return options


def _selected_alternative(text: str) -> str | None:
    patterns = [
        r"(?:确认|选定|选择|选|就要|要|按|定)\s*备选\s*([一二两1-9])",
        r"备选\s*([一二两1-9])\s*(?:吧|了|的)?",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            prefix = text[max(0, match.start() - 4) : match.start()]
            if "之一" in text[match.start() : match.end() + 2]:
                continue
            if pattern.endswith(r"(?:吧|了|的)?") and not re.search(r"确认|选|就|按|定|要", prefix):
                continue
            raw = match.group(1)
            return {"一": "1", "二": "2", "两": "2"}.get(raw, raw)
    return None


def read_message(message: Message, packet: Packet, resolver: Resolver) -> Reading:
    aliases = _aliases(packet)
    notes: list[str] = []
    missing: list[str] = []
    references: list[tuple[str, str, str]] = []

    for ref in message.references:
        if ref in packet.messages:
            references.append((ref, ref, "本条直接回复或转述"))
        else:
            missing.append(ref)

    prior = resolver.prior_for(message)
    prior_reading = resolver.readings.get(prior.id) if prior else None
    offer_root = resolver.offer_root(message)
    root_reading = resolver.readings.get(offer_root.id) if offer_root else None

    # 前文的物品数量，供省略句和“两种/各一箱”继承。优先紧邻的前文，
    # 找不到实物时再沿转述链回到最初的捐出方原话。
    inherited_map: dict[str, float] = {}
    for source_reading in (prior_reading, root_reading):
        if not source_reading:
            continue
        if inherited_map and source_reading is not prior_reading:
            break
        # “一起／一并／那批”回指成套的那组，不把同一条消息里另说的
        # 单送物品也算进来。
        groups = source_reading.groups
        if re.search(r"一起|一并|成套|成对|整套|那批|这批", message.text):
            joint_groups = [group for group in groups if group.split_allowed is False]
            if joint_groups:
                groups = joint_groups
        for group in groups:
            for item in group.items:
                inherited_map.setdefault(item.sku, item.cartons)
    inherited_deadline = prior_reading.deadline if prior_reading else (root_reading.deadline if root_reading else None)

    alternatives = _parse_alternatives(message.text, message.sent, packet, aliases, inherited_map)
    selected = _selected_alternative(message.text)
    chosen_option: Alternative | None = None
    if selected:
        pool: list[Alternative] = []
        for source_reading in (prior_reading, root_reading):
            if source_reading and source_reading.alternatives:
                pool = source_reading.alternatives
                references.append((prior.id or source_reading.message.id, source_reading.message.id, "被选择的备选"))
                break
        chosen_option = next((alt for alt in pool if alt.label == selected), None)
        if chosen_option:
            inherited_map = {item.sku: item.cartons for item in chosen_option.items}
            notes.append(f"选择前文备选{selected}：{'、'.join(item.sku for item in chosen_option.items)}")

    groups: list[ItemGroup] = []
    message_target: str | None = None
    message_location: str | None = None
    message_deadline: Deadline | None = None
    message_fallback: str | None = None
    explicit_mention = bool(_mentions(message.text, aliases))

    for clauses in _sentence_parts(message.text):
        sentence = "，".join(clauses)
        target = _target(sentence, packet)
        fallback = _fallback(sentence, packet)
        warehouse = _warehouse(sentence)
        if target:
            message_target = target
        if fallback:
            message_fallback = fallback
        if warehouse:
            message_location = warehouse

        # 期限只属于写明“到某时无人确认便改给”的那个分句；同一句里
        # 前面列出的物品才受它约束，下一句“备用护腕可单送”不受影响。
        sentence_deadline: Deadline | None = None

        sentence_target = target
        sentence_start = len(groups)
        for clause in clauses:
            # 条件从句只提供条件，不单独构成承诺；但“若……我们周六提走”
            # 的条件句本身也是附条件的承诺，等条件解析后再补物品。
            conditional_dead = (
                _CONDITION_RE.search(clause)
                and not re.search(r"我们|本人|我方|到时|届时", clause)
            )
            if conditional_dead:
                continue

            mentioned = _stem_words(packet, clause, aliases)
            polarity = _polarity(clause)
            policy = _split_policy(clause, polarity)
            if chosen_option and polarity == "neutral":
                polarity = "accept"
            # 同一句拆答不同物品时，接受方按分句各自落地；分句没点名
            # 就沿用同一句里最近一次点名的对象。
            clause_target = _clause_target(clause, packet) or sentence_target or target
            if clause_target and clause_target != sentence_target:
                sentence_target = clause_target
            if _DEADLINE_CUE_RE.search(clause) or _NO_ACCEPT_CUE_RE.search(clause):
                candidate = parse_deadline(
                    clause, message.sent, inherited_deadline, require_cue=True
                )
                if candidate and sentence_deadline is None:
                    sentence_deadline = candidate
                    # “前两箱给岭东；周五前无人确认便改给盐田”的后半句
                    # 约束同一句里在它之前列出的物品。
                    for existing in groups[sentence_start:]:
                        if existing.deadline is None:
                            existing.deadline = candidate
                            existing.fallback = existing.fallback or message_fallback
                    if message_deadline is None:
                        message_deadline = candidate
            group_deadline = sentence_deadline if mentioned else None
            if mentioned:
                for position, word, sku in mentioned:
                    quantity, source = _quantity_for(clause, word, position, len(mentioned), aliases)
                    if quantity is None:
                        quantity = inherited_map.get(sku)
                        source = "inherited"
                    if quantity is None:
                        quantity = 1.0
                        source = "implied"
                        notes.append(f"{word} 未写箱数，也未找到前文箱数，按一箱记并待澄清")
                    groups.append(
                        ItemGroup(
                            [ItemQuantity(sku, quantity, source)],
                            split_allowed=policy,
                            target=clause_target,
                            text=clause,
                            polarity=polarity,
                            deadline=group_deadline,
                            fallback=fallback,
                        )
                    )
                continue
            # 没有物品名，但用“两种／各一箱／照上次”回指前文。
            if re.search(
                r"两种|两样|各一箱|各\s*[一二两\d]+\s*箱|都|同样|照上次|就按上次|"
                r"一起|一并|那批|这批|照旧|全要|全收",
                clause,
            ) and inherited_map:
                # 同一句别处已写明物品、且这里是“就照上次……改成……”时，
                # 回指只是引子，以改后的具体物品为准。
                if explicit_mention and re.search(r"照上次|就按上次", clause):
                    continue
                items = [ItemQuantity(sku, qty, "inherited") for sku, qty in inherited_map.items()]
                groups.append(
                    ItemGroup(
                        items,
                        split_allowed=policy,
                        target=clause_target,
                        text=clause,
                        polarity=polarity,
                        deadline=group_deadline,
                        fallback=fallback,
                    )
                )

    if not groups and chosen_option:
        for option_group in chosen_option.groups:
            groups.append(
                ItemGroup(
                    [ItemQuantity(item.sku, item.cartons, "selected", item.batch) for item in option_group.items],
                    split_allowed=option_group.split_allowed,
                    target=option_group.target or message_target,
                    text=option_group.text,
                    polarity="accept",
                    batch=option_group.batch,
                )
            )

    # 选择备选但其分句没有写明可否拆分时，沿用被选备选的原话态度。
    if chosen_option:
        for group in groups:
            if group.split_allowed is None:
                group.split_allowed = chosen_option.split_allowed

    if not groups and alternatives:
        # 本条只是转述备选，不自行承诺物品。
        pass

    # 条件按分句提取，期限只认本条明写的时限语气；没写时间时沿用上游。
    conditions: list[Condition] = []
    for clauses in _sentence_parts(message.text):
        for clause in clauses:
            clause_deadline = parse_deadline(clause, message.sent, inherited_deadline, require_cue=True)
            if _SIGNED_RE.search(clause) and (_CONDITION_RE.search(clause) or _NO_ACCEPT_RE.search(clause)):
                item = next((sku for _, _, sku in _mentions(clause, aliases)), None)
                conditions.append(Condition("item_signed", clause, item=item))
            elif _NO_ACCEPT_RE.search(clause):
                party = next((name for name in packet.parties if name in clause), None)
                effective = clause_deadline or message_deadline or inherited_deadline
                conditions.append(
                    Condition(
                        "no_acceptance_by_deadline",
                        clause,
                        party=party,
                        deadline=effective.at if effective else None,
                    )
                )
                # 期限属于同句里被点名的物品（“前两箱给岭东……到了便改给
                # 盐田”），在分句循环结束后统一挂到该句的物品组上。
                pass
            elif _CONDITION_RE.search(clause):
                conditions.append(Condition("custom", clause, deadline=None))

    request = None
    if _REQUEST_RE.search(message.text):
        match = re.search(
            r"(请[^。！？]{0,30}|能否[^。！？]{0,20}|[^。！？]*(?:哪个|哪一种|选哪)[^。！？]*)",
            message.text,
        )
        request = match.group(0).strip("，。；; ") if match else "请求确认"

    is_relay = bool(message.forwards) or bool(_QUOTE_RE.search(message.text))
    withdrawal = bool(_WITHDRAW_RE.search(message.text))
    # “请不要单独派来”拒绝的是拆开单送；如果拆不开就没法交割，按拒绝该
    # 物品登记，并保留“不可拆分”的原话态度。
    refusal = bool(_REFUSE_RE.search(message.text))
    reduction = bool(re.search(r"少发|只送|只给|减为|减到|先来|先送", message.text))

    polarities = {group.polarity for group in groups}
    has_condition = bool(conditions)
    if withdrawal:
        kind = "withdraw"
    elif refusal and has_condition:
        kind = "conditional_withdrawal"
    elif refusal:
        kind = "refusal"
    elif alternatives and request:
        kind = "relay_request"
    elif request and not (selected and "accept" in polarities):
        kind = "request_confirmation"
    elif "counter" in polarities or reduction:
        kind = "counter"
    elif selected and "accept" in polarities:
        kind = "accept"
    elif has_condition and ("accept" in polarities or _COMMIT_RE.search(message.text)):
        kind = "conditional_accept"
    elif "accept" in polarities:
        kind = "accept"
    elif message.sender in packet.donors or _OFFER_RE.search(message.text):
        kind = "offer"
    else:
        kind = "info"
    if alternatives and request:
        kind = "relay_request"
    if is_relay and kind in {"offer", "accept"} and not selected:
        kind = "relay"

    binding = not (is_relay and kind in {"relay", "relay_request"})
    if missing:
        binding = False

    if alternatives:
        unresolved = [alt for alt in alternatives if not alt.items]
        if unresolved:
            notes.append("备选内容未能全部落到具体物品，需补上游说明")

    reading = Reading(
        message=message,
        kind=kind,
        is_relay=is_relay,
        binding=binding,
        groups=groups,
        alternatives=alternatives,
        selected_alternative=selected,
        location=message_location,
        target=message_target,
        deadline=message_deadline,
        fallback=message_fallback,
        conditions=conditions,
        references=references,
        missing_refs=sorted(set(missing)),
        notes=notes,
        request=request,
        withdrawal=withdrawal,
        refusal=refusal,
        reduction=reduction,
    )
    resolver.remember(reading)
    return reading


def read_packet(packet: Packet) -> dict[str, Reading]:
    resolver = Resolver(packet)
    readings: dict[str, Reading] = {}
    for message in sorted(packet.messages.values(), key=lambda row: (row.sent, row.id)):
        readings[message.id] = read_message(message, packet, resolver)
    return readings
