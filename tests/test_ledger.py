"""资料包的读取与语义分析测试。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from donation_ledger.ledger import analyse
from donation_ledger.models import load_packet

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "records.json"


def base_records() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


def analyse_records(records: dict):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "records.json"
        path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        return analyse(load_packet(path))


def message(records: dict, message_id: str) -> dict:
    return next(row for row in records["inbox"] if row["id"] == message_id)


class SourcePacketTest(unittest.TestCase):
    """随档合成资料的关键结论。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.analysis = analyse(load_packet(DATA))

    def stance(self, message_id: str):
        return next(row for row in self.analysis.stances if row.message_id == message_id)

    def utterance(self, key: str):
        return next(row for row in self.analysis.turnovers if row.revision == key)

    def test_short_names_resolve_to_register(self):
        reading = self.analysis.readings["M1"]
        self.assertEqual(set(reading.quantity_map), {"G24", "T06", "B11"})
        self.assertEqual(reading.quantity_map["G24"], 1)
        self.assertEqual(reading.quantity_map["B11"], 1)

    def test_elided_reply_inherits_items_from_selected_option(self):
        reading = self.analysis.readings["M5"]
        self.assertEqual(reading.selected_alternative, "1")
        self.assertEqual(set(reading.quantity_map), {"G24", "T06"})
        self.assertIn("M2", {ref for _, ref, _ in reading.references})

    def test_delegation_only_covers_location(self):
        stance = self.stance("M3")
        self.assertEqual(stance.status, "unauthorized")
        self.assertTrue(stance.authority.can_location)
        self.assertFalse(stance.authority.can_commit)
        self.assertIn("物品", stance.authority.reason)

    def test_split_rule_follows_original_wording(self):
        bundle = next(row for row in self.analysis.offers if row.root_id == "M1" and "G24" in row.items)
        self.assertFalse(bundle.split_allowed)

    def test_late_reply_sent_before_deadline_wins_over_ingested_order(self):
        early = self.stance("M5")
        self.assertTrue(early.effective)
        self.assertLess(early.sent, self.stance("M4").sent)

    def test_fallback_condition_unmet_when_primary_accepted(self):
        fallback = self.stance("M4")
        self.assertEqual(fallback.status, "condition_unmet")
        self.assertTrue(any(check.kind == "no_acceptance_by_deadline" for check in fallback.conditions))

    def test_backfilled_message_keeps_original_send_order(self):
        early = self.stance("M5")
        self.assertGreater(early.ingested, self.stance("M4").sent)
        self.assertLess(early.sent, self.stance("M4").sent)

    def test_turnover_needs_both_sides_and_met_conditions(self):
        check = self.utterance("H-67/1")
        self.assertEqual(check.status, "invalid")
        self.assertTrue(any("没有在单据发出前" in item for item in check.unmet))

    def test_signed_disputed_goods_need_manual_ratification(self):
        receipt = self.analysis.receipts[0]
        self.assertEqual(receipt.status, "manual_ratification")
        self.assertIn("不能当余物", receipt.detail)

    def test_pickup_requires_valid_turnover(self):
        self.assertEqual(self.analysis.pickup_view()["now"], [])

    def test_party_export_hides_unseen_messages(self):
        export = self.analysis.export_for("盐田", "H-67/1")
        assert export is not None
        visible = set(self.analysis.visible_refs("盐田"))
        self.assertLessEqual(set(export["cited_messages"]), visible)
        self.assertNotIn("M5", export["cited_messages"])

    def test_missing_reference_is_reported_not_guessed(self):
        reading = self.analysis.readings["M2"]
        self.assertEqual(reading.missing_refs, [])
        self.assertTrue(self.analysis.pickup_view()["pending_questions"])


