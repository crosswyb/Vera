"""Changing the next meeting's destination never relocates existing artifacts."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from live.source_reader import DemoResearcher
from live.store import LiveError
from live.workbench_contracts import AudioEvent, AudioImport, AudioImportFinish, NewMeeting, NewQuestion, RecordingStart, Utterance
from live_api import create_app


class MeetingStorageTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.original = self.root / "meetings"
        self.destination = self.root / "chosen meetings"
        self.open_app()
        members = self.wb.store.seed(fixture=True)["members"]
        self.owner, self.member, self.agent = [self.wb.store.authenticate(m["token"]) for m in (members[0], members[1], members[-1])]
        self.headers = {"Authorization": "Bearer " + members[0]["token"]}
        self.member_headers = {"Authorization": "Bearer " + members[1]["token"]}

    def open_app(self, configured=None):
        with patch.dict(os.environ, {}, clear=True):
            self.app = create_app(self.root / "state.sqlite3", {"TESTING": True,
                "WORKBENCH_ROOT": configured or self.original, "WORKBENCH_SETTINGS_PATH": self.root / "providers.json",
                "WORKBENCH_CAPABILITIES": {"audio_enabled": True, "model_enabled": True, "search_enabled": True}})
        self.wb = self.app.extensions["workbench"]
        self.client = self.app.test_client()

    def create(self, title="合成目录验证", imported=False):
        return self.wb.create(self.owner, NewMeeting(title=title, data_scope="synthetic",
            input_kind="audio_file" if imported else "live"))

    def choose(self, path):
        response = self.client.put("/live/v2/storage", headers=self.headers, json={"output_root": str(path)})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def saved_question(self, meeting, *, before_finish=None):
        self.wb.question(self.owner, meeting["id"], NewQuestion(title="合成问题", public_query="synthetic workflow"))
        work = self.wb.claim(self.agent)
        self.assertEqual(work["meeting"]["id"], meeting["id"])
        if before_finish:
            before_finish()
        result = DemoResearcher().run({**work["meeting"], "mode": "demo"}, work["question"], lambda _: None)
        return self.wb.finish(self.agent, work, result)

    def test_api_is_owner_only_and_invalid_setting_keeps_previous_value(self):
        expected = {"output_root": str(self.original), "default_output_root": str(self.original)}
        self.assertEqual(self.client.get("/live/v2/storage", headers=self.headers).json, expected)
        self.assertEqual(self.client.get("/live/v2/storage").status_code, 401)
        for verb in ("get", "put"):
            kwargs = {"headers": self.member_headers}
            if verb == "put":
                kwargs["json"] = {"output_root": str(self.destination)}
            self.assertEqual(getattr(self.client, verb)("/live/v2/storage", **kwargs).status_code, 403)
        obstacle = self.root / "ordinary.txt"
        obstacle.write_text("user file")
        for body in ({}, {"output_root": "relative"}, {"output_root": "/"}, {"output_root": str(obstacle)},
                     {"output_root": None}, {"output_root": str(self.destination), "extra": True}):
            with self.subTest(body=body):
                response = self.client.put("/live/v2/storage", headers=self.headers, json=body)
                self.assertEqual(response.status_code, 400, response.json)
                self.assertTrue(response.json["error"])
                self.assertEqual(self.wb.storage(self.owner), expected)
        with patch("live.meeting_storage.tempfile.TemporaryFile", side_effect=PermissionError("fixture denied")):
            response = self.client.put("/live/v2/storage", headers=self.headers, json={"output_root": str(self.destination)})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.wb.storage(self.owner), expected)
        self.assertEqual(obstacle.read_text(), "user file")

    def test_switch_preserves_inflight_artifact_and_old_transcript_and_new_meeting_uses_new_root(self):
        old = self.create("之前的会议")
        q = self.saved_question(old, before_finish=lambda: self.choose(self.destination))
        artifact = self.wb.artifact(self.owner, q["artifact_id"])
        self.assertTrue(Path(artifact["document"]["absolute_path"]).is_relative_to(self.original))
        self.assertTrue(artifact["document"]["on_disk"])
        self.wb.utterance(self.owner, old["id"], Utterance(text="旧会议切换后仍追加本地合成转录。"))
        self.wb.control(self.owner, old["id"], "end")
        transcript = self.wb.meeting_file(self.owner, old["id"], "transcript")
        self.assertTrue(Path(transcript["path"]).is_relative_to(self.original))
        new = self.create("切换后的会议")
        new_q = self.saved_question(new)
        new_doc = self.wb.artifact(self.owner, new_q["artifact_id"])["document"]
        self.assertTrue(Path(new_doc["absolute_path"]).is_relative_to(self.destination))
        self.assertTrue(new_doc["on_disk"])
        self.assertEqual(self.wb.snapshot(self.owner, old["id"])["meeting"]["output_root"], str(self.original))
        self.assertEqual(new["output_root"], str(self.destination))
        self.assertFalse((self.destination / old["directory"]).exists())
        self.assertFalse((self.original / new["directory"]).exists())
        self.open_app(configured=self.root / "different-cli-default")
        self.assertEqual(self.wb.storage(self.owner), {"output_root": str(self.destination), "default_output_root": str(self.original)})
        self.assertTrue(self.wb.artifact(self.owner, q["artifact_id"])["document"]["on_disk"])
        self.assertEqual(self.wb.meeting_file(self.owner, old["id"], "transcript"), transcript)
        self.assertEqual(self.create("重开后的会议")["output_root"], str(self.destination))

    def test_legacy_meeting_is_bound_before_switch_and_never_drifts_on_reopen(self):
        old = self.create()
        with self.wb.store.transaction() as db:
            legacy = self.wb.store._get(db, "wb_meeting", old["id"])
            legacy.pop("output_root")
            self.wb.store._put(db, "wb_meeting", old["id"], legacy)
            db.execute("DELETE FROM records WHERE kind='wb_storage'")
        self.open_app()
        self.choose(self.destination)
        self.open_app()
        bound = self.wb.snapshot(self.owner, old["id"])["meeting"]
        self.assertEqual(bound["output_root"], str(self.original))
        self.wb.rename_meeting(self.owner, old["id"], "历史会议重命名")
        self.assertTrue((self.original / old["directory"]).is_dir())
        self.assertFalse((self.destination / old["directory"]).exists())

    def test_deletion_uses_bound_root_and_preserves_other_meetings_and_user_files(self):
        old = self.create("旧目录会议")
        self.choose(self.destination)
        new = self.create("新目录会议")
        sentinel = self.destination / "用户文件.txt"
        sentinel.write_text("保留")
        preview = self.wb.deletion_preview(self.owner, old["id"])
        self.assertEqual(preview["directory"], str(self.original / old["directory"]))
        self.wb.delete_meeting(self.owner, old["id"], old["id"])
        self.assertFalse((self.original / old["directory"]).exists())
        self.assertTrue((self.destination / new["directory"]).is_dir())
        self.wb.delete_meeting(self.owner, new["id"], new["id"])
        self.assertFalse((self.destination / new["directory"]).exists())
        self.assertEqual(sentinel.read_text(), "保留")
        self.assertTrue(self.original.is_dir() and self.destination.is_dir())

    def test_nested_meeting_root_and_symlink_escape_are_rejected(self):
        old = self.create()
        old_directory = self.original / old["directory"]
        for destination in (old_directory, old_directory / "nested"):
            response = self.client.put("/live/v2/storage", headers=self.headers, json={"output_root": str(destination)})
            self.assertEqual(response.status_code, 409)
        escape = self.root / "outside"
        escape.mkdir()
        sentinel = escape / "user.txt"
        sentinel.write_text("保留")
        shutil.rmtree(old_directory)
        old_directory.symlink_to(escape, target_is_directory=True)
        with self.assertRaises(LiveError):
            self.wb.deletion_preview(self.owner, old["id"])
        with self.assertRaises(LiveError):
            self.wb.writer_for(old).write(old["directory"] + "/escape.md", "blocked")
        self.assertEqual(sentinel.read_text(), "保留")
        self.assertFalse((escape / "escape.md").exists())

    def test_unavailable_custom_root_is_not_recreated_or_redirected_on_reopen(self):
        self.choose(self.destination)
        meeting = self.create()
        moved = self.root / "offline"
        self.destination.rename(moved)
        self.open_app()
        self.assertEqual(self.wb.snapshot(self.owner, meeting["id"])["meeting"]["id"], meeting["id"])
        with self.assertRaises(LiveError):
            self.wb.writer_for(meeting).write(meeting["directory"] + "/blocked.md", "blocked")
        with self.assertRaises(LiveError):
            self.create("磁盘离线")
        self.assertFalse(self.destination.exists())
        self.destination.symlink_to(moved, target_is_directory=True)
        self.open_app()
        with self.assertRaises(LiveError):
            self.wb.writer_for(meeting).path(meeting["directory"])
        self.assertTrue((moved / meeting["directory"]).is_dir())

    def test_recording_keeps_original_destination_through_switch_and_caf_archive(self):
        meeting = self.create()
        mid = meeting["id"]
        registration = self.wb.start_recording(self.agent, mid, RecordingStart(recording_id="e1", sources=["microphone"]))
        caf = Path(registration["recording_directory"]) / "01-原始音频-microphone-e1.caf"
        caf.write_bytes(b"synthetic CAF bytes")
        self.choose(self.destination)
        self.wb.audio(self.agent, mid, AudioEvent(meeting_id=mid, capture_epoch="e1", stream_id="mic", seq=1,
            type="audio.recording", source="microphone", occurred_at="2026-11-19T12:00:00Z", state="saved", audio_end_ms=1000))
        self.wb.control(self.owner, mid, "end")
        def encode(source, target):
            self.assertEqual(source, caf)
            Path(target).write_bytes(b"synthetic M4A")
        with patch("live.recording_archive.encode_caf", side_effect=encode):
            self.wb.archive_recording(mid, "e1:microphone")
        recording = self.wb.snapshot(self.owner, mid)["meeting"]["recordings"][0]
        self.assertEqual(recording["archive_state"], "saved")
        self.assertEqual((self.original / recording["path"]).read_bytes(), b"synthetic M4A")
        self.assertFalse(caf.exists())
        self.assertFalse((self.destination / meeting["directory"]).exists())

    def test_offline_recording_recovery_keeps_metadata_and_allows_other_meetings(self):
        self.choose(self.destination)
        meeting = self.create()
        registration = self.wb.start_recording(self.agent, meeting["id"], RecordingStart(recording_id="offline", sources=["system"]))
        caf = Path(registration["recording_directory"]) / "01-原始音频-system-offline.caf"
        caf.write_bytes(b"synthetic interrupted recording")
        self.choose(self.original)
        moved = self.root / "disconnected-drive"
        self.destination.rename(moved)
        self.open_app()
        self.wb.recover()
        saved = self.wb.snapshot(self.owner, meeting["id"])["meeting"]
        self.assertEqual(saved["recordings"][0]["state"], "unavailable")
        self.assertIn("尚未核实", saved["recordings"][0]["storage_error"])
        self.assertEqual(saved["output_root"], str(self.destination))
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.create("其他会议不受影响")["output_root"], str(self.original))
        moved.rename(self.destination)
        self.open_app()
        self.wb.recover()
        restored = self.wb.snapshot(self.owner, meeting["id"])["meeting"]["recordings"][0]
        self.assertEqual(restored["state"], "partial")
        self.assertNotIn("storage_error", restored)
        self.assertEqual(caf.read_bytes(), b"synthetic interrupted recording")

    def test_offline_publication_is_save_failure_and_recovers_to_the_same_directory(self):
        self.choose(self.destination)
        meeting = self.create()
        moved = self.root / "disconnected-artifacts"
        question = self.saved_question(meeting, before_finish=lambda: self.destination.rename(moved))
        snapshot = self.wb.snapshot(self.owner, meeting["id"])
        document = snapshot["documents"][0]
        self.assertEqual(document["state"], "save_failed")
        self.assertIn("保存目录不可用", document["error"])
        self.assertNotIn("人工编辑", document["error"])
        self.assertFalse(self.destination.exists())
        self.wb.control(self.owner, meeting["id"], "end")
        unavailable = self.wb.snapshot(self.owner, meeting["id"])["meeting"]
        self.assertEqual(unavailable["base_files"]["transcript"]["state"], "save_failed")
        moved.rename(self.destination)
        self.open_app()
        self.wb.recover()
        recovered = self.wb.artifact(self.owner, question["artifact_id"])["document"]
        self.assertEqual(recovered["state"], "saved")
        self.assertTrue(recovered["on_disk"])
        self.assertTrue(Path(recovered["absolute_path"]).is_relative_to(self.destination))

    def test_import_finish_and_recovery_stay_with_bound_root(self):
        self.choose(self.destination)
        meeting = self.create(imported=True)
        mid = meeting["id"]
        imported = self.wb.begin_import(self.agent, mid, AudioImport(import_id="fixture", file_name="input.m4a", duration_ms=1000, bytes=32))
        original = self.destination / imported["audio_import"]["original_path"]
        original.write_bytes(b"synthetic import")
        self.choose(self.original)
        self.wb.finish_import(self.agent, mid, AudioImportFinish(import_id="fixture", state="completed"))
        saved = self.wb.snapshot(self.owner, mid)["meeting"]
        self.assertEqual(saved["recordings"][0]["state"], "partial")
        self.assertTrue((self.destination / saved["audio_import"]["transcript_path"]).is_file())
        live = self.create()
        registration = self.wb.start_recording(self.agent, live["id"], RecordingStart(recording_id="e2", sources=["system"]))
        caf = Path(registration["recording_directory"]) / "01-原始音频-system-e2.caf"
        caf.write_bytes(b"synthetic interrupted capture")
        self.choose(self.destination)
        self.open_app()
        self.wb.recover()
        recovered = self.wb.snapshot(self.owner, live["id"])["meeting"]
        self.assertEqual(recovered["recordings"][0]["state"], "partial")
        self.assertTrue(caf.is_file())
        self.assertFalse((self.destination / live["directory"]).exists())


if __name__ == "__main__":
    unittest.main()
