#!/usr/bin/env python3
import os
import sys
import json
import csv
import io
import subprocess
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config_diagnostic import (
    ConfigDiagnostic,
    ConfigResolution,
    ConfigSnapshot,
    DEFAULT_SNAPSHOT_DB,
    DIAGNOSTIC_LOG_FILE,
)


BASE = 'http://127.0.0.1:5000'
TEST_DB = 'test_config_diagnostic.db'
TEST_LOG = 'test_config_diagnostic.log'
TEST_CONFIG_FILE = 'test_config.json'


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


def clean_test_env():
    for f in [TEST_DB, TEST_LOG, TEST_CONFIG_FILE,
              'test_snapshots_export.json', 'test_snapshots_export.csv',
              'test_snapshots_import.json']:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass
    for k in ['TEST_RESERVATION_EXPIRE_MINUTES', 'TEST_DEBUG', 'TEST_LOG_LEVEL']:
        if k in os.environ:
            del os.environ[k]


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


def start_server(env_vars=None):
    clean_test_env()
    if env_vars:
        os.environ.update(env_vars)
    with open('diag_test_server.log', 'w', encoding='utf-8') as logf:
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
        p = 'emergency_supply.db' + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


def verify_log_contains(log_file: str, expected_patterns: list, scenario_name: str):
    if not os.path.exists(log_file):
        check(f'[{scenario_name}] 日志文件存在', False, f'文件不存在: {log_file}')
        return False

    with open(log_file, 'r', encoding='utf-8') as f:
        log_content = f.read()

    all_found = True
    for pattern in expected_patterns:
        found = pattern in log_content
        if not found:
            all_found = False
        check(f'[{scenario_name}] 日志包含 "{pattern[:50]}..."',
              found, f'未找到: {pattern}')

    return all_found


def verify_three_way_consistency(
    scenario_name: str,
    api_result: dict,
    db_snapshot: dict,
    export_data: dict,
    expected_values: dict
):
    check(f'[{scenario_name}] API effective_value == 预期 {expected_values["effective_value"]}',
          api_result.get('effective_value') == expected_values['effective_value'],
          f'API={api_result.get("effective_value")}')
    check(f'[{scenario_name}] API source == 预期 {expected_values["source"]}',
          api_result.get('source') == expected_values['source'],
          f'API={api_result.get("source")}')
    check(f'[{scenario_name}] API fallback == 预期 {expected_values["fallback"]}',
          api_result.get('fallback') == expected_values['fallback'],
          f'API={api_result.get("fallback")}')

    check(f'[{scenario_name}] DB effective_value == 预期 {expected_values["effective_value"]}',
          db_snapshot.get('effective_value') == expected_values['effective_value'],
          f'DB={db_snapshot.get("effective_value")}')
    check(f'[{scenario_name}] DB source == 预期 {expected_values["source"]}',
          db_snapshot.get('source') == expected_values['source'],
          f'DB={db_snapshot.get("source")}')

    check(f'[{scenario_name}] 导出数据 effective_value == 预期 {expected_values["effective_value"]}',
          export_data.get('effective_value') == expected_values['effective_value'],
          f'导出={export_data.get("effective_value")}')
    check(f'[{scenario_name}] 导出数据 source == 预期 {expected_values["source"]}',
          export_data.get('source') == expected_values['source'],
          f'导出={export_data.get("source")}')

    check(f'[{scenario_name}] API <-> DB 一致',
          api_result.get('effective_value') == db_snapshot.get('effective_value') and
          api_result.get('source') == db_snapshot.get('source') and
          api_result.get('fallback') == db_snapshot.get('fallback'))

    check(f'[{scenario_name}] API <-> 导出一致',
          api_result.get('effective_value') == export_data.get('effective_value') and
          api_result.get('source') == export_data.get('source'))

    check(f'[{scenario_name}] DB <-> 导出一致',
          db_snapshot.get('effective_value') == export_data.get('effective_value') and
          db_snapshot.get('source') == export_data.get('source'))


