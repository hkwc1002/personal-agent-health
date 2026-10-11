"""Intervals 日级摘要同步；只读取官方 API，不保存轨迹或凭据。"""
from datetime import date, datetime, timedelta, timezone
import base64
import json
import math
import os
from pathlib import Path
import statistics
import threading
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

API_ROOT = 'https://intervals.icu/api/v1'
TZ = ZoneInfo(os.environ.get('HEALTH_TIMEZONE', 'Asia/Shanghai'))
SYNC_KEY = 'intervals_sync'
ACTIVITY_FIELDS = (
    'id', 'source', 'type', 'start_date_local', 'start_date', 'timezone',
    'moving_time', 'elapsed_time', 'distance', 'average_heartrate',
    'max_heartrate', 'calories')
WELLNESS_FIELDS = ('id', 'updated', 'weight', 'steps', 'sleepSecs', 'sleepScore', 'restingHR')
DAILY_FIELDS = {
    'steps': ('steps', 0, 200000),
    'sleep_seconds': ('sleepSecs', 0, 172800),
    'sleep_score': ('sleepScore', 0, 100),
    'resting_hr_bpm': ('restingHR', 1, 250)}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def iso_day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('同步日期必须为YYYY-MM-DD')
    return value


def metric(value, lower, upper):
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, (float, int))
            or not math.isfinite(value) or not lower <= value <= upper):
        raise ValueError('设备指标数值或单位不合法')
    return value


def identifier(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 100
            or not all(c.isascii() and (c.isalnum() or c in '-_') for c in value)):
        raise ValueError('外部记录标识不合法')
    return value


def activity_record(raw, athlete):
    activity_id = identifier(raw.get('id'))
    # Strava 等来源可能只返回空桩；不能据此创造一条零时长运动。
    if raw.get('start_date_local') is None and raw.get('start_date') is None:
        return None
    start = raw.get('start_date')
    local = raw.get('start_date_local')
    if local is not None:
        dt = datetime.fromisoformat(local.replace('Z', '+00:00'))
        when = dt.astimezone(TZ).date().isoformat() if dt.tzinfo else dt.date().isoformat()
    else:
        dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError('UTC运动时间缺少时区')
        when = dt.astimezone(TZ).date().isoformat()
    seconds = raw.get('moving_time')
    if seconds is None:
        seconds = raw.get('elapsed_time')
    seconds = metric(seconds, 0, 172800)
    if seconds is None:
        raise ValueError('运动缺少实际时长')
    sport = raw.get('type')
    if not isinstance(sport, str) or len(sport) > 80:
        raise ValueError('运动类型不合法')
    category = {
        'Walk': 'walk', 'Hike': 'walk', 'Run': 'run', 'TrailRun': 'run',
        'VirtualRun': 'run', 'Ride': 'cycle', 'VirtualRide': 'cycle',
        'MountainBikeRide': 'cycle', 'GravelRide': 'cycle', 'EBikeRide': 'cycle',
        'WeightTraining': 'strength', 'Yoga': 'mobility', 'Stretching': 'mobility',
        'Pilates': 'mobility'}.get(sport, 'other')
    upstream = raw.get('source')
    if upstream is not None and (not isinstance(upstream, str) or len(upstream) > 80):
        raise ValueError('运动来源不合法')
    data = {
        'provider': 'Intervals.icu', 'upstream_source': upstream,
        'external_id': activity_id, 'sport_type': sport, 'activity': category,
        'start_at': start, 'start_local': local, 'timezone': raw.get('timezone'),
        'duration_seconds': seconds, 'minutes': round(seconds / 60, 4),
        'elapsed_seconds': metric(raw.get('elapsed_time'), 0, 172800),
        'distance_m': metric(raw.get('distance'), 0, 2000000),
        'average_hr_bpm': metric(raw.get('average_heartrate'), 0, 250),
        'max_hr_bpm': metric(raw.get('max_heartrate'), 0, 250),
        'reported_energy_kcal': metric(raw.get('calories'), 0, 30000),
        'rpe': None, 'feedback': '', 'gross_kcal': None, 'net_kcal': None,
        'eatback_applied': False,
        'energy_note': '设备或上游报告值；不视为实测代谢，不抵消饮食摄入'}
    return {'source': f'intervals:{athlete}:activity:{activity_id}', 'day': when, 'data': data}


