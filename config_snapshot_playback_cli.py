#!/usr/bin/env python3
import argparse
import sys
import os
import json
from datetime import datetime

from config_snapshot_playback import (
    ConfigSnapshotPlayback,
    DEFAULT_SNAPSHOT_DB,
    DEFAULT_LOG_FILE,
)


def print_section(title: str):
    line = '=' * 72
    print(f'\n{line}')
    print(f'  {title}')
    print(line)


def print_dict(d: dict, indent: int = 0):
    prefix = '  ' * indent
    for k, v in d.items():
        if isinstance(v, dict):
            print(f'{prefix}{k}:')
            print_dict(v, indent + 1)
        elif isinstance(v, list):
            if v and isinstance(v[0], dict):
                print(f'{prefix}{k}: (list, {len(v)} items)')
                for i, item in enumerate(v[:5]):
                    print(f'{prefix}  [{i}]:')
                    print_dict(item, indent + 2)
                if len(v) > 5:
                    print(f'{prefix}  ... (还有 {len(v) - 5} 条)')
            else:
                val_str = ', '.join(str(x) for x in v[:5])
                if len(v) > 5:
                    val_str += f' ... (共 {len(v)} 项)'
                print(f'{prefix}{k}: [{val_str}]')
        else:
            print(f'{prefix}{k}: {v}')


