"""个人健康账本：默认保存估算，确定性计算，保留来源、缺失与修订。"""
from contextlib import contextmanager, closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import urllib.error
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo
from intervals_sync import health_snapshot, sync_status

NUTRIENTS = ('energy_kcal', 'protein_g', 'fat_g', 'carbs_g', 'sugars_g',
             'added_sugars_g', 'fiber_g', 'sodium_mg')
TZ = ZoneInfo(os.environ.get('HEALTH_TIMEZONE', 'Asia/Shanghai'))
FDC_IDS = {1003: 'protein_g', 1004: 'fat_g', 1005: 'carbs_g', 1079: 'fiber_g',
           2000: 'sugars_g', 1235: 'added_sugars_g', 1093: 'sodium_mg'}
PROFILE_FIELDS = {'age', 'sex', 'height_cm', 'weight_kg', 'waist_cm', 'goal_weight_kg',
                  'activity', 'strength_days', 'screening_complete', 'health_limits',
                  'preferences', 'allergies', 'equipment', 'available_minutes', 'goal_date'}
ACTIVITY = {'sedentary': 1.2, 'light': 1.375, 'moderate': 1.55, 'active': 1.725}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def now():
    return datetime.now(timezone.utc).isoformat()


def day(value=None):
    if value is None:
        return datetime.now(TZ).date().isoformat()
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('日期必须为YYYY-MM-DD')
    return value


def number(value, lo=0, hi=100000):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('数值必须为有限数字')
    if not lo <= value <= hi:
        raise ValueError('数值超出允许范围')
    return float(value)


def text(value, limit=1000):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError('文本类型或长度不合法')
    return value


def object_keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError('对象字段不合法或缺少必需字段')


def interval(value):
    object_keys(value, {'low', 'best', 'high'}, {'low', 'best', 'high'})
    values = {k: number(value[k], hi=20000) for k in ('low', 'best', 'high')}
    if not values['low'] <= values['best'] <= values['high']:
        raise ValueError('估算范围必须满足low≤best≤high')
    return values


