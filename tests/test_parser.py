"""中文解析测试：简称落库、引用消解、分句、行为分类、数量与时间。"""

import unittest

from donation.parser import ItemRegister, parse_message, resolve_references, split_clauses
from donation.model import (
    ACT_ACCEPT,
    ACT_CONDITIONAL_ACCEPT,
    ACT_OFFER,
    ACT_REFUSE,
    ACT_REQUEST_CONFIRM,
    ACT_SUBSTITUTE,
)

REGISTER_ROWS = [
    {"id": "G24", "sku": "G24", "description": "护具", "per_carton": 6},
    {"id": "T06", "sku": "T06", "description": "手作工具", "per_carton": 6},
    {"id": "B11", "sku": "B11", "description": "备用护腕", "per_carton": 12},
]


def base(raw_id, text, **over):
    row = {
        "id": raw_id,
        "thread": "t",
        "sender": "捐出方",
        "sent": "2026-10-05T09:00:00+08:00",
        "ingested": "2026-10-05T09:00:00+08:00",
        "text": text,
        "viewers": ["捐出方", "协调员"],
    }
    row.update(over)
    return row


class ParserTest(unittest.TestCase):
    def setUp(self):
        self.register = ItemRegister(REGISTER_ROWS)

    def test_alias_and_carton_conversion(self):
        msg = parse_message(
            base("X1", "G24 和 T06 每件各一箱成对送。备用护腕可单送。"),
            self.register,
        )
        mentions = {m.sku: m for act in msg.acts for m in act.items}
        self.assertEqual(mentions["G24"].qty, 6)
        self.assertEqual(mentions["T06"].qty, 6)
        self.assertEqual(mentions["B11"].qty, 12)
        self.assertTrue(mentions["G24"].paired)
        self.assertTrue(mentions["B11"].splittable)

    def test_short_alias_resolves_to_sku(self):
        # “护腕”是“备用护腕”的末两字简称，应落到 B11
        hits = {sku for sku, _, _ in self.register.find_skus("先送一箱护腕")}
        self.assertEqual(hits, {"B11"})
        # “护具”落到 G24，不与护腕冲突
        hits = {sku for sku, _, _ in self.register.find_skus("护具和工具")}
        self.assertEqual(hits, {"G24", "T06"})

    def test_deadline_parsed_from_weekday(self):
        msg = parse_message(
            base("X2", "周五17时前无人确认合送和收货，便改给盐田。"),
            self.register,
        )
        self.assertEqual(msg.deadline.isoformat(), "2026-10-09T17:00:00+08:00")
        self.assertEqual(msg.offer_conditions[0].kind, "确认截止")

    def test_conditional_prefix_merges_into_main_clause(self):
        clauses = split_clauses("若岭东过了点还未接，我们周六把护具和工具一起提走。")
        self.assertEqual(len(clauses), 1)
        msg = parse_message(
            base("X3", "若岭东过了点还未接，我们周六把护具和工具一起提走。",
                 sender="盐田", sent="2026-10-09T17:05:00+08:00"),
            self.register,
        )
        self.assertEqual(len(msg.acts), 1)
        self.assertEqual(msg.acts[0].kind, ACT_CONDITIONAL_ACCEPT)
        self.assertEqual({m.sku for m in msg.acts[0].items}, {"G24", "T06"})
        self.assertEqual(msg.acts[0].conditions[0].kind, "他方状态")

    def test_refuse_with_condition_classified_as_refuse(self):
        msg = parse_message(
            base("X4", "如果工具已经给对方签收，我先不接受替换，护具也请不要单独派来。",
                 sender="岭东", sent="2026-10-10T15:00:00+08:00"),
            self.register,
        )
        self.assertEqual(msg.acts[0].kind, ACT_REFUSE)
        self.assertIn(ACT_REFUSE, [a.kind for a in msg.acts])

    def test_options_collected_from_full_text(self):
        msg = parse_message(
            base(
                "X5",
                "备选之一是成套周六来取，另一种是先送护具后续送工具，请说选哪个。",
                sender="协调员",
            ),
            self.register,
        )
        self.assertEqual(len(msg.options), 2)
        resolve_references({"X5": msg}, self.register)
        # 成套选项只含成对物资，不把可单送的 B11 卷进来
        self.assertEqual({m.sku for m in msg.options[0].items}, {"G24", "T06"})
        self.assertEqual({m.sku for m in msg.options[1].items}, {"G24", "T06"})

    def test_reference_resolution_for_option_choice(self):
        offer = parse_message(base("O1", "G24 和 T06 每件各一箱成对送。"), self.register)
        ask = parse_message(
            base("A1", "备选之一是成套周六来取，另一种是先送护具后续送工具，请说选哪个。",
                 sender="协调员", reply_to=None),
            self.register,
        )
        choice = parse_message(
            base("C1", "确认备选一，两种各一箱，周六在南仓接。",
                 sender="岭东", reply_to="A1",
                 sent="2026-10-09T16:51:00+08:00"),
            self.register,
        )
        messages = {m.id: m for m in (offer, ask, choice)}
        resolve_references(messages, self.register)
        accept = next(a for a in choice.acts if a.kind == ACT_ACCEPT)
        self.assertEqual(accept.option_index, 1)
        self.assertIn("成套", accept.option_text)
        self.assertEqual({m.sku: m.qty for m in accept.items}, {"G24": 6, "T06": 6})
        self.assertEqual(accept.location, "南仓")
        self.assertEqual(choice.pickup, "2026-10-10周六")
        self.assertEqual(choice.references_missing, [])

    def test_missing_reference_is_recorded(self):
        choice = parse_message(
            base("C2", "确认备选一，两种各一箱。", sender="岭东", reply_to="GHOST"),
            self.register,
        )
        resolve_references({"C2": choice}, self.register)
        self.assertIn("GHOST", choice.references_missing)

    def test_substitute_half_pair_kept_as_substitute(self):
        msg = parse_message(
            base("S1", "就照上次，南仓可收，改成先来一箱护具吧。",
                 sender="吴代收", sent="2026-10-06T11:00:00+08:00"),
            self.register,
        )
        kinds = [a.kind for a in msg.acts]
        self.assertIn(ACT_SUBSTITUTE, kinds)
        sub = next(a for a in msg.acts if a.kind == ACT_SUBSTITUTE)
        self.assertEqual([m.sku for m in sub.items], ["G24"])


if __name__ == "__main__":
    unittest.main()