class ConfirmationFlowTest(unittest.TestCase):
    """条件满足后应形成有效交接单。"""

    def test_valid_turnover_when_both_sides_agree(self):
        records = base_records()
        for row in records["turnover"]:
            if row["id"] == "H2":
                row["state"] = "双方已确认"
        records["inbox"].append(
            {
                "id": "M8",
                "thread": "独立单",
                "sender": "盐田",
                "sent": "2026-10-09T17:50:00+08:00",
                "ingested": "2026-10-09T17:50:00+08:00",
                "reply_to": "M6",
                "text": "确认收下 B11 一箱。",
                "viewers": ["协调员", "捐出方", "盐田"],
            }
        )
        analysis = analyse_records(records)
        check = next(row for row in analysis.turnovers if row.revision == "H-68/1")
        self.assertEqual(check.status, "valid")
        pickups = analysis.pickup_view()["now"]
        self.assertEqual([row["party"] for row in pickups], ["盐田"])
        self.assertEqual(pickups[0]["items"], {"B11": 1})

    def test_half_of_a_pair_cannot_stand_alone(self):
        records = base_records()
        message(records, "M5")["text"] = "确认备选一，只要护具一箱，周六在南仓接。"
        analysis = analyse_records(records)
        stance = next(row for row in analysis.stances if row.message_id == "M5")
        self.assertEqual(stance.status, "incomplete")
        self.assertIn("不能拆分", "".join(stance.reasons))

    def test_severable_item_can_be_confirmed_alone(self):
        records = base_records()
        records["turnover"] = [row for row in records["turnover"] if row["id"] == "H2"]
        records["receipt"] = []
        records["inbox"] = [row for row in records["inbox"] if row["id"] == "M6"]
        records["inbox"].append(
            {
                "id": "M8",
                "thread": "独立单",
                "sender": "盐田",
                "sent": "2026-10-09T17:50:00+08:00",
                "ingested": "2026-10-09T17:50:00+08:00",
                "reply_to": "M6",
                "text": "确认收下 B11 一箱。",
                "viewers": ["协调员", "捐出方", "盐田"],
            }
        )
        records["turnover"][0]["state"] = "双方已确认"
        analysis = analyse_records(records)
        stance = next(row for row in analysis.stances if row.message_id == "M8")
        self.assertEqual(stance.status, "accepted")
        check = next(row for row in analysis.turnovers if row.revision == "H-68/1")
        self.assertEqual(check.status, "valid")
        self.assertEqual(analysis.pickup_view()["now"][0]["items"], {"B11": 1})

    def test_no_split_pair_reply_cannot_stand_alone(self):
        records = base_records()
        message(records, "M5")["text"] = "护具一箱先来即可，工具再说。"
        analysis = analyse_records(records)
        stance = next(row for row in analysis.stances if row.message_id == "M5")
        # M1 的 G24/T06 写明成对，只答一半不能单独成立。
        self.assertEqual(stance.status, "incomplete")

    def test_two_receivers_accepting_same_goods_need_donor(self):
        records = base_records()
        records["inbox"] = [row for row in records["inbox"] if row["id"] == "M6"]
        records["turnover"] = []
        records["receipt"] = []
        records["inbox"].extend(
            [
                {
                    "id": "M8",
                    "thread": "独立单",
                    "sender": "盐田",
                    "sent": "2026-10-09T18:10:00+08:00",
                    "ingested": "2026-10-09T18:10:00+08:00",
                    "reply_to": "M6",
                    "text": "确认收下 B11 一箱。",
                    "viewers": ["协调员", "捐出方", "盐田", "岭东"],
                },
                {
                    "id": "M9",
                    "thread": "独立单",
                    "sender": "岭东",
                    "sent": "2026-10-09T18:20:00+08:00",
                    "ingested": "2026-10-09T18:20:00+08:00",
                    "reply_to": "M6",
                    "text": "B11 一箱我们也要。",
                    "viewers": ["协调员", "捐出方", "盐田", "岭东"],
                },
            ]
        )
        analysis = analyse_records(records)
        clarifications = analysis.pickup_view()["clarifications"]
        self.assertTrue(clarifications)
        self.assertEqual(clarifications[0]["party"], "捐出方")

    def test_two_possible_recipients_without_order_need_donor(self):
        records = base_records()
        # 去掉期限与改送，让两方都在没有先后说明的情况下接受。
        message(records, "M1")["text"] = "G24 和 T06 每件各一箱成对送，给岭东；备用护腕可单送。"
        message(records, "M4")["text"] = "岭东若未接，我们周六把护具和工具一起提走。"
        analysis = analyse_records(records)
        clarifications = analysis.pickup_view()["clarifications"]
        self.assertTrue(clarifications)
        self.assertEqual(clarifications[0]["party"], "捐出方")

    def test_same_goods_cannot_be_confirmed_to_two_receivers(self):
        records = base_records()
        records["turnover"].append(
            {
                "id": "H3",
                "revision": "H-69/1",
                "source": ["M1", "M5"],
                "state": "双方已确认",
                "receiver": "岭东",
                "items": {"G24": 6},
                "shown_to": ["捐出方", "协调员", "岭东"],
                "time": "2026-10-10T16:00:00+08:00",
            }
        )
        analysis = analyse_records(records)
        check = next(row for row in analysis.turnovers if row.revision == "H-69/1")
        self.assertEqual(check.status, "invalid")
        self.assertTrue(check.conflicts)

    def test_conflict_arriving_after_turnover_freezes_undelivered_part(self):
        records = base_records()
        # 接收方在单据发出前后的早期答复，直到单据发出后才补录。
        records["inbox"].append(
            {
                "id": "M9",
                "thread": "独立单",
                "sender": "盐田",
                "sent": "2026-10-09T17:55:00+08:00",
                "ingested": "2026-10-11T09:00:00+08:00",
                "reply_to": "M6",
                "text": "B11 先不要了，改天再说。",
                "viewers": ["协调员", "捐出方", "盐田"],
            }
        )
        analysis = analyse_records(records)
        check = next(row for row in analysis.turnovers if row.revision == "H-68/1")
        self.assertIn(check.status, {"frozen", "invalid"})
        frozen = analysis.pickup_view()["frozen"]
        self.assertTrue(any("B11" in part["items"] for part in frozen))
        self.assertTrue(
            any(question["party"] == "盐田" for question in analysis.pickup_view()["pending_questions"])
        )

    def test_missing_upstream_letter_does_not_block_independent_offer(self):
        records = base_records()
        records["turnover"].append(
            {
                "id": "H4",
                "revision": "H-70/1",
                "source": ["M99"],
                "state": "待确认",
                "receiver": "盐田",
                "items": {"B11": 12},
                "shown_to": ["捐出方", "协调员", "盐田"],
                "time": "2026-10-09T19:00:00+08:00",
            }
        )
        analysis = analyse_records(records)
        broken = next(row for row in analysis.turnovers if row.revision == "H-70/1")
        self.assertEqual(broken.status, "incomplete")
        self.assertEqual(analysis.readings["M6"].quantity_map, {"B11": 1})
        self.assertTrue(any("M99" in line for line in analysis.unresolved))

    def test_quote_and_withdrawal_are_distinguished(self):
        records = base_records()
        records["inbox"].append(
            {
                "id": "M10",
                "thread": "岭东",
                "sender": "协调员",
                "sent": "2026-10-09T16:55:00+08:00",
                "ingested": "2026-10-09T16:55:00+08:00",
                "forwards": ["M5"],
                "text": "岭东转告：已确认两种各一箱。",
                "viewers": ["协调员", "捐出方", "岭东"],
            }
        )
        analysis = analyse_records(records)
        stance = next(row for row in analysis.stances if row.message_id == "M10")
        self.assertEqual(stance.status, "relay")
        self.assertFalse(stance.effective)

    def test_split_reply_per_item(self):
        records = base_records()
        message(records, "M5")["text"] = "工具一箱确认，护具先不要。"
        analysis = analyse_records(records)
        stance = next(row for row in analysis.stances if row.message_id == "M5")
        self.assertEqual(set(stance.positive_items), {"T06"})
        self.assertEqual(set(stance.refused_items), {"G24"})


