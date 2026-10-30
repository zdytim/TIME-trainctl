"""Convert controller snapshots into the training console's monthly view.

The caller must only set activation_verified after the target's cutover has
been checked. An unavailable snapshot never claims enforcement is ready.
"""
BASE = (('debug', 5 * 3600), ('important', 100 * 3600), ('normal', 500 * 3600))
NANO = 1_000_000_000
MAX_SECONDS = (1 << 53) - 1


def _amount(snapshot, key):
    value = snapshot[key]
    sec, nanos = value['gpu_seconds'], value['nanoseconds']
    if (isinstance(sec, bool) or not isinstance(sec, int) or not 0 <= sec <= MAX_SECONDS or
            isinstance(nanos, bool) or not isinstance(nanos, int) or not 0 <= nanos < NANO):
        raise ValueError('invalid controller amount: ' + key)
    return sec * NANO + nanos


def unavailable(verified=False):
    return {'enabled': False, 'mode': 'unknown' if verified else 'staged',
            'enforcement_ready': False, 'accounting_status': 'unknown',
            'request_available': False,
            'enforcement_limitation': '无法确认控制器额度状态，请联系管理员。' if verified else '月度强制调度尚未启用。',
            'month': None, 'current_tier': None, 'tiers': [], 'usage_verified': False}


def snapshot_view(snapshot, activation_verified=False):
    month = snapshot['month']
    if (isinstance(month, bool) or not isinstance(month, int) or month < 197001 or
            month > 999912 or not 1 <= month % 100 <= 12):
        raise ValueError('invalid controller month')
    used, granted, capacity, remaining, overage = (_amount(snapshot, key) for key in
                                                  ('used', 'granted', 'capacity', 'remaining', 'overage'))
    total = sum(seconds for _, seconds in BASE) * NANO + granted
    if capacity != total or remaining != max(0, total - used) or overage != max(0, used - total):
        raise ValueError('inconsistent controller snapshot')
    unallocated_usage = used
    tiers = []
    for name, base in BASE:
        limit = base * NANO + (granted if name == 'normal' else 0)
        charged = min(unallocated_usage, limit)
        unallocated_usage -= charged
        tiers.append({'name': name, 'limit_gpu_seconds': limit / NANO,
                      'used_gpu_seconds': charged / NANO, 'remaining_gpu_seconds': (limit - charged) / NANO})
    tier = next((item['name'] for item in tiers if item['remaining_gpu_seconds'] > 0), None)
    if snapshot['tier'] != (tier or 'exhausted'):
        raise ValueError('controller tier disagrees with usage')
    verified = activation_verified is True
    return {'enabled': verified, 'mode': 'enforcing' if verified else 'staged',
            'enforcement_ready': verified, 'accounting_status': 'ok', 'usage_verified': True,
            'enforcement_limitation': None if verified else '已读取控制器账本，但强制调度尚未完成上线核验。',
            'timezone': 'Asia/Shanghai', 'month': '%04d-%02d' % (month // 100, month % 100),
            'current_tier': tier, 'used_gpu_seconds': used / NANO,
            'remaining_gpu_seconds': remaining / NANO, 'overage_gpu_seconds': overage / NANO,
            'additional_gpu_seconds': granted / NANO, 'tiers': tiers}