def wellness_record(raw, athlete):
    when = iso_day(raw.get('id'))
    data = {'provider': 'Intervals.icu', 'upstream_source': None,
            'source_note': 'Intervals每日合并摘要，接口未提供每个指标的设备来源',
            'external_id': when, 'upstream_updated_at': raw.get('updated')}
    for field, (upstream, lower, upper) in DAILY_FIELDS.items():
        data[field] = metric(raw.get(upstream), lower, upper)
    for field in ('steps', 'sleep_seconds'):
        if data[field] is not None and int(data[field]) != data[field]:
            raise ValueError('步数和睡眠秒数必须为整数')
    weight = metric(raw.get('weight'), 15, 350)
    return {'source': f'intervals:{athlete}:wellness:{when}', 'day': when,
            'weight_kg': weight, 'data': data}


def import_records(health, athlete, activities, wellness, oldest, newest):
    """先校验整批，随后一次事务更新；不改变饮食、档案或营养目标。"""
    athlete, oldest, newest = identifier(athlete), iso_day(oldest), iso_day(newest)
    if oldest > newest:
        raise ValueError('同步日期范围不合法')
    if not isinstance(activities, list) or not isinstance(wellness, list):
        raise ValueError('官方API应返回记录列表')
    if len(activities) > 5000 or len(wellness) > 366:
        raise ValueError('同步结果过大')
    normalized = {'workouts': [], 'measures': []}
    skipped = 0
    for table, records, normalize in (
            ('workouts', activities, activity_record), ('measures', wellness, wellness_record)):
        seen = set()
        for raw in records:
            if not isinstance(raw, dict):
                raise ValueError('外部记录必须为对象')
            record = normalize(raw, athlete)
            if record is None:
                skipped += 1
                continue
            if not oldest <= record['day'] <= newest:
                raise ValueError('外部记录超出请求日期范围')
            if record['source'] in seen:
                raise ValueError('同一批次存在重复外部标识')
            seen.add(record['source'])
            normalized[table].append(record)
    changed_at = timestamp()
    result = {'oldest': oldest, 'newest': newest, 'inserted_workouts': 0,
              'updated_workouts': 0, 'inserted_days': 0, 'updated_days': 0,
              'unchanged': 0, 'skipped': skipped}
    with health.write() as c:
        state_row = c.execute('SELECT value FROM settings WHERE key=?', (SYNC_KEY,)).fetchone()
        state = json.loads(state_row[0]) if state_row else {}
        if state.get('disabled'):
            return {'changed': False, 'paused': True, 'reason': '用户清空健康数据后已暂停同步'}
        for table, records in normalized.items():
            for record in records:
                previous = c.execute('SELECT * FROM ' + table + ' WHERE source=?',
                                     (record['source'],)).fetchone()
                data = dict(record['data'])
                if (table == 'measures' and previous is None and record['weight_kg'] is None
                        and all(data[k] is None for k in DAILY_FIELDS)):
                    result['skipped'] += 1
                    continue
                old_data = json.loads(previous['data']) if previous else {}
                overrides = old_data.get('manual_overrides', {})
                if overrides:
                    data['manual_overrides'] = overrides
                    data['manual_history'] = old_data.get('manual_history', [])
                    data.update(overrides)
                    if 'minutes' in overrides:
                        data['device_duration_seconds'] = data['duration_seconds']
                        data['duration_seconds'] = round(overrides['minutes'] * 60, 4)
                comparison = {k: v for k, v in old_data.items() if k != 'synced_at'}
                equal = previous and comparison == data
                if table == 'measures':
                    equal = equal and previous['weight_kg'] == record['weight_kg']
                if equal:
                    result['unchanged'] += 1
                    continue
                data['synced_at'] = changed_at
                if table == 'workouts':
                    c.execute('''INSERT INTO workouts(source,day,at,data) VALUES(?,?,?,?)
                        ON CONFLICT(source) DO UPDATE SET day=excluded.day,at=excluded.at,data=excluded.data''',
                        (record['source'], record['day'], changed_at, encoded(data)))
                    result['updated_workouts' if previous else 'inserted_workouts'] += 1
                else:
                    c.execute('''INSERT INTO measures(source,day,at,weight_kg,waist_cm,data)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET day=excluded.day,
                        at=excluded.at,weight_kg=excluded.weight_kg,data=excluded.data''',
                        (record['source'], record['day'], changed_at, record['weight_kg'], None, encoded(data)))
                    result['updated_days' if previous else 'inserted_days'] += 1
        state.update({'provider': 'Intervals.icu', 'athlete_id': athlete,
                      'last_success_at': changed_at, 'last_error': None, 'last_result': result})
        c.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', (SYNC_KEY, encoded(state)))
    result['changed'] = bool(sum(result[k] for k in (
        'inserted_workouts', 'updated_workouts', 'inserted_days', 'updated_days')))
    if result['changed']:
        health.backup()
        snapshots = sorted((health.root / 'backups').glob('health-*.sqlite3'))
        for old in snapshots[:-30]:
            if old.is_file() and not old.is_symlink():
                old.unlink()
    return result


