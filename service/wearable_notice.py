"""首次同步验收消息；不注册周期推送，不展示轨迹、凭据或健康档案。"""


def render_notice(status, today, history=None):
    snapshot = today.get('wearable', {})
    history = history or snapshot
    prefix = 'intervals:' + status['athlete_id'] + ':'
    workouts = [r for r in history.get('workouts', [])
                if r.get('record_id', '').startswith(prefix)]
    days = [r for r in history.get('daily', []) if
            any(source.startswith(prefix) for source in r.get('sources', {}).values())]
    daily = next((d for d in snapshot.get('daily', []) if d['day'] == today['day']), {})
    def value(name, unit, convert=lambda x: x):
        amount = daily.get(name)
        return '未收到' if amount is None else str(convert(amount)) + unit
    return '\n'.join((
        '手环数据已接入健康账本',
        '账本统计范围：' + str(history.get('start', today['day'])) + ' 至 ' +
            str(history.get('end', today['day'])),
        '已保存设备运动 ' + str(len(workouts)) + ' 条、日摘要 ' + str(len(days)) + ' 天。',
        today['day'] + '：步数 ' + value('steps', '步') +
            '；静息心率 ' + value('resting_hr_bpm', '次/分钟') +
            '；睡眠 ' + value('sleep_seconds', '小时', lambda x: round(x / 3600, 2)) + '。',
        '今后可直接问：今天的步数和运动？这周饮食与运动情况？',
        '数据按小时自动同步；当天摘要可能继续补齐，缺失不记为零。'))
