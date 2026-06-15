#!/usr/bin/env python3
import argparse
import sys
import os
import json
from datetime import datetime

from config_diagnostic import (
    ConfigDiagnostic,
    DEFAULT_CONFIG_FILE,
    DEFAULT_SNAPSHOT_DB,
    DIAGNOSTIC_LOG_FILE,
)


def print_section(title: str):
    line = '=' * 70
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
                for i, item in enumerate(v[:3]):
                    print(f'{prefix}  [{i}]:')
                    print_dict(item, indent + 2)
                if len(v) > 3:
                    print(f'{prefix}  ... (还有 {len(v) - 3} 条)')
            else:
                print(f'{prefix}{k}: {v}')
        else:
            print(f'{prefix}{k}: {v}')


def cmd_current(args, diag: ConfigDiagnostic):
    print_section('当前生效配置')
    config = diag.get_current_config_dict(args.key)
    if config is None:
        print(f'  未找到配置项: {args.key}' if args.key else '  暂无配置')
        return

    if isinstance(config, list):
        for cfg in config:
            print(f"\n  配置项: {cfg['key']}")
            print(f"    生效值: {cfg['effective_value']}")
            print(f"    默认值: {cfg['default_value']}")
            print(f"    来源: {cfg['source']}")
            print(f"    是否回退: {cfg['fallback']}")
            if cfg['fallback_reason']:
                print(f"    回退原因: {cfg['fallback_reason']}")
            if cfg['conflict_detected']:
                print(f"    冲突检测: 是")
                print(f"    冲突详情: {cfg['conflict_details']}")
            print(f"    排障结论: {cfg['resolution_explanation']}")
            print(f"    加载时间: {cfg['loaded_at']}")
            print(f"    启动批次: {cfg['boot_sequence']}")
            print(f"    进程ID: {cfg['process_id']}")
    else:
        print_dict(config, indent=1)


def cmd_snapshot(args, diag: ConfigDiagnostic):
    print_section('配置快照')
    if args.latest:
        snap = diag.get_latest_snapshot(args.key)
        if snap:
            print_dict(snap.to_dict(), indent=1)
        else:
            print('  暂无快照')
    elif args.all:
        snaps = diag.get_all_snapshots(args.key, args.limit)
        print(f'  共 {len(snaps)} 条快照:')
        for snap in snaps:
            d = snap.to_dict()
            print(f"\n  [{d['id']}] {d['config_key']} = {d['effective_value']}")
            print(f"      来源: {d['source']}, 回退: {d['fallback']}")
            print(f"      快照时间: {d['snapshot_at']}")
            print(f"      启动批次: {d['boot_sequence']}")
    elif args.boot is not None:
        snaps = diag.get_snapshots_by_boot(args.boot)
        print(f'  启动批次 {args.boot} 共 {len(snaps)} 条快照:')
        for snap in snaps:
            d = snap.to_dict()
            print(f"    [{d['id']}] {d['config_key']} = {d['effective_value']} (来源: {d['source']})")
    elif args.compare:
        uuid1, uuid2 = args.compare
        result = diag.compare_snapshots(uuid1, uuid2)
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


def cmd_diagnose(args, diag: ConfigDiagnostic):
    print_section('配置诊断报告')
    result = diag.diagnose(args.key)
    print(f"  诊断时间: {result['diagnose_at']}")
    print(f"  当前启动批次: {result['current_boot_sequence']}")
    print(f"  历史启动批次: {result['boot_sequences']}")
    print(f"  快照总数: {result['total_snapshots']}")

    if result.get('cross_boot_consistency'):
        cb = result['cross_boot_consistency']
        print(f"\n  跨重启一致性:")
        print(f"    首次启动: {cb['first_boot']}, 末次启动: {cb['last_boot']}")
        print(f"    生效值一致: {cb['effective_value_consistent']}")
        print(f"    来源一致: {cb['source_consistent']}")

    if result.get('latest_snapshot'):
        print(f"\n  最新快照:")
        s = result['latest_snapshot']
        print(f"    {s['config_key']} = {s['effective_value']} (来源: {s['source']})")

    if result.get('current_config'):
        print(f"\n  当前配置:")
        cfg = result['current_config']
        if isinstance(cfg, list):
            for c in cfg:
                print(f"    {c['key']} = {c['effective_value']}")
        else:
            print(f"    {cfg['key']} = {cfg['effective_value']}")


def cmd_export(args, diag: ConfigDiagnostic):
    print_section('导出快照')
    try:
        diag.export_to_file(args.output, args.format, args.key)
        print(f'  导出成功: {args.output}')
        if os.path.exists(args.output):
            size = os.path.getsize(args.output)
            print(f'  文件大小: {size} 字节')
    except Exception as e:
        print(f'  导出失败: {e}')
        sys.exit(1)


def cmd_import(args, diag: ConfigDiagnostic):
    print_section('导入快照')
    try:
        count = diag.import_snapshots(args.input)
        print(f'  导入成功: {count} 条快照')
    except Exception as e:
        print(f'  导入失败: {e}')
        sys.exit(1)


