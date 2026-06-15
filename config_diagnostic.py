import os
import json
import csv
import io
import logging
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, asdict, field


DEFAULT_CONFIG_FILE = 'config.json'
DEFAULT_SNAPSHOT_DB = 'config_diagnostic.db'
DIAGNOSTIC_LOG_FILE = 'config_diagnostic.log'


@dataclass
class ConfigSource:
    name: str
    priority: int
    raw_value: Optional[str] = None
    parsed_value: Optional[Any] = None
    available: bool = False
    error: Optional[str] = None


@dataclass
class ConfigResolution:
    key: str
    effective_value: Any
    source: str
    fallback: bool
    fallback_reason: Optional[str] = None
    raw_env_value: Optional[str] = None
    raw_config_value: Optional[str] = None
    default_value: Optional[Any] = None
    resolution_explanation: str = ""
    loaded_at: str = field(default_factory=lambda: datetime.now().isoformat())
    sources_evaluated: List[ConfigSource] = field(default_factory=list)
    conflict_detected: bool = False
    conflict_details: Optional[str] = None


@dataclass
class ConfigSnapshot:
    id: Optional[int] = None
    snapshot_uuid: str = ""
    snapshot_at: str = field(default_factory=lambda: datetime.now().isoformat())
    config_key: str = ""
    effective_value: Any = None
    source: str = ""
    fallback: bool = False
    fallback_reason: Optional[str] = None
    raw_env_value: Optional[str] = None
    raw_config_value: Optional[str] = None
    default_value: Any = None
    resolution_explanation: str = ""
    loaded_at: str = ""
    conflict_detected: bool = False
    conflict_details: Optional[str] = None
    boot_sequence: int = 0
    process_id: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'snapshot_uuid': self.snapshot_uuid,
            'snapshot_at': self.snapshot_at,
            'config_key': self.config_key,
            'effective_value': self.effective_value,
            'source': self.source,
            'fallback': self.fallback,
            'fallback_reason': self.fallback_reason,
            'raw_env_value': self.raw_env_value,
            'raw_config_value': self.raw_config_value,
            'default_value': self.default_value,
            'resolution_explanation': self.resolution_explanation,
            'loaded_at': self.loaded_at,
            'conflict_detected': self.conflict_detected,
            'conflict_details': self.conflict_details,
            'boot_sequence': self.boot_sequence,
            'process_id': self.process_id,
        }