def cmd_resolve(args, csp: ConfigSnapshotPlayback):
    print_section('解析配置')

    value_parser = None
    validator = None

    if args.type == 'int':
        value_parser = int
    elif args.type == 'float':
        value_parser = float

    if args.type in ('int', 'float') and (args.min is not None or args.max is not None):
        def validator(v):
            if args.min is not None and v < args.min:
                return False
            if args.max is not None and v > args.max:
                return False
            return True

    try:
        default = args.default
        if value_parser and default is not None:
            default = value_parser(default)

        snap = csp.resolve_config(
            key=args.key,
            default_value=default,
            env_key=args.env_key,
            config_key=args.config_key,
            value_parser=value_parser,
            validator=validator,
        )

        print(f"\n  配置项: {snap.config_key}")
        print(f"  生效值: {snap.effective_value}")
        print(f"  来源: {snap.effective_source}")
        print(f"  是否回退: {snap.is_fallback}")
        if snap.fallback_reason:
            print(f"  回退原因: {snap.fallback_reason}")
        print(f"  默认值: {snap.default_value}")
        print(f"  冲突检测: {'是' if snap.conflict_detected else '否'}")
        if snap.conflict_details:
            print(f"  冲突详情: {snap.conflict_details}")
        print(f"  排障结论: {snap.resolution_explanation}")
        print(f"  快照UUID: {snap.snapshot_uuid}")
        print(f"  启动批次: {snap.boot_sequence}")
        print(f"  完整性哈希: {snap._compute_hash()}")

        print(f"\n  来源优先级追溯链:")
        for src in sorted(snap.resolution_chain, key=lambda x: x.priority):
            status = '[OK] 有效' if src.is_valid else '[X] 无效'
            if not src.is_available:
                status = '[-] 未设置'
            print(f"    [{src.priority}] {src.source_name}: {src.raw_value!r} - {status}")
            if src.error_message:
                print(f"         错误: {src.error_message}")

        if snap.diagnostic_notes:
            print(f"\n  诊断备注:")
            for note in snap.diagnostic_notes:
                print(f"    - {note}")

    except Exception as e:
        print(f'  解析失败: {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)


def cmd_current(args, csp: ConfigSnapshotPlayback):
    print_section('当前生效配置')
    snaps = csp.get_all_snapshots(boot_sequence=csp._boot_sequence, limit=1000)

    if not snaps:
        print('  暂无配置')
        return

    print(f'  当前启动批次: {csp._boot_sequence}')
    print(f'  配置项数量: {len(snaps)}')

    for snap in snaps:
        print(f"\n  配置项: {snap.config_key}")
        print(f"    生效值: {snap.effective_value}")
        print(f"    来源: {snap.effective_source}")
        print(f"    回退: {snap.is_fallback}")
        if snap.fallback_reason:
            print(f"    回退原因: {snap.fallback_reason}")


def cmd_snapshot(args, csp: ConfigSnapshotPlayback):
    print_section('配置快照')

    if args.latest:
        snap = csp.get_latest_snapshot(args.key)
        if snap:
            d = snap.to_dict()
            print(f"  配置项: {d['config_key']}")
            print(f"  生效值: {d['effective_value']}")
            print(f"  来源: {d['effective_source']}")
            print(f"  回退: {d['is_fallback']}")
            print(f"  快照时间: {d['snapshot_at']}")
            print(f"  启动批次: {d['boot_sequence']}")
            print(f"  完整性哈希: {d['integrity_hash']}")
            print(f"\n  详细信息:")
            print_dict(d, indent=2)
        else:
            print('  暂无快照')

    elif args.all:
        snaps = csp.get_all_snapshots(args.key, args.limit)
        print(f'  共 {len(snaps)} 条快照:')
        for i, snap in enumerate(snaps):
            d = snap.to_dict()
            print(f"\n  [{i+1}] {d['config_key']} = {d['effective_value']}")
            print(f"      来源: {d['effective_source']}, 回退: {d['is_fallback']}")
            print(f"      批次: {d['boot_sequence']}, 时间: {d['snapshot_at']}")

    elif args.boot is not None:
        boot = csp.get_boot_snapshot(args.boot)
        if boot:
            print(f'  启动批次: {boot.boot_sequence}')
            print(f'  启动时间: {boot.boot_at}')
            print(f'  进程ID: {boot.process_id}')
            print(f'  配置项数量: {len(boot.config_items)}')
            if boot.boot_summary:
                print(f'  启动摘要:')
                print_dict(boot.boot_summary, indent=2)

            print(f'\n  配置项列表:')
            for key, snap in sorted(boot.config_items.items()):
                print(f"    {key}: {snap.effective_value} ({snap.effective_source})")
        else:
            print(f'  未找到启动批次 {args.boot}')

    elif args.compare:
        uuid1, uuid2 = args.compare
        result = csp.compare_snapshots(uuid1, uuid2)
        if 'error' in result:
            print(f'  错误: {result["error"]}')
        else:
            if result['identical']:
                print('  两个快照完全一致')
            else:
                print(f'  发现 {len(result["differences"])} 处差异:')
                for diff in result['differences']:
                    print(f"\n    字段: {diff['field']}")
                    print(f"      快照1: {diff['snapshot_1']}")
                    print(f"      快照2: {diff['snapshot_2']}")


def cmd_boot(args, csp: ConfigSnapshotPlayback):
    print_section('启动批次')

    if args.list:
        boots = csp.list_boot_sequences()
        print(f'  共 {len(boots)} 个启动批次:')
        for boot in boots:
            status = boot.get('summary', {}).get('boot_status', 'unknown')
            print(f"\n    批次 {boot['boot_sequence']}:")
            print(f"      时间: {boot['boot_at']}")
            print(f"      配置项: {boot['item_count']}")
            print(f"      状态: {status}")

    elif args.compare:
        b1, b2 = args.compare
        result = csp.compare_boots(b1, b2)
        if 'error' in result:
            print(f'  错误: {result["error"]}')
        else:
            print(f"  比较: 批次 {result['boot_1']} vs 批次 {result['boot_2']}")
            print(f"  差异项: {result['total_differences']}")
            print(f"  相同项: {result['total_identical']}")

            if result['only_in_boot_1']:
                print(f"\n  仅在批次 {result['boot_1']}: {', '.join(result['only_in_boot_1'])}")
            if result['only_in_boot_2']:
                print(f"\n  仅在批次 {result['boot_2']}: {', '.join(result['only_in_boot_2'])}")

            if result['differences']:
                print(f"\n  差异详情:")
                for diff in result['differences']:
                    print(f"\n    {diff['config_key']}:")
                    for field_diff in diff['diff_fields']:
                        print(f"      {field_diff['field']}:")
                        print(f"        批次{b1}: {field_diff['boot_1']}")
                        print(f"        批次{b2}: {field_diff['boot_2']}")


def cmd_playback(args, csp: ConfigSnapshotPlayback):
    print_section('配置回放')

    boot_seq = args.boot
    if boot_seq is None:
        boots = csp.list_boot_sequences()
        if boots:
            boot_seq = boots[0]['boot_sequence']
        else:
            print('  没有可回放的启动批次')
            return

    conclusion = csp.playback_boot(boot_seq)

    print(f"  回放时间: {conclusion.playback_at}")
    print(f"  启动批次: {boot_seq}")
    print(f"  总配置项: {conclusion.total_items}")
    print(f"  整体状态: {conclusion.overall_status}")
    print(f"\n  摘要: {conclusion.summary_text}")

    print(f"\n  来源分布:")
    for src, count in sorted(conclusion.source_distribution.items()):
        print(f"    {src}: {count} 项")

    print(f"\n  统计:")
    print(f"    回退项: {conclusion.fallback_count}")
    print(f"    冲突项: {conclusion.conflict_count}")
    print(f"    默认值项: {conclusion.missing_count}")

    if conclusion.detailed_findings:
        print(f"\n  详细发现:")
        for finding in conclusion.detailed_findings:
            print(f"    - {finding}")

    if conclusion.changes_from_previous:
        print(f"\n  相比上一启动批次的变化:")
        for change in conclusion.changes_from_previous:
            print(f"    - {change['config_key']}")

    if conclusion.recommendations:
        print(f"\n  建议:")
        for rec in conclusion.recommendations:
            print(f"    - {rec}")


def cmd_export(args, csp: ConfigSnapshotPlayback):
    print_section('导出快照')
    try:
        csp.export_to_file(args.output, args.format, args.key, args.boot)
        print(f'  导出成功: {args.output}')
        if os.path.exists(args.output):
            size = os.path.getsize(args.output)
            print(f'  文件大小: {size} 字节')
    except Exception as e:
        print(f'  导出失败: {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)


def cmd_import(args, csp: ConfigSnapshotPlayback):
    print_section('导入快照')
    try:
        result = csp.import_snapshots(args.input)
        print(f'  导入成功: {result["imported"]} 条快照')
        print(f'  跳过(已存在): {result["skipped"]} 条')
        print(f'  失败: {result["failed"]} 条')
        if result.get('boot_sequences_imported'):
            print(f'  涉及启动批次: {", ".join(str(b) for b in result["boot_sequences_imported"])}')
        if result.get('errors'):
            print(f'\n  错误详情:')
            for err in result['errors'][:5]:
                print(f'    - {err}')
    except Exception as e:
        print(f'  导入失败: {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)


def cmd_logs(args, csp: ConfigSnapshotPlayback):
    print_section(f'诊断日志: {csp.log_file}')
    lines = csp.get_log_tail(args.tail if args.tail else 100)
    if not lines:
        print('  日志文件不存在或为空')
        return

    for line in lines:
        print(f'  {line}')

    print(f'\n  共显示 {len(lines)} 行日志')


def cmd_diagnose(args, csp: ConfigSnapshotPlayback):
    print_section('配置诊断报告')
    result = csp.diagnose(args.key)

    print(f"  诊断时间: {result['diagnose_at']}")
    print(f"  当前启动批次: {result['current_boot_sequence']}")
    print(f"  历史启动批次: {result['boot_sequences']}")
    print(f"  快照总数: {result['total_snapshots']}")

    if result.get('playback_conclusion'):
        pc = result['playback_conclusion']
        print(f"\n  回放结论:")
        print(f"    状态: {pc['overall_status']}")
        print(f"    摘要: {pc['summary_text']}")

    if result.get('latest_snapshot'):
        print(f"\n  最新快照:")
        s = result['latest_snapshot']
        print(f"    {s['config_key']} = {s['effective_value']} (来源: {s['effective_source']})")


def cmd_verify(args, csp: ConfigSnapshotPlayback):
    print_section('往返一致性验证')
    result = csp.verify_round_trip(args.boot)

    print(f"  原始快照数: {result['original_count']}")
    print(f"  JSON 导入数: {result['json_imported']}")
    print(f"  CSV 导入数: {result['csv_imported']}")
    print(f"  JSON 往返一致: {result['json_round_trip_ok']}")
    print(f"  CSV 基础字段一致: {result['csv_basic_fields_ok']}")

    if result.get('details'):
        print(f"\n  详细信息:")
        print_dict(result['details'], indent=2)

    if result['json_round_trip_ok'] and result['csv_basic_fields_ok']:
        print(f"\n  [PASS] 往返一致性验证通过")
    else:
        print(f"\n  [FAIL] 往返一致性验证失败")
        sys.exit(1)


def cmd_clear(args, csp: ConfigSnapshotPlayback):
    print_section('清除快照')
    if not args.yes:
        confirm = input('  确定要清除所有快照吗? (yes/no): ')
        if confirm.lower() != 'yes':
            print('  已取消')
            return

    count = csp.clear_snapshots()
    print(f'  已清除 {count} 条快照')


def main():
    parser = argparse.ArgumentParser(
        description='配置快照与回放工具 - 完整的配置追溯、回放与诊断系统',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 解析并记录配置项
  python config_snapshot_playback_cli.py resolve --key my_config --default 100 --type int --min 1

  # 查看当前启动批次配置
  python config_snapshot_playback_cli.py current

  # 查看最新快照
  python config_snapshot_playback_cli.py snapshot --latest

  # 查看所有历史快照
  python config_snapshot_playback_cli.py snapshot --all --limit 20

  # 按启动批次查看
  python config_snapshot_playback_cli.py snapshot --boot 1

  # 列出所有启动批次
  python config_snapshot_playback_cli.py boot --list

  # 比较两个启动批次
  python config_snapshot_playback_cli.py boot --compare 1 2

  # 回放某启动批次并生成结论
  python config_snapshot_playback_cli.py playback --boot 1

  # 导出快照到 JSON
  python config_snapshot_playback_cli.py export --output snapshots.json

  # 导出快照到 CSV
  python config_snapshot_playback_cli.py export --output snapshots.csv --format csv

  # 导入快照
  python config_snapshot_playback_cli.py import --input snapshots.json

  # 验证往返一致性
  python config_snapshot_playback_cli.py verify

  # 查看诊断报告
  python config_snapshot_playback_cli.py diagnose --key reservation_expire_minutes

  # 查看日志
  python config_snapshot_playback_cli.py logs --tail 50
        """
    )

    parser.add_argument('--snapshot-db', default=DEFAULT_SNAPSHOT_DB,
                        help=f'快照数据库路径 (默认: {DEFAULT_SNAPSHOT_DB})')
    parser.add_argument('--log-file', default=DEFAULT_LOG_FILE,
                        help=f'诊断日志文件路径 (默认: {DEFAULT_LOG_FILE})')
    parser.add_argument('--config-file', default=None,
                        help='配置文件路径 (可选)')

    subparsers = parser.add_subparsers(dest='command', required=True)

    p_resolve = subparsers.add_parser('resolve', help='解析并记录配置项')
    p_resolve.add_argument('--key', required=True, help='配置项名称')
    p_resolve.add_argument('--default', help='默认值')
    p_resolve.add_argument('--env-key', help='环境变量名（默认自动转大写）')
    p_resolve.add_argument('--config-key', help='配置文件键名')
    p_resolve.add_argument('--type', choices=['str', 'int', 'float'], default='str',
                           help='值类型')
    p_resolve.add_argument('--min', type=float, help='数值最小值（仅数值类型）')
    p_resolve.add_argument('--max', type=float, help='数值最大值（仅数值类型）')
    p_resolve.set_defaults(func=cmd_resolve)

    p_current = subparsers.add_parser('current', help='查看当前启动批次配置')
    p_current.add_argument('--key', help='配置项名称（不指定则显示所有）')
    p_current.set_defaults(func=cmd_current)

    p_snap = subparsers.add_parser('snapshot', help='查看快照')
    p_snap.add_argument('--key', help='配置项名称')
    snap_group = p_snap.add_mutually_exclusive_group(required=True)
    snap_group.add_argument('--latest', action='store_true', help='查看最新快照')
    snap_group.add_argument('--all', action='store_true', help='查看所有快照')
    snap_group.add_argument('--boot', type=int, help='按启动批次查看')
    snap_group.add_argument('--compare', nargs=2, metavar=('UUID1', 'UUID2'),
                            help='比较两个快照')
    p_snap.add_argument('--limit', type=int, default=100, help='显示条数限制')
    p_snap.set_defaults(func=cmd_snapshot)

    p_boot = subparsers.add_parser('boot', help='启动批次管理')
    boot_group = p_boot.add_mutually_exclusive_group(required=True)
    boot_group.add_argument('--list', action='store_true', help='列出所有启动批次')
    boot_group.add_argument('--compare', nargs=2, type=int, metavar=('BOOT1', 'BOOT2'),
                            help='比较两个启动批次')
    p_boot.set_defaults(func=cmd_boot)

    p_play = subparsers.add_parser('playback', help='回放配置并生成诊断结论')
    p_play.add_argument('--boot', type=int, help='指定启动批次（默认最新）')
    p_play.set_defaults(func=cmd_playback)

    p_export = subparsers.add_parser('export', help='导出快照')
    p_export.add_argument('--output', '-o', required=True, help='输出文件路径')
    p_export.add_argument('--format', '-f', choices=['json', 'csv'],
                          help='输出格式（默认根据扩展名推断）')
    p_export.add_argument('--key', help='指定配置项导出')
    p_export.add_argument('--boot', type=int, help='指定启动批次导出')
    p_export.set_defaults(func=cmd_export)

    p_import = subparsers.add_parser('import', help='导入快照')
    p_import.add_argument('--input', '-i', required=True, help='输入文件路径')
    p_import.set_defaults(func=cmd_import)

    p_logs = subparsers.add_parser('logs', help='查看诊断日志')
    p_logs.add_argument('--tail', type=int, help='显示最后 N 行')
    p_logs.set_defaults(func=cmd_logs)

    p_diag = subparsers.add_parser('diagnose', help='生成诊断报告')
    p_diag.add_argument('--key', help='指定配置项进行诊断')
    p_diag.set_defaults(func=cmd_diagnose)

    p_verify = subparsers.add_parser('verify', help='验证往返一致性')
    p_verify.add_argument('--boot', type=int, help='指定启动批次验证')
    p_verify.set_defaults(func=cmd_verify)

    p_clear = subparsers.add_parser('clear', help='清除所有快照')
    p_clear.add_argument('--yes', action='store_true', help='跳过确认')
    p_clear.set_defaults(func=cmd_clear)

    args = parser.parse_args()

    csp = ConfigSnapshotPlayback(
        snapshot_db=args.snapshot_db,
        log_file=args.log_file,
        config_file=args.config_file,
    )
    csp.start_boot_snapshot()

    args.func(args, csp)


if __name__ == '__main__':
    main()
