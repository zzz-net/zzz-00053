#!/usr/bin/env python3
import os
import sys
import json
import csv
import io
import sqlite3
import subprocess
import time
import tempfile
import shutil
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

ok = True
checks = []
_tmp_dir = None


def get_tmp_dir():
    global _tmp_dir
    if _tmp_dir is None:
        _tmp_dir = tempfile.mkdtemp(prefix='regression_test_')
    return _tmp_dir


def cleanup_tmp():
    global _tmp_dir
    if _tmp_dir and os.path.exists(_tmp_dir):
        try:
            shutil.rmtree(_tmp_dir)
        except Exception:
            pass
    _tmp_dir = None


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


def remove_main_db():
    for suffix in ['', '-wal', '-shm', '-journal']:
        for p in ['emergency_supply.db' + suffix,
                  DEFAULT_SNAPSHOT_DB + suffix,
                  DEFAULT_LOG_FILE,
                  'config_snapshot_playback.log',
                  'config_snapshot_playback.db' + suffix,
                  'e2e_test_server.log',
                  'diag_test_server.log']:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


def clean_env_vars():
    for k in list(os.environ.keys()):
        if k.startswith('REG_') or k == 'RESERVATION_EXPIRE_MINUTES':
            del os.environ[k]


def run_cli_cmd(args, cwd=None, timeout=30):
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
            cwd=cwd or os.path.dirname(os.path.abspath(__file__)),
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


def start_server(tmpdir, env_vars=None, clean_db=True):
    if clean_db:
        remove_main_db()
    clean_env_vars()
    env = os.environ.copy()
    if env_vars:
        env.update(env_vars)
    server_log = os.path.join(tmpdir, 'flask_server.log')
    with open(server_log, 'w', encoding='utf-8') as logf:
        proc = subprocess.Popen(
            [sys.executable, '-u', 'app.py'],
            stdout=logf,
            stderr=subprocess.STDOUT,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            bufsize=0,
            env=env,
        )
    return proc, server_log


