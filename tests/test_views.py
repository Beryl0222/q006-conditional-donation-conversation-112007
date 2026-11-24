"""按方视图与取货报告测试：可见性过滤、争议库存、问询归集。"""

import unittest

from donation.analyzer import Analyzer
from donation.views import PartyView, pickup_report
from tests.test_analyzer import PAIR_OFFER, msg, packet


class ViewTest(unittest.TestCase):
    def _build(self):
        inbox = [
            PAIR_OFFER,
            msg("M2", "协调员",
                "备选之一是成套周六来取，另一种是先送护具后续送工具，请说选哪个。",
                "2026-10-05T10:00:00+08:00", thread="岭东", forwards=["M1"],
                viewers=["捐出方", "协调员", "岭东"]),
            msg("M5", "岭东", "确认备选一，两种各一箱，周六在南仓接。",
                "2026-10-09T16:51:00+08:00", thread="岭东", reply_to="M2",
                viewers=["协调员", "岭东", "捐出方"],
                ingested="2026-10-10T14:00:00+08:00"),
            msg("M4", "盐田", "若岭东过了点还未接，我们周六把护具和工具一起提走。",
                "2026-10-09T17:05:00+08:00", thread="盐田", reply_to="M1",
                viewers=["协调员", "盐田", "捐出方"]),
        ]
        turnovers = [{
            "id": "H1", "revision": "H-67/1", "source": ["M1", "M4"],
            "state": "双方已确认", "receiver": "盐田",
            "items": {"G24": 6, "T06": 6},
            "shown_to": ["捐出方", "协调员", "盐田"],
            "time": "2026-10-09T17:30:00+08:00",
        }]
        receipts = [{
            "id": "R1", "turnover": "H1", "receiver": "盐田",
            "items": {"T06": 6}, "signed_at": "2026-10-10T12:00:00+08:00",
        }]
        return Analyzer(packet(inbox, turnovers=turnovers, receipts=receipts)).run()

    def test_yantian_cannot_see_lingdong_messages(self):
        ev = self._build()
        view = PartyView(ev, "盐田").export()
        all_refs = {ref for item in view["交接单"] for ref in item["可见依据"]}
        self.assertIn("M4", all_refs)
        self.assertNotIn("M5", all_refs)
        self.assertNotIn("M2", all_refs)

    def test_lingdong_sees_its_effective_commitment(self):
        ev = self._build()
        view = PartyView(ev, "岭东").export()
        effective = [c for c in view["承诺"] if c["状态"] == "有效"]
        self.assertTrue(effective)
        self.assertEqual(effective[0]["对应方"], "岭东")
        # 岭东没被 shown_to 展示过 H1，交接单区域应为空
        self.assertEqual(view["交接单"], [])

    def test_pickup_report_freezes_disputed_stock_and_flags_receipt(self):
        ev = self._build()
        report = pickup_report(ev)
        self.assertEqual(report["冻结库存"], {"G24": 6})
        review = ev.turnover_reviews[0]
        self.assertEqual(review.received_lines, {"T06": 6})
        self.assertTrue(any(q["code"] == "争议冻结" for q in report["待回问题"]))

    def test_donor_sees_everything(self):
        ev = self._build()
        view = PartyView(ev, "捐出方").export()
        refs = {ref for item in view["交接单"] for ref in item["可见依据"]}
        self.assertEqual(refs, {"M1", "M4"})
        self.assertTrue(view["待办与问询"])


if __name__ == "__main__":
    unittest.main()
