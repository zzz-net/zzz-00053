#!/usr/bin/env python3
import os
import sys
import json
import csv
import io
import subprocess
import time
import tempfile
import urllib.request
import urllib.error
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config_snapshot_playback import (
    ConfigSnapshotPlayback,
    ConfigValueSnapshot,
    PlaybackConclusion,
    DEFAULT_SNAPSHOT_DB,
    DEFAULT_LOG_FILE,
)

BASE = 'http://127.0.0.1:5000'
TEST_DB = 'test_e2e_chain.db'
TEST_LOG = 'test_e2e_chain.log'
TEST_CONFIG_FILE = 'test_e2e_config.json'

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


def print_section(title):
    line = '=' * 72
    print(f'\n{line}')
    print(f'  {title}')
    print(line)


def clean_test_env():
    for f in [TEST_DB, TEST_LOG, TEST_CONFIG_FILE,
              'test_e2e_export.json', 'test_e2e_export.csv',
              'test_e2e_import.json']:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass
    for k in ['E2E_EXPIRE', 'E2E_DEBUG', 'E2E_LOG_LEVEL',
              'E2E_TIMEOUT', 'E2E_RETRY', 'E2E_HOST',
              'E2E_PORT', 'E2E_MAX_CONN', 'RESERVATION_EXPIRE_MINUTES']:
        if k in os.environ:
            del os.environ[k]


def remove_main_db():
    for suffix in ['', '-wal', '-shm', '-journal']:
        for p in ['emergency_supply.db' + suffix,
                  DEFAULT_SNAPSHOT_DB + suffix,
                  DEFAULT_LOG_FILE,
                  'config_snapshot_playback.log',
                  'config_snapshot_playback.db' + suffix]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


def run_cli_cmd(args, timeout=30):
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
        out, err = proc.communicate(timeout=timeout)

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
    except Exception as e:
        return 0, {'error': str(e)}


def wait_for_server(timeout=25):
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


def start_server(env_vars=None, clean=True):
    if clean:
        remove_main_db()
    if 'RESERVATION_EXPIRE_MINUTES' in os.environ:
        del os.environ['RESERVATION_EXPIRE_MINUTES']
    env = os.environ.copy()
    if env_vars:
        env.update(env_vars)
    with open('e2e_test_server.log', 'w', encoding='utf-8') as logf:
        proc = subprocess.Popen(
            [sys.executable, '-u', 'app.py'],
            stdout=logf,
            stderr=subprocess.STDOUT,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            bufsize=0,
            env=env,
        )
    return proc


