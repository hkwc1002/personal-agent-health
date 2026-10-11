"""Hermes健康工具桥：从会话上下文取消息标识，实际读取图片计算哈希。"""
from contextlib import closing
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3

from gateway.session_context import get_session_env
HERMES_HOME = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
SOCKET = '/run/personal-agent-health/health.sock'
STATE_DB = HERMES_HOME / 'state.db'
IMAGE_ROOTS = (HERMES_HOME / 'cache', HERMES_HOME / 'image_cache')
OWNER_ID = ''
READ_ACTIONS = {'status', 'today', 'record_detail', 'food_search', 'food_detail', 'preview',
                'next_meal', 'trend', 'week', 'exercise_plan', 'exercise_search', 'export'}
MANAGE_ACTIONS = {'profile', 'targets', 'amend', 'weight', 'workout', 'complete', 'backup', 'erase'}


class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET)


def context(kwargs, writing):
    platform = get_session_env('HERMES_SESSION_PLATFORM')
    cron = get_session_env('HERMES_CRON_SESSION') == '1'
    if writing and cron:
        raise ValueError('后台任务只能查询健康数据，不能自动修改')
    if platform == 'feishu':
        owner = OWNER_ID
        user = get_session_env('HERMES_SESSION_USER_ID')
        alt = get_session_env('HERMES_SESSION_USER_ID_ALT')
        if not owner or owner not in (user, alt):
            raise ValueError('健康数据仅属于已授权本人')
        if get_session_env('HERMES_SESSION_CHAT_TYPE') not in ('dm', 'private', 'p2p'):
            raise ValueError('健康工具仅用于本人私聊，不能在群聊中读取或记录')
    elif platform not in ('', 'cli', 'cron'):
        raise ValueError('健康工具不用于其他渠道')
    message = get_session_env('HERMES_SESSION_MESSAGE_ID')
    session = get_session_env('HERMES_SESSION_ID') or kwargs.get('session_id', '')
    task = kwargs.get('user_task') or ''
    if writing and not message and not kwargs.get('task_id') and not task:
        raise ValueError('缺少可信消息上下文，不执行写入')
    stable = {'platform': platform or 'cli', 'message': message} if message else {
        'platform': platform or 'cli', 'session': session,
        'message': kwargs.get('task_id') or hashlib.sha256(task.encode()).hexdigest()}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest(), task


def call(operation, args, kwargs):
    try:
        source, user_task = context(kwargs, operation not in READ_ACTIONS)
        if not isinstance(args, dict):
            raise ValueError('参数必须是对象')
        args = dict(args)
        if operation == 'record':
            hashes = []
            paths = args.pop('image_paths', [])
            if not isinstance(paths, list) or len(paths) > 12:
                raise ValueError('图片路径列表不合法')
            if paths and not user_task and get_session_env('HERMES_SESSION_PLATFORM') == 'feishu':
                session = get_session_env('HERMES_SESSION_ID') or kwargs.get('session_id', '')
                message = get_session_env('HERMES_SESSION_MESSAGE_ID')
                if not session or not message:
                    raise ValueError('缺少当前照片的会话或消息标识，不执行入账')
                # 真实工具调度不传user_task；只读取可信会话中这一条原始消息。
                with closing(sqlite3.connect(STATE_DB.resolve().as_uri() + '?mode=ro',
                                             uri=True, timeout=10)) as state:
                    row = state.execute('''SELECT m.content FROM messages m
                        JOIN sessions s ON s.id=m.session_id
                        WHERE m.session_id=? AND m.platform_message_id=? AND m.role='user'
                          AND s.source='feishu' AND s.user_id IN (?,?)
                        ORDER BY m.id DESC LIMIT 1''', (session, message,
                            get_session_env('HERMES_SESSION_USER_ID'),
                            get_session_env('HERMES_SESSION_USER_ID_ALT'))).fetchone()
                if not row or not isinstance(row[0], str):
                    raise ValueError('无法匹配当前照片的原始消息，不执行入账')
                user_task = row[0]
            for raw in paths:
                p = Path(raw)
                resolved = p.resolve(strict=True)
                permitted = any(resolved.is_relative_to(root.resolve()) for root in IMAGE_ROOTS)
                if (not permitted or p.is_symlink() or str(p) not in user_task
                        or p.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp', '.heic')
                        or not 0 < p.stat().st_size <= 20*1024*1024):
                    raise ValueError('只能使用当前消息提供的图片缓存路径')
                hashes.append(hashlib.sha256(p.read_bytes()).hexdigest())
            args['image_hashes'] = hashes
        conn = Connection('localhost', timeout=90)
        try:
            conn.request('POST', '/call', body=json.dumps({'operation': operation, 'args': args,
                'source': source}, ensure_ascii=False).encode(), headers={'Content-Type': 'application/json'})
            response = conn.getresponse()
            content = response.read(2000001)
            if len(content) > 2000000:
                raise ValueError('健康结果过大，请缩小查询范围')
            result = json.loads(content)
            return json.dumps(result, ensure_ascii=False)
        finally:
            conn.close()
    except ValueError as exc:
        return json.dumps({'ok': False, 'error': str(exc)[:240], 'do_not_claim_saved': True}, ensure_ascii=False)
    except (OSError, TypeError, http.client.HTTPException, sqlite3.Error):
        return json.dumps({'ok': False, 'error': '健康工具失败；请检查参数或查询实际记录，不要声称已保存',
                           'do_not_claim_saved': True}, ensure_ascii=False)