def run_unit_tests():
    print_section('单元测试: ConfigDiagnostic 核心功能')

    clean_test_env()

    diag = ConfigDiagnostic(
        config_file=TEST_CONFIG_FILE,
        snapshot_db=TEST_DB,
        log_file=TEST_LOG,
    )

    print('\n--- 测试场景 1: 默认值 (无配置) ---')
    r1 = diag.resolve_config(
        key='test_default',
        default_value=30,
        env_key='TEST_RESERVATION_EXPIRE_MINUTES',
        value_parser=int,
        validator=lambda v: v > 0,
    )
    check('[默认值] effective_value == 30', r1.effective_value == 30, f'实际={r1.effective_value}')
    check('[默认值] source == "default"', r1.source == 'default', f'实际={r1.source}')
    check('[默认值] fallback == False', r1.fallback == False)
    check('[默认值] raw_env_value == None', r1.raw_env_value is None)
    check('[默认值] resolution_explanation 非空', len(r1.resolution_explanation) > 0)
    check('[默认值] loaded_at 为 ISO 格式', isinstance(r1.loaded_at, str) and len(r1.loaded_at) > 0)
    check('[默认值] conflict_detected == False', r1.conflict_detected == False)

    print('\n--- 测试场景 2: 显式配置 (环境变量) ---')
    os.environ['TEST_RESERVATION_EXPIRE_MINUTES'] = '5'
    r2 = diag.resolve_config(
        key='test_env',
        default_value=30,
        env_key='TEST_RESERVATION_EXPIRE_MINUTES',
        value_parser=int,
        validator=lambda v: v > 0,
    )
    check('[显式配置] effective_value == 5', r2.effective_value == 5, f'实际={r2.effective_value}')
    check('[显式配置] source == "env"', r2.source == 'env', f'实际={r2.source}')
    check('[显式配置] fallback == False', r2.fallback == False)
    check('[显式配置] raw_env_value == "5"', r2.raw_env_value == '5')
    del os.environ['TEST_RESERVATION_EXPIRE_MINUTES']

    print('\n--- 测试场景 3: 非法值回退 (非数字) ---')
    os.environ['TEST_RESERVATION_EXPIRE_MINUTES'] = 'abc'
    r3 = diag.resolve_config(
        key='test_fallback_invalid',
        default_value=30,
        env_key='TEST_RESERVATION_EXPIRE_MINUTES',
        value_parser=int,
        validator=lambda v: v > 0,
    )
    check('[非法值回退] effective_value == 30', r3.effective_value == 30, f'实际={r3.effective_value}')
    check('[非法值回退] source == "default(fallback)"', r3.source == 'default(fallback)', f'实际={r3.source}')
    check('[非法值回退] fallback == True', r3.fallback == True)
    check('[非法值回退] fallback_reason 包含"非法"',
          r3.fallback_reason and '非法' in r3.fallback_reason,
          f'实际={r3.fallback_reason}')
    del os.environ['TEST_RESERVATION_EXPIRE_MINUTES']

    print('\n--- 测试场景 4: 非法值回退 (负数) ---')
    os.environ['TEST_RESERVATION_EXPIRE_MINUTES'] = '-5'
    r4 = diag.resolve_config(
        key='test_fallback_negative',
        default_value=30,
        env_key='TEST_RESERVATION_EXPIRE_MINUTES',
        value_parser=int,
        validator=lambda v: v > 0,
    )
    check('[负数回退] effective_value == 30', r4.effective_value == 30, f'实际={r4.effective_value}')
    check('[负数回退] source == "default(fallback)"', r4.source == 'default(fallback)', f'实际={r4.source}')
    check('[负数回退] fallback == True', r4.fallback == True)
    check('[负数回退] fallback_reason 包含"非正数"或"验证失败"',
          r4.fallback_reason and ('非正数' in r4.fallback_reason or '验证失败' in r4.fallback_reason),
          f'实际={r4.fallback_reason}')
    del os.environ['TEST_RESERVATION_EXPIRE_MINUTES']

    print('\n--- 测试场景 5: 配置文件配置 ---')
    with open(TEST_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump({'test_config_key': 15}, f)

    r5 = diag.resolve_config(
        key='test_config_file',
        default_value=30,
        config_key='test_config_key',
        value_parser=int,
        validator=lambda v: v > 0,
    )
    check('[配置文件] effective_value == 15', r5.effective_value == 15, f'实际={r5.effective_value}')
    check('[配置文件] source == "config_file"', r5.source == 'config_file', f'实际={r5.source}')
    check('[配置文件] raw_config_value == 15', r5.raw_config_value == 15, f'实际={r5.raw_config_value}')

    print('\n--- 测试场景 6: 多来源冲突 (配置文件 vs 环境变量) ---')
    os.environ['TEST_CONFLICT'] = '20'
    with open(TEST_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump({'test_conflict': 10}, f)

    r6 = diag.resolve_config(
        key='test_conflict',
        default_value=30,
        env_key='TEST_CONFLICT',
        config_key='test_conflict',
        value_parser=int,
        validator=lambda v: v > 0,
    )
    check('[多来源冲突] conflict_detected == True', r6.conflict_detected == True)
    check('[多来源冲突] conflict_details 非空', r6.conflict_details and len(r6.conflict_details) > 0)
    check('[多来源冲突] 优先级生效 (config_file > env)',
          r6.effective_value == 10, f'实际={r6.effective_value}, 预期=10 (配置文件优先级更高)')
    check('[多来源冲突] source == "config_file"', r6.source == 'config_file', f'实际={r6.source}')
    del os.environ['TEST_CONFLICT']

    print('\n--- 测试场景 7: 空字符串视为未设置 ---')
    os.environ['TEST_EMPTY'] = ''
    r7 = diag.resolve_config(
        key='test_empty',
        default_value=30,
        env_key='TEST_EMPTY',
        value_parser=int,
        validator=lambda v: v > 0,
    )
    check('[空字符串] source == "default"', r7.source == 'default', f'实际={r7.source}')
    check('[空字符串] effective_value == 30', r7.effective_value == 30, f'实际={r7.effective_value}')
    del os.environ['TEST_EMPTY']

    print('\n--- 测试场景 8: 零值回退 ---')
    os.environ['TEST_ZERO'] = '0'
    r8 = diag.resolve_config(
        key='test_zero',
        default_value=30,
        env_key='TEST_ZERO',
        value_parser=int,
        validator=lambda v: v > 0,
    )
    check('[零值回退] effective_value == 30', r8.effective_value == 30, f'实际={r8.effective_value}')
    check('[零值回退] source == "default(fallback)"', r8.source == 'default(fallback)', f'实际={r8.source}')
    check('[零值回退] fallback == True', r8.fallback == True)
    del os.environ['TEST_ZERO']

    print('\n--- 测试场景 9: 快照查询功能 ---')
    latest = diag.get_latest_snapshot()
    check('[快照查询] get_latest_snapshot 返回非空', latest is not None)
    if latest:
        check('[快照查询] 最新快照有有效配置', latest.effective_value is not None)

    all_snaps = diag.get_all_snapshots()
    check(f'[快照查询] get_all_snapshots 返回 {len(all_snaps)} 条', len(all_snaps) >= 8)

    snaps_by_key = diag.get_all_snapshots(key='test_env')
    check(f'[快照查询] 按键查询返回 {len(snaps_by_key)} 条', len(snaps_by_key) >= 1)
    if snaps_by_key:
        check('[快照查询] 按键查询结果正确', snaps_by_key[0].config_key == 'test_env')

    boot_snaps = diag.get_snapshots_by_boot(diag._boot_sequence)
    check(f'[快照查询] 按启动批次查询返回 {len(boot_snaps)} 条', len(boot_snaps) >= 8)

    print('\n--- 测试场景 10: 诊断功能 ---')
    diag_result = diag.diagnose(key='test_env')
    check('[诊断] diagnose_at 存在', 'diagnose_at' in diag_result)
    check('[诊断] current_config 存在', 'current_config' in diag_result)
    check('[诊断] latest_snapshot 存在', 'latest_snapshot' in diag_result)
    check('[诊断] all_snapshots 为列表', isinstance(diag_result.get('all_snapshots'), list))
    check('[诊断] boot_sequences 包含当前批次', diag._boot_sequence in diag_result.get('boot_sequences', []))

    print('\n--- 测试场景 11: 快照比较功能 ---')
    if len(all_snaps) >= 2:
        comp_result = diag.compare_snapshots(all_snaps[-1].snapshot_uuid, all_snaps[0].snapshot_uuid)
        check('[快照比较] 返回结果包含 differences', 'differences' in comp_result)
        check('[快照比较] 返回结果包含 identical 字段', 'identical' in comp_result)

    print('\n--- 测试场景 12: 导出功能 (JSON/CSV) ---')
    json_export = diag.export_snapshots(fmt='json')
    check('[导出] JSON 导出非空', len(json_export) > 0)
    json_data = json.loads(json_export)
    check('[导出] JSON 包含 snapshots 字段', 'snapshots' in json_data)
    check('[导出] JSON snapshots 数量正确', len(json_data['snapshots']) == len(all_snaps))

    csv_export = diag.export_snapshots(fmt='csv')
    check('[导出] CSV 导出非空', len(csv_export) > 0)
    csv_reader = csv.reader(io.StringIO(csv_export))
    csv_rows = list(csv_reader)
    check('[导出] CSV 有表头', len(csv_rows) > 1)
    check('[导出] CSV 行数正确', len(csv_rows) == len(all_snaps) + 1)

    diag.export_to_file('test_snapshots_export.json')
    check('[导出] JSON 文件导出成功', os.path.exists('test_snapshots_export.json'))

    diag.export_to_file('test_snapshots_export.csv')
    check('[导出] CSV 文件导出成功', os.path.exists('test_snapshots_export.csv'))

    print('\n--- 测试场景 13: 导入功能 ---')
    import_count = diag.import_snapshots('test_snapshots_export.json')
    check(f'[导入] 成功导入 {import_count} 条 (幂等性测试)', import_count == 0)

    with open('test_snapshots_import.json', 'w', encoding='utf-8') as f:
        json.dump({
            'exported_at': datetime.now().isoformat(),
            'export_source': 'test',
            'snapshots': [{
                'snapshot_uuid': 'test-uuid-import-001',
                'snapshot_at': datetime.now().isoformat(),
                'config_key': 'test_import',
                'effective_value': '999',
                'source': 'imported',
                'fallback': 0,
                'resolution_explanation': '测试导入数据',
                'loaded_at': datetime.now().isoformat(),
                'conflict_detected': 0,
                'boot_sequence': 999,
                'process_id': 12345,
            }]
        }, f)

    import_count2 = diag.import_snapshots('test_snapshots_import.json')
    check(f'[导入] 成功导入新数据 {import_count2} 条', import_count2 == 1)

    print('\n--- 测试场景 14: 日志验证 ---')
    log_patterns = [
        '配置解析完成',
        '快照已保存',
        '快照数据库初始化完成',
    ]
    verify_log_contains(TEST_LOG, log_patterns, '日志验证')

    print('\n--- 测试场景 15: get_current_config 功能 ---')
    current_all = diag.get_current_config_dict()
    check('[当前配置] get_current_config_dict 返回列表', isinstance(current_all, list))
    check('[当前配置] 列表长度正确', len(current_all) >= 8)

    current_single = diag.get_current_config_dict(key='test_env')
    check('[当前配置] 单键查询返回字典', isinstance(current_single, dict))
    if current_single:
        check('[当前配置] 单键查询正确', current_single.get('key') == 'test_env')

    print('\n--- 测试场景 16: 三边一致性验证 (内存/DB/导出) ---')
    api_like = diag.get_current_config_dict(key='test_env')
    db_like = diag.get_latest_snapshot(key='test_env')
    export_like = json_data['snapshots']
    export_test_env = next((s for s in export_like if s['config_key'] == 'test_env'), None)

    if api_like and db_like and export_test_env:
        verify_three_way_consistency(
            '三边一致性',
            api_like,
            db_like.to_dict(),
            export_test_env,
            {'effective_value': 5, 'source': 'env', 'fallback': False}
        )

    clean_test_env()


def run_cross_restart_tests():
    print_section('集成测试: 跨重启一致性')

    remove_db()

    print('\n--- 集成场景 1: 默认值启动 ---')
    proc = start_server(None)
    try:
        if not wait_for_server():
            check('[默认值启动] 服务启动', False, '超时')
            return

        code, cfg = api('/api/config/v2')
        check('[默认值启动] /api/config/v2 返回 200', code == 200, f'code={code}')
        if code == 200:
            env_config = next((c for c in cfg if c['key'] == 'reservation_expire_minutes'), None)
            check('[默认值启动] effective_value == 30',
                  env_config and env_config['effective_value'] == 30,
                  f'实际={env_config["effective_value"] if env_config else None}')
            check('[默认值启动] source == "default"',
                  env_config and env_config['source'] == 'default',
                  f'实际={env_config["source"] if env_config else None}')

        boot_1_sequence = None
        code, diag_result = api('/api/config/v2/diagnose?key=reservation_expire_minutes')
        check('[默认值启动] /api/config/v2/diagnose 返回 200', code == 200)
        if code == 200:
            boot_1_sequence = diag_result.get('current_boot_sequence')
            check('[默认值启动] 当前启动批次 > 0', boot_1_sequence and boot_1_sequence > 0)
            check('[默认值启动] latest_snapshot 存在', diag_result.get('latest_snapshot') is not None)

        stop_server(proc)
        proc = None
        time.sleep(1)

        print('\n--- 集成场景 2: 环境变量显式配置启动 ---')
        proc = start_server({'RESERVATION_EXPIRE_MINUTES': '5'})
        try:
            if not wait_for_server():
                check('[显式配置启动] 服务启动', False, '超时')
                return

            code, cfg2 = api('/api/config/v2')
            check('[显式配置启动] /api/config/v2 返回 200', code == 200)
            if code == 200:
                env_config2 = next((c for c in cfg2 if c['key'] == 'reservation_expire_minutes'), None)
                check('[显式配置启动] effective_value == 5',
                      env_config2 and env_config2['effective_value'] == 5,
                      f'实际={env_config2["effective_value"] if env_config2 else None}')
                check('[显式配置启动] source == "env"',
                      env_config2 and env_config2['source'] == 'env',
                      f'实际={env_config2["source"] if env_config2 else None}')

            code, diag2 = api('/api/config/v2/diagnose?key=reservation_expire_minutes')
            check('[显式配置启动] /api/config/v2/diagnose 返回 200', code == 200)
            if code == 200:
                boot_2_sequence = diag2.get('current_boot_sequence')
                check('[显式配置启动] 启动批次递增',
                      boot_1_sequence and boot_2_sequence and boot_2_sequence == boot_1_sequence + 1,
                      f'批次1={boot_1_sequence}, 批次2={boot_2_sequence}')

                all_snaps = diag2.get('all_snapshots', [])
                check('[显式配置启动] 累计快照数 >= 2', len(all_snaps) >= 2, f'实际={len(all_snaps)}')

                boot_sequences = sorted(set(s['boot_sequence'] for s in all_snaps))
                check('[显式配置启动] 两个不同启动批次', len(boot_sequences) >= 2)

                cb = diag2.get('cross_boot_consistency')
                check('[显式配置启动] cross_boot_consistency 存在', cb is not None)
                if cb:
                    check('[显式配置启动] 跨重启生效值不一致 (预期)',
                          cb['effective_value_consistent'] == False)
                    check('[显式配置启动] 跨重启来源不一致 (预期)',
                          cb['source_consistent'] == False)

            stop_server(proc)
            proc = None
            time.sleep(1)

            print('\n--- 集成场景 3: 非法值回退启动 ---')
            proc = start_server({'RESERVATION_EXPIRE_MINUTES': 'abc'})
            try:
                if not wait_for_server():
                    check('[非法值启动] 服务启动', False, '超时')
                    return

                code, cfg3 = api('/api/config/v2')
                check('[非法值启动] /api/config/v2 返回 200', code == 200)
                if code == 200:
                    env_config3 = next((c for c in cfg3 if c['key'] == 'reservation_expire_minutes'), None)
                    check('[非法值启动] effective_value == 30 (回退)',
                          env_config3 and env_config3['effective_value'] == 30,
                          f'实际={env_config3["effective_value"] if env_config3 else None}')
                    check('[非法值启动] source == "default(fallback)"',
                          env_config3 and env_config3['source'] == 'default(fallback)',
                          f'实际={env_config3["source"] if env_config3 else None}')
                    check('[非法值启动] fallback == True',
                          env_config3 and env_config3['fallback'] == True)
                    check('[非法值启动] fallback_reason 包含"非法"',
                          env_config3 and env_config3.get('fallback_reason') and
                          '非法' in env_config3['fallback_reason'])
                    check('[非法值启动] resolution_explanation 非空',
                          env_config3 and len(env_config3.get('resolution_explanation', '')) > 0)

                stop_server(proc)
                proc = None
                time.sleep(1)

                print('\n--- 集成场景 4: 同配置重启一致性验证 ---')
                proc = start_server({'RESERVATION_EXPIRE_MINUTES': '5'})
                try:
                    if not wait_for_server():
                        check('[同配置重启] 服务启动', False, '超时')
                        return

                    code, diag4 = api('/api/config/v2/diagnose?key=reservation_expire_minutes')
                    check('[同配置重启] /api/config/v2/diagnose 返回 200', code == 200)
                    if code == 200:
                        all_snaps4 = diag4.get('all_snapshots', [])
                        env_snaps = [s for s in all_snaps4 if s['source'] == 'env' and s['effective_value'] == 5]
                        check('[同配置重启] 至少2条 env 来源快照', len(env_snaps) >= 2, f'实际={len(env_snaps)}')

                        if len(env_snaps) >= 2:
                            s1 = env_snaps[0]
                            s2 = env_snaps[1]
                            check('[同配置重启] 两次 env 配置生效值一致',
                                  s1['effective_value'] == s2['effective_value'])
                            check('[同配置重启] 两次 env 配置来源一致',
                                  s1['source'] == s2['source'])
                            check('[同配置重启] 两次启动批次不同',
                                  s1['boot_sequence'] != s2['boot_sequence'])

                finally:
                    stop_server(proc)

            finally:
                if proc is not None:
                    stop_server(proc)

        finally:
            if proc is not None:
                stop_server(proc)

    except Exception as e:
        check('[跨重启测试] 异常', False, str(e))
        import traceback
        traceback.print_exc()
    finally:
        if proc is not None:
            try:
                stop_server(proc)
            except Exception:
                pass

    remove_db()


def run_export_import_api_tests():
    print_section('集成测试: API 导出导入功能')

    remove_db()
    proc = start_server({'RESERVATION_EXPIRE_MINUTES': '10'})
    try:
        if not wait_for_server():
            check('[导出导入API] 服务启动', False, '超时')
            return

        print('\n--- API 场景 1: 导出 JSON ---')
        req = urllib.request.Request(BASE + '/api/config/v2/snapshots/export.json')
        with urllib.request.urlopen(req) as resp:
            json_text = resp.read().decode('utf-8')
        json_data = json.loads(json_text)
        check('[导出JSON] 包含 snapshots 字段', 'snapshots' in json_data)
        check('[导出JSON] 至少1条快照', len(json_data.get('snapshots', [])) >= 1)

        print('\n--- API 场景 2: 导出 CSV ---')
        req = urllib.request.Request(BASE + '/api/config/v2/snapshots/export.csv')
        with urllib.request.urlopen(req) as resp:
            csv_text = resp.read().decode('utf-8-sig')
        csv_reader = csv.reader(io.StringIO(csv_text))
        csv_rows = list(csv_reader)
        check('[导出CSV] 有数据', len(csv_rows) > 1)
        check('[导出CSV] 表头包含 config_key', 'config_key' in csv_rows[0])

        print('\n--- API 场景 3: 快照列表查询 ---')
        code, snaps = api('/api/config/v2/snapshots')
        check('[快照列表] 返回 200', code == 200)
        check('[快照列表] 至少1条', len(snaps) >= 1)

        code, latest_snap = api('/api/config/v2/snapshots?latest=true')
        check('[最新快照] 返回 200', code == 200)
        check('[最新快照] 非空', latest_snap is not None)

        print('\n--- API 场景 4: 诊断接口 ---')
        code, diag = api('/api/config/v2/diagnose?key=reservation_expire_minutes')
        check('[诊断接口] 返回 200', code == 200)
        if code == 200:
            check('[诊断接口] 包含 current_config', 'current_config' in diag)
            check('[诊断接口] 包含 latest_snapshot', 'latest_snapshot' in diag)
            check('[诊断接口] 包含 all_snapshots', 'all_snapshots' in diag)
            check('[诊断接口] 包含 boot_sequences', 'boot_sequences' in diag)

        print('\n--- API 场景 5: 三边一致性 (API/DB/导出) ---')
        api_config = diag.get('current_config')
        db_snap = diag.get('latest_snapshot')
        export_snap = json_data['snapshots'][0] if json_data.get('snapshots') else None

        if api_config and db_snap and export_snap:
            verify_three_way_consistency(
                'API三边一致性',
                api_config,
                db_snap,
                export_snap,
                {'effective_value': 10, 'source': 'env', 'fallback': False}
            )

        print('\n--- API 场景 6: 日志验证 ---')
        verify_log_contains(
            DIAGNOSTIC_LOG_FILE,
            [
                'reservation_expire_minutes',
                '配置解析完成',
                '快照已保存',
            ],
            'API日志'
        )

    except Exception as e:
        check('[导出导入API] 异常', False, str(e))
        import traceback
        traceback.print_exc()
    finally:
        stop_server(proc)
        remove_db()


def run_cli_tests():
    print_section('CLI 工具测试')

    clean_test_env()

    def run_cli_cmd(args):
        try:
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            env['PYTHONUNBUFFERED'] = '1'

            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                cwd=os.path.dirname(os.path.abspath(__file__)),
                env=env
            )
            out, err = proc.communicate(timeout=30)

            class Result:
                def __init__(self, rc, so, se):
                    self.returncode = rc
                    self.stdout = so or ''
                    self.stderr = se or ''

            return Result(proc.returncode, out, err)
        except Exception as e:
            class DummyResult:
                returncode = 1
                stdout = ''
                stderr = str(e)
            return DummyResult()

    print('\n--- CLI 场景 1: resolve 命令 ---')
    result = run_cli_cmd(
        [sys.executable, 'config_diagnostic_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'resolve', '--key', 'cli_test', '--default', '30',
         '--type', 'int', '--min', '1']
    )
    check('[CLI resolve] 命令成功', result.returncode == 0, f'stderr={result.stderr}')
    check('[CLI resolve] 输出包含 effective_value', result.stdout and '生效值' in result.stdout)
    check('[CLI resolve] 输出包含 source', result.stdout and '来源' in result.stdout)

    print('\n--- CLI 场景 2: current 命令 ---')
    result = run_cli_cmd(
        [sys.executable, 'config_diagnostic_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'current', '--key', 'cli_test']
    )
    check('[CLI current] 命令成功', result.returncode == 0)
    check('[CLI current] 输出包含配置项', result.stdout and 'cli_test' in result.stdout)

    print('\n--- CLI 场景 3: snapshot --latest 命令 ---')
    result = run_cli_cmd(
        [sys.executable, 'config_diagnostic_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'snapshot', '--latest']
    )
    check('[CLI snapshot latest] 命令成功', result.returncode == 0)
    check('[CLI snapshot latest] 输出包含快照信息', result.stdout and ('effective_value' in result.stdout or '最新快照' in result.stdout))

    print('\n--- CLI 场景 4: snapshot --all 命令 ---')
    result = run_cli_cmd(
        [sys.executable, 'config_diagnostic_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'snapshot', '--all', '--limit', '5']
    )
    check('[CLI snapshot all] 命令成功', result.returncode == 0)

    print('\n--- CLI 场景 5: diagnose 命令 ---')
    result = run_cli_cmd(
        [sys.executable, 'config_diagnostic_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'diagnose', '--key', 'cli_test']
    )
    check('[CLI diagnose] 命令成功', result.returncode == 0)
    check('[CLI diagnose] 输出包含诊断信息', result.stdout and '诊断报告' in result.stdout)

    print('\n--- CLI 场景 6: export 命令 (JSON) ---')
    result = run_cli_cmd(
        [sys.executable, 'config_diagnostic_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'export', '--output', 'test_cli_export.json']
    )
    check('[CLI export JSON] 命令成功', result.returncode == 0)
    check('[CLI export JSON] 文件存在', os.path.exists('test_cli_export.json'))

    print('\n--- CLI 场景 7: export 命令 (CSV) ---')
    result = run_cli_cmd(
        [sys.executable, 'config_diagnostic_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'export', '--output', 'test_cli_export.csv', '--format', 'csv']
    )
    check('[CLI export CSV] 命令成功', result.returncode == 0)
    check('[CLI export CSV] 文件存在', os.path.exists('test_cli_export.csv'))

    print('\n--- CLI 场景 8: logs 命令 ---')
    result = run_cli_cmd(
        [sys.executable, 'config_diagnostic_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'logs', '--tail', '10']
    )
    check('[CLI logs] 命令成功', result.returncode == 0)

    print('\n--- CLI 场景 9: import 命令 ---')
    result = run_cli_cmd(
        [sys.executable, 'config_diagnostic_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'import', '--input', 'test_cli_export.json']
    )
    check('[CLI import] 命令成功', result.returncode == 0)

    print('\n--- CLI 场景 10: clear 命令 ---')
    result = run_cli_cmd(
        [sys.executable, 'config_diagnostic_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'clear', '--yes']
    )
    check('[CLI clear] 命令成功', result.returncode == 0)

    clean_test_env()
    for f in ['test_cli_export.json', 'test_cli_export.csv']:
        if os.path.exists(f):
            os.remove(f)


def print_section(title):
    line = '=' * 70
    print(f'\n{line}')
    print(f'  {title}')
    print(line)


def main():
    print('=' * 70)
    print('配置诊断与快照模块 - 完整测试套件')
    print('=' * 70)
    print(f'测试范围: 单元测试、集成测试、CLI测试、跨重启一致性、三边一致性')
    print(f'测试场景: 默认值、显式配置、非法值、冲突、重启对比、导出导入')
    print('=' * 70)

    try:
        run_unit_tests()
        run_cross_restart_tests()
        run_export_import_api_tests()
        run_cli_tests()
    except Exception as e:
        print(f'\n测试执行异常: {e}')
        import traceback
        traceback.print_exc()
    finally:
        clean_test_env()

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


if __name__ == '__main__':
    main()
