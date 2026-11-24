"""中文群消息与转发邮件解析。

设计原则：
- 简称、别名落到 item_register 的具体 sku；消息原文片段始终保留。
- 一句话先按标点拆成小句，再逐小句判定言语行为；同一句不同小句可对
  不同物品/批次形成不同行为（接受一半、拒绝另一半）。
- 解析分两阶段：parse_message 只依赖本句与物品名录；resolve_references
  在全部消息解析完后，把“上次”“备选一”“两种”这类省略/引用落到具体前文。
- 无法机判的前提保留原文并标记，交由协调员人工判断，引擎不替人决定。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

from .model import (
    ACT_ACCEPT,
    ACT_CONDITIONAL_ACCEPT,
    ACT_INSTRUCTION,
    ACT_OFFER,
    ACT_OTHER,
    ACT_REFUSE,
    ACT_REQUEST_CONFIRM,
    ACT_SUBSTITUTE,
    ACT_WITHDRAW,
    ACT_RELAY,
    COND_CUSTOM,
    COND_DEADLINE,
    COND_OTHER_PARTY,
    COND_PROCEDURE,
    Act,
    Condition,
    ItemMention,
    ParsedMessage,
    parse_dt,
)

WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
CARTON_WORDS = ("箱", "件箱")
UNIT_WORDS = ("件", "个", "套")
SPLIT_CLAUSE = re.compile(r"[。；;！!？?]+|，|、")
MERGE_PREFIX = ("便", "就", "否则", "不然", "再")


class ItemRegister:
    """物品名录：sku、全称、简称与箱件换算。"""

    def __init__(self, rows: list[dict]):
        self.rows = {row["sku"]: row for row in rows}
        self.alias: dict[str, str] = {}
        tails: dict[str, list[str]] = {}
        for row in rows:
            sku = row["sku"]
            desc = row["description"]
            self.alias[sku] = sku
            self.alias[desc] = sku
            # 名称末两字作为简称（如 手作工具→工具，备用护腕→护腕），冲突则弃用
            tail = desc[-2:]
            tails.setdefault(tail, []).append(sku)
        for tail, skus in tails.items():
            if len(skus) == 1:
                self.alias[tail] = skus[0]
        # 长别名优先匹配，避免“备用护腕”被“护腕”截断
        self._aliases = sorted(self.alias, key=len, reverse=True)

    def per_carton(self, sku: str) -> int:
        return int(self.rows[sku]["per_carton"])

    def find_skus(self, text: str) -> list[tuple[str, int, int]]:
        """返回文本中出现的 (sku, 起始, 结束)，按位置排序。"""
        hits: list[tuple[str, int, int]] = []
        for alias in self._aliases:
            start = text.find(alias)
            while start != -1:
                end = start + len(alias)
                if not any(h_start <= start and end <= h_end for _, h_start, h_end in hits):
                    hits.append((self.alias[alias], start, end))
                start = text.find(alias, end)
        return sorted(hits, key=lambda item: item[1])


# --------------------------------------------------------------------------
# 数量与物品行
# --------------------------------------------------------------------------

def _quantity_near(text: str, register: ItemRegister, sku: str) -> tuple[int, Optional[float], str]:
    """换算某物品在小句中的数量，返回 (最小单位数, 箱数或None, 原文数量片段)。

    没有明确数量词时返回 0 并标记，由二阶段引用消解从前文继承，
    避免把“先送护具”误当成 1 件。
    """
    per = register.per_carton(sku)
    carton_match = re.search(r"([一二两三四五六七八九十\d]+)\s*箱", text)
    if carton_match:
        cartons = _cn_number(carton_match.group(1))
        return per * cartons, float(cartons), carton_match.group(0)
    unit_match = re.search(r"([一二两三四五六七八九十\d]+)\s*件", text)
    if unit_match:
        units = _cn_number(unit_match.group(1))
        return units, None, unit_match.group(0)
    if "各一箱" in text or "每件各一箱" in text or "一箱" in text:
        return per, 1.0, "一箱"
    # “B11 一箱独立给盐田”这类描述里，独立/单送本身隐含一箱
    if re.search(r"独立|可单送|单独", text):
        return per, 1.0, "一箱"
    return 0, None, ""


def _cn_number(token: str) -> int:
    cn = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if token.isdigit():
        return int(token)
    if token in cn:
        return cn[token]
    if "十" in token:
        left, _, right = token.partition("十")
        tens = cn.get(left, 1) if left else 1
        ones = cn.get(right, 0) if right else 0
        return tens * 10 + ones
    raise ValueError(f"无法解析数量词 {token}")


def extract_mentions(clause: str, register: ItemRegister) -> list[ItemMention]:
    mentions: list[ItemMention] = []
    paired = bool(re.search(r"成对|成套|一起|各一箱|两种|两套", clause))
    splittable = bool(re.search(r"可单送|独立|单独", clause))
    for sku, start, end in register.find_skus(clause):
        qty, cartons, raw = _quantity_near(clause, register, sku)
        mentions.append(
            ItemMention(
                sku=sku,
                qty=qty,
                cartons=cartons,
                raw=raw or clause[start:end],
                paired=paired,
                splittable=splittable,
            )
        )
    return mentions


# --------------------------------------------------------------------------
# 时间与条件
# --------------------------------------------------------------------------

def parse_weekday_deadline(text: str, anchor: datetime) -> Optional[datetime]:
    """解析“周五17时前”这类截止约定，按消息发出日推算具体北京时间。"""
    day = re.search(r"周([一二三四五六日天])", text)
    if not day:
        return None
    target_weekday = WEEKDAYS[day.group(1)]
    delta = (target_weekday - anchor.weekday()) % 7
    target_date = (anchor + timedelta(days=delta)).date()
    clock = re.search(r"(\d{1,2})\s*[时点：:](\d{0,2})", text)
    if clock:
        hour = int(clock.group(1))
        minute = int(clock.group(2) or 0)
    elif re.search(r"17\s*时", text):
        hour, minute = 17, 0
    else:
        hour, minute = 23, 59
    return datetime.combine(target_date, datetime.min.time(), tzinfo=anchor.tzinfo).replace(
        hour=hour, minute=minute
    )


def parse_pickup(text: str, anchor: datetime) -> Optional[str]:
    if not re.search(r"取|接|提走|提货", text):
        return None
    day = re.search(r"周([一二三四五六日天])", text)
    if not day:
        return None
    target_weekday = WEEKDAYS[day.group(1)]
    delta = (target_weekday - anchor.weekday()) % 7
    return (anchor + timedelta(days=delta)).date().isoformat() + f"周{day.group(1)}"

def _party_condition(clause: str) -> Optional[Condition]:
    # 若岭东过了点还未接 / 如果周五还没人回
    match = re.search(
        r"[若如](?:果)?(?P<party>[一-龥]{2,4}?)(?:过了点|过了周[一二三四五六日天]|逾期)?(?P<neg>还未|未|没有|没人)?(?P<verb>接|确认|签收|回|答复)",
        clause,
    )
    if match:
        neg = match.group("neg")
        expected = ("未" + match.group("verb")) if neg else match.group("verb")
        return Condition(
            kind=COND_OTHER_PARTY,
            text=clause,
            party=match.group("party"),
            expected=expected,
            raw=clause,
        )
    # 如果工具已经给对方签收
    match = re.search(r"如果(?P<item>[一-龥A-Za-z0-9]+?)已经给(?P<party>对方|[一-龥]{2,4})签收", clause)
    if match:
        return Condition(
            kind=COND_OTHER_PARTY,
            text=clause,
            party=match.group("party"),
            expected="已签收",
            raw=clause,
        )
    return None


# --------------------------------------------------------------------------
# 小句切分与行为分类
# --------------------------------------------------------------------------

def split_clauses(text: str) -> list[str]:
    pieces = [piece.strip() for piece in SPLIT_CLAUSE.split(text) if piece.strip()]
    merged: list[str] = []
    for piece in pieces:
        if merged and piece.startswith(MERGE_PREFIX):
            merged[-1] = merged[-1] + "，" + piece
        elif piece.startswith(("若", "如果", "假如")) and not re.search(
            r"接受|同意|拒绝|不要|提走|接货|收货", piece
        ):
            # 纯条件前缀（“若岭东过了点还未接”）并入随后的主句
            merged.append(piece)
        elif merged and merged[-1].startswith(("若", "如果", "假如")) and not re.search(
            r"接受|同意|拒绝|不要|提走|接货|收货", merged[-1]
        ):
            merged[-1] = merged[-1] + "，" + piece
        else:
            merged.append(piece)
    return merged


def _classify(clause: str, mentions: list[ItemMention], sender: str = "") -> str:
    # “确认备选一”是做出选择，而不是请人确认；先于请求确认规则判断
    if re.search(r"确认备选|备选[一二两12]|就照上次", clause) and re.search(r"确认|就照", clause):
        return ACT_ACCEPT
    if re.search(r"撤回|取消(?!签收)|我不要了", clause):
        return ACT_WITHDRAW
    if re.search(r"不要|不接受|拒绝|不能单独|先不接受|暂不接受|以后再说|先不|算了", clause):
        return ACT_REFUSE
    if re.search(r"补签|补办|手续|追认", clause):
        return ACT_INSTRUCTION
    if re.search(r"改成?|换成|替代|先送.{0,8}后续|只送|先来.{0,6}吧|拆", clause):
        return ACT_SUBSTITUTE
    if re.search(r"转述|转达|他们说|有人表示|意见是", clause):
        return ACT_RELAY
    if re.search(r"备选|请说选|选哪个|请选择|请确认|哪种|哪个方案", clause):
        return ACT_REQUEST_CONFIRM
    if clause.startswith(("若", "如果", "假如")) or re.search(r"前提是|条件是", clause):
        if re.search(r"不接受|不要|拒绝|不能|请不要", clause):
            return ACT_REFUSE
        return ACT_CONDITIONAL_ACCEPT if mentions else ACT_OTHER
    if re.search(r"确认|就照上次|可收|来取|提走|接货|收货|我们接|我接|同意|答应|我们要了|我要了|要了", clause):
        return ACT_ACCEPT
    if mentions and re.search(r"送|给|提供|备|自取|来自取|可领", clause):
        return ACT_OFFER
    # 捐出方陈述里“某物品 + 数量”本身即构成要约行；接收方的同类小句留给归并处理
    if sender == "捐出方" and mentions and re.search(r"箱|件|套|个", clause):
        return ACT_OFFER
    return ACT_OTHER


def _location_of(clause: str) -> Optional[str]:
    # “在南仓接/收”取“在”之后的仓/站；否则取末尾带 仓/站 的专名。
    # “过了点”是时间用语，不当作地点。
    match = re.search(r"在\s*([一-龥]{1,4}?[仓站])", clause)
    if match:
        return match.group(1)
    match = re.search(r"([一-龥]{2,4}?[仓站])(?![一-龥])", clause)
    return match.group(1) if match else None


def _extract_options(text: str, register: ItemRegister) -> list[Act]:
    """从整封来信里抽出“备选之一是…另一种是…”的各备选说法。"""
    options: list[Act] = []
    pattern = re.compile(r"(?:备选(?:之)?[一二12]是|另一种是)")
    matches = list(pattern.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        opt_text = text[match.start():end].rstrip("，。；; ")
        mentions = extract_mentions(opt_text, register)
        options.append(
            Act(
                kind=ACT_OFFER,
                items=mentions,
                text=opt_text,
                location=_location_of(opt_text),
                option_index=index + 1,
                inherit_items=not bool(mentions),
            )
        )
    return options


def parse_message(raw: dict, register: ItemRegister) -> ParsedMessage:
    sent = parse_dt(raw["sent"])
    full_text = raw["text"]
    clauses = split_clauses(full_text)
    acts: list[Act] = []
    options: list[Act] = _extract_options(full_text, register)
    deadline: Optional[datetime] = None
    offer_conditions: list[Condition] = []

    for clause in clauses:
        mentions = extract_mentions(clause, register)
        is_option_clause = bool(re.search(r"备选(?:之)?[一二12]是|另一种是", clause))
        if is_option_clause:
            kind = ACT_REQUEST_CONFIRM
        else:
            kind = _classify(clause, mentions, raw.get("sender", ""))

        # 截止约定：必须是“周X时点前无人/没人确认……便改给”的改派句式，
        # 不能把“若他方过了点还未接”这类普通条件小句当成截止约定吞掉
        clause_deadline = parse_weekday_deadline(clause, sent)
        is_deadline_clause = (
            clause_deadline is not None
            and re.search(r"(无人|没人).{0,12}(确认|回|答复)", clause)
            and re.search(r"改给|便给|就给", clause)
        )
        if is_deadline_clause:
            deadline = clause_deadline
            fallback = re.search(r"改给\s*([一-龥]{2,4})", clause)
            offer_conditions.append(
                Condition(
                    kind=COND_DEADLINE,
                    text=clause,
                    deadline=clause_deadline,
                    party=fallback.group(1) if fallback else None,
                    expected="无人确认",
                    raw=clause,
                )
            )
            continue

        party_cond = _party_condition(clause)
        conditions: list[Condition] = []
        if party_cond is not None:
            conditions.append(party_cond)
        if re.search(r"补签|补办", clause):
            conditions.append(Condition(kind=COND_PROCEDURE, text=clause, raw=clause))

        act = Act(
            kind=kind,
            items=mentions,
            text=clause,
            location=_location_of(clause),
            conditions=conditions,
            relayed_from=(raw.get("forwards") or [None])[0] if kind == ACT_RELAY else None,
        )

        # 备选一/备选之二：记录选项序号
        option_match = re.search(r"备选(?:之)?([一二两12])", clause)
        if option_match:
            token = option_match.group(1)
            act.option_index = {"一": 1, "二": 2, "两": 2, "1": 1, "2": 2}[token]
            act.option_text = clause
        acts.append(act)

    parsed = ParsedMessage(
        id=raw["id"],
        thread=raw["thread"],
        sender=raw["sender"],
        sent=sent,
        ingested=parse_dt(raw["ingested"]),
        text=raw["text"],
        reply_to=raw.get("reply_to"),
        forwards=list(raw.get("forwards") or []),
        viewers=list(raw.get("viewers") or []),
        acts=acts,
        options=options,
        deadline=deadline,
        offer_conditions=offer_conditions,
        pickup=parse_pickup(raw["text"], sent),
    )
    return parsed


# --------------------------------------------------------------------------
# 二阶段：引用与省略消解
# --------------------------------------------------------------------------

def resolve_references(messages: dict[str, ParsedMessage], register: ItemRegister) -> None:
    """把简称之外的省略落到具体前文，缺失引用显式登记。"""
    for msg in messages.values():
        _merge_intent_clauses(msg)
    for msg in messages.values():
        # 先处理协调员备选项里无品名的“成套”选项，从其转发的上游要约补物品
        own_mentions = [m for act in msg.acts for m in act.items]
        for option in msg.options:
            if option.inherit_items:
                source = _follow_chain(msg, messages)
                pool = source.item_mentions if source is not None else own_mentions
                inherited = [
                    _one_carton(m, register)
                    for m in pool
                    if _option_allows(option.text, m, register)
                ]
                if inherited:
                    option.items = inherited
                    option.inherit_items = False
        for act in msg.acts:
            # “两种各一箱”“两套”这类无品名小句：继承前文物品。
            # 若同一条消息里已有显式选择（确认备选一）并带物品，不再重复继承。
            explicit = any(
                other is not act and other.option_index is not None and other.items
                for other in msg.acts
            )
            if not act.items and not explicit and re.search(
                r"两种|两套|各一箱|成对|成套", act.text
            ):
                source = _follow_chain(msg, messages)
                if source is not None and source.item_mentions:
                    act.items = [
                        _one_carton(m, register)
                        for m in source.item_mentions
                        if _option_allows(act.text, m, register)
                    ]
                else:
                    _mark_missing(msg, msg.reply_to or "<前文>")
            # 确认备选一：把选项原文挂到行为上，便于核对“答应的是哪一版”
            if act.option_index is not None and act.kind in (ACT_ACCEPT, ACT_SUBSTITUTE):
                target = _direct_target(msg, messages)
                if target is not None and target.options:
                    idx = act.option_index - 1
                    if 0 <= idx < len(target.options):
                        chosen = target.options[idx]
                        act.option_text = chosen.text
                        if not act.items and chosen.items:
                            act.items = [_one_carton(m, register) for m in chosen.items]
                elif target is not None:
                    # 回复目标存在但没给出备选说法，属于表述差异而非来信缺失
                    continue
                else:
                    _mark_missing(msg, msg.reply_to or "<备选前文>")
            # “就照上次”：绑定到回复目标里的备选说法
            if "就照上次" in act.text or "照上次" in act.text:
                target = _direct_target(msg, messages)
                if target is None:
                    _mark_missing(msg, msg.reply_to or "<前文>")
                else:
                    act.relayed_from = target.id
            # 数量缺失（“先送护具”没给箱数）或仅笼统提及：按上游要约数量补齐
            if act.kind in (ACT_ACCEPT, ACT_CONDITIONAL_ACCEPT, ACT_SUBSTITUTE) and act.items:
                source = _follow_chain(msg, messages)
                if source is not None:
                    offered = {m.sku: m for m in source.item_mentions}
                    for mention in act.items:
                        if mention.qty == 0 and mention.sku in offered:
                            mention.qty = offered[mention.sku].qty
                            mention.cartons = offered[mention.sku].cartons
                        elif mention.qty <= 1 and mention.sku in offered:
                            mention.qty = offered[mention.sku].qty
                            mention.cartons = offered[mention.sku].cartons
            # 接受/替代行为没带地点时，用同条消息其他小句里的地点补齐
            if act.kind in (ACT_ACCEPT, ACT_CONDITIONAL_ACCEPT, ACT_SUBSTITUTE) and not act.location:
                for other in msg.acts:
                    if other is not act and other.location:
                        act.location = other.location
                        break


def _option_allows(option_text: str, mention: ItemMention, register: ItemRegister) -> bool:
    """备选项继承上游物品时，排除选项原话根本没涉及的物品。"""
    if mention.sku in option_text:
        return True
    # 用名录里的全称/简称再判一次，避免“护具”这类简称匹配不到 sku
    desc = register.rows.get(mention.sku, {}).get("description", "")
    if desc and desc in option_text:
        return True
    if desc and desc[-2:] in option_text:
        return True
    # “成套/两种/各一/一起”只覆盖成对物资，不能把声明可单送的独立物品卷进来
    if not mention.splittable and any(word in option_text for word in
                                      ("成套", "两种", "各一", "一起")):
        return True
    return False


def _merge_intent_clauses(msg: ParsedMessage) -> None:
    """合并同一条消息里被标点拆散的“承诺动词 + 物品/地点”小句。

    例如“确认，G24 和 T06 两种各一箱，周六在南仓接。”被切成三个小句，
    言语行为是同一个接受，需在进入规则判定前合并，否则物品小句会落为“其他”。
    规则：找到无物品的承诺小句后，把同消息内紧随的物品/地点小句吸收进来。
    """
    intent_kinds = (ACT_ACCEPT, ACT_CONDITIONAL_ACCEPT, ACT_REFUSE, ACT_SUBSTITUTE)
    acts = msg.acts
    for pos, act in enumerate(acts):
        if act.kind not in intent_kinds:
            continue
        for nxt in acts[pos + 1:]:
            if nxt.kind in intent_kinds:
                break
            if nxt.kind != ACT_OTHER:
                continue
            # 已通过“确认备选一”等显式选择带了物品的，不再吸收其他带物品小句，
            # 避免把独立可单送的物资错误并入成套承诺；无物品的结构小句仍吸收
            if nxt.items:
                if act.items:
                    continue
                act.items = list(nxt.items)
                nxt.items = []
            if nxt.location and not act.location:
                act.location = nxt.location
            if nxt.conditions:
                act.conditions.extend(nxt.conditions)
            act.text = act.text + "，" + nxt.text
            nxt._consumed = True  # type: ignore[attr-defined]
    msg.acts = [act for act in acts if not getattr(act, "_consumed", False)]


def _one_carton(mention: ItemMention, register: ItemRegister) -> ItemMention:
    return ItemMention(
        sku=mention.sku,
        qty=register.per_carton(mention.sku),
        cartons=1.0,
        raw="各一箱",
        paired=True,
        splittable=False,
    )


def _mark_missing(msg: ParsedMessage, ref: str) -> None:
    if ref not in msg.references_missing:
        msg.references_missing.append(ref)


def _direct_target(msg: ParsedMessage, messages: dict[str, ParsedMessage]) -> Optional[ParsedMessage]:
    ref = msg.reply_to or (msg.forwards[0] if msg.forwards else None)
    if ref is None:
        return None
    target = messages.get(ref)
    if target is None:
        _mark_missing(msg, ref)
    return target


def _follow_chain(msg: ParsedMessage, messages: dict[str, ParsedMessage]) -> Optional[ParsedMessage]:
    """沿回复/转发链找到携带物品的最早前文（通常是捐出方要约）。"""
    visited: set[str] = set()
    current = msg
    while True:
        ref = current.reply_to or (current.forwards[0] if current.forwards else None)
        if ref is None:
            return current if current.item_mentions else None
        if ref in visited:
            return None
        visited.add(ref)
        nxt = messages.get(ref)
        if nxt is None:
            _mark_missing(msg, ref)
            return None
        current = nxt
