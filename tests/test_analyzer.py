"""分析器规则测试：授权边界、配套拆分、发送顺序、争议冻结、缺失上游。"""

import copy
import unittest

from donation.analyzer import Analyzer
from donation.model import STATE_BLOCKED, STATE_EFFECTIVE, STATE_PENDING, TURNOVER_DISPUTED

ITEMS = [
    {"id": "G24", "sku": "G24", "description": "护具", "per_carton": 6},
    {"id": "T06", "sku": "T06", "description": "手作工具", "per_carton": 6},
    {"id": "B11", "sku": "B11", "description": "备用护腕", "per_carton": 12},
]


def msg(mid, sender, text, sent, *, thread="t", reply_to=None, forwards=None,
        viewers=None, ingested=None):
    return {
        "id": mid,
        "thread": thread,
        "sender": sender,
        "sent": sent,
        "ingested": ingested or sent,
        "text": text,
        "reply_to": reply_to,
        "forwards": forwards or [],
        "viewers": viewers or ["捐出方", "协调员", "岭东", "盐田", "吴代收"],
    }


def packet(inbox, grants=None, turnovers=None, receipts=None):
    return {
        "item_register": copy.deepcopy(ITEMS),
        "inbox": inbox,
        "grant": grants or [],
        "turnover": turnovers or [],
        "receipt": receipts or [],
    }


PAIR_OFFER = msg(
    "M1", "捐出方",
    "G24 和 T06 每件各一箱成对送，前两箱给岭东；周五17时前无人确认合送和收货，便改给盐田。",
    "2026-10-05T09:00:00+08:00", thread="主约",
)


