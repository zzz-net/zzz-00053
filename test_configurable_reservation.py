import urllib.request
import urllib.error
import json
import sqlite3
import time
import sys
import os
import csv
import io
import subprocess
from datetime import datetime, timedelta

BASE = 'http://127.0.0.1:5000'
DB = 'emergency_supply.db'
DEFAULT_EXPIRE_MINUTES = 30

EXPECTED_SOURCE = os.environ.get('TEST_EXPECTED_SOURCE')
EXPECTED_FALLBACK = os.environ.get('TEST_EXPECTED_FALLBACK', '').lower() == 'true'
EXPECTED_MINUTES = int(os.environ.get('TEST_EXPECTED_MINUTES', DEFAULT_EXPIRE_MINUTES))

print(f'测试预期: source={EXPECTED_SOURCE}, fallback={EXPECTED_FALLBACK}, minutes={EXPECTED_MINUTES}')


def api(path, method='GET', data=None):
    url = BASE + path
    body = None
    headers = {'Content-Type': 'application/json'}
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode('utf-8')
            return resp.status, json.loads(text) if text else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))


ok = True
checks = []


def check(label, cond, detail=''):
    global ok
    status = 'PASS' if cond else 'FAIL'
    msg = f'  [{status}] {label}'
    if detail:
        msg += f' ({detail})'
    print(msg)
    checks.append((label, cond, detail))
    if not cond:
        ok = False


def db_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


print('=' * 70)
print('回归测试: 可配置预占过期策略')
print('=' * 70)

print('\n--- 1. /api/config 接口返回当前生效配置 ---')
code, cfg = api('/api/config')
check('/api/config 返回 200', code == 200, f'code={code}')
if code == 200:
    check('config 含 reservation_expire_minutes',
          'reservation_expire_minutes' in cfg)
    check('config 含 default_reservation_expire_minutes',
          'default_reservation_expire_minutes' in cfg)
    check('config 含 config_source', 'config_source' in cfg)
    check('config 含 config_fallback', 'config_fallback' in cfg)
    check(f'default_reservation_expire_minutes = {DEFAULT_EXPIRE_MINUTES}',
          cfg['default_reservation_expire_minutes'] == DEFAULT_EXPIRE_MINUTES)
    check('reservation_expire_minutes > 0',
          isinstance(cfg.get('reservation_expire_minutes'), int) and cfg['reservation_expire_minutes'] > 0)
    effective_minutes = cfg['reservation_expire_minutes']
    actual_source = cfg.get('config_source')
    actual_fallback = cfg.get('config_fallback')
    print(f'    当前生效预占过期分钟数: {effective_minutes}')
    print(f'    配置来源: {actual_source}, 是否回退: {actual_fallback}')

    if EXPECTED_SOURCE is not None:
        check(f'config_source == 预期 "{EXPECTED_SOURCE}"',
              actual_source == EXPECTED_SOURCE,
              f'实际={actual_source}, 预期={EXPECTED_SOURCE}')
    check(f'config_fallback == 预期 {EXPECTED_FALLBACK}',
          actual_fallback == EXPECTED_FALLBACK,
          f'实际={actual_fallback}, 预期={EXPECTED_FALLBACK}')
    check(f'reservation_expire_minutes == 预期 {EXPECTED_MINUTES}',
          effective_minutes == EXPECTED_MINUTES,
          f'实际={effective_minutes}, 预期={EXPECTED_MINUTES}')

    check('三种 source 取值互斥且合法',
          actual_source in ('env', 'default', 'default(fallback)'),
          f'实际 source={actual_source}')
    if actual_source == 'env':
        check('env 来源时 fallback 必为 False',
              actual_fallback == False,
              f'source=env 但 fallback={actual_fallback}')
    if actual_source == 'default':
        check('default 来源时 fallback 必为 False',
              actual_fallback == False,
              f'source=default 但 fallback={actual_fallback}')
    if actual_source == 'default(fallback)':
        check('default(fallback) 来源时 fallback 必为 True',
              actual_fallback == True,
              f'source=default(fallback) 但 fallback={actual_fallback}')

print('\n--- 2. /api/stats 接口中包含 config ---')
code, stats = api('/api/stats')
check('/api/stats 返回 200', code == 200)
if code == 200:
    check('stats.config 存在', 'config' in stats)
    if 'config' in stats:
        check('stats.config.reservation_expire_minutes 与 /api/config 一致',
              stats['config']['reservation_expire_minutes'] == cfg['reservation_expire_minutes'])

print('\n--- 3. 创建并提交订单，验证 expires_at 与配置一致 ---')
code, order = api('/api/orders', 'POST', {
    'requester_id': 1,
    'source_warehouse_id': 1,
    'target_warehouse_id': 2,
    'material_id': 1,
    'quantity': 10,
    'remark': '配置回归测试订单'
})
check('创建订单返回 201', code == 201)
order_id = order['id'] if code == 201 else None