class ConfigDiagnostic:
    def __init__(
        self,
        config_file: str = DEFAULT_CONFIG_FILE,
        snapshot_db: str = DEFAULT_SNAPSHOT_DB,
        log_file: str = DIAGNOSTIC_LOG_FILE,
    ):
        self.config_file = config_file
        self.snapshot_db = snapshot_db
        self.log_file = log_file
        self.logger = self._setup_logger()
        self._resolutions: Dict[str, ConfigResolution] = {}
        self._boot_sequence = self._get_next_boot_sequence()
        self._process_id = os.getpid()
        self._init_snapshot_db()

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger('ConfigDiagnostic')
        logger.setLevel(logging.DEBUG)
        logger.handlers.clear()

        fh = logging.FileHandler(self.log_file, encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        fh.setFormatter(formatter)
        logger.addHandler(fh)

        return logger

    def _init_snapshot_db(self) -> None:
        conn = sqlite3.connect(self.snapshot_db)
        c = conn.cursor()
        c.executescript('''
            CREATE TABLE IF NOT EXISTS config_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_uuid TEXT NOT NULL UNIQUE,
                snapshot_at TIMESTAMP NOT NULL,
                config_key TEXT NOT NULL,
                effective_value TEXT,
                source TEXT NOT NULL,
                fallback INTEGER NOT NULL DEFAULT 0,
                fallback_reason TEXT,
                raw_env_value TEXT,
                raw_config_value TEXT,
                default_value TEXT,
                resolution_explanation TEXT NOT NULL,
                loaded_at TIMESTAMP NOT NULL,
                conflict_detected INTEGER NOT NULL DEFAULT 0,
                conflict_details TEXT,
                boot_sequence INTEGER NOT NULL,
                process_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_key ON config_snapshots(config_key);
            CREATE INDEX IF NOT EXISTS idx_snapshots_boot ON config_snapshots(boot_sequence);
            CREATE INDEX IF NOT EXISTS idx_snapshots_at ON config_snapshots(snapshot_at);
        ''')
        conn.commit()
        conn.close()
        self.logger.info('快照数据库初始化完成')

    def _get_next_boot_sequence(self) -> int:
        try:
            conn = sqlite3.connect(self.snapshot_db)
            c = conn.cursor()
            c.execute('SELECT MAX(boot_sequence) FROM config_snapshots')
            result = c.fetchone()
            conn.close()
            return (result[0] or 0) + 1
        except Exception:
            return 1

    def _read_config_file(self) -> Dict[str, Any]:
        if not os.path.exists(self.config_file):
            return {}
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            self.logger.error(f'读取配置文件失败: {e}')
            return {}

    def resolve_config(
        self,
        key: str,
        default_value: Any,
        env_key: Optional[str] = None,
        config_key: Optional[str] = None,
        validator=None,
        value_parser=None,
    ) -> ConfigResolution:
        if env_key is None:
            env_key = key.upper()
        if config_key is None:
            config_key = key

        sources_evaluated: List[ConfigSource] = []
        config_data = self._read_config_file()

        raw_env = os.environ.get(env_key)
        raw_cfg = config_data.get(config_key)

        if raw_env is not None and raw_env.strip() == '':
            raw_env = None
        if raw_cfg is not None and str(raw_cfg).strip() == '':
            raw_cfg = None

        source_list = [
            ('config_file', 1, raw_cfg),
            ('env', 2, raw_env),
            ('default', 3, default_value),
        ]

        conflict_detected = False
        conflict_details = None
        valid_values = []
        parse_errors = []

        for source_name, priority, raw_val in source_list:
            source = ConfigSource(
                name=source_name,
                priority=priority,
                raw_value=raw_val,
                available=raw_val is not None,
            )

            if raw_val is not None and source_name != 'default':
                if value_parser:
                    try:
                        parsed = value_parser(raw_val)
                        source.parsed_value = parsed
                        if validator and not validator(parsed):
                            source.error = f'验证失败: 值 {parsed} 不满足约束'
                            source.available = False
                            parse_errors.append(f'{source_name}="{raw_val}" 验证失败')
                        else:
                            valid_values.append((priority, source_name, parsed, raw_val))
                    except Exception as e:
                        source.error = f'解析失败: {e}'
                        source.available = False
                        parse_errors.append(f'{source_name}="{raw_val}" 解析失败')
                else:
                    source.parsed_value = raw_val
                    if validator and not validator(raw_val):
                        source.error = f'验证失败'
                        source.available = False
                        parse_errors.append(f'{source_name}="{raw_val}" 验证失败')
                    else:
                        valid_values.append((priority, source_name, raw_val, raw_val))

            sources_evaluated.append(source)

        if len(valid_values) >= 2:
            conflict_detected = True
            v1 = valid_values[0]
            v2 = valid_values[1]
            conflict_details = (
                f'多来源冲突: {v1[1]}="{v1[3]}" (优先级{v1[0]}) vs '
                f'{v2[1]}="{v2[3]}" (优先级{v2[0]}), '
                f'最终采用 {v1[1]} (更高优先级)'
            )
            self.logger.warning(f'[{key}] {conflict_details}')

        fallback = False
        fallback_reason = None
        effective_value = default_value
        source = 'default'
        resolution_explanation = ''

        has_config_source = raw_env is not None or raw_cfg is not None

        if valid_values:
            selected = valid_values[0]
            effective_value = selected[2]
            source = selected[1]

            if source == 'env':
                resolution_explanation = (
                    f'环境变量 {env_key}="{raw_env}" 显式配置，生效值 = {effective_value}'
                )
            elif source == 'config_file':
                resolution_explanation = (
                    f'配置文件 {config_key}="{raw_cfg}" 显式配置，生效值 = {effective_value}'
                )

            if conflict_detected:
                resolution_explanation += f'。{conflict_details}'
        elif not has_config_source:
            source = 'default'
            resolution_explanation = (
                f'环境变量 {env_key} 和配置文件 {config_key} 均未设置，采用内置默认值 {default_value}'
            )
        else:
            fallback = True
            reasons = []
            if raw_env is not None:
                reasons.append(f'环境变量 {env_key}="{raw_env}" 非法')
            if raw_cfg is not None:
                reasons.append(f'配置文件 {config_key}="{raw_cfg}" 非法')
            if parse_errors:
                reasons.extend(parse_errors)

            fallback_reason = '; '.join(reasons)
            source = 'default(fallback)'
            resolution_explanation = (
                f'{fallback_reason}，自动回退到内置默认值 {default_value}'
            )

        self.logger.info(
            f'[{key}] 配置解析完成: effective={effective_value}, '
            f'source={source}, fallback={fallback}'
        )
        if fallback_reason:
            self.logger.info(f'[{key}] 回退原因: {fallback_reason}')
        self.logger.info(f'[{key}] 排障结论: {resolution_explanation}')

        resolution = ConfigResolution(
            key=key,
            effective_value=effective_value,
            source=source,
            fallback=fallback,
            fallback_reason=fallback_reason,
            raw_env_value=raw_env,
            raw_config_value=raw_cfg,
            default_value=default_value,
            resolution_explanation=resolution_explanation,
            sources_evaluated=sources_evaluated,
            conflict_detected=conflict_detected,
            conflict_details=conflict_details,
        )

        self._resolutions[key] = resolution
        self._save_snapshot(resolution)

        return resolution

    def _save_snapshot(self, resolution: ConfigResolution) -> ConfigSnapshot:
        import uuid
        snapshot_uuid = str(uuid.uuid4())

        snapshot = ConfigSnapshot(
            snapshot_uuid=snapshot_uuid,
            config_key=resolution.key,
            effective_value=resolution.effective_value,
            source=resolution.source,
            fallback=resolution.fallback,
            fallback_reason=resolution.fallback_reason,
            raw_env_value=resolution.raw_env_value,
            raw_config_value=resolution.raw_config_value,
            default_value=resolution.default_value,
            resolution_explanation=resolution.resolution_explanation,
            loaded_at=resolution.loaded_at,
            conflict_detected=resolution.conflict_detected,
            conflict_details=resolution.conflict_details,
            boot_sequence=self._boot_sequence,
            process_id=self._process_id,
        )

        try:
            conn = sqlite3.connect(self.snapshot_db)
            c = conn.cursor()
            c.execute('''
                INSERT INTO config_snapshots (
                    snapshot_uuid, snapshot_at, config_key, effective_value,
                    source, fallback, fallback_reason, raw_env_value,
                    raw_config_value, default_value, resolution_explanation,
                    loaded_at, conflict_detected, conflict_details,
                    boot_sequence, process_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                snapshot.snapshot_uuid,
                snapshot.snapshot_at,
                snapshot.config_key,
                str(snapshot.effective_value) if snapshot.effective_value is not None else None,
                snapshot.source,
                int(snapshot.fallback),
                snapshot.fallback_reason,
                snapshot.raw_env_value,
                snapshot.raw_config_value,
                str(snapshot.default_value) if snapshot.default_value is not None else None,
                snapshot.resolution_explanation,
                snapshot.loaded_at,
                int(snapshot.conflict_detected),
                snapshot.conflict_details,
                snapshot.boot_sequence,
                snapshot.process_id,
            ))
            snapshot.id = c.lastrowid
            conn.commit()
            conn.close()
            self.logger.info(
                f'[{resolution.key}] 快照已保存: id={snapshot.id}, '
                f'uuid={snapshot_uuid[:8]}...'
            )
        except Exception as e:
            self.logger.error(f'保存快照失败: {e}')

        return snapshot

    def get_current_config(self, key: Optional[str] = None) -> Union[ConfigResolution, Dict[str, ConfigResolution]]:
        if key:
            return self._resolutions.get(key)
        return self._resolutions.copy()

    def get_current_config_dict(self, key: Optional[str] = None) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
        def resolution_to_dict(r: ConfigResolution) -> Dict[str, Any]:
            return {
                'key': r.key,
                'effective_value': r.effective_value,
                'default_value': r.default_value,
                'source': r.source,
                'fallback': r.fallback,
                'fallback_reason': r.fallback_reason,
                'raw_env_value': r.raw_env_value,
                'raw_config_value': r.raw_config_value,
                'resolution_explanation': r.resolution_explanation,
                'loaded_at': r.loaded_at,
                'conflict_detected': r.conflict_detected,
                'conflict_details': r.conflict_details,
                'boot_sequence': self._boot_sequence,
                'process_id': self._process_id,
            }

        if key:
            r = self._resolutions.get(key)
            return resolution_to_dict(r) if r else None
        return [resolution_to_dict(r) for r in self._resolutions.values()]

    def get_latest_snapshot(self, key: Optional[str] = None) -> Optional[ConfigSnapshot]:
        try:
            conn = sqlite3.connect(self.snapshot_db)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            if key:
                c.execute(
                    'SELECT * FROM config_snapshots WHERE config_key = ? ORDER BY id DESC LIMIT 1',
                    (key,)
                )
            else:
                c.execute('SELECT * FROM config_snapshots ORDER BY id DESC LIMIT 1')
            row = c.fetchone()
            conn.close()
            if row:
                return self._row_to_snapshot(row)
            return None
        except Exception as e:
            self.logger.error(f'获取最新快照失败: {e}')
            return None

    def get_all_snapshots(self, key: Optional[str] = None, limit: int = 100) -> List[ConfigSnapshot]:
        try:
            conn = sqlite3.connect(self.snapshot_db)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            if key:
                c.execute(
                    'SELECT * FROM config_snapshots WHERE config_key = ? ORDER BY id DESC LIMIT ?',
                    (key, limit)
                )
            else:
                c.execute('SELECT * FROM config_snapshots ORDER BY id DESC LIMIT ?', (limit,))
            rows = c.fetchall()
            conn.close()
            return [self._row_to_snapshot(r) for r in rows]
        except Exception as e:
            self.logger.error(f'获取所有快照失败: {e}')
            return []

    def get_snapshots_by_boot(self, boot_sequence: int) -> List[ConfigSnapshot]:
        try:
            conn = sqlite3.connect(self.snapshot_db)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute(
                'SELECT * FROM config_snapshots WHERE boot_sequence = ? ORDER BY id',
                (boot_sequence,)
            )
            rows = c.fetchall()
            conn.close()
            return [self._row_to_snapshot(r) for r in rows]
        except Exception as e:
            self.logger.error(f'按启动批次获取快照失败: {e}')
            return []

    def _row_to_snapshot(self, row: sqlite3.Row) -> ConfigSnapshot:
        def try_parse(val: str) -> Any:
            if val is None:
                return None
            try:
                return int(val)
            except (ValueError, TypeError):
                try:
                    return float(val)
                except (ValueError, TypeError):
                    return val

        return ConfigSnapshot(
            id=row['id'],
            snapshot_uuid=row['snapshot_uuid'],
            snapshot_at=row['snapshot_at'],
            config_key=row['config_key'],
            effective_value=try_parse(row['effective_value']),
            source=row['source'],
            fallback=bool(row['fallback']),
            fallback_reason=row['fallback_reason'],
            raw_env_value=row['raw_env_value'],
            raw_config_value=row['raw_config_value'],
            default_value=try_parse(row['default_value']),
            resolution_explanation=row['resolution_explanation'],
            loaded_at=row['loaded_at'],
            conflict_detected=bool(row['conflict_detected']),
            conflict_details=row['conflict_details'],
            boot_sequence=row['boot_sequence'],
            process_id=row['process_id'],
        )

    def diagnose(self, key: Optional[str] = None) -> Dict[str, Any]:
        current = self.get_current_config_dict(key)
        latest_snapshot = self.get_latest_snapshot(key)
        all_snapshots = self.get_all_snapshots(key)

        boot_sequences = sorted(set(s.boot_sequence for s in all_snapshots))
        cross_boot_consistency = None

        if len(boot_sequences) >= 2 and key:
            key_snapshots = [s for s in all_snapshots if s.config_key == key]
            if len(key_snapshots) >= 2:
                first = key_snapshots[-1]
                last = key_snapshots[0]
                cross_boot_consistency = {
                    'first_boot': first.boot_sequence,
                    'last_boot': last.boot_sequence,
                    'effective_value_consistent': first.effective_value == last.effective_value,
                    'source_consistent': first.source == last.source,
                    'first_snapshot': first.to_dict(),
                    'last_snapshot': last.to_dict(),
                }

        return {
            'diagnose_at': datetime.now().isoformat(),
            'current_config': current,
            'latest_snapshot': latest_snapshot.to_dict() if latest_snapshot else None,
            'all_snapshots': [s.to_dict() for s in all_snapshots],
            'boot_sequences': boot_sequences,
            'current_boot_sequence': self._boot_sequence,
            'cross_boot_consistency': cross_boot_consistency,
            'total_snapshots': len(all_snapshots),
        }

    def export_snapshots(self, fmt: str = 'json', key: Optional[str] = None) -> str:
        snapshots = self.get_all_snapshots(key)
        data = [s.to_dict() for s in snapshots]

        if fmt == 'json':
            return json.dumps({
                'exported_at': datetime.now().isoformat(),
                'export_source': 'ConfigDiagnostic',
                'total_snapshots': len(data),
                'snapshots': data,
            }, ensure_ascii=False, indent=2)
        elif fmt == 'csv':
            output = io.StringIO()
            if data:
                fieldnames = list(data[0].keys())
                writer = csv.DictWriter(output, fieldnames=fieldnames)
                writer.writeheader()
                for row in data:
                    writer.writerow({k: v if v is not None else '' for k, v in row.items()})
            return output.getvalue()
        else:
            raise ValueError(f'不支持的导出格式: {fmt}')

    def export_to_file(self, filepath: str, fmt: Optional[str] = None, key: Optional[str] = None) -> None:
        if fmt is None:
            if filepath.endswith('.json'):
                fmt = 'json'
            elif filepath.endswith('.csv'):
                fmt = 'csv'
            else:
                fmt = 'json'

        content = self.export_snapshots(fmt=fmt, key=key)
        with open(filepath, 'w', encoding='utf-8-sig') as f:
            f.write(content)
        self.logger.info(f'快照已导出到文件: {filepath} (格式: {fmt})')

    def import_snapshots(self, filepath: str) -> int:
        if not os.path.exists(filepath):
            raise FileNotFoundError(f'文件不存在: {filepath}')

        imported = 0
        try:
            with open(filepath, 'r', encoding='utf-8-sig') as f:
                if filepath.endswith('.json'):
                    data = json.load(f)
                    snapshots = data.get('snapshots', data if isinstance(data, list) else [])
                elif filepath.endswith('.csv'):
                    reader = csv.DictReader(f)
                    snapshots = list(reader)
                else:
                    try:
                        data = json.load(f)
                        snapshots = data.get('snapshots', data if isinstance(data, list) else [])
                    except Exception:
                        f.seek(0)
                        reader = csv.DictReader(f)
                        snapshots = list(reader)

            conn = sqlite3.connect(self.snapshot_db)
            c = conn.cursor()

            for snap_data in snapshots:
                try:
                    c.execute('''
                        INSERT OR IGNORE INTO config_snapshots (
                            snapshot_uuid, snapshot_at, config_key, effective_value,
                            source, fallback, fallback_reason, raw_env_value,
                            raw_config_value, default_value, resolution_explanation,
                            loaded_at, conflict_detected, conflict_details,
                            boot_sequence, process_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        snap_data.get('snapshot_uuid', ''),
                        snap_data.get('snapshot_at', datetime.now().isoformat()),
                        snap_data.get('config_key', ''),
                        snap_data.get('effective_value'),
                        snap_data.get('source', 'imported'),
                        int(snap_data.get('fallback', 0) if isinstance(snap_data.get('fallback'), (int, bool)) else 0),
                        snap_data.get('fallback_reason'),
                        snap_data.get('raw_env_value'),
                        snap_data.get('raw_config_value'),
                        snap_data.get('default_value'),
                        snap_data.get('resolution_explanation', ''),
                        snap_data.get('loaded_at', datetime.now().isoformat()),
                        int(snap_data.get('conflict_detected', 0) if isinstance(snap_data.get('conflict_detected'), (int, bool)) else 0),
                        snap_data.get('conflict_details'),
                        int(snap_data.get('boot_sequence', 0)),
                        int(snap_data.get('process_id', 0)),
                    ))
                    if c.rowcount > 0:
                        imported += 1
                except Exception as e:
                    self.logger.error(f'导入单条快照失败: {e}, 数据={snap_data}')

            conn.commit()
            conn.close()
            self.logger.info(f'快照导入完成: 成功 {imported} 条')
            return imported

        except Exception as e:
            self.logger.error(f'导入快照文件失败: {e}')
            raise

    def compare_snapshots(self, snapshot_uuid1: str, snapshot_uuid2: str) -> Dict[str, Any]:
        conn = sqlite3.connect(self.snapshot_db)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        c.execute('SELECT * FROM config_snapshots WHERE snapshot_uuid = ?', (snapshot_uuid1,))
        row1 = c.fetchone()
        c.execute('SELECT * FROM config_snapshots WHERE snapshot_uuid = ?', (snapshot_uuid2,))
        row2 = c.fetchone()
        conn.close()

        if not row1 or not row2:
            return {'error': '找不到指定的快照'}

        s1 = self._row_to_snapshot(row1)
        s2 = self._row_to_snapshot(row2)

        differences = []
        s1_dict = s1.to_dict()
        s2_dict = s2.to_dict()

        for key in ['effective_value', 'source', 'fallback', 'fallback_reason',
                    'raw_env_value', 'raw_config_value', 'resolution_explanation',
                    'conflict_detected', 'conflict_details', 'boot_sequence']:
            v1 = s1_dict.get(key)
            v2 = s2_dict.get(key)
            if v1 != v2:
                differences.append({
                    'field': key,
                    'snapshot_1': v1,
                    'snapshot_2': v2,
                })

        return {
            'snapshot_1': s1_dict,
            'snapshot_2': s2_dict,
            'differences': differences,
            'identical': len(differences) == 0,
        }

    def clear_snapshots(self) -> int:
        try:
            conn = sqlite3.connect(self.snapshot_db)
            c = conn.cursor()
            c.execute('DELETE FROM config_snapshots')
            count = c.rowcount
            conn.commit()
            conn.close()
            self.logger.info(f'已清除 {count} 条快照记录')
            return count
        except Exception as e:
            self.logger.error(f'清除快照失败: {e}')
            return 0
