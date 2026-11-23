"""Meeting-local search must cover history without leaking other meetings."""

import tempfile
import unittest
from pathlib import Path

from live.workbench_contracts import NewMeeting, NewQuestion, Utterance
from live_api import create_app


class MeetingSearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.app = create_app(root / "state.sqlite3", {"TESTING": True, "WORKBENCH_ROOT": root / "meetings"})
        self.store = self.app.extensions["live_store"]
        members = self.store.seed(fixture=True)["members"]
        self.owner = self.store.authenticate(members[0]["token"])
        self.wb = self.app.extensions["workbench"]
        self.mid = self.wb.create(self.owner, NewMeeting(title="搜索验收", data_scope="synthetic"))["id"]
        self.client = self.app.test_client()
        self.headers = {"Authorization": "Bearer fixture-owner-0"}

    def search(self, query):
        return self.client.get(f"/live/v2/meetings/{self.mid}/search", query_string={"q": query}, headers=self.headers)

    def test_search_finds_early_final_utterance_and_question(self):
        self.wb.utterance(self.owner, self.mid, Utterance(segment_id="early", text="查一下工单统计口径"))
        question = self.wb.question(self.owner, self.mid, NewQuestion(title="工单是否只统计一次"))
        # Emulate a long imported recording with history beyond the snapshot.
        with self.store.transaction() as db:
            for i in range(250):
                segment = {"id": f"{self.mid}:later-{i}", "meeting_id": self.mid, "segment_id": f"later-{i}", "revision": 1,
                           "event_seq": 1000 + i, "type": "transcript.final", "text": "后续讨论"}
                self.store._put(db, "wb_segment", f"{self.mid}:later-{i}", segment)
        self.assertNotIn("manual:early", [row["segment_id"] for row in self.wb.snapshot(self.owner, self.mid)["segments"]])
        result = self.search("工单").get_json()
        self.assertEqual(result["segments"][0]["text"], "查一下工单统计口径")
        self.assertIsInstance(result["segments"][0]["event_seq"], int)
        self.assertEqual(result["questions"][0]["id"], question["id"])

    def test_search_is_literal_bounded_and_excludes_unsettled_text(self):
        with self.store.transaction() as db:
            for i in range(24):
                segment = {"meeting_id": self.mid, "segment_id": f"s-{i}", "revision": 1,
                           "event_seq": i, "type": "transcript.final", "text": "SQL_% literal"}
                if i == 22:
                    segment["type"] = "transcript.interim"
                if i == 23:
                    segment["type"] = "transcript.retracted"
                self.store._put(db, "wb_segment", f"{self.mid}:s-{i}", segment)
        result = self.search("_%").get_json()
        self.assertEqual(len(result["segments"]), 20)
        self.assertTrue(result["more_segments"])
        self.assertEqual(result["segments"][0]["event_seq"], 21)
        self.assertEqual(self.search("literal").status_code, 200)
        self.assertEqual(self.search("does not exist").get_json()["segments"], [])
        self.assertEqual(self.search("   ").status_code, 400)
        self.assertEqual(self.search("x" * 201).status_code, 400)

    def test_search_pages_each_result_type(self):
        with self.store.transaction() as db:
            for i in range(25):
                segment = {"meeting_id": self.mid, "segment_id": f"page-{i}", "revision": 1,
                           "event_seq": i, "type": "transcript.final", "text": "分页关键词"}
                self.store._put(db, "wb_segment", f"{self.mid}:page-{i}", segment)
        first = self.client.get(f"/live/v2/meetings/{self.mid}/search",
                                query_string={"q": "分页关键词"}, headers=self.headers).get_json()
        second = self.client.get(f"/live/v2/meetings/{self.mid}/search",
                                 query_string={"q": "分页关键词", "segment_offset": 20},
                                 headers=self.headers).get_json()
        self.assertEqual(first["segment_total"], 25)
        self.assertTrue(first["more_segments"])
        self.assertEqual(len(second["segments"]), 5)
        self.assertFalse(second["more_segments"])
        self.assertEqual(second["segments"][0]["event_seq"], 4)

    def test_search_rejects_invalid_paging_position(self):
        response = self.client.get(f"/live/v2/meetings/{self.mid}/search",
                                   query_string={"q": "关键词", "segment_offset": -1}, headers=self.headers)
        self.assertEqual(response.status_code, 400)

    def test_search_is_confined_to_authorized_meeting(self):
        other = self.wb.create(self.owner, NewMeeting(title="另一次会议", data_scope="synthetic"))["id"]
        self.wb.utterance(self.owner, other, Utterance(segment_id="other", text="仅在另一会议的工单"))
        self.wb.question(self.owner, other, NewQuestion(title="另一会议的工单"))
        self.assertEqual(self.search("工单").get_json()["segments"], [])
        self.assertEqual(self.search("工单").get_json()["questions"], [])
        outsiders = self.store.seed(synthetic=True)["members"]
        outsider = {"Authorization": "Bearer " + outsiders[0]["token"]}
        response = self.client.get(f"/live/v2/meetings/{self.mid}/search?q=工单", headers=outsider)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get(f"/live/v2/meetings/{self.mid}/search?q=工单").status_code, 401)


if __name__ == "__main__":
    unittest.main()