def health_record(args, **kwargs):
    return call('record', args, kwargs)


def health_query(args, **kwargs):
    if not isinstance(args, dict) or args.get('action') not in READ_ACTIONS:
        return json.dumps({'ok': False, 'error': '不支持的健康查询'}, ensure_ascii=False)
    return call(args['action'], {k:v for k,v in args.items() if k != 'action'}, kwargs)


def health_manage(args, **kwargs):
    if not isinstance(args, dict) or args.get('action') not in MANAGE_ACTIONS:
        return json.dumps({'ok': False, 'error': '不支持的健康管理操作'}, ensure_ascii=False)
    return call(args['action'], args.get('data', {}), kwargs)


RANGE = {'type': 'object', 'additionalProperties': False, 'required': ['low', 'best', 'high'],
         'properties': {k: {'type': 'number', 'minimum': 0, 'maximum': 20000} for k in ('low','best','high')}}
DENSITY = {'type': 'object', 'additionalProperties': False, 'properties': {
    k: {'type': ['number', 'null'], 'minimum': 0} for k in
    ('energy_kcal','energy_kj','protein_g','fat_g','carbs_g','sugars_g','added_sugars_g','fiber_g','sodium_mg','salt_g')}}
ITEM = {'type': 'object', 'additionalProperties': False, 'required': ['name'], 'properties': {
    'key': {'type': 'string', 'description': '一餐内稳定菜品键，多图同一盘菜相同键，不重复计算'},
    'name': {'type': 'string'}, 'mass_g': RANGE, 'volume_ml': RANGE,
    'food_id': {'type': 'string', 'description': 'health_query实际返回的食品ID，不能臆造'},
    'cooking': {'type': 'string'}, 'confidence': {'type': 'string', 'enum': ['low','medium','high']},
    'unknowns': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 12},
    'carb_definition': {'type': 'string', 'enum': ['by_difference','available','unknown']},
    'label': {'type': 'object', 'additionalProperties': False, 'required': ['basis','nutrients'], 'properties': {
        'basis': {'type': 'string', 'enum': ['per_100_g','per_serving_g','per_100_ml','per_serving_ml']},
        'basis_amount': {'type': 'number', 'minimum': .1}, 'nutrients': DENSITY,
        'description': {'type': 'string', 'description': '只引用可见标签或用户明确给出的配方值，不填写模型猜测营养密度'}}},
}}
ITEMS = {'type': 'array', 'items': ITEM, 'minItems': 1, 'maxItems': 30}
PROFILE = {'type': 'object', 'additionalProperties': False, 'properties': {
    **{k: {'type': 'number'} for k in ('age','height_cm','weight_kg','waist_cm','goal_weight_kg','strength_days','available_minutes')},
    'sex': {'type': 'string', 'enum': ['male','female']},
    'activity': {'type': 'string', 'enum': ['sedentary','light','moderate','active']},
    'screening_complete': {'type': 'boolean', 'description': '只有本人明确说明疾病、用药、妊娠、受伤等健康限制后才为true'},
    **{k: {'type': 'array', 'items': {'type': 'string'}} for k in ('health_limits','preferences','allergies','equipment')},
    'goal_date': {'type': 'string'},
}}
RECORD_SCHEMA = {'name': 'health_record', 'description':
    '食物照片或明确饮食记录默认直接入账，不逐餐询问确认。先读取personal-agent-health:personal-health技能，'
    '识别所有菜品、估算份量范围并使用health_query营养来源。所有图片一餐一次调用；'
    '未知营养也保存并标明缺失。油量作为单独食物估算，避免与熟菜重复加油。'
    '自动去重，成功后返回记录、版本和当日累计；用户之后可修改。',
    'parameters': {'type':'object','additionalProperties':False,'required':['items'],'properties': {
        'items': ITEMS, 'day': {'type':'string'}, 'meal': {'type':'string'}, 'note': {'type':'string'},
        'image_paths': {'type':'array','items':{'type':'string'},'maxItems':12},
        'record_id': {'type':'string','description':'同一餐补图时关联已存在餐次，同key项目更新而非重复相加'},
        'new_meal': {'type':'boolean','description':'仅本人明确同照片代表另一餐时为true'},
    }}}
