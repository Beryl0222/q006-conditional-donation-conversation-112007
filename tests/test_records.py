"""核对随档资料可以读取，以及原始记录引用完整。"""

from pathlib import Path
import tempfile
import unittest

from inspect_records import load_records


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.data = load_records(Path(__file__).resolve().parents[1] / "data" / "records.json")

    def test_record_references(self):
        for source, field, target in [('inbox', 'reply_to', 'inbox'), ('inbox', 'forwards', 'inbox'), ('turnover', 'source', 'inbox'), ('receipt', 'turnover', 'turnover')]:
            group, _, key = target.partition(".")
            keys = {row[key or "id"] for row in self.data[group]}
            for row in self.data[source]:
                refs = row.get(field)
                if refs is None:
                    continue
                refs = refs if isinstance(refs, list) else [refs]
                with self.subTest(kind=source, record=row["id"], field=field):
                    self.assertLessEqual(set(refs), keys)

    def test_duplicate_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text('{"组":[{"id":"1"},{"id":"1"}]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "编号重复"):
                load_records(path)

    def test_source_packet(self):
        from datetime import datetime
        inbox = {r["id"]: r for r in self.data["inbox"]}
        late = inbox["M5"]
        when = datetime.fromisoformat
        self.assertLess(when(late["sent"]), when(inbox["M4"]["sent"]))
        self.assertLess(when(self.data["receipt"][0]["signed_at"]), when(late["ingested"]))
        self.assertEqual(self.data["grant"][0]["agent"], inbox["M3"]["sender"])
        stock_ids = {r["sku"] for r in self.data["item_register"]}
        self.assertTrue(all(set(item["items"]) <= stock_ids for item in self.data["turnover"]))


if __name__ == "__main__":
    unittest.main()