def sync_status(health):
    with health.connect() as c:
        row = c.execute('SELECT value FROM settings WHERE key=?', (SYNC_KEY,)).fetchone()
    return json.loads(row[0]) if row else {'provider': 'Intervals.icu', 'last_success_at': None,
                                         'configured': False}


def health_snapshot(health, oldest, newest):
    oldest, newest = iso_day(oldest), iso_day(newest)
    with health.connect() as c:
        measurements = [dict(r) for r in c.execute(
            'SELECT * FROM measures WHERE day BETWEEN ? AND ? ORDER BY day,at,source', (oldest, newest))]
        workouts = [dict(r) for r in c.execute(
            'SELECT * FROM workouts WHERE day BETWEEN ? AND ? ORDER BY day,at,source', (oldest, newest))]
    days = {}
    for row in measurements:
        data = json.loads(row['data'])
        entry = days.setdefault(row['day'], {'day': row['day'], 'weight_kg': None, 'waist_cm': None,
                                            **{k: None for k in DAILY_FIELDS}, 'sources': {}})
        for field in ('weight_kg', 'waist_cm', *DAILY_FIELDS):
            value = row.get(field) if field in ('weight_kg', 'waist_cm') else data.get(field)
            previous_source = entry['sources'].get(field, '')
            # 设备晚到更新不能覆盖同一天用户明确记录的身体数值。
            if value is not None and (not row['source'].startswith('intervals:')
                    or not previous_source or previous_source.startswith('intervals:')):
                entry[field] = value
                entry['sources'][field] = row['source']
        if data.get('synced_at'):
            entry['synced_at'] = data['synced_at']
    activities = [{'record_id': r['source'], 'day': r['day'], **json.loads(r['data'])} for r in workouts]
    aggregates = {}
    for field in ('steps', 'sleep_seconds', 'sleep_score', 'resting_hr_bpm'):
        values = [r[field] for r in days.values() if r[field] is not None]
        aggregates[field] = {'recorded_days': len(values),
                             'mean': round(statistics.mean(values), 2) if values else None}
    return {'start': oldest, 'end': newest, 'daily': list(days.values()), 'workouts': activities,
            'workout_count': len(activities),
            'workout_minutes': round(sum(w['minutes'] for w in activities), 2),
            'aggregates': aggregates, 'sync': sync_status(health),
            'coverage': '仅收到的摘要；缺失不是零，当天数据可能继续补齐',
            'automatic_target_change': False, 'exercise_eatback_applied': False}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('拒绝凭据请求重定向')