def validate_density(value):
    object_keys(value, set(NUTRIENTS) | {'energy_kj', 'salt_g'})
    result = {k: None if value.get(k) is None else number(value[k]) for k in NUTRIENTS}
    if result['energy_kcal'] is None and value.get('energy_kj') is not None:
        result['energy_kcal'] = number(value['energy_kj']) / 4.184
    if result['sodium_mg'] is None and value.get('salt_g') is not None:
        result['sodium_mg'] = number(value['salt_g'], hi=100) * 400
    for k in ('protein_g', 'fat_g', 'carbs_g', 'sugars_g', 'added_sugars_g', 'fiber_g'):
        if result[k] is not None and result[k] > 100:
            raise ValueError('每100克成分不能超过100克')
    if result['energy_kcal'] is not None and result['energy_kcal'] > 1000:
        raise ValueError('每100克能量异常')
    if (result['added_sugars_g'] is not None and result['sugars_g'] is not None
            and result['added_sugars_g'] > result['sugars_g'] + 0.5):
        raise ValueError('添加糖不能高于总糖')
    return result


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Health:
    def __init__(self, root, network=True):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / 'health.sqlite3'
        self.network = network
        with self.connect() as c:
            c.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS profiles(id INTEGER PRIMARY KEY,at TEXT NOT NULL,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS targets(id TEXT PRIMARY KEY,day TEXT NOT NULL,at TEXT NOT NULL,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS foods(id TEXT PRIMARY KEY,at TEXT NOT NULL,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meals(id TEXT PRIMARY KEY,day TEXT NOT NULL,meal TEXT NOT NULL,
                    version INTEGER NOT NULL,status TEXT NOT NULL,data TEXT NOT NULL,at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS meals_day ON meals(day,status);
                CREATE TABLE IF NOT EXISTS events(source TEXT PRIMARY KEY,meal_id TEXT NOT NULL REFERENCES meals(id));
                CREATE TABLE IF NOT EXISTS images(hash TEXT NOT NULL,day TEXT NOT NULL,
                    meal_id TEXT NOT NULL REFERENCES meals(id),PRIMARY KEY(hash,meal_id));
                CREATE TABLE IF NOT EXISTS revisions(id INTEGER PRIMARY KEY,meal_id TEXT NOT NULL REFERENCES meals(id),
                    version INTEGER NOT NULL,at TEXT NOT NULL,reason TEXT NOT NULL,data TEXT NOT NULL,
                    UNIQUE(meal_id,version));
                CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS measures(source TEXT PRIMARY KEY,day TEXT NOT NULL,at TEXT NOT NULL,
                    weight_kg REAL,waist_cm REAL);
                CREATE TABLE IF NOT EXISTS workouts(source TEXT PRIMARY KEY,day TEXT NOT NULL,at TEXT NOT NULL,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS complete_days(day TEXT PRIMARY KEY,at TEXT NOT NULL);
            ''')
            if 'data' not in {r['name'] for r in c.execute('PRAGMA table_info(measures)')}:
                c.execute("ALTER TABLE measures ADD COLUMN data TEXT NOT NULL DEFAULT '{}'")
        if os.name != 'nt':
            os.chmod(self.db, 0o600)

    def connect(self):
        c = sqlite3.connect(self.db, timeout=20, factory=ClosingConnection)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA busy_timeout=20000')
        return c

    @contextmanager
    def write(self):
        c = self.connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def request_json(self, url):
        if not self.network:
            raise ValueError('当前测试环境不联网')
        req = urllib.request.Request(url, headers={'User-Agent': 'HermesPersonalHealth/1.0 (single-user)'})
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                content = response.read(4000001)
            if len(content) > 4000000:
                raise ValueError('数据服务返回过大')
            return json.loads(content)
        except (OSError, urllib.error.URLError, ValueError):
            raise ValueError('营养数据查询失败；可先保存未知营养的估算记录，稍后修正') from None

    def search_foods(self, query):
        query = text(query, 160).strip()
        if not query:
            raise ValueError('查询词不能为空')
        with self.connect() as c:
            cached = [json.loads(r['data']) for r in c.execute('SELECT data FROM foods')]
        separators = str.maketrans({c: ' ' for c in ',()/-;'})
        terms = query.casefold().translate(separators).split()
        if not terms:
            raise ValueError('查询词需要包含食品名称')
        matches = [f for f in cached if query in f.get('aliases', [])
                   or all(t in f['name'].casefold().translate(separators).split() for t in terms)]
        matches.sort(key=lambda f: (query not in f.get('aliases', []),
            not f['name'].casefold().startswith(terms[0]), len(f['name']), f['name']))
        matches = matches[:10]
        if matches:
            return {'foods': matches, 'cached': True}
        if not self.network:
            return {'foods': [], 'cached': True}
        params = urllib.parse.urlencode({'query': query, 'pageSize': 8,
            'api_key': self.fdc_key()})
        data = self.request_json('https://api.nal.usda.gov/fdc/v1/foods/search?' + params)
        return {'foods': [{'id': 'fdc:' + str(f['fdcId']), 'name': f['description'],
            'data_type': f.get('dataType'), 'note': '选择符合生熟状态和食品类型的条目，再查询详情'}
            for f in data.get('foods', [])], 'cached': False}

    def food(self, food_id):
        food_id = text(food_id, 80)
        with self.connect() as c:
            row = c.execute('SELECT data FROM foods WHERE id=?', (food_id,)).fetchone()
        if row:
            return json.loads(row['data'])
        if food_id.startswith('fdc:') and food_id[4:].isdigit():
            params = urllib.parse.urlencode({'api_key': self.fdc_key()})
            data = self.request_json('https://api.nal.usda.gov/fdc/v1/food/' + food_id[4:] + '?' + params)
            values = {k: None for k in NUTRIENTS}
            energies = {}
            for v in data.get('foodNutrients', []):
                nutrient = v.get('nutrient', {})
                ni, unit, amount = nutrient.get('id'), nutrient.get('unitName', '').lower(), v.get('amount')
                if amount is None:
                    continue
                if ni in (1008, 2047, 2048) and unit == 'kcal':
                    energies[ni] = amount
                key = FDC_IDS.get(ni)
                if key and unit == ('mg' if key == 'sodium_mg' else 'g'):
                    values[key] = amount
            values['energy_kcal'] = next((energies[k] for k in (1008, 2048, 2047) if k in energies), None)
            food = {'id': food_id, 'name': data['description'], 'basis': 'per_100_g',
                'nutrients': validate_density(values), 'carb_definition': 'by_difference',
                'source': {'kind': 'USDA FDC', 'url': 'https://fdc.nal.usda.gov/food-details/' + food_id[4:],
                           'data_type': data.get('dataType'), 'retrieved_at': now()}, 'aliases': []}
        elif food_id.startswith('off:') and food_id[4:].isdigit() and 8 <= len(food_id[4:]) <= 14:
            data = self.request_json('https://world.openfoodfacts.org/api/v3/product/' + food_id[4:] + '.json')
            product = data.get('product', {})
            n = product.get('nutriments', {})
            if not product or product.get('nutrition_data_per') not in ('100g', 'serving'):
                raise ValueError('包装食品缺少可确认的营养计量基础，请读取实际标签')
            if str(product.get('quantity', '')).lower().endswith(('ml', 'cl', 'l')):
                raise ValueError('液体标签须按毫升读取，不能默认1毫升等于1克')
            values = {'energy_kcal': n.get('energy-kcal_100g'), 'protein_g': n.get('proteins_100g'),
                'fat_g': n.get('fat_100g'), 'carbs_g': n.get('carbohydrates_100g'),
                'sugars_g': n.get('sugars_100g'), 'added_sugars_g': n.get('added-sugars_100g'),
                'fiber_g': n.get('fiber_100g'),
                'sodium_mg': None if n.get('sodium_100g') is None else n['sodium_100g'] * 1000}
            food = {'id': food_id, 'name': product.get('product_name') or food_id,
                'basis': 'per_100_g', 'nutrients': validate_density(values), 'carb_definition': 'available',
                'source': {'kind': 'Open Food Facts label', 'url': 'https://world.openfoodfacts.org/product/' + food_id[4:],
                           'retrieved_at': now(), 'license': 'ODbL; 众包，须与实际包装核对'}, 'aliases': []}
        else:
            raise ValueError('食品标识必须为已保存食品、fdc:编号或off:条码')
        self.cache_food(food)
        return food

    def cache_food(self, food):
        with self.write() as c:
            c.execute('INSERT OR REPLACE INTO foods VALUES(?,?,?)', (food['id'], now(), dumps(food)))

    @staticmethod
    def fdc_key():
        path = os.environ.get('HEALTH_FDC_KEY_FILE')
        if path and Path(path).is_file():
            return Path(path).read_text(encoding='utf-8').strip()
        return os.environ.get('FDC_API_KEY', 'DEMO_KEY')

    def normalize_items(self, items):
        if not isinstance(items, list) or not 1 <= len(items) <= 30:
            raise ValueError('一餐应有1至30个食物项目')
        result, keys = [], set()
        for item in items:
            object_keys(item, {'key', 'name', 'mass_g', 'volume_ml', 'food_id', 'label', 'unknowns',
                'confidence', 'cooking', 'carb_definition'}, {'name'})
            name = text(item['name'], 120)
            key = text(item.get('key', name), 120)
            if key in keys:
                raise ValueError('同一餐的项目key必须不同，多张图同一菜只保留一个项目')
            keys.add(key)
            is_liquid = 'volume_ml' in item
            if is_liquid == ('mass_g' in item):
                raise ValueError('食物须提供mass_g或volume_ml之一，不混用克和毫升')
            mass = interval(item['volume_ml'] if is_liquid else item['mass_g'])
            unknowns = item.get('unknowns', [])
            if not isinstance(unknowns, list) or len(unknowns) > 12:
                raise ValueError('未知项应为最多12条文本')
            unknowns = [text(x, 240) for x in unknowns]
            confidence = item.get('confidence', 'low')
            if confidence not in ('low', 'medium', 'high'):
                raise ValueError('confidence应为low/medium/high，不是概率')
            density, source, food_id, carb = {k: None for k in NUTRIENTS}, {'kind': 'unknown'}, None, 'unknown'
            if item.get('food_id') and not is_liquid:
                try:
                    f = self.food(item['food_id'])
                    density, source, food_id, carb = f['nutrients'], f['source'], f['id'], f.get('carb_definition', 'unknown')
                except ValueError:
                    unknowns.append('食品详情查询失败，营养未计全')
            if item.get('label') is not None:
                label = item['label']
                object_keys(label, {'basis', 'basis_amount', 'nutrients', 'description'}, {'basis', 'nutrients'})
                basis = label['basis']
                if basis not in ('per_100_ml', 'per_serving_ml') if is_liquid else basis not in ('per_100_g', 'per_serving_g'):
                    raise ValueError('标签计量基础须与实际克数或毫升一致')
                values = label['nutrients']
                if basis in ('per_serving_g', 'per_serving_ml'):
                    amount = number(label.get('basis_amount'), 0.1, 2000)
                    values = {k: None if v is None else number(v) * 100 / amount for k,v in values.items()}
                density = validate_density(values)
                source = {'kind': 'user_visible_label', 'description': text(label.get('description', '照片营养标签'), 400),
                          'basis': basis, 'retrieved_at': now()}
                food_id, carb = None, item.get('carb_definition', 'unknown')
            if carb not in ('by_difference', 'available', 'unknown'):
                raise ValueError('未知碳水定义')
            if carb == 'by_difference' and density['carbs_g'] is not None and density['fiber_g'] is not None:
                source = {**source, 'carbs_original_g': density['carbs_g'],
                          'carb_conversion': '已知差减碳水减去纤维，得到可利用碳水近似值，非独立实验测量'}
                density = {**density, 'carbs_g': max(0, density['carbs_g']-density['fiber_g'])}
                carb = 'available'
            nutrients = {k: None if density[k] is None else {
                b: round(density[k] * mass[b] / 100, 5) for b in ('low', 'best', 'high')} for k in NUTRIENTS}
            result.append({'key': key, 'name': name, 'mass_g': None if is_liquid else mass,
                'volume_ml': mass if is_liquid else None, 'food_id': food_id,
                'density': density, 'nutrients': nutrients, 'source': source, 'carb_definition': carb,
                'confidence': confidence, 'cooking': text(item.get('cooking', 'unknown'), 120),
                'unknowns': unknowns})
        return result

    def profile(self):
        with self.connect() as c:
            row = c.execute('SELECT data FROM profiles ORDER BY id DESC LIMIT 1').fetchone()
        return json.loads(row['data']) if row else {}

    def targets_for(self, when):
        with self.connect() as c:
            row = c.execute('SELECT data FROM targets WHERE day<=? ORDER BY day DESC,at DESC LIMIT 1', (when,)).fetchone()
        return json.loads(row['data']) if row else None

    def auto_targets(self, profile):
        required = ('age', 'sex', 'height_cm', 'weight_kg', 'activity')
        if not all(k in profile for k in required):
            return {'eligible': False, 'reason': '档案尚缺年龄、计算性别、身高、体重或活动量'}
        if (profile['age'] < 18 or not profile.get('screening_complete')
                or profile.get('health_limits') or profile['weight_kg'] / (profile['height_cm']/100)**2 < 18.5):
            return {'eligible': False, 'reason': '需要完成健康限制评估；本版不自动制定特殊人群减脂热量'}
        w, h, a = profile['weight_kg'], profile['height_cm'], profile['age']
        ree = 10*w + 6.25*h - 5*a + (5 if profile['sex'] == 'male' else -161)
        tdee = ree * ACTIVITY[profile['activity']]
        deficit = min(400, tdee * 0.15) if profile.get('goal_weight_kg', w) < w else 0
        kcal = round((tdee - deficit) / 10) * 10
        reference_weight = min(w, 25 * (h/100)**2)
        protein = round(reference_weight * (1.6 if profile.get('strength_days', 0) >= 2 else 1.2))
        fat = round(kcal * 0.30 / 9)
        return {'eligible': True, 'energy_kcal': kcal, 'protein_g': protein, 'fat_g': fat,
                'carbs_g': round(max(0, kcal - protein*4 - fat*9) / 4), 'ree_estimate': round(ree),
                'tdee_initial': round(tdee), 'reference_weight_kg': round(reference_weight, 1),
                'method': 'Mifflin-St Jeor + 活动系数初值；普通成年人温和缺口设计',
                'carb_definition': 'available',
                'limitations': '非实测代谢；肥胖者参考体重及宏量分配需按实际反馈调整',
                'auto_exercise_eatback': False}

    def update_profile(self, changes, source):
        object_keys(changes, PROFILE_FIELDS)
        for key in ('age', 'height_cm', 'weight_kg', 'waist_cm', 'goal_weight_kg', 'strength_days', 'available_minutes'):
            if key in changes:
                bounds = {'age': (1, 110), 'height_cm': (70, 230), 'weight_kg': (15, 350),
                    'waist_cm': (30, 250), 'goal_weight_kg': (15, 350), 'strength_days': (0, 7), 'available_minutes': (1, 300)}
                changes[key] = number(changes[key], *bounds[key])
        if 'sex' in changes and changes['sex'] not in ('male', 'female'):
            raise ValueError('公式计算性别须为male/female，不能猜测')
        if 'activity' in changes and changes['activity'] not in ACTIVITY:
            raise ValueError('活动量应为sedentary/light/moderate/active')
        if 'screening_complete' in changes and not isinstance(changes['screening_complete'], bool):
            raise ValueError('健康限制评估状态必须是布尔值')
        for key in ('health_limits', 'preferences', 'allergies', 'equipment'):
            if key in changes:
                if not isinstance(changes[key], list) or len(changes[key]) > 30:
                    raise ValueError('偏好或限制应为最多30项文本')
                changes[key] = [text(v, 300) for v in changes[key]]
        if 'goal_date' in changes:
            day(changes['goal_date'])
        op = hashlib.sha256((source + ':profile:' + dumps(changes)).encode()).hexdigest()
        with self.write() as c:
            previous = c.execute('SELECT result FROM operations WHERE id=?', (op,)).fetchone()
            if previous:
                return json.loads(previous['result'])
            row = c.execute('SELECT data FROM profiles ORDER BY id DESC LIMIT 1').fetchone()
            profile = {**(json.loads(row['data']) if row else {}), **changes}
            c.execute('INSERT INTO profiles(at,data) VALUES(?,?)', (now(), dumps(profile)))
            target = self.auto_targets(profile)
            current = c.execute('SELECT data FROM targets ORDER BY day DESC,at DESC LIMIT 1').fetchone()
            save_target = not current or not json.loads(current['data']).get('eligible') or not target['eligible']
            if save_target:
                c.execute('INSERT INTO targets VALUES(?,?,?,?)', (str(uuid.uuid4()), day(), now(), dumps(target)))
            result = {'profile': profile, 'initial_target': target, 'target_saved': save_target,
                      'note': '已有目标保持原值，身体档案更新不会自动改写目标'}
            c.execute('INSERT INTO operations VALUES(?,?)', (op, dumps(result)))
        return result

    def set_targets(self, changes, when, source):
        object_keys(changes, {'energy_kcal', 'protein_g', 'fat_g', 'carbs_g'}, {'energy_kcal', 'protein_g', 'fat_g', 'carbs_g'})
        proposed = self.auto_targets(self.profile())
        if not proposed['eligible']:
            raise ValueError(proposed['reason'])
        values = {k: number(v, 0, 6000 if k == 'energy_kcal' else 1000) for k, v in changes.items()}
        if not 0.75 * proposed['tdee_initial'] <= values['energy_kcal'] <= 1.25 * proposed['tdee_initial']:
            raise ValueError('目标超出普通成年人本版调整范围；请先进行专业评估')
        macros = values['protein_g']*4 + values['fat_g']*9 + values['carbs_g']*4
        if abs(macros - values['energy_kcal']) > max(100, values['energy_kcal'] * 0.08):
            raise ValueError('宏量营养目标与总热量不一致')
        values = {**proposed, **values, 'method': '用户指定，程序检查能量与适用范围'}
        when = day(when)
        op = hashlib.sha256((source + ':targets:' + when + dumps(values)).encode()).hexdigest()
        with self.write() as c:
            row = c.execute('SELECT result FROM operations WHERE id=?', (op,)).fetchone()
            if row:
                return json.loads(row['result'])
            c.execute('INSERT INTO targets VALUES(?,?,?,?)', (str(uuid.uuid4()), when, now(), dumps(values)))
            result = {'target': values, 'effective_day': when}
            c.execute('INSERT INTO operations VALUES(?,?)', (op, dumps(result)))
        return result

    def meal(self, meal_id, c=None):
        if c is None:
            with self.connect() as con:
                return self.meal(meal_id, con)
        row = c.execute('SELECT * FROM meals WHERE id=?', (text(meal_id, 80),)).fetchone()
        if not row:
            raise ValueError('餐次不存在')
        return {**dict(row), 'data': json.loads(row['data'])}

    def record(self, args, source):
        object_keys(args, {'items', 'day', 'meal', 'note', 'image_hashes', 'record_id', 'new_meal'}, {'items'})
        when, meal_name = day(args.get('day')), text(args.get('meal', '未分类'), 30)
        hashes = args.get('image_hashes', [])
        if not isinstance(hashes, list) or len(hashes) > 12 or any(
                not isinstance(h, str) or len(h) != 64 or any(x not in '0123456789abcdef' for x in h) for h in hashes):
            raise ValueError('图片哈希必须由可信适配器计算')
        with self.connect() as c:
            row = c.execute('SELECT meal_id FROM events WHERE source=?', (source,)).fetchone()
            if row:
                return {'duplicate': True, 'record': self.meal(row['meal_id'], c)}
        items = self.normalize_items(args['items'])
        with self.write() as c:
            row = c.execute('SELECT meal_id FROM events WHERE source=?', (source,)).fetchone()
            if row:
                return {'duplicate': True, 'record': self.meal(row['meal_id'], c)}
            if not args.get('new_meal') and not args.get('record_id'):
                for h in hashes:
                    duplicate = c.execute('SELECT i.meal_id FROM images i JOIN meals m ON m.id=i.meal_id '
                        'WHERE i.hash=? AND i.day=? AND m.status<>?', (h, when, 'deleted')).fetchone()
                    if duplicate:
                        c.execute('INSERT INTO events VALUES(?,?)', (source, duplicate['meal_id']))
                        return {'duplicate': True, 'record': self.meal(duplicate['meal_id'], c)}
            if args.get('record_id'):
                old = self.meal(args['record_id'], c)
                if old['status'] == 'deleted':
                    raise ValueError('已删除餐次不能追加')
                when, meal_name = old['day'], old['meal']
                merged = {v['key']: v for v in old['data']['items']}
                merged.update({v['key']: v for v in items})
                items = list(merged.values())
                record_id, version = old['id'], old['version'] + 1
                note = old['data'].get('note', '') + '\n' + text(args.get('note', ''), 2000)
            else:
                record_id, version = str(uuid.uuid4()), 1
                note = text(args.get('note', ''), 2000)
            data = {'items': items, 'note': note, 'estimate_only': True,
                    'interval_type': '情景估算，未经统计校准', 'source_event': source}
            status = 'estimated' if all(v['density']['energy_kcal'] is not None for v in items) else 'incomplete'
            c.execute('INSERT INTO meals VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                'version=excluded.version,status=excluded.status,data=excluded.data,at=excluded.at',
                (record_id, when, meal_name, version, status, dumps(data), now()))
            c.execute('INSERT INTO events VALUES(?,?)', (source, record_id))
            for h in hashes:
                c.execute('INSERT OR IGNORE INTO images VALUES(?,?,?)', (h, when, record_id))
            c.execute('INSERT INTO revisions(meal_id,version,at,reason,data) VALUES(?,?,?,?,?)',
                (record_id, version, now(), '自动估算入账或补图', dumps(data)))
            c.execute('DELETE FROM complete_days WHERE day=?', (when,))
            result = self.meal(record_id, c)
        return {'duplicate': False, 'record': result, 'daily': self.summary(when)}

    def amend(self, args, source):
        object_keys(args, {'record_id', 'expected_version', 'items', 'item_changes', 'reason', 'delete', 'day', 'meal'},
                    {'record_id', 'expected_version', 'reason'})
        if not args.get('delete') and not any(k in args for k in ('items', 'item_changes', 'day', 'meal')):
            raise ValueError('修订应包含项目、日期、餐别或删除操作')
        op = hashlib.sha256((source + ':amend:' + dumps(args)).encode()).hexdigest()
        items = self.normalize_items(args['items']) if 'items' in args else None
        with self.write() as c:
            done = c.execute('SELECT result FROM operations WHERE id=?', (op,)).fetchone()
            if done:
                return json.loads(done['result'])
            old = self.meal(args['record_id'], c)
            if old['version'] != args['expected_version']:
                raise ValueError('记录已被更新，请重新读取版本后修改')
            data = {**old['data'], 'items': items if items is not None else old['data']['items']}
            if 'item_changes' in args:
                changes = args['item_changes']
                if not isinstance(changes, list) or not 1 <= len(changes) <= 30 or items is not None:
                    raise ValueError('份量修改列表不合法或与完整items同时提供')
                mapped = {i['key']: i for i in data['items']}
                for change in changes:
                    object_keys(change, {'key', 'mass_g', 'volume_ml'}, {'key'})
                    if change['key'] not in mapped:
                        raise ValueError('修订项目不存在')
                    entry = mapped[change['key']]
                    field = 'volume_ml' if entry.get('volume_ml') is not None else 'mass_g'
                    if set(change) != {'key', field}:
                        raise ValueError('份量修改的单位与原记录不一致')
                    amount = interval(change[field])
                    entry[field] = amount
                    entry['nutrients'] = {k: None if entry['density'][k] is None else {
                        b: round(entry['density'][k]*amount[b]/100, 5) for b in ('low','best','high')} for k in NUTRIENTS}
            version, when = old['version'] + 1, day(args.get('day', old['day']))
            meal_name = text(args.get('meal', old['meal']), 30)
            status = 'deleted' if args.get('delete') else (
                'estimated' if all(v['density']['energy_kcal'] is not None for v in data['items']) else 'incomplete')
            c.execute('UPDATE meals SET version=?,day=?,meal=?,status=?,data=?,at=? WHERE id=?',
                (version, when, meal_name, status, dumps(data), now(), old['id']))
            c.execute('UPDATE images SET day=? WHERE meal_id=?', (when, old['id']))
            c.execute('INSERT INTO revisions(meal_id,version,at,reason,data) VALUES(?,?,?,?,?)',
                (old['id'], version, now(), text(args['reason'], 500), dumps({'status': status, 'day': when, **data})))
            c.execute('DELETE FROM complete_days WHERE day IN (?,?)', (when, old['day']))
            result = {'record': self.meal(old['id'], c)}
            c.execute('INSERT INTO operations VALUES(?,?)', (op, dumps(result)))
        return result

    @staticmethod
    def totals(items):
        result = {}
        carb_defs = {i['carb_definition'] for i in items if i['density']['carbs_g'] is not None}
        for k in NUTRIENTS:
            known = [i['nutrients'][k] for i in items if i['nutrients'][k] is not None]
            missing = len(items) - len(known)
            incompatible = k == 'carbs_g' and (len(carb_defs) > 1 or 'unknown' in carb_defs)
            result[k] = {b: round(sum(v[b] for v in known), 2) if not missing and not incompatible else None
                         for b in ('low', 'best', 'high')}
            result[k].update({'known_best': round(sum(v['best'] for v in known), 2),
                'missing_items': missing, 'complete': not missing and not incompatible,
                'definition_mixed': incompatible})
        return result

    def summary(self, when=None):
        when = day(when)
        with self.connect() as c:
            rows = c.execute('SELECT id FROM meals WHERE day=? AND status<>? ORDER BY at', (when, 'deleted')).fetchall()
            meals = [self.meal(r['id'], c) for r in rows]
            complete = bool(c.execute('SELECT 1 FROM complete_days WHERE day=?', (when,)).fetchone())
        items = [i for m in meals for i in m['data']['items']]
        totals, target = self.totals(items), self.targets_for(when)
        remaining = {}
        if target and target.get('eligible') and items:
            for k in ('energy_kcal', 'protein_g', 'fat_g', 'carbs_g'):
                v = totals[k]
                remaining[k] = None if not v['complete'] else {
                    'low': round(target[k] - v['high'], 2), 'best': round(target[k] - v['best'], 2),
                    'high': round(target[k] - v['low'], 2)}
                if k == 'carbs_g' and any(i['carb_definition'] != target.get('carb_definition', 'available') for i in items):
                    remaining[k] = None
        return {'day': when, 'meal_count': len(meals), 'records': meals, 'totals': totals,
                'target': target, 'remaining': remaining, 'user_marked_complete': complete,
                'coverage': '已记录餐次；未记录食物不等于零摄入', 'estimate_only': True,
                'wearable': health_snapshot(self, when, when),
                'sugar_note': '总糖、添加糖分别统计；未知不是零，总糖不能替代WHO游离糖指标'}

    def record_measure(self, args, source):
        object_keys(args, {'day', 'weight_kg', 'waist_cm'})
        if not any(k in args for k in ('weight_kg', 'waist_cm')):
            raise ValueError('请提供体重或腰围')
        when = day(args.get('day'))
        weight = number(args['weight_kg'], 15, 350) if 'weight_kg' in args else None
        waist = number(args['waist_cm'], 30, 250) if 'waist_cm' in args else None
        with self.write() as c:
            previous = c.execute('SELECT * FROM measures WHERE source=?', (source,)).fetchone()
            if previous:
                return {'measurement': dict(previous), 'duplicate': True}
            c.execute('INSERT INTO measures(source,day,at,weight_kg,waist_cm) VALUES(?,?,?,?,?)',
                      (source, when, now(), weight, waist))
        return {'day': when, 'weight_kg': weight, 'waist_cm': waist, 'duplicate': False}

    def workout(self, args, source):
        if args.get('record_id'):
            object_keys(args, {'record_id', 'activity', 'minutes', 'rpe', 'feedback'})
            changes = {k: v for k, v in args.items() if k != 'record_id'}
            if not changes:
                raise ValueError('运动修订缺少修改内容')
            for k in ('minutes', 'rpe'):
                if k in changes:
                    changes[k] = number(changes[k], 1, 360 if k == 'minutes' else 10)
            if 'activity' in changes and changes['activity'] not in (
                    'walk', 'brisk_walk', 'run', 'cycle', 'strength', 'mobility', 'other'):
                raise ValueError('运动类型不在本版支持目录内')
            if 'feedback' in changes:
                changes['feedback'] = text(changes['feedback'], 1000)
            op = hashlib.sha256((source + ':workout:' + dumps(args)).encode()).hexdigest()
            with self.write() as c:
                done = c.execute('SELECT result FROM operations WHERE id=?', (op,)).fetchone()
                if done:
                    return json.loads(done['result'])
                row = c.execute('SELECT data FROM workouts WHERE source=?',
                                (text(args['record_id'], 200),)).fetchone()
                if not row:
                    raise ValueError('运动记录不存在')
                data = json.loads(row['data'])
                data['manual_overrides'] = {**data.get('manual_overrides', {}), **changes}
                data.setdefault('manual_history', []).append({'at': now(), 'changes': changes})
                data.update(changes)
                if 'minutes' in changes and data.get('provider') == 'Intervals.icu':
                    data.setdefault('device_duration_seconds', data['duration_seconds'])
                    data['duration_seconds'] = round(changes['minutes'] * 60, 4)
                c.execute('UPDATE workouts SET data=?,at=? WHERE source=?',
                          (dumps(data), now(), args['record_id']))
                result = {'record_id': args['record_id'], 'workout': data, 'corrected': True}
                c.execute('INSERT INTO operations VALUES(?,?)', (op, dumps(result)))
            return result
        object_keys(args, {'day', 'activity', 'minutes', 'rpe', 'feedback'}, {'activity', 'minutes'})
        activity = args['activity']
        mets = {'walk': 3.3, 'brisk_walk': 4.3, 'run': 8.0, 'cycle': 6.0, 'strength': 3.5,
                'mobility': 2.3, 'other': None}
        if activity not in mets:
            raise ValueError('运动类型不在本版支持目录内')
        minutes = number(args['minutes'], 1, 360)
        profile, met = self.profile(), mets[activity]
        kcal = None if met is None or not profile.get('weight_kg') else met * 3.5 * profile['weight_kg'] / 200 * minutes
        data = {'activity': activity, 'minutes': minutes, 'rpe': number(args.get('rpe', 5), 1, 10),
            'feedback': text(args.get('feedback', ''), 1000), 'MET_reference': met,
            'source': '2024 Compendium活动类别参考；实际速度和强度不明，非测量',
            'gross_kcal': None if kcal is None else {'low': round(kcal*.7), 'best': round(kcal), 'high': round(kcal*1.3)},
            'net_kcal': None if kcal is None else round((met-1)*3.5*profile['weight_kg']/200*minutes),
            'interval_type': '设计情景范围，未经个人校准', 'eatback_applied': False}
        with self.write() as c:
            row = c.execute('SELECT data FROM workouts WHERE source=?', (source,)).fetchone()
            if row:
                return {'workout': json.loads(row['data']), 'duplicate': True}
            c.execute('INSERT INTO workouts VALUES(?,?,?,?)', (source, day(args.get('day')), now(), dumps(data)))
        return {'workout': data, 'duplicate': False}

    def trend(self, end=None, days=28):
        end = day(end)
        days = int(number(days, 7, 90))
        start = (date.fromisoformat(end) - timedelta(days=days-1)).isoformat()
        with self.connect() as c:
            rows = [dict(r) for r in c.execute('SELECT day,weight_kg,waist_cm FROM measures WHERE day BETWEEN ? AND ? ORDER BY day,at', (start, end))]
            workouts = [json.loads(r['data']) for r in c.execute('SELECT data FROM workouts WHERE day BETWEEN ? AND ?', (start, end))]
            completed = [r['day'] for r in c.execute('SELECT day FROM complete_days WHERE day BETWEEN ? AND ?', (start, end))]
        wearable = health_snapshot(self, start, end)
        weights = {r['day']: r['weight_kg'] for r in wearable['daily'] if r['weight_kg'] is not None}
        series = []
        for d in sorted(weights):
            lower = (date.fromisoformat(d)-timedelta(days=6)).isoformat()
            values = [v for k, v in weights.items() if lower <= k <= d]
            series.append({'day': d, 'weight_kg': weights[d], 'mean_7d': round(statistics.mean(values), 3),
                           'samples_7d': len(values)})
        result = {'start': start, 'end': end, 'measurements': rows, 'weight_series': series,
            'complete_days': len(completed), 'logged_workout_minutes': sum(w['minutes'] for w in workouts),
            'wearable': wearable,
            'weekly_change_kg': None, 'goal_eta_weeks': None, 'automatic_target_change': False,
            'note': '水分、盐、测量条件和漏记会影响趋势；不诊断停滞原因，不用照片账本推算精确代谢'}
        if len(series) >= 8 and (date.fromisoformat(series[-1]['day'])-date.fromisoformat(series[0]['day'])).days >= 14:
            xs = [(date.fromisoformat(s['day'])-date.fromisoformat(series[0]['day'])).days for s in series]
            ys = [s['mean_7d'] for s in series]
            xm, ym = statistics.mean(xs), statistics.mean(ys)
            slope = sum((x-xm)*(y-ym) for x,y in zip(xs,ys))/sum((x-xm)**2 for x in xs)*7
            result['weekly_change_kg'] = round(slope, 3)
            goal = self.profile().get('goal_weight_kg')
            delta = series[-1]['mean_7d'] - goal if goal is not None else None
            if delta is not None and delta > 0 and slope < -.05:
                weeks = delta / -slope
                result['goal_eta_weeks'] = {'low': round(weeks/1.5, 1), 'high': round(weeks/0.5, 1),
                    'note': '仅当前趋势情景外推，长期能量需求会变化，不是保证日期'}
        return result

    def next_meal(self, when=None):
        summary, profile = self.summary(when), self.profile()
        return {'daily': summary, 'preferences': profile.get('preferences', []),
            'allergies': profile.get('allergies', []), 'health_limits': profile.get('health_limits', []),
            'guidance': ['按剩余区间、饥饿感和可获得食物选择候选，用preview计算食物份量',
                '没有完整热量数据时不要声称准确剩余额度',
                '高热量一餐后回到规律饮食，不要求断食或惩罚性运动',
                '不把运动消耗全额加回可吃额度']}

    def exercise_plan(self):
        profile = self.profile()
        if profile.get('health_limits') or not profile.get('screening_complete'):
            return {'individual_plan_ready': False, 'recent_activity': self.summary()['wearable'],
                    'note': '先了解活动限制；不自动安排疾病或受伤人群训练'}
        duration = min(profile.get('available_minutes', 20), 30)
        return {'individual_plan_ready': True, 'week': [
            {'day': 1, 'activity': '轻至中等强度步行 + 基础力量', 'minutes': duration},
            {'day': 2, 'activity': '步行', 'minutes': duration},
            {'day': 3, 'activity': '步行或轻活动', 'minutes': duration},
            {'day': 4, 'activity': '轻至中等强度步行 + 基础力量', 'minutes': duration},
            {'day': 5, 'activity': '步行', 'minutes': duration},
            {'day': 6, 'activity': '可选步行或喜欢的活动', 'minutes': duration},
            {'day': 7, 'activity': '休息或轻活动', 'minutes': 10}],
            'equipment': profile.get('equipment', []),
            'recent_activity': health_snapshot(self, (date.fromisoformat(day())-timedelta(days=6)).isoformat(), day()),
            'progression': '依据完成、主观强度与疼痛反馈逐步调整；逐步接近WHO每周150–300分钟有氧及至少2天力量',
            'note': '本版提供起步安排，动作细节由Agent结合经验与限制说明；疼痛或异常症状停止并评估'}

    def mark_complete(self, when):
        when = day(when)
        with self.write() as c:
            c.execute('INSERT OR REPLACE INTO complete_days VALUES(?,?)', (when, now()))
        return {'day': when, 'user_marked_complete': True}

    def week(self, end=None):
        end = day(end)
        days = [(date.fromisoformat(end)-timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
        summaries = [self.summary(d) for d in days]
        usable = [s for s in summaries if s['user_marked_complete'] and s['meal_count']
                  and s['totals']['energy_kcal']['complete'] and s['target'] and s['target'].get('eligible')]
        protein_days = [s for s in usable if s['totals']['protein_g']['complete']]
        return {'days': [{'day': s['day'], 'meal_count': s['meal_count'], 'totals': s['totals'],
                         'target': s['target'], 'complete': s['user_marked_complete']} for s in summaries],
            'complete_energy_days': len(usable),
            'energy_target_within_10_percent_days': sum(abs(s['totals']['energy_kcal']['best']/s['target']['energy_kcal']-1) <= .1 for s in usable),
            'protein_evaluable_days': len(protein_days),
            'protein_target_days': sum(s['totals']['protein_g']['best'] >= s['target']['protein_g'] for s in protein_days),
            'wearable': health_snapshot(self, days[0], end),
            'trend': self.trend(end), 'note': '仅用户声明记录完整且营养可计算的日期进入完成率分母；不自动更改目标'}

    def export(self):
        with self.connect() as c:
            result = {table: [dict(r) for r in c.execute('SELECT * FROM ' + table)] for table in
                      ('profiles', 'targets', 'meals', 'revisions', 'measures', 'workouts', 'complete_days', 'foods')}
        return {'format': 'hermes-health-json', 'exported_at': now(), 'data': result}

    def backup(self):
        directory = self.root / 'backups'
        directory.mkdir(exist_ok=True)
        target = directory / ('health-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.sqlite3')
        with self.connect() as src, closing(sqlite3.connect(target)) as dest:
            src.backup(dest)
            if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('备份完整性检查失败')
        if os.name != 'nt':
            os.chmod(target, 0o600)
        return {'backup': target.name, 'integrity': 'ok', 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}

    def erase(self, phrase):
        if phrase != '删除全部健康数据':
            raise ValueError('完整删除须由用户明确要求删除全部健康数据')
        with self.write() as c:
            device_sync = c.execute("SELECT 1 FROM settings WHERE key='intervals_sync'").fetchone()
            for table in ('images', 'events', 'revisions', 'meals', 'profiles', 'targets', 'measures',
                          'workouts', 'complete_days', 'operations', 'foods', 'settings'):
                c.execute('DELETE FROM ' + table)
            if device_sync:
                c.execute('INSERT INTO settings VALUES(?,?)',
                          ('intervals_sync', dumps({'disabled': True})))
        with self.connect() as c:
            c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            c.execute('VACUUM')
        for f in (self.root / 'backups').glob('health-*.sqlite3'):
            if f.is_file() and not f.is_symlink():
                f.unlink()
        return {'deleted': True, 'device_sync_paused': bool(device_sync),
                'note': '当前健康库与本服务快照已清除，已接入的设备同步暂停；Hermes/飞书/模型日志及异地备份各自保留政策另行处理'}

    def dispatch(self, operation, args, source):
        text(source, 200)
        if not source:
            raise ValueError('缺少可信请求标识')
        allowed = {'targets': {'values', 'day'}, 'complete': {'day'}, 'food_search': {'query'},
            'food_detail': {'food_id'}, 'preview': {'items'}, 'today': {'day'},
            'record_detail': {'record_id'}, 'trend': {'day', 'days'}, 'week': {'day'},
            'next_meal': {'day'}, 'exercise_plan': set(), 'exercise_search': {'query'},
            'export': set(), 'backup': set(), 'erase': {'phrase'}, 'status': set()}
        if operation in allowed:
            object_keys(args, allowed[operation])
        if operation == 'record':
            return self.record(args, source)
        if operation == 'amend':
            return self.amend(args, source)
        if operation == 'profile':
            return self.update_profile(args, source)
        if operation == 'targets':
            return self.set_targets(args['values'], args.get('day'), source)
        if operation == 'weight':
            return self.record_measure(args, source)
        if operation == 'workout':
            return self.workout(args, source)
        if operation == 'complete':
            return self.mark_complete(args.get('day'))
        if operation == 'food_search':
            return self.search_foods(args['query'])
        if operation == 'food_detail':
            return self.food(args['food_id'])
        if operation == 'exercise_search':
            query = text(args['query'], 100).strip()
            data = self.request_json('https://wger.de/api/v2/exerciseinfo/?' +
                urllib.parse.urlencode({'limit': 8, 'search': query}))
            return {'source': 'wger公开运动目录', 'query': query, 'results': data.get('results', []),
                    'note': '检查名称和肌群是否匹配，不将目录动作视为个人医疗处方'}
        if operation == 'preview':
            items = self.normalize_items(args['items'])
            return {'items': items, 'totals': self.totals(items), 'saved': False}
        if operation == 'today':
            return self.summary(args.get('day'))
        if operation == 'record_detail':
            record = self.meal(args['record_id'])
            with self.connect() as c:
                record['revisions'] = [dict(r) for r in c.execute('SELECT * FROM revisions WHERE meal_id=? ORDER BY version', (record['id'],))]
            return record
        if operation == 'trend':
            return self.trend(args.get('day'), args.get('days', 28))
        if operation == 'week':
            return self.week(args.get('day'))
        if operation == 'next_meal':
            return self.next_meal(args.get('day'))
        if operation == 'exercise_plan':
            return self.exercise_plan()
        if operation == 'export':
            return self.export()
        if operation == 'backup':
            return self.backup()
        if operation == 'erase':
            return self.erase(args.get('phrase'))
        if operation == 'status':
            return {'profile_exists': bool(self.profile()), 'profile': self.profile(),
                    'target': self.targets_for(day()), 'default_record': True,
                    'reminders_enabled': False, 'timezone': str(TZ), 'database': 'SQLite',
                    'wearable_sync': sync_status(self),
                    'network_queries': self.network, 'photo_storage': '本服务不另存原图；Hermes缓存另行管理'}
        raise ValueError('不支持的健康操作')