class AnalyzerTest(unittest.TestCase):
    def test_timely_full_accept_is_effective(self):
        inbox = [
            PAIR_OFFER,
            msg("M2", "岭东", "确认，G24 和 T06 两种各一箱，周六在南仓接。",
                "2026-10-09T16:00:00+08:00", reply_to="M1"),
        ]
        ev = Analyzer(packet(inbox)).run()
        commit = next(c for c in ev.commitments if c.state == STATE_EFFECTIVE)
        self.assertEqual(commit.receiver, "岭东")
        self.assertEqual(commit.items, {"G24": 6, "T06": 6})
        self.assertEqual(commit.location, "南仓")

    def test_late_accept_when_deadline_not_triggered_is_invalid(self):
        inbox = [
            PAIR_OFFER,
            msg("M2", "岭东", "确认，两种各一箱。",
                "2026-10-09T16:51:00+08:00", reply_to="M1"),
            msg("M3", "盐田", "若岭东过了点还未接，我们周六把护具和工具一起提走。",
                "2026-10-09T17:05:00+08:00", reply_to="M1"),
        ]
        ev = Analyzer(packet(inbox)).run()
        effective = [c for c in ev.commitments if c.state == STATE_EFFECTIVE]
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective[0].receiver, "岭东")
        yantian = next(s for s in ev.statements if s.message_id == "M3" and s.valid is False)
        self.assertIn("截止", yantian.invalid_reason)

    def test_backfilled_early_reply_reopens_decision_order(self):
        # 单据先开给盐田，岭东更早发出的答复之后才补录进来
        inbox = [
            PAIR_OFFER,
            msg("M3", "盐田", "若岭东过了点还未接，我们周六把护具和工具一起提走。",
                "2026-10-09T17:05:00+08:00", reply_to="M1",
                ingested="2026-10-09T17:05:00+08:00"),
            msg("M5", "岭东", "确认，两种各一箱，周六在南仓接。",
                "2026-10-09T16:51:00+08:00", reply_to="M1",
                ingested="2026-10-10T14:00:00+08:00"),
        ]
        turnovers = [{
            "id": "H1", "revision": "H-67/1", "source": ["M1", "M3"],
            "state": "双方已确认", "receiver": "盐田",
            "items": {"G24": 6, "T06": 6},
            "shown_to": ["捐出方", "协调员", "盐田"],
            "time": "2026-10-09T17:30:00+08:00",
        }]
        receipts = [{
            "id": "R1", "turnover": "H1", "receiver": "盐田",
            "items": {"T06": 6}, "signed_at": "2026-10-10T12:00:00+08:00",
        }]
        ev = Analyzer(packet(inbox, turnovers=turnovers, receipts=receipts)).run()
        effective = next(c for c in ev.commitments if c.state == STATE_EFFECTIVE)
        self.assertEqual(effective.receiver, "岭东")
        review = ev.turnover_reviews[0]
        self.assertEqual(review.state, TURNOVER_DISPUTED)
        self.assertEqual(review.frozen_lines, {"G24": 6})
        self.assertEqual(review.received_lines, {"T06": 6})
        self.assertIn("M5", review.conflicts)

    def test_agent_with_location_only_scope_cannot_accept_or_reduce(self):
        grants = [{
            "id": "A1", "principal": "岭东", "agent": "吴代收",
            "scopes": ["指定收货地点"], "expires": "2026-10-15",
        }]
        inbox = [
            PAIR_OFFER,
            msg("M3", "吴代收", "就照上次，南仓可收，改成先来一箱护具吧。",
                "2026-10-06T11:00:00+08:00", reply_to="M1"),
        ]
        ev = Analyzer(packet(inbox, grants=grants)).run()
        self.assertFalse(any(c.state == STATE_EFFECTIVE for c in ev.commitments))
        sub = next(s for s in ev.statements
                   if s.message_id == "M3" and s.act_kind == "提出替代")
        self.assertFalse(sub.valid)
        self.assertIn("授权", sub.invalid_reason)
        # 地点指定本身仍记录下来
        location_stmt = next(s for s in ev.statements if s.message_id == "M3" and "南仓" in s.summary)
        self.assertIn("南仓", location_stmt.summary)

    def test_expired_grant_is_rejected(self):
        grants = [{
            "id": "A2", "principal": "岭东", "agent": "吴代收",
            "scopes": ["指定收货地点", "确认收货"], "expires": "2026-10-01",
        }]
        inbox = [
            PAIR_OFFER,
            msg("M3", "吴代收", "南仓可收，两种各一箱我们接。",
                "2026-10-06T11:00:00+08:00", reply_to="M1"),
        ]
        ev = Analyzer(packet(inbox, grants=grants)).run()
        self.assertFalse(any(c.state == STATE_EFFECTIVE for c in ev.commitments))
        self.assertTrue(
            any(s.valid is False and "过期" in (s.invalid_reason or "")
                for s in ev.statements)
        )

    def test_paired_offer_cannot_be_split_against_original_wording(self):
        inbox = [
            PAIR_OFFER,
            msg("M2", "岭东", "护具一箱我们要了，工具以后再说。",
                "2026-10-08T10:00:00+08:00", reply_to="M1"),
        ]
        ev = Analyzer(packet(inbox)).run()
        self.assertFalse(any(c.state == STATE_EFFECTIVE for c in ev.commitments))
        stmt = next(s for s in ev.statements if s.message_id == "M2")
        self.assertFalse(stmt.valid)
        self.assertIn("成对", stmt.invalid_reason)

    def test_splittable_item_can_be_accepted_alone(self):
        inbox = [
            msg("M6", "捐出方", "B11 一箱独立给盐田，翻阅来电人补签即可。",
                "2026-10-06T12:00:00+08:00", thread="独立单"),
            msg("M7", "盐田", "B11 一箱我们确认接收。",
                "2026-10-07T09:00:00+08:00", thread="独立单", reply_to="M6"),
        ]
        ev = Analyzer(packet(inbox)).run()
        commit = next(c for c in ev.commitments if c.thread == "独立单")
        # 补签手续未在资料中出现凭证，因此仍是待确认而非有效
        self.assertEqual(commit.state, STATE_PENDING)
        self.assertTrue(any("补签" in u for u in commit.unmet))

    def test_simultaneous_conditions_without_order_go_to_donor(self):
        # 原话没有首选/后备先后，两方条件都成立：只能发问询
        offer = msg(
            "O1", "捐出方",
            "G24 一箱，若需要可来自取。",
            "2026-10-05T09:00:00+08:00",
        )
        inbox = [
            offer,
            msg("A", "岭东", "G24 一箱我们确认。", "2026-10-06T10:00:00+08:00", reply_to="O1"),
            msg("B", "盐田", "G24 一箱我们也确认。", "2026-10-06T11:00:00+08:00", reply_to="O1"),
        ]
        ev = Analyzer(packet(inbox)).run()
        blocked = [c for c in ev.commitments if c.state == STATE_BLOCKED]
        self.assertTrue(blocked)
        self.assertTrue(any(q.code == "条件同时满足" for q in ev.questions))

    def test_independent_commitment_accepted_when_other_thread_missing_upstream(self):
        # 主约的上游来信缺失，但 B11 独立单资料齐全，应继续受理
        inbox = [
            msg("X1", "岭东", "确认备选一，两种各一箱。",
                "2026-10-09T16:00:00+08:00", reply_to="GHOST", thread="断链"),
            msg("M6", "捐出方", "B11 一箱独立给盐田。",
                "2026-10-06T12:00:00+08:00", thread="独立单"),
            msg("M7", "盐田", "B11 一箱我们确认接收。",
                "2026-10-07T09:00:00+08:00", thread="独立单", reply_to="M6"),
        ]
        ev = Analyzer(packet(inbox)).run()
        self.assertIn("GHOST", ev.missing_upstream)
        independent = next(c for c in ev.commitments if c.thread == "独立单")
        self.assertIn(independent.state, (STATE_EFFECTIVE, STATE_PENDING))
        self.assertTrue(any("断链" in q.topic for q in ev.questions if q.code == "上游缺失"))

    def test_same_stock_cannot_be_confirmed_to_two_points(self):
        inbox = [
            PAIR_OFFER,
            msg("M2", "岭东", "确认，两种各一箱。",
                "2026-10-08T10:00:00+08:00", reply_to="M1"),
        ]
        # 交接单却开给盐田：复核必须发现归属冲突
        turnovers = [{
            "id": "H9", "revision": "H-90/1", "source": ["M1"],
            "state": "双方已确认", "receiver": "盐田",
            "items": {"G24": 6, "T06": 6},
            "shown_to": ["捐出方", "协调员", "盐田"],
            "time": "2026-10-09T18:00:00+08:00",
        }]
        ev = Analyzer(packet(inbox, turnovers=turnovers)).run()
        review = ev.turnover_reviews[0]
        self.assertEqual(review.state, TURNOVER_DISPUTED)
        self.assertEqual(review.frozen_lines, {"G24": 6, "T06": 6})


if __name__ == "__main__":
    unittest.main()