class IntervalsClient:
    def __init__(self, athlete, api_key):
        self.athlete = identifier(athlete)
        if not isinstance(api_key, str) or not 8 <= len(api_key) <= 256:
            raise ValueError('Intervals密钥未配置或格式不合法')
        self.authorization = 'Basic ' + base64.b64encode(('API_KEY:' + api_key).encode()).decode()
        self.opener = urllib.request.build_opener(NoRedirect())

    def read(self, resource, oldest, newest, fields):
        url = (API_ROOT + '/athlete/' + self.athlete + '/' + resource + '?' +
               urllib.parse.urlencode({'oldest': oldest, 'newest': newest, 'fields': ','.join(fields)}))
        request = urllib.request.Request(url, headers={
            'Authorization': self.authorization, 'Accept': 'application/json',
            'User-Agent': 'HermesPersonalHealth (single-user read-only sync)'}, method='GET')
        try:
            with self.opener.open(request, timeout=25) as response:
                raw = response.read(4000001)
            if len(raw) > 4000000:
                raise ValueError('Intervals返回过大')
            data = json.loads(raw)
            if not isinstance(data, list):
                raise ValueError('Intervals返回格式不合法')
            return data
        except urllib.error.HTTPError as exc:
            raise ValueError('Intervals读取失败，HTTP ' + str(exc.code)) from None
        except (urllib.error.URLError, OSError):
            raise ValueError('Intervals网络读取失败') from None

    def fetch(self, oldest, newest):
        return (self.read('activities', oldest, newest + 'T23:59:59', ACTIVITY_FIELDS),
                self.read('wellness.json', oldest, newest, WELLNESS_FIELDS))


class IntervalsSync:
    """复用健康进程的后台线程，不注册 Hermes cron 或新服务。"""
    def __init__(self, health, config):
        self.health = health
        self.client = IntervalsClient(config['athlete_id'], config['api_key'])
        self.period = int(metric(config.get('interval_seconds', 3600), 300, 86400))
        self.backfill_days = int(metric(config.get('initial_days', 30), 7, 90))
        self.stop = threading.Event()

    def once(self):
        today = datetime.now(TZ).date()
        previous = sync_status(self.health)
        if previous.get('disabled'):
            return {'changed': False, 'paused': True}
        days = 7 if previous.get('last_success_at') else self.backfill_days
        start = today - timedelta(days=days - 1)
        if previous.get('last_success_at'):
            last_day = datetime.fromisoformat(previous['last_success_at']).astimezone(TZ).date()
            start = min(start, last_day - timedelta(days=1))
            if (today - start).days > 365:
                raise ValueError('同步中断超过一年，需明确历史补录范围')
        oldest, newest = start.isoformat(), today.isoformat()
        activities, wellness = self.client.fetch(oldest, newest)
        return import_records(self.health, self.client.athlete, activities, wellness, oldest, newest)

    def loop(self):
        while not self.stop.is_set():
            try:
                result = self.once()
                print(encoded({'intervals_sync': 'ok', **result}), flush=True)
            except Exception as exc:
                # 不记录密钥、响应正文或包含凭据的异常信息。
                error = str(exc)[:160] if isinstance(exc, ValueError) else '设备同步内部错误'
                with self.health.write() as c:
                    state = sync_status(self.health)
                    state.update({'last_attempt_at': timestamp(), 'last_error': error})
                    c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (SYNC_KEY, encoded(state)))
                print(encoded({'intervals_sync': 'failed', 'error': error}), flush=True)
            self.stop.wait(self.period)

    def start(self):
        thread = threading.Thread(target=self.loop, name='intervals-sync', daemon=True)
        thread.start()
        return thread


def configured_sync(health):
    path = os.environ.get('HEALTH_INTERVALS_CONFIG')
    if not path:
        return None
    config_path = Path(path)
    if not config_path.exists() and not config_path.is_symlink():
        return None
    if config_path.is_symlink() or not config_path.is_file():
        raise ValueError('设备同步凭据文件不可用')
    if os.name != 'nt' and config_path.stat().st_mode & 0o077:
        raise ValueError('设备同步凭据文件必须仅服务账号可读')
    config = json.loads(config_path.read_text(encoding='utf-8'))
    return IntervalsSync(health, config)