class VersionAndExportTest(unittest.TestCase):
    """版本保留、暴露范围与缺资料的部分受理。"""

    def test_turnover_versions_keep_what_each_side_saw(self):
        records = base_records()
        records["turnover"].append(
            {
                "id": "H5",
                "revision": "H-68/2",
                "source": ["M6"],
                "state": "待确认",
                "receiver": "盐田",
                "items": {"B11": 12},
                "shown_to": ["捐出方", "协调员", "盐田"],
                "time": "2026-10-10T09:00:00+08:00",
            }
        )
        analysis = analyse_records(records)
        versions = analysis.status()["versions"]
        old = next(row for row in versions if row["revision"] == "H-68/1")
        self.assertEqual(old["superseded_by"], "H-68/2")
        self.assertEqual(old["shown_to"], ["捐出方", "协调员", "盐田"])

    def test_export_only_carries_visible_refs(self):
        analysis = analyse(load_packet(DATA))
        # 盐田看不到 M5（岭东的答复），导出给盐田时不能带出这条。
        export = analysis.export_for("盐田", "H-67/1")
        assert export is not None
        visible = set(analysis.visible_refs("盐田"))
        self.assertLessEqual(set(export["cited_messages"]), visible)
        self.assertNotIn("M5", export["cited_messages"])

    def test_export_unknown_party_gets_no_citations(self):
        analysis = analyse(load_packet(DATA))
        export = analysis.export_for("路人", "H-67/1")
        assert export is not None
        self.assertEqual(export["cited_messages"], [])

    def test_missing_upstream_letter_marks_partial_but_keeps_rest(self):
        records = base_records()
        records["inbox"] = [row for row in records["inbox"] if row["id"] != "M1"]
        for row in records["turnover"]:
            row["source"] = ["M4"] if "M4" in row["source"] else row["source"]
        analysis = analyse_records(records)
        affected = next(row for row in analysis.turnovers if row.revision == "H-67/1")
        self.assertEqual(affected.status, "incomplete")
        self.assertIn("M1", affected.missing_refs)
        # 独立承诺仍能受理：B11 的待确认单据还在，不被上游缺口连坐。
        independent = next(row for row in analysis.turnovers if row.revision == "H-68/1")
        self.assertEqual(independent.status, "pending_confirmation")
        self.assertTrue(
            any("M1" in line for line in analysis.unresolved)
        )

    def test_reply_against_missing_letter_is_registered_not_guessed(self):
        records = base_records()
        records["inbox"] = [row for row in records["inbox"] if row["id"] != "M1"]
        analysis = analyse_records(records)
        stance = next(row for row in analysis.stances if row.message_id == "M4")
        self.assertEqual(stance.status, "unverifiable")
        self.assertIn("M1", stance.missing_refs)


if __name__ == "__main__":
    unittest.main()