if order_id:
    before_submit = datetime.now()
    code, submit_result = api(f'/api/orders/{order_id}/submit', 'POST', {'operator_id': 1})
    after_submit = datetime.now()
    check('提交订单返回 200', code == 200, f'code={code}')
    if code == 200:
        check('submit 返回含 expires_at', 'expires_at' in submit_result)
        expires_at_str = submit_result.get('expires_at')
        reservation_id = submit_result.get('reservation_id')
        print(f'    接口返回 expires_at = {expires_at_str}')

        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            expected_min = before_submit + timedelta(minutes=effective_minutes)
            expected_max = after_submit + timedelta(minutes=effective_minutes)
            tolerance = timedelta(seconds=5)
            check(f'expires_at 约等于提交时间 + {effective_minutes}分钟',
                  expected_min - tolerance <= expires_at <= expected_max + tolerance,
                  f'预期区间 [{expected_min}, {expected_max}], 实际 {expires_at}')
        except Exception as e:
            check(f'expires_at 格式合法 (ISO)', False, f'解析失败: {e}')

        print('\n--- 4. 订单详情接口 reservation_expires_at 一致性 ---')
        code, order_detail = api(f'/api/orders/{order_id}')
        check('订单详情返回 200', code == 200)
        if code == 200:
            check('订单详情含 reservation_expires_at',
                  'reservation_expires_at' in order_detail)
            check('订单详情含 config', 'config' in order_detail)
            check('订单详情 reservation_expires_at == submit 返回 expires_at',
                  order_detail.get('reservation_expires_at') == expires_at_str,
                  f'订单详情={order_detail.get("reservation_expires_at")}, submit={expires_at_str}')
            check('订单详情 config.reservation_expire_minutes 一致',
                  order_detail['config']['reservation_expire_minutes'] == effective_minutes)

        print('\n--- 5. 数据库中 reservations.expires_at 一致性 ---')
        conn = db_conn()
        db_row = conn.execute(
            'SELECT expires_at FROM reservations WHERE id = ?',
            (reservation_id,)
        ).fetchone()
        conn.close()
        check(f'DB reservations.expires_at 存在', db_row is not None)
        if db_row:
            check('DB expires_at == submit 返回 expires_at',
                  db_row['expires_at'] == expires_at_str,
                  f'DB={db_row["expires_at"]}, submit={expires_at_str}')

        print('\n--- 6. 审计日志 submit_reserve 中 expires_at 一致性 ---')
        code, audits = api(f'/api/audit?order_id={order_id}')
        check(f'审计查询返回 200', code == 200)
        if code == 200:
            submit_audits = [a for a in audits if a['action'] == 'submit_reserve']
            check(f'存在 submit_reserve 审计', len(submit_audits) >= 1)
            if submit_audits:
                details = submit_audits[0].get('details', {})
                if isinstance(details, str):
                    try:
                        details = json.loads(details)
                    except:
                        details = {}
                check('审计 details.expires_at == submit 返回',
                      details.get('expires_at') == expires_at_str,
                      f'审计={details.get("expires_at")}, submit={expires_at_str}')

        print('\n--- 7. JSON 审计导出中 expires_at 一致性 ---')
        req = urllib.request.Request(BASE + '/api/audit/export.json')
        with urllib.request.urlopen(req) as resp:
            json_audits = json.loads(resp.read().decode('utf-8'))
        order_audits = [a for a in json_audits if a.get('order_id') == order_id and a.get('action') == 'submit_reserve']
        check(f'JSON 导出中存在 order#{order_id} submit_reserve', len(order_audits) >= 1)
        if order_audits:
            det = order_audits[0].get('details')
            if isinstance(det, str):
                try:
                    det = json.loads(det)
                except:
                    det = {}
            check('JSON 审计导出 expires_at 一致',
                  det.get('expires_at') == expires_at_str,
                  f'JSON导出={det.get("expires_at")}, submit={expires_at_str}')

        print('\n--- 8. CSV 审计导出中 expires_at 一致性 ---')
        req = urllib.request.Request(BASE + '/api/audit/export.csv')
        with urllib.request.urlopen(req) as resp:
            csv_text = resp.read().decode('utf-8-sig')
        reader = csv.reader(io.StringIO(csv_text))
        csv_rows = list(reader)
        check('CSV 导出有表头', len(csv_rows) > 1)
        if len(csv_rows) > 1:
            header = csv_rows[0]
            action_col = header.index('action')
            details_col = header.index('details')
            order_id_col = header.index('order_id')
            csv_matches = []
            for r in csv_rows[1:]:
                if r[order_id_col] == str(order_id) and r[action_col] == 'submit_reserve':
                    csv_matches.append(r)
            check(f'CSV 导出中存在 order#{order_id} submit_reserve', len(csv_matches) >= 1)
            if csv_matches:
                det_raw = csv_matches[0][details_col]
                try:
                    det = json.loads(det_raw) if det_raw.startswith('{') else {}
                except:
                    det = {}
                check('CSV 审计导出 expires_at 一致',
                      det.get('expires_at') == expires_at_str,
                      f'CSV导出={det.get("expires_at")}, submit={expires_at_str}')

print('\n--- 9. 接口可用性验证（模拟非法配置场景下服务仍可用）---')
code, _ = api('/api/warehouses')
check('/api/warehouses 可用', code == 200)
code, inv = api('/api/inventory')
check('/api/inventory 可用', code == 200)
code, _ = api('/api/materials')
check('/api/materials 可用', code == 200)

print('\n--- 10. expires_at 为未来时间（正常场景）---')
if order_id and reservation_id:
    conn = db_conn()
    db_expires = conn.execute(
        'SELECT expires_at FROM reservations WHERE id = ?', (reservation_id,)
    ).fetchone()
    conn.close()
    if db_expires:
        try:
            exp_dt = datetime.fromisoformat(db_expires['expires_at'])
            check('DB expires_at 是未来时间', exp_dt > datetime.now(),
                  f'expires_at={exp_dt}, now={datetime.now()}')
        except Exception as e:
            check('DB expires_at 可解析为时间', False, str(e))

print(f'\n{"=" * 70}')
passed = sum(1 for _, c, _ in checks if c)
total = len(checks)
print(f'检查总计: {passed} / {total} 通过')
print(f'{"=" * 70}')

if not ok:
    print('\n失败检查项:')
    for label, cond, detail in checks:
        if not cond:
            print(f'  - {label}: {detail}')

sys.exit(0 if ok else 1)
