#!/usr/bin/env python3
import os
import sys
import json
import csv
import io
import subprocess
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config_snapshot_playback import (
    ConfigSnapshotPlayback,
    ConfigValueSnapshot,
    SourceEvaluation,
    PlaybackConclusion,
    DEFAULT_SNAPSHOT_DB,
    DEFAULT_LOG_FILE,
)


TEST_DB = 'test_snapshot_playback.db'
TEST_LOG = 'test_snapshot_playback.log'
TEST_CONFIG_FILE = 'test_sp_config.json'

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
              'test_sp_export.json', 'test_sp_export.csv',
              'test_sp_import.json', 'test_sp_import.csv']:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass
    for k in ['TEST_SP_EXPIRE', 'TEST_SP_DEBUG', 'TEST_SP_LOG_LEVEL',
              'TEST_SP_TIMEOUT', 'TEST_SP_RETRY', 'TEST_SP_HOST',
              'TEST_SP_PORT', 'TEST_SP_MAX_CONN']:
        if k in os.environ:
            del os.environ[k]


def verify_log_contains(log_file, expected_patterns, scenario_name):
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
        check(f'[{scenario_name}] 日志包含 "{pattern[:60]}..."',
              found, f'未找到: {pattern}')

    return all_found


def _val_eq(a, b):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return str(a) == str(b)


def _parse_bool_strict(v):
    if not isinstance(v, str):
        v = str(v)
    v = v.strip().lower()
    if v in ('true', '1', 'yes', 'on'):
        return True
    if v in ('false', '0', 'no', 'off'):
        return False
    raise ValueError(f'invalid boolean value: {v}')


def verify_four_way_consistency(
    scenario_name,
    query_result,
    log_content,
    export_data,
    playback_conclusion,
    expected_values,
):
    check(f'[{scenario_name}] 查询结果 effective_value == 预期',
          _val_eq(query_result.get('effective_value'), expected_values['effective_value']),
          f'查询={query_result.get("effective_value")}, 预期={expected_values["effective_value"]}')

    check(f'[{scenario_name}] 查询结果 effective_source == 预期',
          query_result.get('effective_source') == expected_values['effective_source'],
          f'查询={query_result.get("effective_source")}, 预期={expected_values["effective_source"]}')

    check(f'[{scenario_name}] 查询结果 is_fallback == 预期',
          query_result.get('is_fallback') == expected_values['is_fallback'],
          f'查询={query_result.get("is_fallback")}, 预期={expected_values["is_fallback"]}')

    check(f'[{scenario_name}] 导出数据 effective_value == 预期',
          export_data.get('effective_value') == str(expected_values['effective_value'])
          if isinstance(export_data.get('effective_value'), str)
          else export_data.get('effective_value') == expected_values['effective_value'],
          f'导出={export_data.get("effective_value")}')

    check(f'[{scenario_name}] 导出数据 effective_source == 预期',
          export_data.get('effective_source') == expected_values['effective_source'],
          f'导出={export_data.get("effective_source")}')

    check(f'[{scenario_name}] 导出数据 is_fallback == 预期',
          bool(export_data.get('is_fallback')) == expected_values['is_fallback'],
          f'导出={export_data.get("is_fallback")}')

    check(f'[{scenario_name}] 日志包含配置键名',
          expected_values['config_key'] in log_content,
          f'日志中未找到: {expected_values["config_key"]}')

    check(f'[{scenario_name}] 日志包含来源信息',
          expected_values['effective_source'] in log_content,
          f'日志中未找到来源: {expected_values["effective_source"]}')

    if expected_values['is_fallback']:
        check(f'[{scenario_name}] 回放结论 fallback_count > 0',
              playback_conclusion.get('fallback_count', 0) > 0)
        check(f'[{scenario_name}] 日志包含回退原因',
              '回退' in log_content or 'fallback' in log_content.lower())

    check(f'[{scenario_name}] 查询 <-> 导出 一致 (effective_value)',
          str(query_result.get('effective_value')) == str(export_data.get('effective_value'))
          if query_result.get('effective_value') is not None
          else export_data.get('effective_value') is None)

    check(f'[{scenario_name}] 查询 <-> 导出 一致 (effective_source)',
          query_result.get('effective_source') == export_data.get('effective_source'))

    check(f'[{scenario_name}] 查询 <-> 日志 一致',
          expected_values['config_key'] in log_content)