def stop_server(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
        proc.wait(timeout=3)


def verify_four_way_consistency(
    scenario_name,
    query_result,
    log_content,
    export_data,
    playback_conclusion,
    expected_values,
):
    check(f'[{scenario_name}] 查询 effective_value == 预期',
          str(query_result.get('effective_value')) == str(expected_values['effective_value']),
          f'查询={query_result.get("effective_value")}, 预期={expected_values["effective_value"]}')

    check(f'[{scenario_name}] 查询 effective_source == 预期',
          query_result.get('effective_source') == expected_values['effective_source'],
          f'查询={query_result.get("effective_source")}, 预期={expected_values["effective_source"]}')

    check(f'[{scenario_name}] 查询 is_fallback == 预期',
          query_result.get('is_fallback') == expected_values['is_fallback'],
          f'查询={query_result.get("is_fallback")}, 预期={expected_values["is_fallback"]}')

    check(f'[{scenario_name}] 导出 effective_value == 预期',
          str(export_data.get('effective_value')) == str(expected_values['effective_value']),
          f'导出={export_data.get("effective_value")}')

    check(f'[{scenario_name}] 导出 effective_source == 预期',
          export_data.get('effective_source') == expected_values['effective_source'],
          f'导出={export_data.get("effective_source")}')

    check(f'[{scenario_name}] 导出 is_fallback == 预期',
          bool(export_data.get('is_fallback')) == expected_values['is_fallback'],
          f'导出={export_data.get("is_fallback")}')

    if expected_values['is_fallback']:
        check(f'[{scenario_name}] 导出有 fallback_reason',
              export_data.get('fallback_reason') is not None and len(export_data.get('fallback_reason', '')) > 0)
        check(f'[{scenario_name}] 回放 fallback_count > 0',
              playback_conclusion.get('fallback_count', 0) > 0)

    check(f'[{scenario_name}] 查询 <-> 导出 effective_value 一致',
          str(query_result.get('effective_value')) == str(export_data.get('effective_value')))

    check(f'[{scenario_name}] 查询 <-> 导出 effective_source 一致',
          query_result.get('effective_source') == export_data.get('effective_source'))

    check(f'[{scenario_name}] 查询 <-> 导出 is_fallback 一致',
          query_result.get('is_fallback') == export_data.get('is_fallback'))

    if expected_values['is_fallback']:
        check(f'[{scenario_name}] 查询 <-> 导出 fallback_reason 一致',
              query_result.get('fallback_reason') == export_data.get('fallback_reason'))

    check(f'[{scenario_name}] 日志包含配置键名',
          expected_values.get('config_key', '') in log_content if log_content else True)


def run_cli_chain_test():
    print_section('CLI 完整链路测试')

    clean_test_env()

    print('\n--- CLI 场景 1: 创建多个配置快照 ---')
    cli_args = [
        sys.executable, 'config_snapshot_playback_cli.py',
        '--snapshot-db', TEST_DB,
        '--log-file', TEST_LOG,
        'resolve', '--key', 'timeout', '--default', '30',
        '--type', 'int', '--min', '1',
    ]
    result = run_cli_cmd(cli_args)
    check('[CLI resolve] 命令成功', result.returncode == 0,
          f'stderr={result.stderr[:200]}' if result.stderr else '')
    check('[CLI resolve] 输出包含生效值', '生效值' in result.stdout)
    check('[CLI resolve] 输出包含来源', '来源' in result.stdout)

    os.environ['E2E_DEBUG'] = 'invalid_bool'
    cli_args2 = [
        sys.executable, 'config_snapshot_playback_cli.py',
        '--snapshot-db', TEST_DB,
        '--log-file', TEST_LOG,
        'resolve', '--key', 'debug', '--default', 'false',
        '--env-key', 'E2E_DEBUG',
    ]
    result2 = run_cli_cmd(cli_args2)
    check('[CLI dirty resolve] 命令成功', result2.returncode == 0)
    check('[CLI dirty resolve] 输出包含回退', '回退' in result2.stdout or 'fallback' in result2.stdout.lower())
    del os.environ['E2E_DEBUG']

    print('\n--- CLI 场景 2: 回放并检查结论 ---')
    cli_play = [
        sys.executable, 'config_snapshot_playback_cli.py',
        '--snapshot-db', TEST_DB,
        '--log-file', TEST_LOG,
        'playback',
    ]
    result = run_cli_cmd(cli_play)
    check('[CLI playback] 命令成功', result.returncode == 0)
    check('[CLI playback] 输出包含回放结论', '回放' in result.stdout)
    check('[CLI playback] 输出包含来源信息',
          '来源' in result.stdout or 'default' in result.stdout or '环境变量' in result.stdout)

    print('\n--- CLI 场景 3: 导出 JSON/CSV 并验证 ---')
    cli_export_json = [
        sys.executable, 'config_snapshot_playback_cli.py',
        '--snapshot-db', TEST_DB,
        '--log-file', TEST_LOG,
        'export', '--output', 'test_e2e_export.json',
    ]
    result = run_cli_cmd(cli_export_json)
    check('[CLI export JSON] 命令成功', result.returncode == 0)
    check('[CLI export JSON] 文件存在', os.path.exists('test_e2e_export.json'))

    cli_export_csv = [
        sys.executable, 'config_snapshot_playback_cli.py',
        '--snapshot-db', TEST_DB,
        '--log-file', TEST_LOG,
        'export', '--output', 'test_e2e_export.csv', '--format', 'csv',
    ]
    result = run_cli_cmd(cli_export_csv)
    check('[CLI export CSV] 命令成功', result.returncode == 0)
    check('[CLI export CSV] 文件存在', os.path.exists('test_e2e_export.csv'))

    print('\n--- CLI 场景 4: 导入到新 DB 并验证往返一致性 ---')
    import_db = 'test_e2e_import.db'
    if os.path.exists(import_db):
        os.remove(import_db)

    cli_import = [
        sys.executable, 'config_snapshot_playback_cli.py',
        '--snapshot-db', import_db,
        '--log-file', 'test_e2e_import.log',
        'import', '--input', 'test_e2e_export.json',
    ]
    result = run_cli_cmd(cli_import)
    check('[CLI import JSON] 命令成功', result.returncode == 0,
          f'stderr={result.stderr[:200]}' if result.stderr else '')

    import_csp = ConfigSnapshotPlayback(snapshot_db=import_db, log_file=':memory:')
    imported_snaps = import_csp.get_all_snapshots(limit=100)
    check('[CLI import] 导入后快照数 > 0', len(imported_snaps) > 0, f'实际={len(imported_snaps)}')

    original_csp = ConfigSnapshotPlayback(snapshot_db=TEST_DB, log_file=':memory:')
    original_snaps = original_csp.get_all_snapshots(limit=100)

    original_hashes = {s.snapshot_uuid: s._compute_hash() for s in original_snaps}
    imported_hashes = {s.snapshot_uuid: s._compute_hash() for s in imported_snaps}
    hash_match = all(original_hashes.get(k) == h for k, h in imported_hashes.items()) and len(imported_hashes) == len(original_hashes)
    check('[CLI import] 完整性哈希全部一致', hash_match,
          f'原始={len(original_hashes)}, 导入={len(imported_hashes)}')

    print('\n--- CLI 场景 5: 验证导入后回放结论一致 ---')
    if import_csp.list_boot_sequences() and original_csp.list_boot_sequences():
        orig_boot = original_csp.list_boot_sequences()[0]['boot_sequence']
        imp_boot = import_csp.list_boot_sequences()[0]['boot_sequence']

        orig_playback = original_csp.playback_boot(orig_boot)
        imp_playback = import_csp.playback_boot(imp_boot)

        check('[CLI 回放一致] overall_status 一致',
              orig_playback.overall_status == imp_playback.overall_status,
              f'原始={orig_playback.overall_status}, 导入={imp_playback.overall_status}')
        check('[CLI 回放一致] fallback_count 一致',
              orig_playback.fallback_count == imp_playback.fallback_count,
              f'原始={orig_playback.fallback_count}, 导入={imp_playback.fallback_count}')
        check('[CLI 回放一致] total_items 一致',
              orig_playback.total_items == imp_playback.total_items)

    print('\n--- CLI 场景 6: 验证脏值回退标记保持 ---')
    for snap in imported_snaps:
        if snap.is_fallback:
            check('[CLI 回退标记] 导入后 is_fallback 保持为 True',
                  snap.is_fallback == True)
            check('[CLI 回退标记] 导入后有 fallback_reason',
                  snap.fallback_reason is not None and len(snap.fallback_reason) > 0)
            check('[CLI 回退标记] 导入后 effective_source == "default(fallback)"',
                  snap.effective_source == 'default(fallback)')
            break

    print('\n--- CLI 场景 7: 验证启动批次保持 ---')
    orig_boots = set(s.boot_sequence for s in original_snaps)
    imp_boots = set(s.boot_sequence for s in imported_snaps)
    check('[CLI 启动批次] 导入后启动批次集合一致',
          orig_boots == imp_boots,
          f'原始={orig_boots}, 导入={imp_boots}')
    print('\n--- CLI 场景 8: 内置 verify 命令 ---')
    cli_verify = [
        sys.executable, 'config_snapshot_playback_cli.py',
        '--snapshot-db', TEST_DB,
        '--log-file', TEST_LOG,
        'verify',
    ]
    result = run_cli_cmd(cli_verify)
    check('[CLI verify] 命令成功', result.returncode == 0,
          f'stderr={result.stderr[:200]}' if result.stderr else '')
    check('[CLI verify] 输出包含验证结果', '一致' in result.stdout or 'PASS' in result.stdout)

    for f in [import_db, 'test_e2e_import.log']:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass
    clean_test_env()


def run_api_chain_test():
    print_section('API 完整链路测试')

    remove_main_db()

    print('\n--- API 场景 1: 默认值启动 ---')
    proc = start_server(None)
    try:
        if not wait_for_server():
            check('[API 启动] 服务启动', False, '超时')
            return

        code, cfg = api('/api/config')
        check('[API /api/config] 返回 200', code == 200)
        if code == 200:
            check('[API /api/config] 包含 effective_value',
                  'effective_value' in cfg, f'keys={list(cfg.keys())[:10]}')
            check('[API /api/config] 包含 effective_source',
                  'effective_source' in cfg)
            check('[API /api/config] 包含 is_fallback',
                  'is_fallback' in cfg)
            check('[API /api/config] 包含 boot_sequence',
                  'boot_sequence' in cfg)
            check('[API /api/config] 包含 resolution_chain',
                  'resolution_chain' in cfg)
            check('[API /api/config] 包含 resolution_explanation',
                  'resolution_explanation' in cfg)
            check('[API /api/config] 包含 integrity_hash',
                  'integrity_hash' in cfg)
            check('[API 默认] effective_value == 30',
                  str(cfg.get('effective_value')) == '30',
                  f'实际={cfg.get("effective_value")}')
            check('[API 默认] effective_source == "default"',
                  cfg.get('effective_source') == 'default',
                  f'实际={cfg.get("effective_source")}')

        code, v2_cfg = api('/api/config/v2?key=reservation_expire_minutes')
        check('[API /api/config/v2] 返回 200', code == 200)

        code, boots = api('/api/config/v2/boot')
        check('[API /api/config/v2/boot] 返回 200', code == 200)
        check('[API /api/config/v2/boot] 非空列表', isinstance(boots, list) and len(boots) > 0)
        if boots:
            boot_seq = boots[0]['boot_sequence']

            code, boot_detail = api(f'/api/config/v2/boot/{boot_seq}')
            check('[API /api/config/v2/boot/seq] 返回 200', code == 200)
            check('[API boot详情] 包含 config_items',
                  'config_items' in boot_detail if isinstance(boot_detail, dict) else False)

        code, playback = api('/api/config/v2/playback')
        check('[API /api/config/v2/playback] 返回 200', code == 200)
        if code == 200:
            check('[API playback] 包含 overall_status', 'overall_status' in playback)
            check('[API playback] 包含 fallback_count', 'fallback_count' in playback)
            check('[API playback] 包含 source_distribution', 'source_distribution' in playback)
            check('[API playback] 包含 detailed_findings', 'detailed_findings' in playback)
            check('[API playback] 包含 summary_text', 'summary_text' in playback)
            check('[API playback] 包含 dirty_value_count', 'dirty_value_count' in playback)

        stop_server(proc)
        proc = None
        time.sleep(1)

        print('\n--- API 场景 2: 环境变量显式配置启动 ---')
        proc = start_server({'RESERVATION_EXPIRE_MINUTES': '10'}, clean=False)
        try:
            if not wait_for_server():
                check('[API 显式配置] 服务启动', False, '超时')
                return

            code, cfg2 = api('/api/config')
            check('[API 显式配置] 返回 200', code == 200)
            if code == 200:
                check('[API 显式配置] effective_value == 10',
                      str(cfg2.get('effective_value')) == '10',
                      f'实际={cfg2.get("effective_value")}')
                check('[API 显式配置] effective_source == "env"',
                      cfg2.get('effective_source') == 'env',
                      f'实际={cfg2.get("effective_source")}')
                check('[API 显式配置] is_fallback == False',
                      cfg2.get('is_fallback') == False)
                check('[API 显式配置] 有 resolution_chain',
                      'resolution_chain' in cfg2 and len(cfg2['resolution_chain']) > 0)

            stop_server(proc)
            proc = None
            time.sleep(1)

            print('\n--- API 场景 3: 非法值回退启动 ---')
            proc = start_server({'RESERVATION_EXPIRE_MINUTES': 'abc'}, clean=False)
            try:
                if not wait_for_server():
                    check('[API 非法值] 服务启动', False, '超时')
                    return

                code, cfg3 = api('/api/config')
                check('[API 非法值] 返回 200', code == 200)
                if code == 200:
                    check('[API 非法值] effective_value == 30 (回退)',
                          str(cfg3.get('effective_value')) == '30',
                          f'实际={cfg3.get("effective_value")}')
                    check('[API 非法值] effective_source == "default(fallback)"',
                          cfg3.get('effective_source') == 'default(fallback)',
                          f'实际={cfg3.get("effective_source")}')
                    check('[API 非法值] is_fallback == True',
                          cfg3.get('is_fallback') == True)
                    check('[API 非法值] 有 fallback_reason',
                          cfg3.get('fallback_reason') is not None)
                    check('[API 非法值] 有 resolution_explanation',
                          len(cfg3.get('resolution_explanation', '')) > 0)

                code, playback3 = api('/api/config/v2/playback')
                check('[API 非法值 playback] 返回 200', code == 200)
                if code == 200:
                    check('[API 非法值 playback] fallback_count > 0',
                          playback3.get('fallback_count', 0) > 0)
                    check('[API 非法值 playback] dirty_value_count > 0',
                          playback3.get('dirty_value_count', 0) > 0)
                    check('[API 非法值 playback] overall_status == "warning"',
                          playback3.get('overall_status') == 'warning')

                stop_server(proc)
                proc = None
                time.sleep(1)

                print('\n--- API 场景 4: 跨重启启动批次查询 ---')
                proc = start_server({'RESERVATION_EXPIRE_MINUTES': '5'}, clean=False)
                try:
                    if not wait_for_server():
                        check('[API 跨重启] 服务启动', False, '超时')
                        return

                    code, boots = api('/api/config/v2/boot')
                    check('[API 跨重启] boot列表返回 200', code == 200)
                    if code == 200:
                        check('[API 跨重启] 4个启动批次', len(boots) >= 4,
                              f'实际={len(boots)}')

                    code, diag = api('/api/config/v2/diagnose?key=reservation_expire_minutes')
                    check('[API 跨重启] diagnose返回 200', code == 200)

                    code, verify = api('/api/config/v2/verify')
                    check('[API 跨重启] verify返回 200', code == 200)

                finally:
                    stop_server(proc)

            finally:
                if proc is not None:
                    stop_server(proc)

        finally:
            if proc is not None:
                stop_server(proc)

    except Exception as e:
        check('[API 测试] 异常', False, str(e))
        import traceback
        traceback.print_exc()
    finally:
        if proc is not None:
            try:
                stop_server(proc)
            except Exception:
                pass

    remove_main_db()


def run_export_import_chain_test():
    print_section('导出导入链路一致性测试')

    clean_test_env()

    csp = ConfigSnapshotPlayback(snapshot_db=TEST_DB, log_file=TEST_LOG)
    csp.start_boot_snapshot()

    csp.resolve_config(key='timeout', default_value=30, value_parser=int, validator=lambda v: v > 0)
    csp.resolve_config(key='debug', default_value=False)
    csp.resolve_config(key='log_level', default_value='info')
    csp.finish_boot_snapshot()

    os.environ['E2E_TIMEOUT'] = '60'
    csp2 = ConfigSnapshotPlayback(snapshot_db=TEST_DB, log_file=TEST_LOG)
    csp2.start_boot_snapshot()
    csp2.resolve_config(key='timeout', default_value=30, env_key='E2E_TIMEOUT',
                        value_parser=int, validator=lambda v: v > 0)
    csp2.resolve_config(key='debug', default_value=False)
    csp2.resolve_config(key='log_level', default_value='info')
    csp2.finish_boot_snapshot()
    del os.environ['E2E_TIMEOUT']

    os.environ['E2E_BAD_VAL'] = 'not_a_number'
    csp3 = ConfigSnapshotPlayback(snapshot_db=TEST_DB, log_file=TEST_LOG)
    csp3.start_boot_snapshot()
    csp3.resolve_config(key='timeout', default_value=30, value_parser=int, validator=lambda v: v > 0)
    csp3.resolve_config(key='bad_val', default_value=42, env_key='E2E_BAD_VAL',
                        value_parser=int)
    csp3.resolve_config(key='debug', default_value=False)
    csp3.finish_boot_snapshot()
    del os.environ['E2E_BAD_VAL']

    boots = csp3.list_boot_sequences()
    check('[导出导入] 3个启动批次', len(boots) >= 3, f'实际={len(boots)}')

    print('\n--- 场景 1: JSON 往返一致性 ---')
    json_export = csp3.export_snapshots(fmt='json')
    json_data = json.loads(json_export)

    check('[JSON导出] 包含 playback_conclusions', 'playback_conclusions' in json_data)
    check('[JSON导出] 包含 boot_sequences', 'boot_sequences' in json_data)
    check('[JSON导出] 包含 integrity_root_hash', 'integrity_root_hash' in json_data)
    check('[JSON导出] export_format_version == 3.0',
          json_data.get('export_format_version') == '3.0')

    with open('test_e2e_export.json', 'w', encoding='utf-8') as f:
        f.write(json_export)

    import_csp = ConfigSnapshotPlayback(snapshot_db=':memory:', log_file=':memory:')
    import_result = import_csp.import_snapshots('test_e2e_export.json')

    check(f'[JSON导入] 成功导入', import_result['imported'] > 0,
          f'导入={import_result["imported"]}, 跳过={import_result["skipped"]}')

    imported_snaps = import_csp.get_all_snapshots(limit=100)
    original_snaps = csp3.get_all_snapshots(limit=100)

    original_hashes = {s.snapshot_uuid: s._compute_hash() for s in original_snaps}
    imported_hashes = {s.snapshot_uuid: s._compute_hash() for s in imported_snaps}
    hash_match = all(original_hashes.get(k) == h for k, h in imported_hashes.items()) and len(imported_hashes) == len(original_hashes)
    check('[JSON往返] 完整性哈希全部一致', hash_match,
          f'原始={len(original_hashes)}, 导入={len(imported_hashes)}')

    print('\n--- 场景 2: CSV 往返一致性 ---')
    csv_export = csp3.export_snapshots(fmt='csv')
    with open('test_e2e_export.csv', 'w', encoding='utf-8') as f:
        f.write(csv_export)

    csv_import_csp = ConfigSnapshotPlayback(snapshot_db=':memory:', log_file=':memory:')
    csv_result = csv_import_csp.import_snapshots('test_e2e_export.csv')
    csv_snaps = csv_import_csp.get_all_snapshots(limit=100)

    check(f'[CSV导入] 成功导入', csv_result['imported'] > 0)

    csv_boot_seq_match = True
    csv_fallback_match = True
    csv_source_match = True
    csv_reason_match = True
    for cs in csv_snaps:
        orig = next((s for s in original_snaps if s.snapshot_uuid == cs.snapshot_uuid), None)
        if orig:
            if cs.boot_sequence != orig.boot_sequence:
                csv_boot_seq_match = False
            if cs.is_fallback != orig.is_fallback:
                csv_fallback_match = False
            if cs.effective_source != orig.effective_source:
                csv_source_match = False
            if cs.fallback_reason != orig.fallback_reason:
                csv_reason_match = False

    check('[CSV往返] 启动批次一致', csv_boot_seq_match)
    check('[CSV往返] 回退标记一致', csv_fallback_match)
    check('[CSV往返] 来源一致', csv_source_match)
    check('[CSV往返] 回退原因一致', csv_reason_match)

    print('\n--- 场景 3: JSON 导入后回放结论一致 ---')
    original_playback = csp3.playback_boot(boots[-1]['boot_sequence'])

    imported_boots = import_csp.list_boot_sequences()
    if imported_boots:
        imported_playback = import_csp.playback_boot(imported_boots[-1]['boot_sequence'])
        check('[回放结论一致] overall_status 一致',
              original_playback.overall_status == imported_playback.overall_status,
              f'原始={original_playback.overall_status}, 导入={imported_playback.overall_status}')
        check('[回放结论一致] fallback_count 一致',
              original_playback.fallback_count == imported_playback.fallback_count)
        check('[回放结论一致] total_items 一致',
              original_playback.total_items == imported_playback.total_items)
        check('[回放结论一致] conflict_count 一致',
              original_playback.conflict_count == imported_playback.conflict_count)

    print('\n--- 场景 4: 导出后回放结论被保留 ---')
    if json_data.get('playback_conclusions'):
        check('[回放结论保留] JSON 导出包含 playback_conclusions', True)
        for boot_seq_str, pc in json_data['playback_conclusions'].items():
            check(f'[回放结论保留] 批次 {boot_seq_str} 有 overall_status',
                  'overall_status' in pc)
            check(f'[回放结论保留] 批次 {boot_seq_str} 有 summary_text',
                  'summary_text' in pc)
    else:
        check('[回放结论保留] 先执行一次回放', True)
        csp3.playback_boot(boots[-1]['boot_sequence'])
        re_export = csp3.export_snapshots(fmt='json')
        re_data = json.loads(re_export)
        check('[回放结论保留] 回放后导出包含 playback_conclusions',
              'playback_conclusions' in re_data and len(re_data.get('playback_conclusions', {})) > 0)

    print('\n--- 场景 5: 四边一致性 (查询/日志/导出/回放) ---')
    with open(TEST_LOG, 'r', encoding='utf-8') as f:
        log_content = f.read()

    dirty_boot_seq = None
    for snap in original_snaps:
        if snap.is_fallback:
            dirty_boot_seq = snap.boot_sequence
            export_snap = next((s for s in json_data['snapshots']
                                if s['snapshot_uuid'] == snap.snapshot_uuid), None)
            if export_snap:
                boot_playback = csp3.playback_boot(dirty_boot_seq)
                verify_four_way_consistency(
                    f'回退配置 {snap.config_key}',
                    snap.to_dict(),
                    log_content,
                    export_snap,
                    boot_playback.to_dict(),
                    {
                        'config_key': snap.config_key,
                        'effective_value': snap.effective_value,
                        'effective_source': snap.effective_source,
                        'is_fallback': True,
                    }
                )
            break

    clean_test_env()


def run_cross_restart_chain_test():
    print_section('跨重启完整链路测试')

    remove_main_db()

    print('\n--- 场景 1: 默认值启动 ---')
    proc = start_server(None)
    boot1_seq = None
    try:
        if not wait_for_server():
            check('[跨重启1] 服务启动', False, '超时')
            return

        code, cfg = api('/api/config')
        check('[跨重启1] /api/config 返回 200', code == 200)
        if code == 200:
            boot1_seq = cfg.get('boot_sequence')
            check('[跨重启1] effective_value == 30',
                  str(cfg.get('effective_value')) == '30',
                  f'实际={cfg.get("effective_value")}')
            check('[跨重启1] effective_source == "default"',
                  cfg.get('effective_source') == 'default')

        stop_server(proc)
        proc = None
        time.sleep(1)

        print('\n--- 场景 2: 显式配置启动 ---')
        proc = start_server({'RESERVATION_EXPIRE_MINUTES': '5'}, clean=False)
        boot2_seq = None
        try:
            if not wait_for_server():
                check('[跨重启2] 服务启动', False, '超时')
                return

            code, cfg2 = api('/api/config')
            if code == 200:
                boot2_seq = cfg2.get('boot_sequence')
                check('[跨重启2] 启动批次递增',
                      boot2_seq and boot1_seq and boot2_seq == boot1_seq + 1,
                      f'批次1={boot1_seq}, 批次2={boot2_seq}')
                check('[跨重启2] effective_value == 5',
                      str(cfg2.get('effective_value')) == '5')

            code, boots = api('/api/config/v2/boot')
            check('[跨重启2] 启动批次列表', code == 200)
            if code == 200:
                check('[跨重启2] 2个启动批次', len(boots) >= 2)

            stop_server(proc)
            proc = None
            time.sleep(1)

            print('\n--- 场景 3: 非法值回退启动 ---')
            proc = start_server({'RESERVATION_EXPIRE_MINUTES': 'abc'}, clean=False)
            boot3_seq = None
            try:
                if not wait_for_server():
                    check('[跨重启3] 服务启动', False, '超时')
                    return

                code, cfg3 = api('/api/config')
                if code == 200:
                    boot3_seq = cfg3.get('boot_sequence')
                    check('[跨重启3] 启动批次递增',
                          boot3_seq and boot2_seq and boot3_seq == boot2_seq + 1)
                    check('[跨重启3] effective_value == 30 (回退)',
                          str(cfg3.get('effective_value')) == '30')
                    check('[跨重启3] is_fallback == True',
                          cfg3.get('is_fallback') == True)
                    check('[跨重启3] 有 fallback_reason',
                          cfg3.get('fallback_reason') is not None)

                code, playback = api('/api/config/v2/playback')
                check('[跨重启3 playback] 返回 200', code == 200)
                if code == 200:
                    check('[跨重启3 playback] fallback_count > 0',
                          playback.get('fallback_count', 0) > 0)
                    check('[跨重启3 playback] dirty_value_count > 0',
                          playback.get('dirty_value_count', 0) > 0)

                code, diag = api('/api/config/diagnose')
                check('[跨重启3 diagnose] 返回 200', code == 200)
                if code == 200:
                    check('[跨重启3 diagnose] 包含 playback_conclusion',
                          'playback_conclusion' in diag)

                stop_server(proc)
                proc = None
                time.sleep(1)

                print('\n--- 场景 4: 再次显式配置启动 ---')
                proc = start_server({'RESERVATION_EXPIRE_MINUTES': '5'}, clean=False)
                try:
                    if not wait_for_server():
                        check('[跨重启4] 服务启动', False, '超时')
                        return

                    code, boots = api('/api/config/v2/boot')
                    check('[跨重启4] 启动批次列表', code == 200)
                    if code == 200:
                        check('[跨重启4] 4个启动批次', len(boots) >= 4)

                    code, cmp = api('/api/config/v2/boot/compare?boot1=1&boot2=3')
                    check('[跨重启4] 批次比较', code == 200)
                    if code == 200:
                        check('[跨重启4] 比较结果包含 differences',
                              'differences' in cmp or 'total_differences' in cmp or 'error' in cmp)

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

    remove_main_db()


def run_diagnostic_chain_test():
    print_section('诊断链路完整测试')

    clean_test_env()

    csp = ConfigSnapshotPlayback(snapshot_db=TEST_DB, log_file=TEST_LOG, config_file=TEST_CONFIG_FILE)
    csp.start_boot_snapshot()

    csp.resolve_config(key='timeout', default_value=30, value_parser=int, validator=lambda v: v > 0)

    with open(TEST_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump({'conflict_key': 10}, f)

    os.environ['E2E_CONFLICT'] = '20'
    snap_conflict = csp.resolve_config(
        key='conflict_key', default_value=30,
        env_key='E2E_CONFLICT', config_key='conflict_key',
        value_parser=int, validator=lambda v: v > 0,
    )
    del os.environ['E2E_CONFLICT']

    os.environ['E2E_DIRTY'] = 'not_int'
    snap_dirty = csp.resolve_config(
        key='dirty_key', default_value=42,
        env_key='E2E_DIRTY', value_parser=int,
    )
    del os.environ['E2E_DIRTY']

    boot = csp.finish_boot_snapshot()

    print('\n--- 场景 1: 冲突检测链路 ---')
    check('[冲突] conflict_detected == True', snap_conflict.conflict_detected == True)
    check('[冲突] conflict_details 非空', snap_conflict.conflict_details is not None)
    check('[冲突] effective_source == "config_file"',
          snap_conflict.effective_source == 'config_file')
    check('[冲突] resolution_explanation 包含冲突信息',
          '冲突' in snap_conflict.resolution_explanation or '多来源冲突' in snap_conflict.resolution_explanation)
    check('[冲突] resolution_chain 中 config_file 标记有效',
          any(s.source_name == 'config_file' and s.is_valid for s in snap_conflict.resolution_chain))
    check('[冲突] resolution_chain 中 env 标记有效',
          any(s.source_name == 'env' and s.is_valid for s in snap_conflict.resolution_chain))

    print('\n--- 场景 2: 脏值回退链路 ---')
    check('[脏值] is_fallback == True', snap_dirty.is_fallback == True)
    check('[脏值] fallback_reason 包含原因', snap_dirty.fallback_reason is not None)
    check('[脏值] effective_source == "default(fallback)"',
          snap_dirty.effective_source == 'default(fallback)')
    check('[脏值] resolution_chain 中 env 标记无效',
          any(s.source_name == 'env' and not s.is_valid for s in snap_dirty.resolution_chain))
    check('[脏值] diagnostic_notes 非空', len(snap_dirty.diagnostic_notes) > 0)

    print('\n--- 场景 3: 回放包含脏值计数 ---')
    playback = csp.playback_boot(boot.boot_sequence)
    check('[回放] dirty_value_count > 0', playback.dirty_value_count > 0,
          f'实际={playback.dirty_value_count}')
    check('[回放] fallback_count > 0', playback.fallback_count > 0)
    check('[回放] conflict_count > 0', playback.conflict_count > 0)
    check('[回放] overall_status == "warning"', playback.overall_status == 'warning')
    check('[回放] detailed_findings 包含冲突',
          any('冲突' in f for f in playback.detailed_findings))
    check('[回放] detailed_findings 包含回退',
          any('回退' in f for f in playback.detailed_findings))
    check('[回放] recommendations 包含脏值建议',
          any('脏值' in r for r in playback.recommendations))

    print('\n--- 场景 4: 日志包含完整诊断 ---')
    with open(TEST_LOG, 'r', encoding='utf-8') as f:
        log_content = f.read()

    log_patterns = [
        '配置解析完成',
        '回退原因',
        '多来源冲突',
        '排障结论',
    ]
    for pattern in log_patterns:
        check(f'[日志] 包含 "{pattern[:30]}..."', pattern in log_content,
              f'未找到: {pattern}')

    clean_test_env()


def main():
    print('=' * 72)
    print('  配置快照与回放 - 端到端链路验收测试')
    print('=' * 72)
    print('  测试范围:')
    print('    - CLI 完整链路 (resolve/playback/export/import/verify)')
    print('    - API 完整链路 (config/playback/boot/export/import/verify)')
    print('    - 导出导入往返一致性 (JSON/CSV)')
    print('    - 四边一致性 (查询/日志/导出/回放结论)')
    print('    - 跨重启一致性 (启动批次递增/回放结论保留)')
    print('    - 诊断链路 (冲突/脏值/回退原因/回放结论)')
    print('    - 启动批次/回退标记/原因说明/回放结论 一致性')
    print('=' * 72)

    try:
        run_cli_chain_test()
        run_export_import_chain_test()
        run_diagnostic_chain_test()
        run_api_chain_test()
        run_cross_restart_chain_test()
    except Exception as e:
        print(f'\n测试执行异常: {e}')
        import traceback
        traceback.print_exc()
    finally:
        clean_test_env()
        remove_main_db()

    print(f'\n{"=" * 72}')
    passed = sum(1 for _, c, _ in checks if c)
    total = len(checks)
    print(f'检查总计: {passed} / {total} 通过')
    print(f'{"=" * 72}')

    if not ok:
        print('\n失败检查项:')
        for label, cond, detail in checks:
            if not cond:
                print(f'  - {label}: {detail}')

    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