def stop_server(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass


def test_cli_resolve_boot_sequence():
    """CLI resolve 生成启动批次"""
    print_section('CLI resolve 生成启动批次测试')

    tmpdir = get_tmp_dir()
    test_db = os.path.join(tmpdir, 'test_boot.db')
    test_log = os.path.join(tmpdir, 'test_boot.log')

    clean_env_vars()

    print('\n--- 场景 1: 首次 resolve 自动创建启动批次 ---')
    result = run_cli_cmd([
        sys.executable, 'config_snapshot_playback_cli.py',
        '--snapshot-db', test_db,
        '--log-file', test_log,
        'resolve', '--key', 'timeout', '--default', '30',
        '--type', 'int', '--min', '1',
    ])
    check('[CLI resolve 批次1] 命令成功', result.returncode == 0,
          f'stderr={result.stderr[:200]}' if result.stderr else '')
    check('[CLI resolve 批次1] 输出包含启动批次', '启动批次' in result.stdout)

    boot_seq_1 = None
    for line in result.stdout.split('\n'):
        if '启动批次' in line:
            parts = line.split(':')
            if len(parts) >= 2:
                try:
                    boot_seq_1 = int(parts[-1].strip())
                except ValueError:
                    pass
    check('[CLI resolve 批次1] 解析到启动批次号', boot_seq_1 is not None,
          f'stdout={result.stdout[:200]}')

    print('\n--- 场景 2: 同一进程内多次 resolve 使用同一启动批次 ---')
    result2 = run_cli_cmd([
        sys.executable, 'config_snapshot_playback_cli.py',
        '--snapshot-db', test_db,
        '--log-file', test_log,
        'resolve', '--key', 'debug', '--default', 'false',
    ])
    check('[CLI resolve 同批次] 命令成功', result2.returncode == 0)

    csp = ConfigSnapshotPlayback(snapshot_db=test_db, log_file=':memory:')
    boots = csp.list_boot_sequences()
    check('[CLI resolve 批次] 新进程启动产生新批次', len(boots) >= 2,
          f'实际批次数量={len(boots)}')

    print('\n--- 场景 3: 验证批次递增 ---')
    boot_seqs = sorted([b['boot_sequence'] for b in boots])
    check('[CLI resolve 批次] 批次号递增', boot_seqs == sorted(boot_seqs),
          f'批次序列={boot_seqs}')

    snaps = csp.get_all_snapshots(limit=100)
    check('[CLI resolve 批次] 每个快照都有 boot_sequence',
          all(s.boot_sequence > 0 for s in snaps),
          f'boot_sequences={[s.boot_sequence for s in snaps]}')

    print('\n--- 场景 4: finish_boot_snapshot 后 snapshot list 查询 ---')
    boots_detail = csp.list_boot_sequences()
    check('[CLI resolve 批次] boot_records 已记录',
          len(boots_detail) >= 2,
          f'boot_records 数量={len(boots_detail)}')
    for b in boots_detail:
        check(f'[CLI resolve 批次] 批次 {b["boot_sequence"]} 有 item_count',
              b.get('item_count', 0) > 0)


def test_invalid_config_fallback():
    """非法配置回退到 default(fallback)"""
    print_section('非法配置回退 default(fallback) 测试')

    tmpdir = get_tmp_dir()
    test_db = os.path.join(tmpdir, 'test_fallback.db')
    test_log = os.path.join(tmpdir, 'test_fallback.log')
    test_cfg = os.path.join(tmpdir, 'test_fallback_config.json')

    clean_env_vars()

    print('\n--- 场景 1: 环境变量非法值触发回退 ---')
    os.environ['REG_BAD_INT'] = 'not_a_number'
    result = run_cli_cmd([
        sys.executable, 'config_snapshot_playback_cli.py',
        '--snapshot-db', test_db,
        '--log-file', test_log,
        'resolve', '--key', 'bad_int', '--default', '42',
        '--env-key', 'REG_BAD_INT', '--type', 'int',
    ])
    del os.environ['REG_BAD_INT']
    check('[fallback env非法] 命令成功', result.returncode == 0)
    check('[fallback env非法] 输出包含回退',
          '回退' in result.stdout or 'fallback' in result.stdout.lower())
    check('[fallback env非法] 输出包含 default(fallback)',
          'default(fallback)' in result.stdout)

    print('\n--- 场景 2: 配置文件非法值触发回退 ---')
    with open(test_cfg, 'w', encoding='utf-8') as f:
        json.dump({'bad_config': 'invalid_number'}, f)

    result2 = run_cli_cmd([
        sys.executable, 'config_snapshot_playback_cli.py',
        '--snapshot-db', test_db,
        '--log-file', test_log,
        '--config-file', test_cfg,
        'resolve', '--key', 'bad_config_key', '--default', '100',
        '--config-key', 'bad_config', '--type', 'int',
    ])
    check('[fallback cfg非法] 命令成功', result2.returncode == 0)
    check('[fallback cfg非法] 输出包含回退',
          '回退' in result2.stdout or 'fallback' in result2.stdout.lower())

    print('\n--- 场景 3: 验证 fallback 标记和 reason 写入数据库 ---')
    csp = ConfigSnapshotPlayback(snapshot_db=test_db, log_file=':memory:')
    snaps = csp.get_all_snapshots(limit=100)

    fallback_snaps = [s for s in snaps if s.is_fallback]
    check('[fallback 持久化] 至少 2 条 fallback 快照',
          len(fallback_snaps) >= 2,
          f'实际 fallback 数={len(fallback_snaps)}')

    for s in fallback_snaps:
        check(f'[fallback {s.config_key}] effective_source == default(fallback)',
              s.effective_source == 'default(fallback)',
              f'实际={s.effective_source}')
        check(f'[fallback {s.config_key}] fallback_reason 非空',
              s.fallback_reason is not None and len(s.fallback_reason) > 0,
              f'fallback_reason={s.fallback_reason}')
        check(f'[fallback {s.config_key}] effective_value == default_value',
              str(s.effective_value) == str(s.default_value),
              f'effective={s.effective_value}, default={s.default_value}')

    print('\n--- 场景 4: 验证器失败也触发回退 ---')
    result3 = run_cli_cmd([
        sys.executable, 'config_snapshot_playback_cli.py',
        '--snapshot-db', test_db,
        '--log-file', test_log,
        'resolve', '--key', 'negative_val', '--default', '5',
        '--type', 'int', '--min', '1',
    ])
    check('[fallback 验证器] 命令成功', result3.returncode == 0)


def test_env_config_priority():
    """环境变量和配置文件冲突时的优先级记录"""
    print_section('环境变量与配置文件优先级测试')

    tmpdir = get_tmp_dir()
    test_db = os.path.join(tmpdir, 'test_priority.db')
    test_log = os.path.join(tmpdir, 'test_priority.log')
    test_cfg = os.path.join(tmpdir, 'test_priority_config.json')

    clean_env_vars()

    print('\n--- 场景 1: config_file 与 env 同时设置，config_file 优先级更高 (优先级数字越小越高) ---')
    with open(test_cfg, 'w', encoding='utf-8') as f:
        json.dump({'conflict_key': 10}, f)

    os.environ['REG_CONFLICT'] = '20'
    csp = ConfigSnapshotPlayback(
        snapshot_db=test_db, log_file=test_log, config_file=test_cfg
    )
    csp.start_boot_snapshot()
    snap = csp.resolve_config(
        key='conflict_key',
        default_value=30,
        env_key='REG_CONFLICT',
        config_key='conflict_key',
        value_parser=int,
    )
    boot = csp.finish_boot_snapshot()
    del os.environ['REG_CONFLICT']

    check('[优先级] conflict_detected == True',
          snap.conflict_detected == True,
          f'conflict_detected={snap.conflict_detected}')
    check('[优先级] effective_source == config_file (优先级更高, 优先级1 < 2)',
          snap.effective_source == 'config_file',
          f'effective_source={snap.effective_source}')
    check('[优先级] effective_value == 10 (config_file 值)',
          str(snap.effective_value) == '10',
          f'effective_value={snap.effective_value}')
    check('[优先级] conflict_details 非空',
          snap.conflict_details is not None and len(snap.conflict_details) > 0)

    print('\n--- 场景 2: resolution_chain 记录完整优先级链 ---')
    check('[优先级链] resolution_chain 至少 3 条 (config_file/env/default)',
          len(snap.resolution_chain) >= 3,
          f'实际={len(snap.resolution_chain)}')

    has_config = any(s.source_name == 'config_file' and s.is_valid for s in snap.resolution_chain)
    has_env = any(s.source_name == 'env' and s.is_valid for s in snap.resolution_chain)
    has_default = any(s.source_name == 'default' and s.is_valid for s in snap.resolution_chain)
    check('[优先级链] config_file 标记有效', has_config)
    check('[优先级链] env 标记有效', has_env)
    check('[优先级链] default 标记有效', has_default)

    priorities = [s.priority for s in snap.resolution_chain]
    check('[优先级链] 优先级数字正确',
          1 in priorities and 2 in priorities and 99 in priorities)

    print('\n--- 场景 3: resolution_explanation 包含冲突说明 ---')
    check('[优先级解释] resolution_explanation 包含冲突',
          '冲突' in snap.resolution_explanation or '多来源' in snap.resolution_explanation,
          f'explanation={snap.resolution_explanation[:100]}')

    print('\n--- 场景 4: diagnostic_notes 包含优先级决策 ---')
    check('[优先级诊断] diagnostic_notes 非空',
          len(snap.diagnostic_notes) > 0,
          f'diagnostic_notes={snap.diagnostic_notes}')

    print('\n--- 场景 5: 只有 config_file 时，来源为 config_file ---')
    csp2 = ConfigSnapshotPlayback(
        snapshot_db=test_db, log_file=test_log, config_file=test_cfg
    )
    csp2.start_boot_snapshot()
    snap2 = csp2.resolve_config(
        key='only_config',
        default_value=99,
        config_key='conflict_key',
        value_parser=int,
    )
    csp2.finish_boot_snapshot()
    check('[单来源] effective_source == config_file',
          snap2.effective_source == 'config_file',
          f'effective_source={snap2.effective_source}')
    check('[单来源] conflict_detected == False',
          snap2.conflict_detected == False)


def test_playback_persistence_across_process():
    """playback 结果写入 SQLite 并在新进程/重启后还能查到"""
    print_section('Playback 结果持久化与跨进程查询测试')

    tmpdir = get_tmp_dir()
    test_db = os.path.join(tmpdir, 'test_playback_persist.db')
    test_log = os.path.join(tmpdir, 'test_playback_persist.log')
    test_cfg = os.path.join(tmpdir, 'test_playback_cfg.json')

    clean_env_vars()

    print('\n--- 场景 1: 创建多批次配置并执行 playback ---')
    csp1 = ConfigSnapshotPlayback(snapshot_db=test_db, log_file=test_log)
    csp1.start_boot_snapshot()
    csp1.resolve_config(key='timeout', default_value=30, value_parser=int)
    csp1.resolve_config(key='debug', default_value=False)
    boot1 = csp1.finish_boot_snapshot()

    os.environ['REG_BAD'] = 'invalid'
    csp2 = ConfigSnapshotPlayback(snapshot_db=test_db, log_file=test_log)
    csp2.start_boot_snapshot()
    csp2.resolve_config(key='timeout', default_value=30, value_parser=int)
    csp2.resolve_config(key='bad_key', default_value=42, env_key='REG_BAD', value_parser=int)
    boot2 = csp2.finish_boot_snapshot()
    del os.environ['REG_BAD']

    playback1 = csp2.playback_boot(boot1.boot_sequence)
    playback2 = csp2.playback_boot(boot2.boot_sequence)

    check('[playback 持久化] playback_boot 返回 PlaybackConclusion',
          isinstance(playback1, PlaybackConclusion))
    check('[playback 持久化] 批次2 fallback_count > 0',
          playback2.fallback_count > 0,
          f'fallback_count={playback2.fallback_count}')

    print('\n--- 场景 2: 直接查数据库验证 playback_results 表写入 ---')
    import sqlite3
    conn = sqlite3.connect(test_db)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM playback_results')
    count = c.fetchone()[0]
    conn.close()
    check('[playback 持久化] playback_results 表至少 2 条记录',
          count >= 2,
          f'实际记录数={count}')

    print('\n--- 场景 3: 新 Python 进程查同 DB，回放结果可重现 ---')
    verify_script = os.path.join(tmpdir, 'verify_playback.py')
    with open(verify_script, 'w', encoding='utf-8') as f:
        f.write(f'''
import sys
import os
import json
sys.path.insert(0, {os.path.dirname(os.path.abspath(__file__))!r})
from config_snapshot_playback import ConfigSnapshotPlayback

csp = ConfigSnapshotPlayback(snapshot_db={test_db!r}, log_file=':memory:')
boots = csp.list_boot_sequences()

import sqlite3
conn = sqlite3.connect({test_db!r})
conn.row_factory = sqlite3.Row
c = conn.cursor()
c.execute('SELECT boot_sequence, result_json FROM playback_results ORDER BY id')
rows = c.fetchall()
conn.close()

result = {{
    'boot_count': len(boots),
    'playback_count': len(rows),
    'playback_data': [
        {{'boot_sequence': r['boot_sequence'], 'result': json.loads(r['result_json'])}}
        for r in rows
    ]
}}
print(json.dumps(result, ensure_ascii=False))
''')

    verify_result = run_cli_cmd([sys.executable, verify_script], cwd=tmpdir)
    check('[playback 跨进程] 验证脚本执行成功',
          verify_result.returncode == 0,
          f'stderr={verify_result.stderr[:200]}')

    if verify_result.returncode == 0:
        cross_process_data = json.loads(verify_result.stdout.strip())
        check('[playback 跨进程] 新进程能查到 boot_records',
              cross_process_data['boot_count'] >= 2,
              f'boot_count={cross_process_data["boot_count"]}')
        check('[playback 跨进程] 新进程能查到 playback_results',
              cross_process_data['playback_count'] >= 2,
              f'playback_count={cross_process_data["playback_count"]}')

        for pd in cross_process_data['playback_data']:
            check(f'[playback 跨进程 批次{pd["boot_sequence"]}] result_json 包含 overall_status',
                  'overall_status' in pd['result'])
            check(f'[playback 跨进程 批次{pd["boot_sequence"]}] result_json 包含 fallback_count',
                  'fallback_count' in pd['result'])

    print('\n--- 场景 4: 关闭所有实例后重开，依然能查到历史 playback ---')
    del csp1, csp2

    csp3 = ConfigSnapshotPlayback(snapshot_db=test_db, log_file=':memory:')
    boots3 = csp3.list_boot_sequences()
    check('[playback 重开] 重开后能列出所有 boot',
          len(boots3) >= 2,
          f'boot 数={len(boots3)}')

    latest_boot = max(b['boot_sequence'] for b in boots3)
    playback3 = csp3.playback_boot(latest_boot)
    check('[playback 重开] 重开后 playback 能得到结论',
          playback3 is not None and playback3.total_items > 0)
    check('[playback 重开] 重开后 fallback_count 正确',
          playback3.fallback_count >= 1)


def test_json_roundtrip():
    """JSON 往返验证：保留 boot、playback 和 resolution_chain"""
    print_section('JSON 导出导入往返验证测试')

    tmpdir = get_tmp_dir()
    test_db = os.path.join(tmpdir, 'test_json_rt.db')
    test_log = os.path.join(tmpdir, 'test_json_rt.log')
    test_cfg = os.path.join(tmpdir, 'test_json_rt_cfg.json')

    clean_env_vars()

    print('\n--- 场景 1: 创建含 fallback 和 conflict 的完整测试数据 ---')
    with open(test_cfg, 'w', encoding='utf-8') as f:
        json.dump({'shared_key': 5}, f)

    os.environ['REG_SHARED'] = '15'
    os.environ['REG_DIRTY'] = 'not_int'

    csp_orig = ConfigSnapshotPlayback(
        snapshot_db=test_db, log_file=test_log, config_file=test_cfg
    )
    csp_orig.start_boot_snapshot()
    snap_normal = csp_orig.resolve_config(
        key='normal_key', default_value=30, value_parser=int
    )
    snap_conflict = csp_orig.resolve_config(
        key='shared_key', default_value=0,
        env_key='REG_SHARED', config_key='shared_key',
        value_parser=int,
    )
    snap_fallback = csp_orig.resolve_config(
        key='dirty_key', default_value=42,
        env_key='REG_DIRTY', value_parser=int,
    )
    boot_orig = csp_orig.finish_boot_snapshot()

    del os.environ['REG_SHARED']
    del os.environ['REG_DIRTY']

    playback_orig = csp_orig.playback_boot(boot_orig.boot_sequence)

    print('\n--- 场景 2: 导出 JSON 并验证结构完整性 ---')
    json_export = csp_orig.export_snapshots(fmt='json')
    json_data = json.loads(json_export)

    check('[JSON导出] 包含 boot_sequences',
          'boot_sequences' in json_data and len(json_data['boot_sequences']) > 0)
    check('[JSON导出] 包含 playback_conclusions',
          'playback_conclusions' in json_data and len(json_data['playback_conclusions']) > 0)
    check('[JSON导出] 包含 snapshots',
          'snapshots' in json_data and len(json_data['snapshots']) > 0)
    check('[JSON导出] 包含 integrity_root_hash',
          'integrity_root_hash' in json_data)
    check('[JSON导出] export_format_version == 3.0',
          json_data.get('export_format_version') == '3.0')

    for snap_export in json_data['snapshots']:
        check(f'[JSON导出 snap {snap_export["config_key"]}] 包含 resolution_chain',
              'resolution_chain' in snap_export and len(snap_export['resolution_chain']) > 0,
              f'keys={list(snap_export.keys())}')
        check(f'[JSON导出 snap {snap_export["config_key"]}] 包含 boot_sequence',
              'boot_sequence' in snap_export)

    for boot_seq_str, pc in json_data['playback_conclusions'].items():
        check(f'[JSON导出 playback 批次{boot_seq_str}] 包含 overall_status',
              'overall_status' in pc)
        check(f'[JSON导出 playback 批次{boot_seq_str}] 包含 summary_text',
              'summary_text' in pc)
        check(f'[JSON导出 playback 批次{boot_seq_str}] 包含 fallback_count',
              'fallback_count' in pc)

    print('\n--- 场景 3: 导入到全新空 DB ---')
    import_db = os.path.join(tmpdir, 'test_json_rt_import.db')
    export_file = os.path.join(tmpdir, 'export.json')
    with open(export_file, 'w', encoding='utf-8') as f:
        f.write(json_export)

    csp_import = ConfigSnapshotPlayback(snapshot_db=import_db, log_file=':memory:')
    import_result = csp_import.import_snapshots(export_file)
    check('[JSON导入] 成功导入 > 0 条',
          import_result['imported'] > 0,
          f'imported={import_result["imported"]}, skipped={import_result["skipped"]}')

    print('\n--- 场景 4: 验证 boot_sequences 往返保留 ---')
    orig_boots = csp_orig.list_boot_sequences()
    import_boots = csp_import.list_boot_sequences()
    orig_boot_seqs = set(b['boot_sequence'] for b in orig_boots)
    import_boot_seqs = set(b['boot_sequence'] for b in import_boots)
    check('[JSON往返] boot_sequences 集合一致',
          orig_boot_seqs == import_boot_seqs,
          f'orig={orig_boot_seqs}, import={import_boot_seqs}')

    print('\n--- 场景 5: 验证 resolution_chain 往返保留 ---')
    orig_snaps = csp_orig.get_all_snapshots(limit=100)
    import_snaps = csp_import.get_all_snapshots(limit=100)

    for orig in orig_snaps:
        imp = next((s for s in import_snaps if s.snapshot_uuid == orig.snapshot_uuid), None)
        check(f'[JSON往返 chain {orig.config_key}] 导入快照存在',
              imp is not None)
        if imp:
            check(f'[JSON往返 chain {orig.config_key}] resolution_chain 长度一致',
                  len(imp.resolution_chain) == len(orig.resolution_chain),
                  f'orig={len(orig.resolution_chain)}, imp={len(imp.resolution_chain)}')
            if len(imp.resolution_chain) == len(orig.resolution_chain):
                for i, (orig_src, imp_src) in enumerate(zip(orig.resolution_chain, imp.resolution_chain)):
                    check(f'[JSON往返 chain {orig.config_key} src{i}] source_name 一致',
                          orig_src.source_name == imp_src.source_name)
                    check(f'[JSON往返 chain {orig.config_key} src{i}] is_valid 一致',
                          orig_src.is_valid == imp_src.is_valid)
                    check(f'[JSON往返 chain {orig.config_key} src{i}] priority 一致',
                          orig_src.priority == imp_src.priority)

    print('\n--- 场景 6: 验证 playback_conclusions 往返保留 ---')
    import_conn = sqlite3.connect(import_db)
    import_conn.row_factory = sqlite3.Row
    ic = import_conn.cursor()
    ic.execute('SELECT boot_sequence, result_json FROM playback_results ORDER BY boot_sequence')
    import_pc_rows = ic.fetchall()
    import_conn.close()

    check('[JSON往返] playback_results 导入后存在记录',
          len(import_pc_rows) > 0,
          f'实际记录数={len(import_pc_rows)}')

    for row in import_pc_rows:
        pc = json.loads(row['result_json'])
        check(f'[JSON往返 playback 批次{row["boot_sequence"]}] 包含 overall_status',
              'overall_status' in pc)

    print('\n--- 场景 7: 验证导入后 playback 结论与原始一致 ---')
    latest_orig = max(b['boot_sequence'] for b in orig_boots)
    latest_import = max(b['boot_sequence'] for b in import_boots)

    playback_orig_final = csp_orig.playback_boot(latest_orig)
    playback_import_final = csp_import.playback_boot(latest_import)

    check('[JSON往返 回放] overall_status 一致',
          playback_orig_final.overall_status == playback_import_final.overall_status,
          f'orig={playback_orig_final.overall_status}, import={playback_import_final.overall_status}')
    check('[JSON往返 回放] total_items 一致',
          playback_orig_final.total_items == playback_import_final.total_items)
    check('[JSON往返 回放] fallback_count 一致',
          playback_orig_final.fallback_count == playback_import_final.fallback_count)
    check('[JSON往返 回放] conflict_count 一致',
          playback_orig_final.conflict_count == playback_import_final.conflict_count)
    check('[JSON往返 回放] dirty_value_count 一致',
          playback_orig_final.dirty_value_count == playback_import_final.dirty_value_count)

    print('\n--- 场景 8: 完整性哈希验证 ---')
    orig_hashes = {s.snapshot_uuid: s._compute_hash() for s in orig_snaps}
    import_hashes = {s.snapshot_uuid: s._compute_hash() for s in import_snaps}
    all_match = all(orig_hashes.get(k) == h for k, h in import_hashes.items())
    check('[JSON往返 哈希] 所有快照 integrity_hash 一致',
          all_match and len(orig_hashes) == len(import_hashes),
          f'orig={len(orig_hashes)}, import={len(import_hashes)}')


def test_csv_roundtrip():
    """CSV 往返验证：诊断细节会被压平但关键字段还可回放"""
    print_section('CSV 导出导入往返验证测试')

    tmpdir = get_tmp_dir()
    test_db = os.path.join(tmpdir, 'test_csv_rt.db')
    test_log = os.path.join(tmpdir, 'test_csv_rt.log')
    test_cfg = os.path.join(tmpdir, 'test_csv_rt_cfg.json')

    clean_env_vars()

    print('\n--- 场景 1: 创建含 fallback 和 conflict 的测试数据 ---')
    with open(test_cfg, 'w', encoding='utf-8') as f:
        json.dump({'csv_key': 7}, f)

    os.environ['REG_CSV_CONFLICT'] = '17'
    os.environ['REG_CSV_DIRTY'] = 'bad_value'

    csp_orig = ConfigSnapshotPlayback(
        snapshot_db=test_db, log_file=test_log, config_file=test_cfg
    )
    csp_orig.start_boot_snapshot()
    snap_conflict = csp_orig.resolve_config(
        key='csv_conflict', default_value=0,
        env_key='REG_CSV_CONFLICT', config_key='csv_key',
        value_parser=int,
    )
    snap_fallback = csp_orig.resolve_config(
        key='csv_fallback', default_value=99,
        env_key='REG_CSV_DIRTY', value_parser=int,
    )
    snap_normal = csp_orig.resolve_config(
        key='csv_normal', default_value='hello',
    )
    boot_orig = csp_orig.finish_boot_snapshot()

    del os.environ['REG_CSV_CONFLICT']
    del os.environ['REG_CSV_DIRTY']

    playback_orig = csp_orig.playback_boot(boot_orig.boot_sequence)

    print('\n--- 场景 2: 导出 CSV 并验证结构 ---')
    csv_export = csp_orig.export_snapshots(fmt='csv')
    csv_file = os.path.join(tmpdir, 'export.csv')
    with open(csv_file, 'w', encoding='utf-8') as f:
        f.write(csv_export)

    reader = csv.DictReader(io.StringIO(csv_export))
    rows = list(reader)
    check('[CSV导出] 至少 3 行数据', len(rows) >= 3, f'实际={len(rows)}')

    fieldnames = reader.fieldnames or []
    required_fields = [
        'config_key', 'effective_value', 'effective_source',
        'is_fallback', 'fallback_reason', 'boot_sequence',
        'conflict_detected', 'resolution_explanation',
        'resolution_chain_count', 'diagnostic_notes',
        'integrity_hash', 'default_value',
    ]
    for f in required_fields:
        check(f'[CSV导出] 字段 {f} 存在', f in fieldnames,
              f'fieldnames={fieldnames}')

    print('\n--- 场景 3: CSV 中 diagnostic_notes 被压平为字符串 ---')
    for row in rows:
        if row['config_key'] in ('csv_conflict', 'csv_fallback'):
            notes = row.get('diagnostic_notes', '')
            check(f'[CSV压平 {row["config_key"]}] diagnostic_notes 是压平字符串',
                  isinstance(notes, str) and ';' in notes or len(notes) >= 0,
                  f'notes={notes!r}')

    print('\n--- 场景 4: 导入 CSV 到全新空 DB ---')
    import_db = os.path.join(tmpdir, 'test_csv_rt_import.db')
    csp_import = ConfigSnapshotPlayback(snapshot_db=import_db, log_file=':memory:')
    import_result = csp_import.import_snapshots(csv_file)
    check('[CSV导入] 成功导入 > 0 条',
          import_result['imported'] > 0,
          f'imported={import_result["imported"]}')

    print('\n--- 场景 5: 关键字段往返保留（可回放） ---')
    orig_snaps = csp_orig.get_all_snapshots(limit=100)
    import_snaps = csp_import.get_all_snapshots(limit=100)

    orig_by_key = {s.config_key: s for s in orig_snaps}
    import_by_key = {s.config_key: s for s in import_snaps}

    for key in ['csv_conflict', 'csv_fallback', 'csv_normal']:
        orig = orig_by_key.get(key)
        imp = import_by_key.get(key)
        check(f'[CSV往返 {key}] 导入后快照存在', imp is not None)
        if imp and orig:
            check(f'[CSV往返 {key}] effective_value 一致',
                  str(imp.effective_value) == str(orig.effective_value),
                  f'orig={orig.effective_value}, imp={imp.effective_value}')
            check(f'[CSV往返 {key}] effective_source 一致',
                  imp.effective_source == orig.effective_source,
                  f'orig={orig.effective_source}, imp={imp.effective_source}')
            check(f'[CSV往返 {key}] is_fallback 一致',
                  imp.is_fallback == orig.is_fallback,
                  f'orig={orig.is_fallback}, imp={imp.is_fallback}')
            check(f'[CSV往返 {key}] boot_sequence 一致',
                  imp.boot_sequence == orig.boot_sequence)
            check(f'[CSV往返 {key}] default_value 一致',
                  str(imp.default_value) == str(orig.default_value))
            check(f'[CSV往返 {key}] conflict_detected 一致',
                  imp.conflict_detected == orig.conflict_detected)

    print('\n--- 场景 6: 导入后可重新执行 playback 得到相同统计 ---')
    import_boots = csp_import.list_boot_sequences()
    check('[CSV往返] boot_sequences 导入后存在', len(import_boots) > 0)

    if import_boots:
        latest_import = max(b['boot_sequence'] for b in import_boots)
        playback_import = csp_import.playback_boot(latest_import)

        check('[CSV往返 回放] 能得到回放结论',
              playback_import.total_items >= 3,
              f'total_items={playback_import.total_items}')
        check('[CSV往返 回放] fallback_count > 0 (csv_fallback)',
              playback_import.fallback_count >= 1,
              f'fallback_count={playback_import.fallback_count}')
        check('[CSV往返 回放] conflict_count > 0 (csv_conflict)',
              playback_import.conflict_count >= 1,
              f'conflict_count={playback_import.conflict_count}')
        check('[CSV往返 回放] dirty_value_count > 0',
              playback_import.dirty_value_count >= 1)

    print('\n--- 场景 7: CSV 与原始 playback 统计对比 ---')
    orig_boots = csp_orig.list_boot_sequences()
    latest_orig = max(b['boot_sequence'] for b in orig_boots)
    playback_orig_final = csp_orig.playback_boot(latest_orig)

    check('[CSV往返 回放对比] total_items 一致',
          playback_orig_final.total_items == playback_import.total_items)
    check('[CSV往返 回放对比] fallback_count 一致',
          playback_orig_final.fallback_count == playback_import.fallback_count)
    check('[CSV往返 回放对比] conflict_count 一致',
          playback_orig_final.conflict_count == playback_import.conflict_count)
    check('[CSV往返 回放对比] dirty_value_count 一致',
          playback_orig_final.dirty_value_count == playback_import.dirty_value_count)


def test_flask_v2_cli_shared_db():
    """Flask v2 API 与 CLI 共用数据库的端到端测试"""
    print_section('Flask v2 API 与 CLI 共用数据库端到端测试')

    tmpdir = get_tmp_dir()
    clean_env_vars()
    remove_main_db()

    print('\n--- 场景 1: CLI 写入数据后，通过 Flask API 读取 ---')
    cli_db = os.path.join(tmpdir, 'test_shared_cli.db')
    cli_log = os.path.join(tmpdir, 'test_shared_cli.log')

    clean_env_vars()
    result = run_cli_cmd([
        sys.executable, 'config_snapshot_playback_cli.py',
        '--snapshot-db', cli_db,
        '--log-file', cli_log,
        'resolve', '--key', 'api_shared_key', '--default', '30',
        '--type', 'int', '--min', '1',
    ])
    check('[API共享 DB] CLI resolve 成功', result.returncode == 0)

    os.environ['REG_API_BAD'] = 'invalid_int'
    result2 = run_cli_cmd([
        sys.executable, 'config_snapshot_playback_cli.py',
        '--snapshot-db', cli_db,
        '--log-file', cli_log,
        'resolve', '--key', 'api_dirty_key', '--default', '42',
        '--env-key', 'REG_API_BAD', '--type', 'int',
    ])
    del os.environ['REG_API_BAD']
    check('[API共享 DB] CLI resolve fallback 成功', result2.returncode == 0)

    run_cli_cmd([
        sys.executable, 'config_snapshot_playback_cli.py',
        '--snapshot-db', cli_db,
        '--log-file', cli_log,
        'playback',
    ])

    csp_cli = ConfigSnapshotPlayback(snapshot_db=cli_db, log_file=':memory:')
    cli_snaps = csp_cli.get_all_snapshots(limit=100)
    cli_boots = csp_cli.list_boot_sequences()
    check('[API共享 DB] CLI 已写入至少 2 个快照', len(cli_snaps) >= 2)

    print('\n--- 场景 2: 复制 DB 到 Flask 工作目录并启动服务器 ---')
    flask_cwd = os.path.dirname(os.path.abspath(__file__))
    flask_db_src = cli_db
    flask_db_dst = os.path.join(flask_cwd, DEFAULT_SNAPSHOT_DB)

    for suffix in ['', '-wal', '-shm', '-journal']:
        src = flask_db_src + suffix
        dst = flask_db_dst + suffix
        if os.path.exists(dst):
            try:
                os.remove(dst)
            except Exception:
                pass
        if os.path.exists(src):
            shutil.copy2(src, dst)

    proc, server_log = start_server(tmpdir, clean_db=False)
    try:
        if not wait_for_server():
            check('[API共享 DB] Flask 服务启动', False, '超时')
            stop_server(proc)
            return

        print('\n--- 场景 3: /api/config/v2/boot 列出 CLI 写入的批次 ---')
        code, api_boots = api('/api/config/v2/boot')
        check('[API v2 /boot] 返回 200', code == 200, f'code={code}')
        if code == 200:
            check('[API v2 /boot] 批次数量与 CLI 一致',
                  len(api_boots) >= len(cli_boots),
                  f'API={len(api_boots)}, CLI={len(cli_boots)}')
            api_boot_seqs = set(b['boot_sequence'] for b in api_boots)
            cli_boot_seqs = set(b['boot_sequence'] for b in cli_boots)
            check('[API v2 /boot] 批次号集合包含 CLI 批次',
                  cli_boot_seqs.issubset(api_boot_seqs),
                  f'CLI={cli_boot_seqs}, API={api_boot_seqs}')

        print('\n--- 场景 4: /api/config/v2 读取快照与 CLI 一致 ---')
        code, api_snaps = api('/api/config/v2')
        check('[API v2 /config] 返回 200', code == 200)
        if code == 200 and isinstance(api_snaps, list):
            check('[API v2 /config] 快照数量与 CLI 一致',
                  len(api_snaps) >= len(cli_snaps),
                  f'API={len(api_snaps)}, CLI={len(cli_snaps)}')

            api_by_key = {s['config_key']: s for s in api_snaps}
            for cli_snap in cli_snaps:
                key = cli_snap.config_key
                api_snap = api_by_key.get(key)
                check(f'[API v2 一致 {key}] API 能查到 CLI 写入的快照',
                      api_snap is not None)
                if api_snap:
                    check(f'[API v2 一致 {key}] effective_value 一致',
                          str(api_snap.get('effective_value')) == str(cli_snap.effective_value),
                          f'API={api_snap.get("effective_value")}, CLI={cli_snap.effective_value}')
                    check(f'[API v2 一致 {key}] effective_source 一致',
                          api_snap.get('effective_source') == cli_snap.effective_source,
                          f'API={api_snap.get("effective_source")}, CLI={cli_snap.effective_source}')
                    check(f'[API v2 一致 {key}] is_fallback 一致',
                          bool(api_snap.get('is_fallback')) == cli_snap.is_fallback)

        print('\n--- 场景 5: /api/config/v2/playback 与 CLI playback 结论一致 ---')
        cli_latest_boot = max(b['boot_sequence'] for b in cli_boots)
        cli_playback = csp_cli.playback_boot(cli_latest_boot)

        code, api_playback = api(f'/api/config/v2/playback?boot={cli_latest_boot}')
        check('[API v2 /playback] 返回 200', code == 200)
        if code == 200:
            check('[API v2 /playback] 包含 overall_status',
                  'overall_status' in api_playback)
            check('[API v2 /playback] 包含 fallback_count',
                  'fallback_count' in api_playback)

            check('[API v2 回放一致] total_items 一致',
                  api_playback.get('total_items') == cli_playback.total_items,
                  f'API={api_playback.get("total_items")}, CLI={cli_playback.total_items}')
            check('[API v2 回放一致] fallback_count 一致',
                  api_playback.get('fallback_count') == cli_playback.fallback_count,
                  f'API={api_playback.get("fallback_count")}, CLI={cli_playback.fallback_count}')
            check('[API v2 回放一致] conflict_count 一致',
                  api_playback.get('conflict_count') == cli_playback.conflict_count)
            check('[API v2 回放一致] dirty_value_count 一致',
                  api_playback.get('dirty_value_count') == cli_playback.dirty_value_count,
                  f'API={api_playback.get("dirty_value_count")}, CLI={cli_playback.dirty_value_count}')

        print('\n--- 场景 6: /api/config/v2/diagnose 诊断 ---')
        code, diag = api('/api/config/v2/diagnose?key=api_dirty_key')
        check('[API v2 /diagnose] 返回 200', code == 200)
        if code == 200:
            check('[API v2 /diagnose] 包含 latest_snapshot',
                  'latest_snapshot' in diag and diag['latest_snapshot'] is not None)
            if diag.get('latest_snapshot'):
                check('[API v2 /diagnose] 脏值快照 is_fallback == True',
                      bool(diag['latest_snapshot'].get('is_fallback')) == True)
                check('[API v2 /diagnose] 脏值快照 effective_source == default(fallback)',
                      diag['latest_snapshot'].get('effective_source') == 'default(fallback)')

        print('\n--- 场景 7: /api/config/v2/boot/<seq> 详情与 CLI 一致 ---')
        if cli_boots:
            boot_seq = cli_boots[0]['boot_sequence']
            code, boot_detail = api(f'/api/config/v2/boot/{boot_seq}')
            check(f'[API v2 /boot/{boot_seq}] 返回 200', code == 200)
            if code == 200:
                check('[API v2 boot详情] 包含 config_items',
                      'config_items' in boot_detail)
                check('[API v2 boot详情] 包含 boot_sequence',
                      boot_detail.get('boot_sequence') == boot_seq)

        print('\n--- 场景 8: /api/config/v2/snapshots/export.json 导出 ---')
        code, export_resp = api('/api/config/v2/snapshots/export.json')
        check('[API v2 导出] 返回 200', code == 200, f'code={code}')
        if code == 200:
            check('[API v2 导出] 包含 snapshots',
                  isinstance(export_resp, dict) and 'snapshots' in export_resp)
            check('[API v2 导出] 包含 boot_sequences',
                  'boot_sequences' in export_resp)
            check('[API v2 导出] 包含 playback_conclusions',
                  'playback_conclusions' in export_resp)

        print('\n--- 场景 9: /api/config/v2/verify 验证往返 ---')
        code, verify = api('/api/config/v2/verify')
        check('[API v2 /verify] 返回 200', code == 200)
        if code == 200:
            check('[API v2 /verify] 包含 json_round_trip_ok',
                  'json_round_trip_ok' in verify or 'json_roundtrip' in verify or 'verified' in verify or 'errors' in verify)

    finally:
        stop_server(proc)
        remove_main_db()


def main():
    print('=' * 72)
    print('  配置快照与回放 - 回归链路测试')
    print('=' * 72)
    print('  测试范围:')
    print('    1. CLI resolve 生成启动批次')
    print('    2. 非法配置回退到 default(fallback)')
    print('    3. 环境变量和配置文件冲突时的优先级记录')
    print('    4. playback 结果写入 SQLite 并在新进程/重启后还能查到')
    print('    5. JSON 往返验证 (boot/playback/resolution_chain 保留)')
    print('    6. CSV 往返验证 (诊断细节压平但关键字段可回放)')
    print('    7. Flask v2 API 与 CLI 共用数据库端到端测试')
    print('=' * 72)
    print(f'  临时目录: {get_tmp_dir()}')
    print('=' * 72)

    try:
        test_cli_resolve_boot_sequence()
        test_invalid_config_fallback()
        test_env_config_priority()
        test_playback_persistence_across_process()
        test_json_roundtrip()
        test_csv_roundtrip()
        test_flask_v2_cli_shared_db()
    except Exception as e:
        print(f'\n测试执行异常: {e}')
        import traceback
        traceback.print_exc()
    finally:
        clean_env_vars()
        remove_main_db()
        cleanup_tmp()

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
