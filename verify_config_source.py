import urllib.request
import urllib.error
import json
import sqlite3
import time
import sys
import os
import subprocess
import signal

BASE = 'http://127.0.0.1:5000'
DB = 'emergency_supply.db'
DEFAULT_EXPIRE_MINUTES = 30

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


def wait_for_server(timeout=15):
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(BASE + '/api/config', timeout=2) as resp:
                if resp.status == 200:
                    time.sleep(0.5)
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def clean_env():
    for k in ['RESERVATION_EXPIRE_MINUTES']:
        if k in os.environ:
            del os.environ[k]


def start_server(env_vars=None):
    clean_env()
    if env_vars:
        os.environ.update(env_vars)
    print(f'    启动参数: {env_vars}')
    with open('server_startup.log', 'w', encoding='utf-8') as logf:
        proc = subprocess.Popen(
            ['python', '-u', 'app.py'],
            stdout=logf,
            stderr=subprocess.STDOUT,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            bufsize=0
        )
    return proc


def stop_server(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
        proc.wait(timeout=3)


def get_startup_log():
    try:
        with open('server_startup.log', 'r', encoding='utf-8') as f:
            return f.read()
    except Exception:
        return ''


def run_scenario(name, env_vars, expected_source, expected_fallback, expected_minutes):
    print(f'\n{"=" * 70}')
    print(f'场景: {name}')
    print(f'预期: source={expected_source}, fallback={expected_fallback}, minutes={expected_minutes}')
    print('=' * 70)

    if os.path.exists(DB):
        os.remove(DB)

    proc = start_server(env_vars)
    try:
        if not wait_for_server():
            check('服务启动成功', False, '超时未就绪')
            return

        startup_log = get_startup_log()
        print(f'    --- 启动日志片段 ---')
        for line in startup_log.split('\n'):
            if '[Config]' in line:
                print(f'    {line.strip()}')
        print(f'    ------------------')

        check('启动日志包含 [Config] 标记', '[Config]' in startup_log)
        check(f'启动日志包含 source={expected_source}',
              f'source={expected_source}' in startup_log,
              f'日志中未找到 source={expected_source}')

        code, cfg = api('/api/config')
        check('/api/config 返回 200', code == 200, f'code={code}')
        if code != 200:
            return

        check(f'config_source == {expected_source}',
              cfg['config_source'] == expected_source,
              f'实际={cfg["config_source"]}')
        check(f'config_fallback == {expected_fallback}',
              cfg['config_fallback'] == expected_fallback,
              f'实际={cfg["config_fallback"]}')
        check(f'reservation_expire_minutes == {expected_minutes}',
              cfg['reservation_expire_minutes'] == expected_minutes,
              f'实际={cfg["reservation_expire_minutes"]}')

        code, stats = api('/api/stats')
        check('/api/stats 返回 200', code == 200)
        if code == 200 and 'config' in stats:
            check('stats.config 与 /api/config 一致',
                  stats['config'] == cfg)

        code, order = api('/api/orders', 'POST', {
            'requester_id': 1,
            'source_warehouse_id': 1,
            'target_warehouse_id': 2,
            'material_id': 1,
            'quantity': 5,
            'remark': f'配置测试-{name}'
        })
        check('创建订单成功', code == 201)
        if code == 201:
            order_id = order['id']
            code, submit_result = api(f'/api/orders/{order_id}/submit', 'POST', {'operator_id': 1})
            check('提交订单成功', code == 200)
            if code == 200:
                expires_at_str = submit_result['expires_at']
                check('submit 返回 expires_at', expires_at_str is not None)

                code, order_detail = api(f'/api/orders/{order_id}')
                check('订单详情返回 200', code == 200)
                if code == 200:
                    check('订单详情含 config', 'config' in order_detail)
                    check('订单详情 config 与 /api/config 一致',
                          order_detail['config'] == cfg)
                    check('订单详情 reservation_expires_at == submit 返回',
                          order_detail.get('reservation_expires_at') == expires_at_str)

                conn = sqlite3.connect(DB)
                conn.row_factory = sqlite3.Row
                db_row = conn.execute(
                    'SELECT expires_at FROM reservations WHERE id = ?',
                    (submit_result['reservation_id'],)
                ).fetchone()
                conn.close()
                check('DB expires_at == submit 返回',
                      db_row['expires_at'] == expires_at_str)

                before_restart_cfg = cfg
                before_restart_expires = expires_at_str

                print(f'\n    --- 重启服务，验证重启前后一致 ---')
                stop_server(proc)
                time.sleep(1)

                proc = start_server(env_vars)
                if not wait_for_server():
                    check('重启后服务启动成功', False, '超时未就绪')
                    return

                code, cfg2 = api('/api/config')
                check('重启后 /api/config 返回 200', code == 200)
                if code == 200:
                    check('重启后 config 与重启前一致', cfg2 == before_restart_cfg,
                          f'重启前={before_restart_cfg}, 重启后={cfg2}')

                code, order_detail2 = api(f'/api/orders/{order_id}')
                check('重启后订单详情返回 200', code == 200)
                if code == 200:
                    check('重启后 reservation_expires_at 不变',
                          order_detail2.get('reservation_expires_at') == before_restart_expires)

                code, inv = api('/api/inventory')
                check('重启后库存查询正常', code == 200)
                code, _ = api('/api/warehouses')
                check('重启后仓库查询正常', code == 200)

                print(f'\n    --- 用户可见排障信息验证 ---')
                print(f'    GET /api/config 返回: {json.dumps(cfg2, ensure_ascii=False, indent=2)}')
                reason = {
                    'env': f'环境变量显式配置为 {expected_minutes} 分钟',
                    'default': f'未配置，使用默认值 {DEFAULT_EXPIRE_MINUTES} 分钟',
                    'default(fallback)': f'配置非法，自动回退到默认值 {DEFAULT_EXPIRE_MINUTES} 分钟'
                }[cfg2['config_source']]
                print(f'    排障结论: 当前预占过期时间 = {cfg2["reservation_expire_minutes"]} 分钟，原因: {reason}')
                check('config_source 可直接用于排障说明', True, reason)

    finally:
        stop_server(proc)
        clean_env()
        if os.path.exists('server_startup.log'):
            os.remove('server_startup.log')


print('=' * 70)
print('配置来源标记修复验证 - 三种场景自动化测试')
print('=' * 70)

SCENARIOS = [
    ('未设置配置（默认值）',
     None,
     'default', False, DEFAULT_EXPIRE_MINUTES),
    ('显式配置 5 分钟（有效）',
     {'RESERVATION_EXPIRE_MINUTES': '5'},
     'env', False, 5),
    ('非法值 负数 -10（回退）',
     {'RESERVATION_EXPIRE_MINUTES': '-10'},
     'default(fallback)', True, DEFAULT_EXPIRE_MINUTES),
    ('非法值 非数字 abc（回退）',
     {'RESERVATION_EXPIRE_MINUTES': 'abc'},
     'default(fallback)', True, DEFAULT_EXPIRE_MINUTES),
    ('非法值 零 0（回退）',
     {'RESERVATION_EXPIRE_MINUTES': '0'},
     'default(fallback)', True, DEFAULT_EXPIRE_MINUTES),
    ('非法值 空字符串（回退默认）',
     {'RESERVATION_EXPIRE_MINUTES': ''},
     'default', False, DEFAULT_EXPIRE_MINUTES),
]

for name, env_vars, exp_src, exp_fb, exp_min in SCENARIOS:
    run_scenario(name, env_vars, exp_src, exp_fb, exp_min)

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