def cmd_logs(args, diag: ConfigDiagnostic):
    print_section(f'诊断日志: {diag.log_file}')
    if not os.path.exists(diag.log_file):
        print('  日志文件不存在')
        return

    with open(diag.log_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    if args.tail:
        lines = lines[-args.tail:]

    for line in lines:
        print(f'  {line.rstrip()}')

    print(f'\n  共显示 {len(lines)} 行日志')


def cmd_resolve(args, diag: ConfigDiagnostic):
    print_section('解析配置')

    def int_validator(v):
        return v > 0

    value_parser = None
    validator = None

    if args.type == 'int':
        value_parser = int
        if args.min is not None or args.max is not None:
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

        resolution = diag.resolve_config(
            key=args.key,
            default_value=default,
            env_key=args.env_key,
            config_key=args.config_key,
            validator=validator,
            value_parser=value_parser,
        )

        print(f"  配置项: {resolution.key}")
        print(f"  生效值: {resolution.effective_value}")
        print(f"  来源: {resolution.source}")
        print(f"  是否回退: {resolution.fallback}")
        if resolution.fallback_reason:
            print(f"  回退原因: {resolution.fallback_reason}")
        print(f"  排障结论: {resolution.resolution_explanation}")

        if resolution.conflict_detected:
            print(f"\n  [WARNING] 检测到多来源冲突:")
            print(f"            {resolution.conflict_details}")

        print(f"\n  已评估的来源:")
        for src in resolution.sources_evaluated:
            status = "[OK] 有效" if src.available else "[X] 无效"
            if src.error:
                status += f" ({src.error})"
            print(f"    [{src.priority}] {src.name}: {src.raw_value!r} - {status}")

    except Exception as e:
        print(f'  解析失败: {e}')
        sys.exit(1)


def cmd_clear(args, diag: ConfigDiagnostic):
    print_section('清除快照')
    if not args.yes:
        confirm = input('  确定要清除所有快照吗? (yes/no): ')
        if confirm.lower() != 'yes':
            print('  已取消')
            return

    count = diag.clear_snapshots()
    print(f'  已清除 {count} 条快照')


def main():
    parser = argparse.ArgumentParser(
        description='配置诊断与快照工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 查看当前生效配置
  python config_diagnostic_cli.py current

  # 查看特定配置项
  python config_diagnostic_cli.py current --key reservation_expire_minutes

  # 解析新配置项
  python config_diagnostic_cli.py resolve --key my_config --default 100 --type int --min 1

  # 查看最新快照
  python config_diagnostic_cli.py snapshot --latest

  # 查看所有快照
  python config_diagnostic_cli.py snapshot --all --limit 10

  # 跨重启一致性诊断
  python config_diagnostic_cli.py diagnose --key reservation_expire_minutes

  # 导出快照
  python config_diagnostic_cli.py export --output snapshots.json
  python config_diagnostic_cli.py export --output snapshots.csv --format csv

  # 导入快照
  python config_diagnostic_cli.py import --input snapshots.json

  # 查看日志
  python config_diagnostic_cli.py logs --tail 20
        """
    )

    parser.add_argument('--config-file', default=DEFAULT_CONFIG_FILE,
                        help=f'配置文件路径 (默认: {DEFAULT_CONFIG_FILE})')
    parser.add_argument('--snapshot-db', default=DEFAULT_SNAPSHOT_DB,
                        help=f'快照数据库路径 (默认: {DEFAULT_SNAPSHOT_DB})')
    parser.add_argument('--log-file', default=DIAGNOSTIC_LOG_FILE,
                        help=f'诊断日志文件路径 (默认: {DIAGNOSTIC_LOG_FILE})')

    subparsers = parser.add_subparsers(dest='command', required=True)

    p_current = subparsers.add_parser('current', help='查看当前生效配置')
    p_current.add_argument('--key', help='配置项名称（不指定则显示所有）')
    p_current.set_defaults(func=cmd_current)

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

    p_diag = subparsers.add_parser('diagnose', help='生成诊断报告')
    p_diag.add_argument('--key', help='指定配置项进行诊断')
    p_diag.set_defaults(func=cmd_diagnose)

    p_export = subparsers.add_parser('export', help='导出快照')
    p_export.add_argument('--output', '-o', required=True, help='输出文件路径')
    p_export.add_argument('--format', '-f', choices=['json', 'csv'],
                          help='输出格式（默认根据扩展名推断）')
    p_export.add_argument('--key', help='指定配置项导出')
    p_export.set_defaults(func=cmd_export)

    p_import = subparsers.add_parser('import', help='导入快照')
    p_import.add_argument('--input', '-i', required=True, help='输入文件路径')
    p_import.set_defaults(func=cmd_import)

    p_logs = subparsers.add_parser('logs', help='查看诊断日志')
    p_logs.add_argument('--tail', type=int, help='显示最后 N 行')
    p_logs.set_defaults(func=cmd_logs)

    p_clear = subparsers.add_parser('clear', help='清除所有快照')
    p_clear.add_argument('--yes', action='store_true', help='跳过确认')
    p_clear.set_defaults(func=cmd_clear)

    args = parser.parse_args()

    diag = ConfigDiagnostic(
        config_file=args.config_file,
        snapshot_db=args.snapshot_db,
        log_file=args.log_file,
    )

    args.func(args, diag)


if __name__ == '__main__':
    main()
