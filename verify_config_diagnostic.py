import urllib.request
import urllib.error
import json
import sqlite3
import time
import sys
import os
import subprocess
import csv
import io
from datetime import datetime, timedelta

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
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode('utf-8')
            return resp.status, json.loads(text) if text else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))


def db_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def wait_for_server(timeout=20):
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
    with open('diag_server.log', 'w', encoding='utf-8') as logf:
        proc = subprocess.Popen(
            [sys.executable, '-u', 'app.py'],
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


def remove_db():
    for suffix in ['', '-wal', '-shm', '-journal']:
        p = DB + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


def verify_config_chain(scenario_name, expected_minutes, expected_source, expected_fallback):
    code, cfg = api('/api/config')
    check(f'[{scenario_name}] /api/config 返回 200', code == 200, f'code={code}')
    if code != 200:
        return None

    check(f'[{scenario_name}] reservation_expire_minutes == {expected_minutes}',
          cfg['reservation_expire_minutes'] == expected_minutes,
          f'实际={cfg["reservation_expire_minutes"]}')
    check(f'[{scenario_name}] config_source == {expected_source}',
          cfg['config_source'] == expected_source,
          f'实际={cfg["config_source"]}')
    check(f'[{scenario_name}] config_fallback == {expected_fallback}',
          cfg['config_fallback'] == expected_fallback,
          f'实际={cfg["config_fallback"]}')
    check(f'[{scenario_name}] raw_env_value 存在', 'raw_env_value' in cfg)
    check(f'[{scenario_name}] loaded_at 存在且为 ISO 格式', 'loaded_at' in cfg)
    check(f'[{scenario_name}] resolution_explanation 存在且非空',
          'resolution_explanation' in cfg and len(cfg['resolution_explanation']) > 0)

    print(f'    排障结论: {cfg["resolution_explanation"]}')

    code, diag = api('/api/config/diagnose')
    check(f'[{scenario_name}] /api/config/diagnose 返回 200', code == 200, f'code={code}')
    if code == 200:
        check(f'[{scenario_name}] diagnose.current_config 与 /api/config 一致',
              diag['current_config'] == cfg,
              f'diagnose.current_config != cfg')
        check(f'[{scenario_name}] diagnose.latest_snapshot 存在',
              diag['latest_snapshot'] is not None)
        if diag['latest_snapshot']:
            snap = diag['latest_snapshot']
            check(f'[{scenario_name}] snapshot.effective_minutes == {expected_minutes}',
                  snap['effective_minutes'] == expected_minutes,
                  f'实际={snap["effective_minutes"]}')
            check(f'[{scenario_name}] snapshot.source == {expected_source}',
                  snap['source'] == expected_source,
                  f'实际={snap["source"]}')
            check(f'[{scenario_name}] snapshot.resolution_explanation 非空',
                  len(snap.get('resolution_explanation', '')) > 0)
            check(f'[{scenario_name}] snapshot.loaded_at 与 APP_CONFIG 一致',
                  snap['loaded_at'] == cfg['loaded_at'],
                  f'snapshot={snap["loaded_at"]}, config={cfg["loaded_at"]}')
        check(f'[{scenario_name}] diagnose.all_snapshots 为列表',
              isinstance(diag.get('all_snapshots'), list))
        check(f'[{scenario_name}] diagnose.reservation_alignment 存在',
              'reservation_alignment' in diag)
        check(f'[{scenario_name}] diagnose.diagnose_at 存在',
              'diagnose_at' in diag)

    return cfg


def verify_order_config_consistency(scenario_name, cfg):
    code, order = api('/api/orders', 'POST', {
        'requester_id': 1,
        'source_warehouse_id': 1,
        'target_warehouse_id': 2,
        'material_id': 1,
        'quantity': 5,
        'remark': f'排障诊断-{scenario_name}'
    })
    check(f'[{scenario_name}] 创建订单返回 201', code == 201, f'code={code}')
    if code != 201:
        return None, None, None

    order_id = order['id']
    before_submit = datetime.now()
    code, submit_result = api(f'/api/orders/{order_id}/submit', 'POST', {'operator_id': 1})
    after_submit = datetime.now()
    check(f'[{scenario_name}] 提交订单返回 200', code == 200, f'code={code}')
    if code != 200:
        return order_id, None, None

    expires_at_str = submit_result.get('expires_at')
    reservation_id = submit_result.get('reservation_id')
    config_minutes_used = submit_result.get('config_expire_minutes_used')

    check(f'[{scenario_name}] submit 返回 expires_at', expires_at_str is not None)
    check(f'[{scenario_name}] submit 返回 config_expire_minutes_used',
          config_minutes_used is not None)
    check(f'[{scenario_name}] config_expire_minutes_used == {cfg["reservation_expire_minutes"]}',
          config_minutes_used == cfg['reservation_expire_minutes'],
          f'实际={config_minutes_used}')

    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            expected_min = before_submit + timedelta(minutes=cfg['reservation_expire_minutes'])
            expected_max = after_submit + timedelta(minutes=cfg['reservation_expire_minutes'])
            tolerance = timedelta(seconds=5)
            check(f'[{scenario_name}] expires_at ≈ 提交时间 + {cfg["reservation_expire_minutes"]}分钟',
                  expected_min - tolerance <= expires_at <= expected_max + tolerance,
                  f'预期[{expected_min}, {expected_max}], 实际={expires_at}')
        except Exception as e:
            check(f'[{scenario_name}] expires_at 格式合法', False, str(e))

    code, order_detail = api(f'/api/orders/{order_id}')
    check(f'[{scenario_name}] 订单详情返回 200', code == 200)
    if code == 200:
        check(f'[{scenario_name}] 订单详情 reservation_expires_at == submit 返回',
              order_detail.get('reservation_expires_at') == expires_at_str,
              f'详情={order_detail.get("reservation_expires_at")}, submit={expires_at_str}')
        check(f'[{scenario_name}] 订单详情 config.reservation_expire_minutes 一致',
              order_detail.get('config', {}).get('reservation_expire_minutes') == cfg['reservation_expire_minutes'])

    conn = db_conn()
    db_row = conn.execute(
        'SELECT expires_at, config_expire_minutes FROM reservations WHERE id = ?',
        (reservation_id,)
    ).fetchone()
    conn.close()
    check(f'[{scenario_name}] DB reservations.expires_at == submit 返回',
          db_row['expires_at'] == expires_at_str if db_row else False,
          f'DB={db_row["expires_at"] if db_row else None}, submit={expires_at_str}')
    check(f'[{scenario_name}] DB reservations.config_expire_minutes == {cfg["reservation_expire_minutes"]}',
          db_row['config_expire_minutes'] == cfg['reservation_expire_minutes'] if db_row else False,
          f'DB={db_row["config_expire_minutes"] if db_row else None}')

    code, audits = api(f'/api/audit?order_id={order_id}')
    check(f'[{scenario_name}] 审计查询返回 200', code == 200)
    if code == 200:
        submit_audits = [a for a in audits if a['action'] == 'submit_reserve']
        check(f'[{scenario_name}] 存在 submit_reserve 审计', len(submit_audits) >= 1)
        if submit_audits:
            details = submit_audits[0].get('details', {})
            if isinstance(details, str):
                try:
                    details = json.loads(details)
                except Exception:
                    details = {}
            check(f'[{scenario_name}] 审计 details.expires_at == submit 返回',
                  details.get('expires_at') == expires_at_str,
                  f'审计={details.get("expires_at")}, submit={expires_at_str}')
            check(f'[{scenario_name}] 审计 details.config_expire_minutes == {cfg["reservation_expire_minutes"]}',
                  details.get('config_expire_minutes') == cfg['reservation_expire_minutes'],
                  f'审计={details.get("config_expire_minutes")}')
            check(f'[{scenario_name}] 审计 details.config_source == {cfg["config_source"]}',
                  details.get('config_source') == cfg['config_source'],
                  f'审计={details.get("config_source")}')

    return order_id, reservation_id, expires_at_str


def verify_export_consistency(scenario_name, order_id, expires_at_str, cfg):
    if order_id is None:
        return

    req = urllib.request.Request(BASE + '/api/audit/export.json')
    with urllib.request.urlopen(req) as resp:
        json_audits = json.loads(resp.read().decode('utf-8'))
    order_audits = [a for a in json_audits
                    if a.get('order_id') == order_id and a.get('action') == 'submit_reserve']
    check(f'[{scenario_name}] JSON 导出含 order#{order_id} submit_reserve',
          len(order_audits) >= 1)
    if order_audits:
        det = order_audits[0].get('details')
        if isinstance(det, str):
            try:
                det = json.loads(det)
            except Exception:
                det = {}
        check(f'[{scenario_name}] JSON导出 expires_at 一致',
              det.get('expires_at') == expires_at_str,
              f'JSON={det.get("expires_at")}, submit={expires_at_str}')
        check(f'[{scenario_name}] JSON导出 config_expire_minutes 一致',
              det.get('config_expire_minutes') == cfg['reservation_expire_minutes'],
              f'JSON={det.get("config_expire_minutes")}')
        check(f'[{scenario_name}] JSON导出 config_source 一致',
              det.get('config_source') == cfg['config_source'],
              f'JSON={det.get("config_source")}')

    req = urllib.request.Request(BASE + '/api/audit/export.csv')
    with urllib.request.urlopen(req) as resp:
        csv_text = resp.read().decode('utf-8-sig')
    reader = csv.reader(io.StringIO(csv_text))
    csv_rows = list(reader)
    check(f'[{scenario_name}] CSV 导出有数据', len(csv_rows) > 1)
    if len(csv_rows) > 1:
        header = csv_rows[0]
        action_col = header.index('action')
        details_col = header.index('details')
        order_id_col = header.index('order_id')
        csv_matches = [r for r in csv_rows[1:]
                       if r[order_id_col] == str(order_id) and r[action_col] == 'submit_reserve']
        check(f'[{scenario_name}] CSV导出含 order#{order_id} submit_reserve',
              len(csv_matches) >= 1)
        if csv_matches:
            det_raw = csv_matches[0][details_col]
            try:
                det = json.loads(det_raw) if det_raw.startswith('{') else {}
            except Exception:
                det = {}
            check(f'[{scenario_name}] CSV导出 expires_at 一致',
                  det.get('expires_at') == expires_at_str,
                  f'CSV={det.get("expires_at")}, submit={expires_at_str}')
            check(f'[{scenario_name}] CSV导出 config_expire_minutes 一致',
                  det.get('config_expire_minutes') == cfg['reservation_expire_minutes'],
                  f'CSV={det.get("config_expire_minutes")}')


def verify_diagnose_alignment(scenario_name, order_id, reservation_id, cfg):
    code, diag = api('/api/config/diagnose')
    check(f'[{scenario_name}] diagnose 预占对齐检查', code == 200)
    if code != 200:
        return

    alignment = diag.get('reservation_alignment', {})
    check(f'[{scenario_name}] reservation_alignment.total_active >= 1',
          alignment.get('total_active', 0) >= 1,
          f'实际={alignment.get("total_active")}')

    if reservation_id and alignment.get('details'):
        target = [d for d in alignment['details'] if d.get('id') == reservation_id]
        if target:
            d = target[0]
            check(f'[{scenario_name}] 预占#{reservation_id} config_expire_minutes == {cfg["reservation_expire_minutes"]}',
                  d.get('config_expire_minutes') == cfg['reservation_expire_minutes'],
                  f'实际={d.get("config_expire_minutes")}')
            check(f'[{scenario_name}] 预占#{reservation_id} 对齐标记为 True',
                  d.get('aligned') is True,
                  f'actual_delta={d.get("actual_delta_minutes")}, aligned={d.get("aligned")}')
        else:
            check(f'[{scenario_name}] 预占#{reservation_id} 在对齐详情中', False, '未找到')


def verify_cross_restart_consistency(env_vars, scenario_name, order_id, expires_at_str, cfg_before):
    print(f'\n    --- {scenario_name}: 跨重启一致性验证 ---')
    proc = start_server(env_vars)
    try:
        if not wait_for_server():
            check(f'[{scenario_name}] 重启后服务可用', False, '超时')
            return None

        code, cfg_after = api('/api/config')
        check(f'[{scenario_name}] 重启后 /api/config 返回 200', code == 200)
        if code != 200:
            return cfg_after

        check(f'[{scenario_name}] 重启后 reservation_expire_minutes 一致',
              cfg_after['reservation_expire_minutes'] == cfg_before['reservation_expire_minutes'],
              f'重启前={cfg_before["reservation_expire_minutes"]}, 重启后={cfg_after["reservation_expire_minutes"]}')
        check(f'[{scenario_name}] 重启后 config_source 一致',
              cfg_after['config_source'] == cfg_before['config_source'],
              f'重启前={cfg_before["config_source"]}, 重启后={cfg_after["config_source"]}')
        check(f'[{scenario_name}] 重启后 resolution_explanation 一致',
              cfg_after['resolution_explanation'] == cfg_before['resolution_explanation'],
              f'重启前={cfg_before["resolution_explanation"]}, 重启后={cfg_after["resolution_explanation"]}')

        code, diag = api('/api/config/diagnose')
        check(f'[{scenario_name}] 重启后 diagnose 返回 200', code == 200)
        if code == 200:
            snapshots = diag.get('all_snapshots', [])
            check(f'[{scenario_name}] 重启后 config_snapshots 条数 >= 2',
                  len(snapshots) >= 2,
                  f'实际={len(snapshots)} 条')
            if len(snapshots) >= 2:
                snap_first = snapshots[0]
                snap_last = snapshots[-1]
                check(f'[{scenario_name}] 跨重启快照 effective_minutes 一致',
                      snap_first['effective_minutes'] == snap_last['effective_minutes'],
                      f'首次={snap_first["effective_minutes"]}, 末次={snap_last["effective_minutes"]}')
                check(f'[{scenario_name}] 跨重启快照 source 一致',
                      snap_first['source'] == snap_last['source'],
                      f'首次={snap_first["source"]}, 末次={snap_last["source"]}')

            latest_snap = diag.get('latest_snapshot')
            if latest_snap:
                check(f'[{scenario_name}] 重启后快照 effective_minutes == {cfg_after["reservation_expire_minutes"]}',
                      latest_snap['effective_minutes'] == cfg_after['reservation_expire_minutes'],
                      f'实际={latest_snap["effective_minutes"]}')
                check(f'[{scenario_name}] 重启后快照 source == {cfg_after["config_source"]}',
                      latest_snap['source'] == cfg_after['config_source'],
                      f'实际={latest_snap["source"]}')
                check(f'[{scenario_name}] 重启后快照 resolution_explanation 与配置一致',
                      latest_snap['resolution_explanation'] == cfg_after.get('resolution_explanation'),
                      f'配置={cfg_after.get("resolution_explanation")}, 快照={latest_snap["resolution_explanation"]}')

        if order_id:
            code, order_detail = api(f'/api/orders/{order_id}')
            check(f'[{scenario_name}] 重启后订单详情返回 200', code == 200)
            if code == 200:
                check(f'[{scenario_name}] 重启后 reservation_expires_at 不变',
                      order_detail.get('reservation_expires_at') == expires_at_str,
                      f'重启前={expires_at_str}, 重启后={order_detail.get("reservation_expires_at")}')

        new_order_code, new_order = api('/api/orders', 'POST', {
            'requester_id': 1,
            'source_warehouse_id': 1,
            'target_warehouse_id': 2,
            'material_id': 2,
            'quantity': 3,
            'remark': f'重启后新订单-{scenario_name}'
        })
        check(f'[{scenario_name}] 重启后创建新订单返回 201',
              new_order_code == 201, f'code={new_order_code}')
        if new_order_code == 201:
            new_order_id = new_order['id']
            new_before = datetime.now()
            code2, new_submit = api(f'/api/orders/{new_order_id}/submit', 'POST', {'operator_id': 1})
            new_after = datetime.now()
            check(f'[{scenario_name}] 重启后提交新订单返回 200', code2 == 200)
            if code2 == 200:
                new_expires_str = new_submit.get('expires_at')
                check(f'[{scenario_name}] 重启后新订单 expires_at 使用当前配置',
                      new_submit.get('config_expire_minutes_used') == cfg_after['reservation_expire_minutes'],
                      f'used={new_submit.get("config_expire_minutes_used")}, cfg={cfg_after["reservation_expire_minutes"]}')
                if new_expires_str:
                    try:
                        new_expires = datetime.fromisoformat(new_expires_str)
                        exp_min = new_before + timedelta(minutes=cfg_after['reservation_expire_minutes'])
                        exp_max = new_after + timedelta(minutes=cfg_after['reservation_expire_minutes'])
                        tolerance = timedelta(seconds=5)
                        check(f'[{scenario_name}] 重启后新订单 expires_at 与配置对齐',
                              exp_min - tolerance <= new_expires <= exp_max + tolerance,
                              f'预期[{exp_min}, {exp_max}], 实际={new_expires}')
                    except Exception as e:
                        check(f'[{scenario_name}] 重启后新订单 expires_at 格式合法', False, str(e))

        return cfg_after
    except Exception as e:
        check(f'[{scenario_name}] 跨重启验证', False, str(e))
        import traceback
        traceback.print_exc()
        return None
    finally:
        stop_server(proc)
        clean_env()


def run_scenario(name, env_vars, expected_minutes, expected_source, expected_fallback):
    print(f'\n{"=" * 70}')
    print(f'场景: {name}')
    print(f'预期: minutes={expected_minutes}, source={expected_source}, fallback={expected_fallback}')
    print('=' * 70)

    remove_db()
    proc = start_server(env_vars)
    try:
        if not wait_for_server():
            check(f'[{name}] 服务启动', False, '超时未就绪')
            return

        cfg = verify_config_chain(name, expected_minutes, expected_source, expected_fallback)
        if cfg is None:
            return

        order_id, reservation_id, expires_at_str = verify_order_config_consistency(name, cfg)

        verify_export_consistency(name, order_id, expires_at_str, cfg)
        verify_diagnose_alignment(name, order_id, reservation_id, cfg)

        stop_server(proc)
        proc = None

        verify_cross_restart_consistency(
            env_vars, name, order_id, expires_at_str, cfg
        )

    except Exception as e:
        check(f'[{name}] 场景执行', False, str(e))
        import traceback
        traceback.print_exc()
    finally:
        if proc is not None:
            try:
                stop_server(proc)
            except Exception:
                pass
        clean_env()


def verify_db_snapshot_consistency():
    print(f'\n{"=" * 70}')
    print('跨重启 DB 快照一致性验证')
    print('=' * 70)

    remove_db()
    proc = start_server(None)
    try:
        if not wait_for_server():
            check('[DB快照] 服务启动', False, '超时')
            return

        time.sleep(1)
        conn = db_conn()
        snapshots_1 = conn.execute('SELECT * FROM config_snapshots ORDER BY id').fetchall()
        conn.close()
        check('[DB快照] 首次启动写入 1 条快照', len(snapshots_1) == 1, f'实际={len(snapshots_1)}')

        if snapshots_1:
            s1 = snapshots_1[0]
            check('[DB快照] 快照1 effective_minutes == 30',
                  s1['effective_minutes'] == 30,
                  f'实际={s1["effective_minutes"]}')
            check('[DB快照] 快照1 source == default',
                  s1['source'] == 'default',
                  f'实际={s1["source"]}')
            check('[DB快照] 快照1 resolution_explanation 非空',
                  len(s1['resolution_explanation']) > 0)
            check('[DB快照] 快照1 raw_env_value 为 None',
                  s1['raw_env_value'] is None,
                  f'实际={s1["raw_env_value"]}')
            first_loaded_at = s1['loaded_at']
            first_explanation = s1['resolution_explanation']

        stop_server(proc)

        proc = start_server({'RESERVATION_EXPIRE_MINUTES': '5'})
        try:
            if not wait_for_server():
                check('[DB快照] 第二次启动', False, '超时')
                return

            time.sleep(1)
            conn = db_conn()
            snapshots_2 = conn.execute('SELECT * FROM config_snapshots ORDER BY id').fetchall()
            conn.close()
            check('[DB快照] 第二次启动累计 2 条快照',
                  len(snapshots_2) == 2, f'实际={len(snapshots_2)}')

            if len(snapshots_2) >= 2:
                s1_again = snapshots_2[0]
                s2 = snapshots_2[1]

                check('[DB快照] 首次快照 loaded_at 未变',
                      s1_again['loaded_at'] == first_loaded_at,
                      f'首次={first_loaded_at}, 当前={s1_again["loaded_at"]}')
                check('[DB快照] 首次快照 resolution_explanation 未变',
                      s1_again['resolution_explanation'] == first_explanation)

                check('[DB快照] 第二次快照 effective_minutes == 5',
                      s2['effective_minutes'] == 5,
                      f'实际={s2["effective_minutes"]}')
                check('[DB快照] 第二次快照 source == env',
                      s2['source'] == 'env',
                      f'实际={s2["source"]}')
                check('[DB快照] 第二次快照 raw_env_value == "5"',
                      s2['raw_env_value'] == '5',
                      f'实际={s2["raw_env_value"]}')
                check('[DB快照] 两次快照 loaded_at 不同',
                      s1_again['loaded_at'] != s2['loaded_at'],
                      f'首次={s1_again["loaded_at"]}, 二次={s2["loaded_at"]}')

            stop_server(proc)

            proc = start_server(None)
            try:
                if not wait_for_server():
                    check('[DB快照] 第三次启动', False, '超时')
                    return

                time.sleep(1)
                conn = db_conn()
                snapshots_3 = conn.execute('SELECT * FROM config_snapshots ORDER BY id').fetchall()
                conn.close()
                check('[DB快照] 第三次启动累计 3 条快照',
                      len(snapshots_3) == 3, f'实际={len(snapshots_3)}')

                if len(snapshots_3) >= 3:
                    s1_final = snapshots_3[0]
                    s2_final = snapshots_3[1]
                    s3 = snapshots_3[2]

                    check('[DB快照] 首次快照在第三次启动后仍完整',
                          s1_final['loaded_at'] == first_loaded_at and
                          s1_final['effective_minutes'] == 30 and
                          s1_final['source'] == 'default')
                    check('[DB快照] 第二次快照在第三次启动后仍完整',
                          s2_final['effective_minutes'] == 5 and
                          s2_final['source'] == 'env' and
                          s2_final['raw_env_value'] == '5')
                    check('[DB快照] 第三次快照恢复 default',
                          s3['effective_minutes'] == 30 and
                          s3['source'] == 'default' and
                          s3['raw_env_value'] is None,
                          f'实际: minutes={s3["effective_minutes"]}, source={s3["source"]}, raw={s3["raw_env_value"]}')

                code, diag = api('/api/config/diagnose')
                check('[DB快照] 第三次启动 diagnose 返回 200', code == 200)
                if code == 200:
                    all_snaps = diag.get('all_snapshots', [])
                    check('[DB快照] diagnose.all_snapshots 条数 == 3',
                          len(all_snaps) == 3, f'实际={len(all_snaps)}')
                    check('[DB快照] diagnose 最新快照 source == default',
                          all_snaps[-1]['source'] == 'default' if all_snaps else False)

            finally:
                stop_server(proc)
                clean_env()

        except Exception as e:
            check('[DB快照] 第二/三次验证', False, str(e))
            import traceback
            traceback.print_exc()
        finally:
            try:
                stop_server(proc)
            except Exception:
                pass
            clean_env()

    except Exception as e:
        check('[DB快照] 首次验证', False, str(e))
        import traceback
        traceback.print_exc()
    finally:
        try:
            stop_server(proc)
        except Exception:
            pass
        clean_env()


SCENARIOS = [
    ('默认值(未配置)', None, 30, 'default', False),
    ('显式短超时 2 分钟', {'RESERVATION_EXPIRE_MINUTES': '2'}, 2, 'env', False),
    ('非法值 abc(回退)', {'RESERVATION_EXPIRE_MINUTES': 'abc'}, 30, 'default(fallback)', True),
    ('非法值 负数 -5(回退)', {'RESERVATION_EXPIRE_MINUTES': '-5'}, 30, 'default(fallback)', True),
    ('非法值 零(回退)', {'RESERVATION_EXPIRE_MINUTES': '0'}, 30, 'default(fallback)', True),
    ('空字符串(默认)', {'RESERVATION_EXPIRE_MINUTES': ''}, 30, 'default', False),
]

print('=' * 70)
print('调拨服务预占超时配置排障诊断 - 完整回归验证')
print('=' * 70)
print(f'验证范围: 配置链路、接口/DB/审计/导出一致性、跨重启快照')
print(f'验证手段: API 返回 + DB 直查 + 诊断端点，不依赖日志匹配')

for name, env_vars, exp_min, exp_src, exp_fb in SCENARIOS:
    run_scenario(name, env_vars, exp_min, exp_src, exp_fb)

verify_db_snapshot_consistency()

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