def run_unit_tests():
    print_section('单元测试: 核心数据结构')

    clean_test_env()

    print('\n--- 测试场景 1: SourceEvaluation 基本功能 ---')
    src = SourceEvaluation(source_name='env', priority=2, raw_value='test', is_available=True)
    check('[SourceEval] source_name 正确', src.source_name == 'env')
    check('[SourceEval] priority 正确', src.priority == 2)
    check('[SourceEval] raw_value 正确', src.raw_value == 'test')
    check('[SourceEval] is_available 默认 False', src.is_valid == False)

    src_dict = src.to_dict()
    check('[SourceEval] to_dict 包含 source_name', 'source_name' in src_dict)
    check('[SourceEval] to_dict 包含 priority', 'priority' in src_dict)

    src2 = SourceEvaluation.from_dict(src_dict)
    check('[SourceEval] from_dict 还原正确', src2.source_name == 'env' and src2.priority == 2)

    print('\n--- 测试场景 2: ConfigValueSnapshot 基本功能 ---')
    snap = ConfigValueSnapshot(
        config_key='test_key',
        effective_value=42,
        effective_source='env',
        is_fallback=False,
        default_value=30,
    )
    check('[ConfigSnap] config_key 正确', snap.config_key == 'test_key')
    check('[ConfigSnap] effective_value 正确', snap.effective_value == 42)
    check('[ConfigSnap] effective_source 正确', snap.effective_source == 'env')
    check('[ConfigSnap] 有 snapshot_uuid', len(snap.snapshot_uuid) > 0)
    check('[ConfigSnap] 有 snapshot_at', len(snap.snapshot_at) > 0)

    snap_dict = snap.to_dict()
    check('[ConfigSnap] to_dict 包含所有关键字段',
          all(k in snap_dict for k in [
              'snapshot_uuid', 'config_key', 'effective_value',
              'effective_source', 'is_fallback', 'integrity_hash'
          ]))

    snap2 = ConfigValueSnapshot.from_dict(snap_dict)
    check('[ConfigSnap] from_dict 还原正确',
          snap2.config_key == 'test_key' and snap2.effective_source == 'env')

    check('[ConfigSnap] 完整性哈希一致',
          snap._compute_hash() == snap2._compute_hash())

    print('\n--- 测试场景 3: 完整性哈希验证 ---')
    snap_a = ConfigValueSnapshot(
        config_key='key_a',
        effective_value=100,
        effective_source='env',
        is_fallback=False,
        resolution_explanation='test',
    )
    snap_b = ConfigValueSnapshot(
        config_key='key_a',
        effective_value=100,
        effective_source='env',
        is_fallback=False,
        resolution_explanation='test',
    )
    check('[完整性哈希] 相同内容哈希一致',
          snap_a._compute_hash() == snap_b._compute_hash(),
          f'a={snap_a._compute_hash()}, b={snap_b._compute_hash()}')

    snap_c = ConfigValueSnapshot(
        config_key='key_a',
        effective_value=200,
        effective_source='env',
        is_fallback=False,
    )
    check('[完整性哈希] 不同内容哈希不同',
          snap_a._compute_hash() != snap_c._compute_hash())

    clean_test_env()


