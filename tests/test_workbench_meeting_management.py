"""Meeting management stays isolated to one meeting and its owned directory."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from live.source_reader import DemoResearcher
from live.store import LiveError
from live.workbench_contracts import AudioImport, AudioImportFinish, NewMeeting, NewQuestion, Utterance
from live_api import create_app


class MeetingManagementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = create_app(self.root / "test.sqlite3", {"TESTING": True, "WORKBENCH_ROOT": self.root / "meetings"})
        self.store = self.app.extensions["live_store"]
        self.seed = self.store.seed(fixture=True)
        self.owner, self.member, _, self.agent = [self.store.authenticate(m["token"]) for m in self.seed["members"]]
        self.wb = self.app.extensions["workbench"]
        self.m = self.wb.create(self.owner, NewMeeting(title="原会议名称", data_scope="synthetic"))
        self.mid = self.m["id"]
        self.url = "/live/v2/meetings/" + self.mid
        self.headers = {"Authorization": "Bearer fixture-owner-0"}
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def delete(self, mid=None):
        return self.client.delete("/live/v2/meetings/" + (mid or self.mid),
                                  json={"confirmation": mid or self.mid}, headers=self.headers)

    def completed_question(self):
        self.wb.demo(self.owner, self.mid)
        self.wb.question(self.owner, self.mid, NewQuestion(title="工作流的定义"))
        work = self.wb.claim(self.agent)
        self.wb.finish(self.agent, work, DemoResearcher().run(work["meeting"], work["question"], lambda _: None))
        return work

    def test_rename_updates_lists_and_keeps_audio_and_directory(self):
        audio = self.wb.writer.path(self.m["directory"] + "/01-原始音频.caf")
        audio.write_bytes(b"synthetic audio")
        response = self.client.patch(self.url, json={"title": "  新会议名称  "}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["title"], "新会议名称")
        self.assertEqual(response.json["directory"], self.m["directory"])
        self.assertEqual(audio.read_bytes(), b"synthetic audio")
        self.assertEqual(self.wb.home(self.owner)["meetings"][0]["title"], "新会议名称")
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["meeting"]["title"], "新会议名称")

    def test_invalid_titles_and_missing_confirmation_do_not_change_data(self):
        for title in ["   ", "\n", "a\x00b", "x" * 121]:
            self.assertEqual(self.client.patch(self.url, json={"title": title}, headers=self.headers).status_code, 400)
        for payload in [{}, {"confirmation": "another-meeting"}]:
            self.assertEqual(self.client.delete(self.url, json=payload, headers=self.headers).status_code, 400)
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["meeting"]["title"], self.m["title"])

    def test_management_requires_owner_and_same_project(self):
        for token in ["fixture-member-1", "fixture-agent-3"]:
            headers = {"Authorization": "Bearer " + token}
            self.assertEqual(self.client.patch(self.url, json={"title": "no"}, headers=headers).status_code, 403)
            self.assertEqual(self.client.get(self.url + "/deletion", headers=headers).status_code, 403)
            self.assertEqual(self.client.delete(self.url, json={"confirmation": self.mid}, headers=headers).status_code, 403)
        other = self.store.seed()
        headers = {"Authorization": "Bearer " + other["members"][0]["token"]}
        self.assertEqual(self.client.delete(self.url, json={"confirmation": self.mid}, headers=headers).status_code, 403)
        self.assertEqual(self.client.delete(self.url, json={"confirmation": self.mid},
                         headers={**self.headers, "Origin": "https://elsewhere.invalid"}).status_code, 403)

    def test_preview_is_read_only_and_reports_impact(self):
        self.completed_question()
        self.wb.utterance(self.owner, self.mid, Utterance(text="合成讨论内容"))
        self.wb.control(self.owner, self.mid, "end")
        directory = self.wb.writer.path(self.m["directory"])
        before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
        response = self.client.get(self.url + "/deletion", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["question_count"], 1)
        self.assertGreater(response.json["segment_count"], 0)
        self.assertEqual(response.json["file_count"], len(before))
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()})

    def test_delete_clears_all_owned_files_records_and_events_only(self):
        self.completed_question()
        directory = self.wb.writer.path(self.m["directory"])
        (directory / "01-原始音频.m4a").write_bytes(b"synthetic audio")
        (directory / "old-version").mkdir()
        (directory / "old-version" / "nested.md").write_text("old generated version")
        source = self.root / "source.m4a"
        source.write_bytes(b"original import")
        other = self.wb.create(self.owner, NewMeeting(title="保留会议", data_scope="synthetic"))
        other_path = self.wb.writer.path(other["directory"] + "/keep.md")
        other_path.write_text("keep")
        response = self.delete()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(directory.exists())
        self.assertTrue(source.exists())
        self.assertEqual(other_path.read_text(), "keep")
        self.assertEqual(self.client.get(self.url + "/snapshot", headers=self.headers).status_code, 404)
        self.assertNotIn(self.mid, [m["id"] for m in self.wb.home(self.owner)["meetings"]])
        self.assertFalse(self.wb.home(self.owner)["materials"])
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE meeting=?", (self.mid,)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records WHERE json_extract(data,'$.meeting_id')=?", (self.mid,)).fetchone()[0], 0)

    def test_late_research_result_cannot_recreate_deleted_directory(self):
        self.wb.demo(self.owner, self.mid)
        self.wb.question(self.owner, self.mid, NewQuestion(title="工作流定义"))
        work = self.wb.claim(self.agent)
        result = DemoResearcher().run(work["meeting"], work["question"], lambda _: None)
        self.assertEqual(self.delete().status_code, 200)
        with self.assertRaises(LiveError):
            self.wb.finish(self.agent, work, result)
        self.wb.fail(self.agent, work, "outcome_unknown")
        self.assertFalse(self.wb.writer.path(self.m["directory"]).exists())

    def test_file_failure_is_visible_retryable_and_fences_background_writes(self):
        with patch("live.workbench.shutil.rmtree", side_effect=PermissionError("private fixture path")):
            response = self.delete()
        self.assertEqual(response.status_code, 409)
        self.assertIn("权限", response.json["error"])
        self.assertNotIn("private fixture", json.dumps(response.json))
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)["meeting"]["state"], "delete_failed")
        with self.assertRaises(LiveError):
            self.wb.utterance(self.owner, self.mid, Utterance(text="late input"))
        self.wb.recover()
        self.assertEqual(self.delete().status_code, 200)

    def test_symlink_cannot_redirect_deletion_outside_meeting_root(self):
        directory = self.wb.writer.path(self.m["directory"])
        outside = self.root / "outside"
        outside.mkdir()
        keep = outside / "keep.txt"
        keep.write_text("keep")
        moved = directory.with_name(directory.name + "-preserved")
        directory.rename(moved)
        directory.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.delete().status_code, 409)
        self.assertEqual(keep.read_text(), "keep")

    def test_in_progress_audio_must_stop_before_deletion(self):
        with self.store.transaction() as db:
            meeting = self.store._get(db, "wb_meeting", self.mid)
            meeting["recordings"] = [{"state": "recording", "path": self.m["directory"] + "/recording.caf"}]
            self.wb._save(db, meeting)
        response = self.delete()
        self.assertEqual(response.status_code, 409)
        self.assertIn("停止", response.json["error"])
        self.assertTrue(self.wb.writer.path(self.m["directory"]).exists())

    def test_deleted_or_expired_empty_directory_can_be_removed(self):
        with self.store.transaction() as db:
            meeting = self.store._get(db, "wb_meeting", self.mid)
            meeting.update(state="expired", directory="")
            self.wb._save(db, meeting)
        self.assertEqual(self.delete().status_code, 200)

    def test_finishing_import_cannot_recreate_files_after_delete(self):
        m = self.wb.create(self.owner, NewMeeting(title="导入结束竞态", data_scope="synthetic", input_kind="audio_file"))
        self.wb.begin_import(self.agent, m["id"], AudioImport(import_id="synthetic", file_name="fixture.m4a", duration_ms=1000, bytes=10))
        original_transcript = self.wb.transcript
        def transcript_then_delete(*args):
            result = original_transcript(*args)
            self.wb.delete_meeting(self.owner, m["id"], m["id"])
            return result
        with patch.object(self.wb, "transcript", side_effect=transcript_then_delete), self.assertRaises(LiveError):
            self.wb.finish_import(self.agent, m["id"], AudioImportFinish(import_id="synthetic", state="completed"))
        self.assertFalse(self.wb.writer.path(m["directory"]).exists())

    def test_late_summary_and_coordinator_cannot_recreate_deleted_meeting(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text="合成会议决定明天继续"))
        self.wb.control(self.owner, self.mid, "end")
        with self.store.transaction() as db:
            m = self.store._get(db, "wb_meeting", self.mid)
            m["permissions"]["model"] = True
            m["summary"]["state"] = "pending"
            self.wb._save(db, m)
        self.wb.capabilities["model_enabled"] = True
        work = self.wb.summary_work(self.agent)
        self.assertIsNotNone(work)
        self.assertEqual(self.delete().status_code, 200)
        with self.assertRaises(LiveError):
            self.wb.finish_summary(self.agent, work, error="outcome_unknown")
        self.wb.coordinator_failed(self.agent, {"meeting": self.m, "cursor": 0}, unknown=True)
        self.assertFalse(self.wb.writer.path(self.m["directory"]).exists())

    def test_nested_symlink_removes_link_without_removing_target(self):
        directory = self.wb.writer.path(self.m["directory"])
        outside = self.root / "keep-original"
        outside.mkdir()
        (outside / "source.wav").write_bytes(b"original")
        (directory / "external-reference").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.delete().status_code, 200)
        self.assertEqual((outside / "source.wav").read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
