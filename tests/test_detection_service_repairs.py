import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from live.detection import DetectionBatch
from live.workbench_contracts import NewMeeting, Utterance
from live.workbench_runtime import WorkbenchRuntime
from live_api import create_app


class DetectionServiceRepairsTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        app = create_app(root / 'state.db', {'TESTING': True, 'WORKBENCH_ROOT': root / 'meetings',
            'WORKBENCH_CAPABILITIES': {'model_enabled': True, 'debounce_seconds': 0}})
        self.wb = app.extensions['workbench']
        members = self.wb.store.seed(fixture=True)['members']
        self.owner, self.agent = (self.wb.store.authenticate(m['token']) for m in (members[0], members[-1]))
        self.mid = self.wb.create(self.owner, NewMeeting(title='服务恢复', data_scope='synthetic'))['id']
        self.model = Mock()
        self.model.decisions.return_value = DetectionBatch()
        self.runtime = WorkbenchRuntime(self.wb, model=self.model, researcher=Mock())
        self.addCleanup(self.runtime.close)
        self.wb.utterance(self.owner, self.mid, Utterance(text='需要补充识别的发言'))
        initial = self.wb.coordinator_work(self.agent)
        self.wb.commit_detection(self.agent, initial, DetectionBatch(saturated=True))
        self.repair = self.wb.detection_recovery_work(self.agent)

    def check_local_commit_error(self, error):
        put = self.wb.store._put

        def fail_completion(db, kind, key, value):
            if kind == 'wb_detection_task' and value['state'] == 'completed':
                raise error
            return put(db, kind, key, value)

        with patch.object(self.wb.store, '_put', side_effect=fail_completion):
            self.runtime._coordinate(self.agent, self.repair)
        snapshot = self.wb.snapshot(self.owner, self.mid)
        task, = snapshot['detection_tasks']
        self.assertEqual(task['state'], 'needs_review')
        self.assertEqual(task['issues'], [{'field': 'saving', 'code': 'local_save_failed'}])
        self.assertNotIn('detection_service_retry', snapshot['meeting'])
        self.assertNotIn('detection_service_error', snapshot['meeting'])
        self.assertIsNone(self.wb.detection_recovery_work(self.agent))
        self.wb.utterance(self.owner, self.mid, Utterance(text='后续发言仍然需要识别'))
        work = self.wb.coordinator_work(self.agent)
        self.assertIsNotNone(work)
        self.runtime._coordinate(self.agent, work)
        self.assertEqual(self.model.decisions.call_count, 2)
        self.assertEqual(self.wb.snapshot(self.owner, self.mid)['meeting']['coordinator_cursor'], work['cursor'])

    def test_sqlite_failure_is_reviewed_locally(self):
        self.check_local_commit_error(sqlite3.OperationalError('synthetic write failure'))

    def test_filesystem_failure_is_reviewed_locally(self):
        self.check_local_commit_error(OSError('synthetic disk failure'))

    def test_success_from_before_rate_limit_cannot_release_either_lane_early(self):
        self.wb.utterance(self.owner, self.mid, Utterance(text='故障前已领取的新发言'))
        in_flight = self.wb.coordinator_work(self.agent)
        details = {'service': 'model', 'code': 'http_error', 'http_status': 429, 'retry_after_seconds': 30}
        self.wb.coordinator_failed(self.agent, self.repair, details=details)
        snapshot = self.wb.snapshot(self.owner, self.mid)
        retry = snapshot['meeting']['detection_service_retry']
        self.runtime._coordinate(self.agent, in_flight)
        meeting = self.wb.snapshot(self.owner, self.mid)['meeting']
        self.assertEqual(meeting['coordinator_cursor'], in_flight['cursor'])
        self.assertEqual(meeting.get('detection_service_retry'), retry)
        self.wb.utterance(self.owner, self.mid, Utterance(text='等待服务恢复的新发言'))
        with patch('live.detection_recovery.time.time', return_value=retry['next_at'] - 1):
            self.assertIsNone(self.wb.coordinator_work(self.agent))
            self.assertIsNone(self.wb.detection_recovery_work(self.agent))
        with patch('live.detection_recovery.time.time', return_value=retry['next_at'] + 1):
            recovery = self.wb.detection_recovery_work(self.agent)
            self.assertIsNotNone(recovery)
            self.runtime._coordinate(self.agent, recovery)
            self.assertIsNotNone(self.wb.coordinator_work(self.agent))
        snapshot = self.wb.snapshot(self.owner, self.mid)
        self.assertNotIn('detection_service_retry', snapshot['meeting'])
        self.assertNotIn('detection_service_error', snapshot['meeting'])
        self.assertFalse(snapshot['detection_tasks'])