QUERY_SCHEMA = {'name':'health_query','description':
    '从结构化健康库获取真实档案、今日累计、下一餐依据、历史趋势和周报。food_search用英文数据库查询词；'
    'food_detail取权威条目；preview确定性计算候选份量。不能从模型记忆计算累计，未知糖不是零。',
    'parameters':{'type':'object','additionalProperties':False,'required':['action'],'properties': {
        'action':{'type':'string','enum':sorted(READ_ACTIONS)}, 'day':{'type':'string'},
        'days':{'type':'integer','minimum':7,'maximum':90}, 'record_id':{'type':'string'},
        'query':{'type':'string'}, 'food_id':{'type':'string'}, 'items':ITEMS,
    }}}
MANAGE_SCHEMA = {'name':'health_manage','description':
    '记录本人健康档案、体重腰围、运动；修改或删除指定餐次；用户明确调整目标时设置目标。'
    'amend必须先读取record_id与expected_version；只改份量用item_changes保留营养来源，换食物用完整items。'
    'profile支持分次收集，健康限制未明确前不自动生成减脂目标。complete只在用户说今天记全后设置。'
    '后台不写入，不创建提醒。erase仅用于用户明确要求删除全部健康数据。',
    'parameters':{'type':'object','additionalProperties':False,'required':['action','data'],'properties': {
        'action':{'type':'string','enum':sorted(MANAGE_ACTIONS)},
        'data':{'type':'object','additionalProperties':False,'properties': {
            **PROFILE['properties'], 'day':{'type':'string'},
            'values':{'type':'object','additionalProperties':False,'properties': {
                k:{'type':'number','minimum':0} for k in ('energy_kcal','protein_g','fat_g','carbs_g')},
                'required':['energy_kcal','protein_g','fat_g','carbs_g']},
            'record_id':{'type':'string'}, 'expected_version':{'type':'integer','minimum':1},
            'items':ITEMS, 'reason':{'type':'string'}, 'delete':{'type':'boolean'}, 'meal':{'type':'string'},
            'item_changes': {'type':'array','minItems':1,'maxItems':30,'items': {
                'type':'object','additionalProperties':False,'required':['key'],'properties': {
                    'key':{'type':'string'},'mass_g':RANGE,'volume_ml':RANGE}}},
            'activity':{'type':'string','description':'档案sedentary/light/moderate/active；运动walk/brisk_walk/run/cycle/strength/mobility/other'},
            'minutes':{'type':'number','minimum':1,'maximum':360}, 'rpe':{'type':'number','minimum':1,'maximum':10},
            'feedback':{'type':'string'}, 'phrase':{'type':'string'},
        }},
    }}}
def register_tools(ctx, config):
    global SOCKET, STATE_DB, IMAGE_ROOTS, OWNER_ID
    SOCKET = config['socket']
    STATE_DB = Path(config['state_db'])
    IMAGE_ROOTS = tuple(Path(p) for p in config['image_roots'])
    OWNER_ID = config.get('owner_id', '')
    if not Path(SOCKET).is_absolute() or not STATE_DB.is_absolute():
        raise ValueError('健康插件路径必须为绝对路径')
    if not IMAGE_ROOTS or any(not p.is_absolute() for p in IMAGE_ROOTS):
        raise ValueError('图片缓存路径必须为绝对路径')
    for name, schema, handler in [('health_record', RECORD_SCHEMA, health_record),
                                  ('health_query', QUERY_SCHEMA, health_query),
                                  ('health_manage', MANAGE_SCHEMA, health_manage)]:
        ctx.register_tool(name=name, toolset='personal_health', schema=schema,
                          handler=handler, emoji='健康')
