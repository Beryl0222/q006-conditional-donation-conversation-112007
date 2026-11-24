"""引擎使用的结构化类型与状态常量。"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Optional


# ---- 言语行为类别 ---------------------------------------------------------

ACT_OFFER = "要约"                 # 捐出方提出赠送及条件
ACT_REQUEST_CONFIRM = "请人确认"   # 协调员要求对方在备选间选择/确认
ACT_ACCEPT = "接受"                # 无条件或带满足前提的接受
ACT_CONDITIONAL_ACCEPT = "附条件接受"
ACT_SUBSTITUTE = "提出替代"        # 只同意一半、换方案等替代主张
ACT_RELAY = "转述"                 # 转述别人的意见
ACT_WITHDRAW = "撤回"
ACT_REFUSE = "拒绝"
ACT_INSTRUCTION = "手续说明"       # 补签等程序性要求
ACT_OTHER = "其他"

ALL_ACTS = {
    ACT_OFFER,
    ACT_REQUEST_CONFIRM,
    ACT_ACCEPT,
    ACT_CONDITIONAL_ACCEPT,
    ACT_SUBSTITUTE,
    ACT_RELAY,
    ACT_WITHDRAW,
    ACT_REFUSE,
    ACT_INSTRUCTION,
    ACT_OTHER,
}

# ---- 条件类型 -------------------------------------------------------------

COND_DEADLINE = "确认截止"         # 某时点前无人确认则改派
COND_OTHER_PARTY = "他方状态"      # 若某点逾期未接/未签
COND_PROCEDURE = "手续"            # 来电人补签等
COND_CUSTOM = "自定义前提"

# ---- 交接单与承诺状态 -----------------------------------------------------

STATE_EFFECTIVE = "有效"
STATE_PENDING = "待确认"
STATE_BLOCKED = "无法结论"
STATE_DISPUTED = "争议冻结"
STATE_WITHDRAWN = "已撤回"

TURNOVER_VALID = "复核有效"
TURNOVER_PENDING = "待确认"
TURNOVER_REJECTED = "复核不成立"
TURNOVER_DISPUTED = "争议冻结"


def parse_dt(value: str) -> datetime:
    """解析带时区的北京时间字符串。"""
    return datetime.fromisoformat(value)


def to_plain(obj: Any) -> Any:
    """dataclass/列表/字典递归转成可 JSON 序列化的普通结构。"""
    if hasattr(obj, "__dataclass_fields__"):
        return {key: to_plain(val) for key, val in asdict(obj).items()}
    if isinstance(obj, dict):
        return {key: to_plain(val) for key, val in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(item) for item in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


@dataclass
class ItemMention:
    """消息里提到的一个物品行，数量按 item_register 换算成最小计数单位。"""

    sku: str
    qty: int                       # 最小单位数量（per_carton 换算后）
    cartons: Optional[float] = None  # 原文若按箱表达，保留箱数
    raw: str = ""                  # 原文片段
    paired: bool = False           # 要约是否要求与他物成对
    splittable: bool = True        # 原话是否允许拆分配套

    def to_dict(self) -> dict:
        return to_plain(self)


@dataclass
class Condition:
    """承诺/要约上的条件。

    antecedent 为可机判结构时给出 kind 与参数；无法机判时保留原文，
    由协调员人工判定，引擎不替人决定。
    """

    kind: str
    text: str
    deadline: Optional[datetime] = None
    party: Optional[str] = None
    expected: Optional[str] = None   # 期望状态，如 "未确认"、"已签收"
    raw: str = ""

    def to_dict(self) -> dict:
        return to_plain(self)


@dataclass
class Act:
    """一句话拆出的一个言语行为（一句话可对不同物品/批次分开答复）。"""

    kind: str
    items: list[ItemMention] = field(default_factory=list)
    text: str = ""
    option_index: Optional[int] = None     # 选中的备选序号，从 1 开始
    option_text: Optional[str] = None
    location: Optional[str] = None
    conditions: list[Condition] = field(default_factory=list)
    relayed_from: Optional[str] = None     # 转述来源消息 id
    withdrawn_target: Optional[str] = None
    inherit_items: bool = False            # 选项/小句无品名，需从前文继承物品

    def to_dict(self) -> dict:
        return to_plain(self)


@dataclass
class ParsedMessage:
    """一条原始消息解析后的结构。"""

    id: str
    thread: str
    sender: str
    sent: datetime
    ingested: datetime
    text: str
    reply_to: Optional[str]
    forwards: list[str]
    viewers: list[str]
    acts: list[Act] = field(default_factory=list)
    options: list[Act] = field(default_factory=list)   # 协调员给出的备选说法
    deadline: Optional[datetime] = None
    offer_conditions: list[Condition] = field(default_factory=list)
    pickup: Optional[str] = None                        # 周六取货等取货日提示
    references_missing: list[str] = field(default_factory=list)

    @property
    def item_mentions(self) -> list[ItemMention]:
        mentions: list[ItemMention] = []
        for act in self.acts:
            mentions.extend(act.items)
        return mentions

    def to_dict(self) -> dict:
        return to_plain(self)


@dataclass
class OfferLine:
    """要约中一个物品行。"""

    sku: str
    qty: int
    paired: bool
    splittable: bool
    raw: str = ""

    def to_dict(self) -> dict:
        return to_plain(self)


@dataclass
class Offer:
    """捐出方发出的一组可交割物资要约（按 thread 归集）。"""

    thread: str
    source_id: str
    sender: str
    lines: list[OfferLine]
    primary_party: Optional[str]
    fallback_party: Optional[str]
    deadline: Optional[datetime]
    conditions: list[Condition] = field(default_factory=list)
    pickup: Optional[str] = None

    @property
    def sku_set(self) -> set[str]:
        return {line.sku for line in self.lines}

    def to_dict(self) -> dict:
        return to_plain(self)


@dataclass
class Statement:
    """某一方在一条消息里明确表达过的说法，供“每方曾确认的说法”查询。"""

    party: str
    message_id: str
    sent: datetime
    act_kind: str
    summary: str
    valid: Optional[bool] = None       # 经规则判定是否有效；None 表示非承诺性说法
    invalid_reason: Optional[str] = None
    location: Optional[str] = None

    def to_dict(self) -> dict:
        return to_plain(self)


@dataclass
class Commitment:
    """一要约组上形成的有效承诺或待确认状态。"""

    offer_source: str
    thread: str
    receiver: Optional[str]
    items: dict[str, int]
    state: str
    basis: list[str]                  # 支撑消息 id（按权限过滤前的全集）
    conditions: list[Condition] = field(default_factory=list)
    unmet: list[str] = field(default_factory=list)
    location: Optional[str] = None
    pickup: Optional[str] = None
    note: str = ""

    def to_dict(self) -> dict:
        return to_plain(self)


@dataclass
class TurnoverReview:
    """交接单复核结果。"""

    turnover_id: str
    revision: str
    state: str
    receiver: str
    items: dict[str, int]
    basis: list[str]
    frozen_lines: dict[str, int] = field(default_factory=dict)
    received_lines: dict[str, int] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return to_plain(self)


@dataclass
class Question:
    """待回问题 / 需人工或捐出方澄清的事项。"""

    code: str
    topic: str
    to_party: Optional[str]
    text: str
    related: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return to_plain(self)