def run_resolution_tests():
    print_section('核心功能测试: 配置解析与快照')

    clean_test_env()

    csp = ConfigSnapshotPlayback(
        snapshot_db=TEST_DB,
        log_file=TEST_LOG,
        config_file=TEST_CONFIG_FILE,
    )
    csp.start_boot_snapshot()

    print('\n--- 测试场景 1: 默认值配置 (无外部配置) ---')
    r1 = csp.resolve_config(
        key='test_default',
        default_value=30,
        env_key='TEST_SP_EXPIRE',
        value_parser=int,
        validator=lambda v: v > 0,
    )
    check('[默认值] effective_value == 30', r1.effective_value == 30, f'实际={r1.effective_value}')
    check('[默认值] effective_source == "default"', r1.effective_source == 'default')
    check('[默认值] is_fallback == False', r1.is_fallback == False)
    check('[默认值] raw_env_value == None', r1.raw_env_value is None)
    check('[默认值] resolution_explanation 非空', len(r1.resolution_explanation) > 0)
    check('[默认值] resolution_chain 有 3 个来源', len(r1.resolution_chain) == 3)
    check('[默认值] conflict_detected == False', r1.conflict_detected == False)
    check('[默认值] boot_sequence > 0', r1.boot_sequence > 0)

    print('\n--- 测试场景 2: 环境变量显式配置 ---')
    os.environ['TEST_SP_EXPIRE'] = '5'
    r2 = csp.resolve_config(
        key='test_env',
        default_value=30,
        env_key='TEST_SP_EXPIRE',
        value_parser=int,
        validator=lambda v: v > 0,
    )
    check('[显式配置] effective_value == 5', r2.effective_value == 5)
    check('[显式配置] effective_source == "env"', r2.effective_source == 'env')
    check('[显式配置] is_fallback == False', r2.is_fallback == False)
    check('[显式配置] raw_env_value == "5"', r2.raw_env_value == '5')
    check('[显式配置] diagnostic_notes 非空', len(r2.diagnostic_notes) > 0)
    del os.environ['TEST_SP_EXPIRE']

    print('\n--- 测试场景 3: 非法值回退 (非数字) ---')
    os.environ['TEST_SP_EXPIRE'] = 'abc'
    r3 = csp.resolve_config(
        key='test_fallback_invalid',
        default_value=30,
        env_key='TEST_SP_EXPIRE',
        value_parser=int,
        validator=lambda v: v > 0,
    )
    check('[非法值回退] effective_value == 30', r3.effective_value == 30)
    check('[非法值回退] effective_source == "default(fallback)"',
          r3.effective_source == 'default(fallback)')
    check('[非法值回退] is_fallback == True', r3.is_fallback == True)
    check('[非法值回退] fallback_reason 包含"解析失败"或"非法"',
          r3.fallback_reason and ('解析失败' in r3.fallback_reason or '非法' in r3.fallback_reason),
          f'实际={r3.fallback_reason}')
    check('[非法值回退] resolution_chain 中 env 标记为无效',
          any(s.source_name == 'env' and not s.is_valid for s in r3.resolution_chain))
    del os.environ['TEST_SP_EXPIRE']

    print('\n--- 测试场景 4: 非法值回退 (验证失败) ---')
    os.environ['TEST_SP_EXPIRE'] = '-5'
    r4 = csp.resolve_config(
        key='test_fallback_negative',
        default_value=30,
        env_key='TEST_SP_EXPIRE',
        value_parser=int,
        validator=lambda v: v > 0,
    )
    check('[验证失败回退] effective_value == 30', r4.effective_value == 30)
    check('[验证失败回退] is_fallback == True', r4.is_fallback == True)
    check('[验证失败回退] fallback_reason 包含"验证失败"',
          r4.fallback_reason and '验证失败' in r4.fallback_reason)
    del os.environ['TEST_SP_EXPIRE']

    print('\n--- 测试场景 5: 配置文件配置 ---')
    with open(TEST_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump({'test_config_key': 15}, f)

    r5 = csp.resolve_config(
        key='test_config_file',
        default_value=30,
        config_key='test_config_key',
        value_parser=int,
        validator=lambda v: v > 0,
    )
    check('[配置文件] effective_value == 15', r5.effective_value == 15)
    check('[配置文件] effective_source == "config_file"', r5.effective_source == 'config_file')
    check('[配置文件] raw_config_value == 15', r5.raw_config_value == 15)

    print('\n--- 测试场景 6: 多来源冲突 (配置文件 vs 环境变量) ---')
    os.environ['TEST_SP_CONFLICT'] = '20'
    with open(TEST_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump({'test_conflict': 10}, f)

    r6 = csp.resolve_config(
        key='test_conflict',
        default_value=30,
        env_key='TEST_SP_CONFLICT',
        config_key='test_conflict',
        value_parser=int,
        validator=lambda v: v > 0,
    )
    check('[多来源冲突] conflict_detected == True', r6.conflict_detected == True)
    check('[多来源冲突] conflict_details 非空', r6.conflict_details and len(r6.conflict_details) > 0)
    check('[多来源冲突] config_file 优先级更高 (值=10)',
          r6.effective_value == 10, f'实际={r6.effective_value}')
    check('[多来源冲突] source == "config_file"', r6.effective_source == 'config_file')
    del os.environ['TEST_SP_CONFLICT']

    print('\n--- 测试场景 7: 空字符串视为未设置 ---')
    os.environ['TEST_SP_EMPTY'] = ''
    r7 = csp.resolve_config(
        key='test_empty',
        default_value=30,
        env_key='TEST_SP_EMPTY',
        value_parser=int,
        validator=lambda v: v > 0,
    )
    check('[空字符串] source == "default"', r7.effective_source == 'default')
    check('[空字符串] effective_value == 30', r7.effective_value == 30)
    del os.environ['TEST_SP_EMPTY']

    boot = csp.finish_boot_snapshot()
    check('[启动快照] boot_sequence 正确', boot.boot_sequence == csp._boot_sequence)
    check('[启动快照] item_count > 0', len(boot.config_items) > 0)
    check('[启动快照] boot_summary 包含统计', 'fallback_count' in boot.boot_summary)

    clean_test_env()


def run_snapshot_query_tests():
    print_section('快照查询测试')

    clean_test_env()

    csp = ConfigSnapshotPlayback(
        snapshot_db=TEST_DB,
        log_file=TEST_LOG,
        config_file=TEST_CONFIG_FILE,
    )
    csp.start_boot_snapshot()

    csp.resolve_config(key='key1', default_value=10, value_parser=int)
    csp.resolve_config(key='key2', default_value=20, value_parser=int)
    csp.resolve_config(key='key3', default_value=30, value_parser=int)

    csp.finish_boot_snapshot()

    print('\n--- 测试场景 1: get_latest_snapshot ---')
    latest = csp.get_latest_snapshot()
    check('[最新快照] 返回非空', latest is not None)
    if latest:
        check('[最新快照] 有效配置值', latest.effective_value is not None)
        check('[最新快照] 有 integrity_hash', 'integrity_hash' in latest.to_dict())

    latest_key1 = csp.get_latest_snapshot('key1')
    check('[按键查询最新] 返回正确的配置键', latest_key1 and latest_key1.config_key == 'key1')

    print('\n--- 测试场景 2: get_all_snapshots ---')
    all_snaps = csp.get_all_snapshots(limit=100)
    check(f'[全部快照] 返回 {len(all_snaps)} 条', len(all_snaps) >= 3)

    snaps_by_key = csp.get_all_snapshots(key='key1')
    check(f'[按键查询] 返回 {len(snaps_by_key)} 条', len(snaps_by_key) >= 1)

    print('\n--- 测试场景 3: get_boot_snapshot ---')
    boot = csp.get_boot_snapshot(csp._boot_sequence)
    check('[启动快照] 返回非空', boot is not None)
    if boot:
        check('[启动快照] 包含所有配置项', len(boot.config_items) >= 3)
        check('[启动快照] 有 boot_summary', len(boot.boot_summary) > 0)

    print('\n--- 测试场景 4: list_boot_sequences ---')
    boot_list = csp.list_boot_sequences()
    check('[启动批次列表] 非空', len(boot_list) >= 1)
    if boot_list:
        check('[启动批次列表] 包含 boot_sequence', 'boot_sequence' in boot_list[0])
        check('[启动批次列表] 包含 summary', 'summary' in boot_list[0])

    print('\n--- 测试场景 5: compare_snapshots ---')
    if len(all_snaps) >= 2:
        comp = csp.compare_snapshots(all_snaps[0].snapshot_uuid, all_snaps[-1].snapshot_uuid)
        check('[快照比较] 包含 differences 字段', 'differences' in comp)
        check('[快照比较] 包含 identical 字段', 'identical' in comp)
        check('[快照比较] 包含两个快照数据', 'snapshot_1' in comp and 'snapshot_2' in comp)

    print('\n--- 测试场景 6: diagnose ---')
    diag = csp.diagnose('key1')
    check('[诊断] 包含 diagnose_at', 'diagnose_at' in diag)
    check('[诊断] 包含 latest_snapshot', 'latest_snapshot' in diag)
    check('[诊断] 包含 playback_conclusion', 'playback_conclusion' in diag)
    check('[诊断] 包含 boot_sequences', 'boot_sequences' in diag)

    clean_test_env()


def run_export_import_tests():
    print_section('导出导入测试')

    clean_test_env()

    csp = ConfigSnapshotPlayback(
        snapshot_db=TEST_DB,
        log_file=TEST_LOG,
        config_file=TEST_CONFIG_FILE,
    )
    csp.start_boot_snapshot()

    os.environ['TEST_SP_TIMEOUT'] = '30'
    os.environ['TEST_SP_DEBUG'] = 'true'

    csp.resolve_config(
        key='timeout',
        default_value=60,
        env_key='TEST_SP_TIMEOUT',
        value_parser=int,
        validator=lambda v: v > 0,
    )
    csp.resolve_config(
        key='debug',
        default_value=False,
        env_key='TEST_SP_DEBUG',
        value_parser=lambda v: v.lower() == 'true',
    )
    csp.resolve_config(
        key='max_conn',
        default_value=100,
        env_key='TEST_SP_MAX_CONN',
        value_parser=int,
        validator=lambda v: v > 0,
    )

    del os.environ['TEST_SP_TIMEOUT']
    del os.environ['TEST_SP_DEBUG']

    csp.finish_boot_snapshot()
    original_snaps = csp.get_all_snapshots(limit=100)
    original_count = len(original_snaps)

    print('\n--- 测试场景 1: JSON 导出 ---')
    json_export = csp.export_snapshots(fmt='json')
    check('[JSON导出] 导出内容非空', len(json_export) > 0)

    json_data = json.loads(json_export)
    check('[JSON导出] 包含 export_format_version', 'export_format_version' in json_data)
    check('[JSON导出] 包含 snapshots 字段', 'snapshots' in json_data)
    check('[JSON导出] snapshots 数量正确', len(json_data['snapshots']) == original_count)
    check('[JSON导出] 包含 integrity_root_hash', 'integrity_root_hash' in json_data)
    check('[JSON导出] 包含 boot_sequences', 'boot_sequences' in json_data)

    for snap in json_data['snapshots']:
        check('[JSON导出] 每条快照有 integrity_hash', 'integrity_hash' in snap)
        check('[JSON导出] 每条快照有 resolution_chain', 'resolution_chain' in snap)
        break

    print('\n--- 测试场景 2: CSV 导出 ---')
    csv_export = csp.export_snapshots(fmt='csv')
    check('[CSV导出] 导出内容非空', len(csv_export) > 0)

    csv_reader = csv.reader(io.StringIO(csv_export))
    csv_rows = list(csv_reader)
    check('[CSV导出] 有表头', len(csv_rows) > 1)
    check('[CSV导出] 行数正确 (含表头)', len(csv_rows) == original_count + 1)
    check('[CSV导出] 表头包含 config_key', 'config_key' in csv_rows[0])
    check('[CSV导出] 表头包含 integrity_hash', 'integrity_hash' in csv_rows[0])

    print('\n--- 测试场景 3: 导出到文件 ---')
    csp.export_to_file('test_sp_export.json')
    check('[文件导出JSON] 文件存在', os.path.exists('test_sp_export.json'))

    csp.export_to_file('test_sp_export.csv')
    check('[文件导出CSV] 文件存在', os.path.exists('test_sp_export.csv'))

    print('\n--- 测试场景 4: JSON 导入 (幂等性) ---')
    import_result = csp.import_snapshots('test_sp_export.json')
    check(f'[JSON导入幂等] 导入 {import_result["imported"]} 条 (应为0)',
          import_result['imported'] == 0)
    check('[JSON导入幂等] 跳过数正确', import_result['skipped'] == original_count)

    print('\n--- 测试场景 5: 往返一致性验证 (JSON) ---')
    verify_db = ConfigSnapshotPlayback(
        snapshot_db=':memory:',
        log_file=':memory:',
    )
    verify_result = verify_db.import_snapshots('test_sp_export.json')
    check('[JSON往返] 导入数量正确', verify_result['imported'] == original_count)

    imported_snaps = verify_db.get_all_snapshots(limit=100)
    check('[JSON往返] 导入后快照数一致', len(imported_snaps) == original_count)

    original_hashes = {s.config_key: s._compute_hash() for s in original_snaps}
    imported_hashes = {s.config_key: s._compute_hash() for s in imported_snaps}

    hash_match = all(
        original_hashes.get(k) == h for k, h in imported_hashes.items()
    ) and len(imported_hashes) == len(original_hashes)

    check('[JSON往返] 完整性哈希全部一致', hash_match,
          f'原始={len(original_hashes)}个, 导入={len(imported_hashes)}个')

    print('\n--- 测试场景 6: 往返一致性验证 (内置 verify_round_trip) ---')
    rt_result = csp.verify_round_trip()
    check('[内置验证] 原始数量正确', rt_result['original_count'] == original_count)
    check('[内置验证] JSON 往返一致', rt_result['json_round_trip_ok'] == True)
    check('[内置验证] CSV 基础字段一致', rt_result['csv_basic_fields_ok'] == True)

    print('\n--- 测试场景 7: 回退标记和原因的往返保持 ---')
    os.environ['TEST_SP_DIRTY'] = 'not_a_number'
    csp2 = ConfigSnapshotPlayback(
        snapshot_db=':memory:',
        log_file=':memory:',
    )
    csp2.start_boot_snapshot()
    dirty_snap = csp2.resolve_config(
        key='dirty_key',
        default_value=42,
        env_key='TEST_SP_DIRTY',
        value_parser=int,
    )
    csp2.finish_boot_snapshot()
    del os.environ['TEST_SP_DIRTY']

    check('[回退往返] 原始 is_fallback == True', dirty_snap.is_fallback == True)
    check('[回退往返] 原始有 fallback_reason', dirty_snap.fallback_reason is not None)

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
        f.write(csp2.export_snapshots(fmt='json'))
        tmp_json = f.name

    verify2 = ConfigSnapshotPlayback(snapshot_db=':memory:', log_file=':memory:')
    verify2.import_snapshots(tmp_json)
    imported_dirty = verify2.get_latest_snapshot('dirty_key')

    check('[回退往返] 导入后 is_fallback 保持', imported_dirty and imported_dirty.is_fallback == True)
    check('[回退往返] 导入后 fallback_reason 保持',
          imported_dirty and imported_dirty.fallback_reason == dirty_snap.fallback_reason)
    check('[回退往返] 导入后 effective_source 保持',
          imported_dirty and imported_dirty.effective_source == dirty_snap.effective_source)
    check('[回退往返] 完整性哈希一致',
          imported_dirty and imported_dirty._compute_hash() == dirty_snap._compute_hash())

    os.unlink(tmp_json)

    clean_test_env()


def run_playback_tests():
    print_section('回放与诊断测试')

    clean_test_env()

    csp = ConfigSnapshotPlayback(
        snapshot_db=TEST_DB,
        log_file=TEST_LOG,
        config_file=TEST_CONFIG_FILE,
    )

    print('\n--- 测试场景 1: 第一个启动批次 (默认配置) ---')
    csp.start_boot_snapshot()
    csp.resolve_config(key='expire_minutes', default_value=30, value_parser=int, validator=lambda v: v > 0)
    csp.resolve_config(key='debug_mode', default_value=False)
    csp.resolve_config(key='log_level', default_value='info')
    boot1 = csp.finish_boot_snapshot()
    boot1_seq = boot1.boot_sequence
    check('[批次1] 启动批次号正确', boot1_seq == 1)
    check('[批次1] 配置项数量 3', len(boot1.config_items) == 3)

    print('\n--- 测试场景 2: 第二个启动批次 (显式配置+回退) ---')
    csp2 = ConfigSnapshotPlayback(
        snapshot_db=TEST_DB,
        log_file=TEST_LOG,
        config_file=TEST_CONFIG_FILE,
    )
    os.environ['EXPIRE_MINUTES'] = '60'
    os.environ['DEBUG_MODE'] = 'invalid_bool'

    csp2.start_boot_snapshot()
    csp2.resolve_config(key='expire_minutes', default_value=30, env_key='EXPIRE_MINUTES',
                        value_parser=int, validator=lambda v: v > 0)
    csp2.resolve_config(key='debug_mode', default_value=False, env_key='DEBUG_MODE',
                        value_parser=_parse_bool_strict)
    csp2.resolve_config(key='log_level', default_value='info')
    csp2.resolve_config(key='max_retries', default_value=3, value_parser=int)
    boot2 = csp2.finish_boot_snapshot()
    boot2_seq = boot2.boot_sequence

    del os.environ['EXPIRE_MINUTES']
    del os.environ['DEBUG_MODE']

    check('[批次2] 启动批次号递增', boot2_seq == boot1_seq + 1)
    check('[批次2] 配置项数量 4', len(boot2.config_items) == 4)
    check('[批次2] expire_minutes 来自 env',
          boot2.config_items['expire_minutes'].effective_source == 'env')
    check('[批次2] debug_mode 回退',
          boot2.config_items['debug_mode'].is_fallback == True)

    print('\n--- 测试场景 3: 回放第一个批次 ---')
    playback1 = csp2.playback_boot(boot1_seq)
    check('[回放1] 是 PlaybackConclusion 类型', isinstance(playback1, PlaybackConclusion))
    check('[回放1] total_items == 3', playback1.total_items == 3)
    check('[回放1] fallback_count == 0', playback1.fallback_count == 0)
    check('[回放1] overall_status == "normal"', playback1.overall_status == 'normal')
    check('[回放1] 有 summary_text', len(playback1.summary_text) > 0)
    check('[回放1] 有 source_distribution', len(playback1.source_distribution) > 0)

    print('\n--- 测试场景 4: 回放第二个批次 ---')
    playback2 = csp2.playback_boot(boot2_seq)
    check('[回放2] total_items == 4', playback2.total_items == 4)
    check('[回放2] fallback_count > 0', playback2.fallback_count > 0)
    check('[回放2] overall_status == "warning"', playback2.overall_status == 'warning')
    check('[回放2] 有 detailed_findings', len(playback2.detailed_findings) > 0)
    check('[回放2] 有 recommendations', len(playback2.recommendations) > 0)

    print('\n--- 测试场景 5: 跨启动批次比较 ---')
    comp = csp2.compare_boots(boot1_seq, boot2_seq)
    check('[批次比较] 包含 boot_1 和 boot_2', 'boot_1' in comp and 'boot_2' in comp)
    check('[批次比较] 有 differences 列表', 'differences' in comp)
    check('[批次比较] 有 identical_keys 列表', 'identical_keys' in comp)
    check('[批次比较] 有 only_in_boot_2', 'only_in_boot_2' in comp)
    check('[批次比较] 有差异 (配置不同)', comp['total_differences'] > 0)
    check('[批次比较] max_retries 仅在批次2', 'max_retries' in comp['only_in_boot_2'])

    print('\n--- 测试场景 6: 回放结论包含与上一批次的变化 ---')
    check('[回放变化] changes_from_previous 非空', len(playback2.changes_from_previous) > 0)

    print('\n--- 测试场景 7: 诊断接口包含回放结论 ---')
    diag = csp2.diagnose('expire_minutes')
    check('[诊断接口] 包含 playback_conclusion', 'playback_conclusion' in diag)
    check('[诊断接口] 回放结论有 overall_status',
          diag['playback_conclusion'].get('overall_status') is not None)

    clean_test_env()


def run_logging_tests():
    print_section('日志落盘测试')

    clean_test_env()

    csp = ConfigSnapshotPlayback(
        snapshot_db=TEST_DB,
        log_file=TEST_LOG,
    )
    csp.start_boot_snapshot()

    os.environ['TEST_SP_LOG_TEST'] = '42'
    csp.resolve_config(
        key='log_test',
        default_value=10,
        env_key='TEST_SP_LOG_TEST',
        value_parser=int,
    )
    del os.environ['TEST_SP_LOG_TEST']

    os.environ['TEST_SP_BAD_VAL'] = 'not_int'
    csp.resolve_config(
        key='bad_val',
        default_value=100,
        env_key='TEST_SP_BAD_VAL',
        value_parser=int,
    )
    del os.environ['TEST_SP_BAD_VAL']

    csp.finish_boot_snapshot()

    log_patterns = [
        '快照数据库初始化完成',
        'log_test',
        '配置解析完成',
        'bad_val',
        '回退原因',
        '快照已保存',
        '快照完成',
    ]
    verify_log_contains(TEST_LOG, log_patterns, '日志落盘')

    check('[日志] get_log_tail 可读取', len(csp.get_log_tail(10)) > 0)

    clean_test_env()


def run_four_way_consistency_tests():
    print_section('四边一致性验证 (查询/日志/导出/回放)')

    clean_test_env()

    csp = ConfigSnapshotPlayback(
        snapshot_db=TEST_DB,
        log_file=TEST_LOG,
        config_file=TEST_CONFIG_FILE,
    )
    csp.start_boot_snapshot()

    os.environ['TEST_CONSIST_KEY'] = '25'
    os.environ['TEST_CONSIST_BAD'] = 'illegal_value'

    good_snap = csp.resolve_config(
        key='consist_good',
        default_value=50,
        env_key='TEST_CONSIST_KEY',
        value_parser=int,
        validator=lambda v: v > 0,
    )

    bad_snap = csp.resolve_config(
        key='consist_bad',
        default_value=10,
        env_key='TEST_CONSIST_BAD',
        value_parser=int,
        validator=lambda v: v > 0,
    )

    boot = csp.finish_boot_snapshot()

    del os.environ['TEST_CONSIST_KEY']
    del os.environ['TEST_CONSIST_BAD']

    with open(TEST_LOG, 'r', encoding='utf-8') as f:
        log_content = f.read()

    json_export = json.loads(csp.export_snapshots(fmt='json'))
    good_export = next((s for s in json_export['snapshots'] if s['config_key'] == 'consist_good'), None)
    bad_export = next((s for s in json_export['snapshots'] if s['config_key'] == 'consist_bad'), None)

    playback = csp.playback_boot(boot.boot_sequence)

    print('\n--- 验证场景 1: 正常配置项四边一致 ---')
    good_query = csp.get_latest_snapshot('consist_good').to_dict()
    verify_four_way_consistency(
        '正常配置',
        good_query,
        log_content,
        good_export if good_export else {},
        playback.to_dict(),
        {
            'config_key': 'consist_good',
            'effective_value': 25,
            'effective_source': 'env',
            'is_fallback': False,
        }
    )

    print('\n--- 验证场景 2: 回退配置项四边一致 ---')
    bad_query = csp.get_latest_snapshot('consist_bad').to_dict()
    verify_four_way_consistency(
        '回退配置',
        bad_query,
        log_content,
        bad_export if bad_export else {},
        playback.to_dict(),
        {
            'config_key': 'consist_bad',
            'effective_value': 10,
            'effective_source': 'default(fallback)',
            'is_fallback': True,
        }
    )

    print('\n--- 验证场景 3: 回放结论与实际数据一致 ---')
    check('[回放一致] fallback_count 与实际回退数匹配',
          playback.fallback_count == sum(1 for s in boot.config_items.values() if s.is_fallback))
    check('[回放一致] total_items 与配置项数匹配',
          playback.total_items == len(boot.config_items))

    source_dist_count = sum(playback.source_distribution.values())
    check('[回放一致] 来源分布总数与配置项数一致',
          source_dist_count == len(boot.config_items))

    print('\n--- 验证场景 4: 完整性哈希链路一致 ---')
    query_good = csp.get_latest_snapshot('consist_good')
    export_good_dict = good_export
    if good_export:
        check('[哈希一致] 查询与导出的 integrity_hash 相同',
              query_good._compute_hash() == export_good_dict.get('integrity_hash'))

    export_root_hash = json_export.get('integrity_root_hash')
    check('[哈希一致] 导出有根哈希', export_root_hash is not None and len(export_root_hash) > 0)

    clean_test_env()


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
        [sys.executable, 'config_snapshot_playback_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'resolve', '--key', 'cli_test', '--default', '30',
         '--type', 'int', '--min', '1']
    )
    check('[CLI resolve] 命令成功', result.returncode == 0,
          f'stderr={result.stderr[:200]}' if result.stderr else '')
    check('[CLI resolve] 输出包含生效值', '生效值' in result.stdout or 'effective' in result.stdout.lower())
    check('[CLI resolve] 输出包含来源', '来源' in result.stdout or 'source' in result.stdout.lower())

    print('\n--- CLI 场景 2: current 命令 ---')
    result = run_cli_cmd(
        [sys.executable, 'config_snapshot_playback_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'current']
    )
    check('[CLI current] 命令成功', result.returncode == 0)

    print('\n--- CLI 场景 3: snapshot --latest 命令 ---')
    result = run_cli_cmd(
        [sys.executable, 'config_snapshot_playback_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'snapshot', '--latest']
    )
    check('[CLI snapshot latest] 命令成功', result.returncode == 0)

    print('\n--- CLI 场景 4: boot --list 命令 ---')
    result = run_cli_cmd(
        [sys.executable, 'config_snapshot_playback_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'boot', '--list']
    )
    check('[CLI boot list] 命令成功', result.returncode == 0)

    print('\n--- CLI 场景 5: playback 命令 ---')
    result = run_cli_cmd(
        [sys.executable, 'config_snapshot_playback_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'playback']
    )
    check('[CLI playback] 命令成功', result.returncode == 0)
    check('[CLI playback] 输出包含回放结论', '回放' in result.stdout or 'playback' in result.stdout.lower())

    print('\n--- CLI 场景 6: export 命令 (JSON) ---')
    result = run_cli_cmd(
        [sys.executable, 'config_snapshot_playback_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'export', '--output', 'test_sp_export.json']
    )
    check('[CLI export JSON] 命令成功', result.returncode == 0)
    check('[CLI export JSON] 文件存在', os.path.exists('test_sp_export.json'))

    print('\n--- CLI 场景 7: export 命令 (CSV) ---')
    result = run_cli_cmd(
        [sys.executable, 'config_snapshot_playback_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'export', '--output', 'test_sp_export.csv', '--format', 'csv']
    )
    check('[CLI export CSV] 命令成功', result.returncode == 0)
    check('[CLI export CSV] 文件存在', os.path.exists('test_sp_export.csv'))

    print('\n--- CLI 场景 8: import 命令 ---')
    clean_test_db2 = 'test_cli_import.db'
    if os.path.exists(clean_test_db2):
        os.remove(clean_test_db2)

    result = run_cli_cmd(
        [sys.executable, 'config_snapshot_playback_cli.py',
         '--snapshot-db', clean_test_db2,
         '--log-file', TEST_LOG,
         'import', '--input', 'test_sp_export.json']
    )
    check('[CLI import] 命令成功', result.returncode == 0)
    if os.path.exists(clean_test_db2):
        os.remove(clean_test_db2)

    print('\n--- CLI 场景 9: verify 命令 ---')
    result = run_cli_cmd(
        [sys.executable, 'config_snapshot_playback_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'verify']
    )
    check('[CLI verify] 命令成功', result.returncode == 0)
    check('[CLI verify] 输出包含验证结果', '一致' in result.stdout or 'OK' in result.stdout or 'PASS' in result.stdout)

    print('\n--- CLI 场景 10: logs 命令 ---')
    result = run_cli_cmd(
        [sys.executable, 'config_snapshot_playback_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'logs', '--tail', '10']
    )
    check('[CLI logs] 命令成功', result.returncode == 0)

    print('\n--- CLI 场景 11: diagnose 命令 ---')
    result = run_cli_cmd(
        [sys.executable, 'config_snapshot_playback_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'diagnose', '--key', 'cli_test']
    )
    check('[CLI diagnose] 命令成功', result.returncode == 0)
    check('[CLI diagnose] 输出包含诊断信息', '诊断' in result.stdout or 'diagnos' in result.stdout.lower())

    print('\n--- CLI 场景 12: clear 命令 ---')
    result = run_cli_cmd(
        [sys.executable, 'config_snapshot_playback_cli.py',
         '--snapshot-db', TEST_DB,
         '--log-file', TEST_LOG,
         'clear', '--yes']
    )
    check('[CLI clear] 命令成功', result.returncode == 0)

    clean_test_env()
    for f in ['test_sp_export.json', 'test_sp_export.csv']:
        if os.path.exists(f):
            os.remove(f)


def run_multi_boot_scenario():
    print_section('综合场景: 多启动批次完整链路')

    clean_test_env()

    boot_sequences = []

    print('\n--- 启动 1: 默认配置 ---')
    csp1 = ConfigSnapshotPlayback(snapshot_db=TEST_DB, log_file=TEST_LOG)
    csp1.start_boot_snapshot()
    csp1.resolve_config(key='timeout', default_value=30, value_parser=int, validator=lambda v: v > 0)
    csp1.resolve_config(key='debug', default_value=False)
    csp1.resolve_config(key='log_level', default_value='info')
    boot1 = csp1.finish_boot_snapshot()
    boot_sequences.append(boot1.boot_sequence)
    check('[启动1] 全部默认值', boot1.boot_summary['fallback_count'] == 0)

    print('\n--- 启动 2: 环境变量配置 (部分回退) ---')
    os.environ['TIMEOUT'] = '60'
    os.environ['DEBUG'] = 'true'
    os.environ['LOG_LEVEL'] = ''

    csp2 = ConfigSnapshotPlayback(snapshot_db=TEST_DB, log_file=TEST_LOG)
    csp2.start_boot_snapshot()
    csp2.resolve_config(key='timeout', default_value=30, env_key='TIMEOUT',
                        value_parser=int, validator=lambda v: v > 0)
    csp2.resolve_config(key='debug', default_value=False, env_key='DEBUG',
                        value_parser=lambda v: v.lower() == 'true')
    csp2.resolve_config(key='log_level', default_value='info', env_key='LOG_LEVEL')
    boot2 = csp2.finish_boot_snapshot()
    boot_sequences.append(boot2.boot_sequence)

    check('[启动2] timeout 来自 env',
          boot2.config_items['timeout'].effective_source == 'env')
    check('[启动2] debug 来自 env',
          boot2.config_items['debug'].effective_source == 'env')
    check('[启动2] log_level 是默认 (空串视为未设置)',
          boot2.config_items['log_level'].effective_source == 'default')

    del os.environ['TIMEOUT']
    del os.environ['DEBUG']
    del os.environ['LOG_LEVEL']

    print('\n--- 启动 3: 配置文件 + 冲突 + 脏值 ---')
    with open(TEST_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump({
            'timeout': 45,
            'max_conn': 'bad_value',
        }, f)

    os.environ['TIMEOUT'] = '90'

    csp3 = ConfigSnapshotPlayback(snapshot_db=TEST_DB, log_file=TEST_LOG, config_file=TEST_CONFIG_FILE)
    csp3.start_boot_snapshot()
    csp3.resolve_config(key='timeout', default_value=30, env_key='TIMEOUT', config_key='timeout',
                        value_parser=int, validator=lambda v: v > 0)
    csp3.resolve_config(key='max_conn', default_value=100, config_key='max_conn',
                        value_parser=int, validator=lambda v: v > 0)
    csp3.resolve_config(key='debug', default_value=False)
    boot3 = csp3.finish_boot_snapshot()
    boot_sequences.append(boot3.boot_sequence)

    check('[启动3] timeout 冲突检测',
          boot3.config_items['timeout'].conflict_detected == True)
    check('[启动3] timeout 采用 config_file (优先级更高)',
          boot3.config_items['timeout'].effective_value == 45)
    check('[启动3] max_conn 回退',
          boot3.config_items['max_conn'].is_fallback == True)
    check('[启动3] debug 正常',
          boot3.config_items['debug'].effective_source == 'default')

    del os.environ['TIMEOUT']

    print('\n--- 跨启动比对: 批次1 vs 批次3 ---')
    comp = csp3.compare_boots(boot_sequences[0], boot_sequences[2])
    check('[跨启动比对] 有差异', comp['total_differences'] > 0)
    check('[跨启动比对] timeout 有变化',
          any(d['config_key'] == 'timeout' for d in comp['differences']))
    check('[跨启动比对] max_conn 仅在批次3',
          'max_conn' in comp['only_in_boot_2'])

    print('\n--- 回放批次3 并生成结论 ---')
    playback = csp3.playback_boot(boot_sequences[2])
    check('[回放批次3] 有回退项', playback.fallback_count > 0)
    check('[回放批次3] 有冲突项', playback.conflict_count > 0)
    check('[回放批次3] 状态为 warning', playback.overall_status == 'warning')
    check('[回放批次3] 有详细发现', len(playback.detailed_findings) > 0)
    check('[回放批次3] 有建议', len(playback.recommendations) > 0)

    print('\n--- 导出批次3 并验证往返 ---')
    rt = csp3.verify_round_trip(boot_sequence=boot_sequences[2])
    check('[往返验证] JSON 往返一致', rt['json_round_trip_ok'])

    print('\n--- 日志验证 ---')
    log_patterns = [
        '启动批次',
        '配置解析完成',
        '快照已保存',
        '回退原因',
        '多来源冲突',
        '回放完成',
    ]
    verify_log_contains(TEST_LOG, log_patterns, '综合场景日志')

    clean_test_env()


def main():
    print('=' * 72)
    print('  配置快照与回放模块 - 完整测试套件')
    print('=' * 72)
    print('  测试范围:')
    print('    - 核心数据结构 (SourceEvaluation, ConfigValueSnapshot, etc.)')
    print('    - 配置解析与快照记录')
    print('    - 快照查询与比较')
    print('    - JSON/CSV 导出导入 (往返一致性)')
    print('    - 回放引擎与诊断结论')
    print('    - 日志落盘')
    print('    - 四边一致性 (查询/日志/导出/回放结论)')
    print('    - CLI 工具')
    print('    - 多启动批次综合场景')
    print('=' * 72)

    try:
        run_unit_tests()
        run_resolution_tests()
        run_snapshot_query_tests()
        run_export_import_tests()
        run_playback_tests()
        run_logging_tests()
        run_four_way_consistency_tests()
        run_cli_tests()
        run_multi_boot_scenario()
    except Exception as e:
        print(f'\n测试执行异常: {e}')
        import traceback
        traceback.print_exc()
    finally:
        clean_test_env()

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
