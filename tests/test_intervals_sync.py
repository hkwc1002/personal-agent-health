"""手环同步业务测试：合成数据、隔离数据库，不调用真实账户。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

from health import Health
from intervals_sync import (
    ACTIVITY_FIELDS, DAILY_FIELDS, WELLNESS_FIELDS, IntervalsClient, IntervalsSync,
    activity_record, health_snapshot, import_records, sync_status)
from wearable_notice import render_notice


class IntervalsTest(unittest.TestCase):
    def setUp(self):
        self.test_root = Path(os.environ.get('HEALTH_TEST_ROOT', Path(__file__).parent / '_test-data'))
        self.test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.test_root)
        self.h = Health(self.temp.name, network=False)
        self.athlete = 'i123456'
        self.day = '2026-10-10'
        self.activity = {
            'id': 'i123', 'source': 'HUAWEI', 'type': 'Walk',
            'start_date_local': self.day + 'T19:08:00',
            'start_date': self.day + 'T11:08:00Z', 'timezone': 'Asia/Shanghai',
            'moving_time': 881, 'elapsed_time': 900, 'distance': 1000,
            'average_heartrate': 112, 'max_heartrate': 135, 'calories': 60}
        self.wellness = {'id': self.day, 'steps': 6786, 'restingHR': 78, 'weight': 74}

    def tearDown(self):
        assert Path(self.temp.name).resolve().is_relative_to(self.test_root.resolve())
        self.temp.cleanup()

    def ingest(self, activities=None, wellness=None):
        return import_records(self.h, self.athlete,
                              [self.activity] if activities is None else activities,
                              [self.wellness] if wellness is None else wellness, self.day, self.day)

    def snapshot(self):
        return health_snapshot(self.h, self.day, self.day)

    def test_existing_database_migrates_without_losing_records(self):
        with tempfile.TemporaryDirectory(dir=self.test_root) as root:
            db = Path(root) / 'health.sqlite3'
            with closing(sqlite3.connect(db)) as c, c:
                c.execute('CREATE TABLE measures(source TEXT PRIMARY KEY,day TEXT NOT NULL,'
                          'at TEXT NOT NULL,weight_kg REAL,waist_cm REAL)')
                c.execute('INSERT INTO measures VALUES(?,?,?,?,?)',
                          ('manual', self.day, 'earlier', 73, 80))
            h = Health(root, network=False)
            row = health_snapshot(h, self.day, self.day)['daily'][0]
            self.assertEqual((row['weight_kg'], row['waist_cm']), (73, 80))
            Health(root, network=False)

    def test_core_fields_units_and_missing_sleep(self):
        result = self.ingest()
        self.assertEqual((result['inserted_workouts'], result['inserted_days']), (1, 1))
        snapshot = self.snapshot()
        daily, workout = snapshot['daily'][0], snapshot['workouts'][0]
        self.assertEqual((daily['steps'], daily['weight_kg'], daily['resting_hr_bpm']), (6786, 74, 78))
        self.assertIsNone(daily['sleep_seconds'])
        self.assertIsNone(daily['sleep_score'])
        self.assertEqual(snapshot['aggregates']['sleep_seconds']['recorded_days'], 0)
        self.assertEqual((workout['distance_m'], workout['average_hr_bpm']), (1000, 112))
        self.assertEqual(workout['duration_seconds'], 881)
        self.assertIsNone(workout['rpe'])
        self.assertFalse(workout['eatback_applied'])

    def test_replay_keeps_one_record_and_no_extra_backup(self):
        self.ingest()
        backup_count = len(list((self.h.root / 'backups').glob('*.sqlite3')))
        for _ in range(10):
            result = self.ingest()
            self.assertFalse(result['changed'])
        self.assertEqual(self.snapshot()['workout_count'], 1)
        self.assertEqual(len(self.snapshot()['daily']), 1)
        self.assertEqual(len(list((self.h.root / 'backups').glob('*.sqlite3'))), backup_count)

    def test_upstream_revision_updates_original(self):
        self.ingest()
        self.activity['distance'] = 1100
        self.wellness['steps'] = 8000
        result = self.ingest()
        self.assertEqual((result['updated_workouts'], result['updated_days']), (1, 1))
        self.assertEqual(self.snapshot()['workout_count'], 1)
        self.assertEqual(self.snapshot()['daily'][0]['steps'], 8000)

    def test_upstream_explicit_missing_clears_stale_values(self):
        self.ingest()
        self.ingest(wellness=[{'id': self.day}])
        daily = self.snapshot()['daily'][0]
        self.assertIsNone(daily['steps'])
        self.assertIsNone(daily['weight_kg'])

    def test_empty_days_are_not_invented(self):
        result = self.ingest(activities=[], wellness=[{'id': self.day}])
        self.assertFalse(result['changed'])
        self.assertEqual(self.snapshot()['daily'], [])

    def test_zero_steps_is_present_not_missing(self):
        self.wellness['steps'] = 0
        self.ingest()
        self.assertEqual(self.snapshot()['daily'][0]['steps'], 0)
        self.assertEqual(self.snapshot()['aggregates']['steps']['recorded_days'], 1)

    def test_manual_weight_wins_after_device_refresh(self):
        self.ingest()
        self.h.record_measure({'day': self.day, 'weight_kg': 73, 'waist_cm': 80}, 'user-message')
        self.wellness['weight'] = 75
        self.ingest()
        row = self.snapshot()['daily'][0]
        self.assertEqual((row['weight_kg'], row['waist_cm']), (73, 80))
        self.assertEqual(self.h.trend(self.day)['weight_series'][0]['weight_kg'], 73)

    def test_workout_correction_survives_upstream_update_and_replay(self):
        self.ingest()
        record = self.snapshot()['workouts'][0]['record_id']
        self.h.workout({'record_id': record, 'minutes': 10, 'rpe': 3, 'feedback': '膝盖不适'}, 'feedback')
        self.activity['moving_time'] = 1000
        self.activity['distance'] = 1200
        self.ingest()
        self.assertFalse(self.ingest()['changed'])
        workout = self.snapshot()['workouts'][0]
        self.assertEqual((workout['minutes'], workout['duration_seconds']), (10, 600))
        self.assertEqual((workout['rpe'], workout['feedback']), (3, '膝盖不适'))
        self.assertEqual(workout['device_duration_seconds'], 1000)
        self.assertEqual(workout['distance_m'], 1200)

    def test_concurrent_import_does_not_duplicate(self):
        with patch.object(self.h, 'backup', return_value={}):
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: self.ingest(), range(8)))
        self.assertEqual(self.snapshot()['workout_count'], 1)
        self.assertEqual(len(self.snapshot()['daily']), 1)

    def test_invalid_batch_rolls_back_all_records_and_checkpoint(self):
        invalid = {**self.wellness, 'id': '2026-10-11', 'steps': -1}
        with self.assertRaises(ValueError):
            self.ingest(wellness=[self.wellness, invalid])
        self.assertEqual(self.snapshot()['workout_count'], 0)
        self.assertIsNone(sync_status(self.h)['last_success_at'])

    def test_duplicate_external_ids_in_batch_are_rejected(self):
        with self.assertRaises(ValueError):
            self.ingest(activities=[self.activity, self.activity])
        self.assertEqual(self.snapshot()['workout_count'], 0)

    def test_bools_nan_and_wrong_units_are_rejected(self):
        for invalid in (True, float('nan'), -1, '6786', 1.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.ingest(wellness=[{**self.wellness, 'steps': invalid}])

    def test_utc_cross_midnight_uses_local_day(self):
        raw = {**self.activity, 'start_date': '2026-10-09T23:00:00Z'}
        del raw['start_date_local']
        self.assertEqual(activity_record(raw, self.athlete)['day'], '2026-10-10')

    def test_empty_stub_is_not_zero_workout(self):
        result = self.ingest(activities=[{'id': 'i234'}], wellness=[])
        self.assertFalse(result['changed'])
        self.assertEqual(self.snapshot()['workout_count'], 0)

    def test_database_reopen_retains_imported_data(self):
        self.ingest()
        reopened = Health(self.temp.name, network=False)
        self.assertEqual(reopened.summary(self.day)['wearable']['workout_count'], 1)
        self.assertEqual(reopened.summary(self.day)['wearable']['daily'][0]['steps'], 6786)

    def test_backup_contains_device_records(self):
        self.ingest()
        backup = sorted((self.h.root / 'backups').glob('*.sqlite3'))[-1]
        with closing(sqlite3.connect(backup)) as c:
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(c.execute('SELECT count(*) FROM workouts').fetchone()[0], 1)
            self.assertIn('6786', c.execute('SELECT data FROM measures').fetchone()[0])

    def test_existing_queries_include_device_data_without_target_changes(self):
        before_targets = self.h.targets_for(self.day)
        self.ingest()
        self.assertEqual(self.h.dispatch('week', {'day': self.day}, 'query')['wearable']['workout_count'], 1)
        self.assertEqual(self.h.next_meal(self.day)['daily']['wearable']['daily'][0]['steps'], 6786)
        self.assertEqual(self.h.targets_for(self.day), before_targets)

    def test_actual_scheduled_sync_uses_recent_window_and_does_not_write_upstream(self):
        sync = IntervalsSync(self.h, {'athlete_id': self.athlete, 'api_key': 'synthetic-test-key'})
        captured = []
        def fake_fetch(oldest, newest):
            captured.append((oldest, newest))
            return [], []
        sync.client.fetch = fake_fetch
        sync.once()
        sync.once()
        self.assertEqual((date_delta(*captured[0]), date_delta(*captured[1])), (29, 6))

    def test_sync_recovers_gap_longer_than_seven_days(self):
        previous = (datetime.now(timezone.utc) - timedelta(days=12)).isoformat()
        with self.h.write() as c:
            c.execute('INSERT INTO settings VALUES(?,?)',
                      ('intervals_sync', json.dumps({'last_success_at': previous})))
        sync = IntervalsSync(self.h, {'athlete_id': self.athlete, 'api_key': 'synthetic-test-key'})
        captured = []
        sync.client.fetch = lambda oldest, newest: (captured.append((oldest, newest)) or [], [])
        sync.once()
        self.assertGreaterEqual(date_delta(*captured[0]), 12)

    def test_client_limits_requested_fields_and_uses_only_get(self):
        client = IntervalsClient(self.athlete, 'synthetic-test-key')
        captured = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit): return b'[]'
        def fake_open(request, timeout):
            captured.append(request)
            return Response()
        with patch.object(client.opener, 'open', fake_open):
            client.fetch(self.day, self.day)
        self.assertEqual([r.get_method() for r in captured], ['GET', 'GET'])
        self.assertIn('/wellness.json?', captured[1].full_url)
        self.assertNotIn('bodyFat', captured[1].full_url)
        self.assertNotIn('menstrual', captured[1].full_url)
        self.assertNotIn('GPS', captured[0].full_url)

    def test_notification_preserves_missing_and_zero_and_excludes_secrets(self):
        self.wellness['steps'] = 0
        self.ingest()
        notice = render_notice(sync_status(self.h), self.h.summary(self.day))
        self.assertIn('步数 0步', notice)
        self.assertIn('睡眠 未收到', notice)
        self.assertNotIn('API_KEY', notice)
        self.assertNotIn('i123456', notice)

    def test_erasure_pauses_sync_and_rejects_inflight_import(self):
        self.ingest()
        result = self.h.erase('删除全部健康数据')
        self.assertTrue(result['device_sync_paused'])
        sync = IntervalsSync(self.h, {'athlete_id': self.athlete, 'api_key': 'synthetic-test-key'})
        with patch.object(sync.client, 'fetch') as fetch:
            self.assertTrue(sync.once()['paused'])
            fetch.assert_not_called()
        self.assertTrue(self.ingest()['paused'])
        self.assertEqual(self.snapshot()['workout_count'], 0)
        self.assertEqual(self.snapshot()['daily'], [])


def date_delta(oldest, newest):
    return (datetime.fromisoformat(newest) - datetime.fromisoformat(oldest)).days


if __name__ == '__main__':
    unittest.main()
