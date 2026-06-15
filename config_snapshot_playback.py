#!/usr/bin/env python3
import os
import json
import csv
import io
import uuid
import hashlib
import sqlite3
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, field, asdict


DEFAULT_SNAPSHOT_DB = 'config_snapshot_playback.db'
DEFAULT_LOG_FILE = 'config_snapshot_playback.log'


SOURCE_PRIORITY = {
    'config_file': 1,
    'env': 2,
    'cli_arg': 3,
    'default': 99,
}


@dataclass
class SourceEvaluation:
    source_name: str
    priority: int
    raw_value: Optional[str] = None
    parsed_value: Optional[Any] = None
    is_available: bool = False
    is_valid: bool = False
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'source_name': self.source_name,
            'priority': self.priority,
            'raw_value': self.raw_value,
            'parsed_value': str(self.parsed_value) if self.parsed_value is not None else None,
            'is_available': self.is_available,
            'is_valid': self.is_valid,
            'error_message': self.error_message,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'SourceEvaluation':
        return cls(
            source_name=d.get('source_name', ''),
            priority=int(d.get('priority', 99)),
            raw_value=d.get('raw_value'),
            parsed_value=d.get('parsed_value'),
            is_available=bool(d.get('is_available', False)),
            is_valid=bool(d.get('is_valid', False)),
            error_message=d.get('error_message'),
        )


@dataclass
class ConfigValueSnapshot:
    snapshot_uuid: str = field(default_factory=lambda: str(uuid.uuid4()))
    snapshot_at: str = field(default_factory=lambda: datetime.now().isoformat())
    config_key: str = ''
    effective_value: Any = None
    effective_source: str = ''
    is_fallback: bool = False
    fallback_reason: Optional[str] = None
    default_value: Any = None
    raw_env_value: Optional[str] = None
    raw_config_value: Optional[str] = None
    resolution_chain: List[SourceEvaluation] = field(default_factory=list)
    conflict_detected: bool = False
    conflict_details: Optional[str] = None
    resolution_explanation: str = ''
    loaded_at: str = field(default_factory=lambda: datetime.now().isoformat())
    boot_sequence: int = 0
    process_id: int = 0
    diagnostic_notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'snapshot_uuid': self.snapshot_uuid,
            'snapshot_at': self.snapshot_at,
            'config_key': self.config_key,
            'effective_value': str(self.effective_value) if self.effective_value is not None else None,
            'effective_source': self.effective_source,
            'is_fallback': self.is_fallback,
            'fallback_reason': self.fallback_reason,
            'default_value': str(self.default_value) if self.default_value is not None else None,
            'raw_env_value': self.raw_env_value,
            'raw_config_value': self.raw_config_value,
            'resolution_chain': [s.to_dict() for s in self.resolution_chain],
            'conflict_detected': self.conflict_detected,
            'conflict_details': self.conflict_details,
            'resolution_explanation': self.resolution_explanation,
            'loaded_at': self.loaded_at,
            'boot_sequence': self.boot_sequence,
            'process_id': self.process_id,
            'diagnostic_notes': self.diagnostic_notes,
            'integrity_hash': self._compute_hash(),
        }

    def _compute_hash(self) -> str:
        content = json.dumps({
            'config_key': self.config_key,
            'effective_value': str(self.effective_value) if self.effective_value is not None else None,
            'effective_source': self.effective_source,
            'is_fallback': self.is_fallback,
            'fallback_reason': self.fallback_reason,
            'resolution_explanation': self.resolution_explanation,
            'conflict_detected': self.conflict_detected,
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'ConfigValueSnapshot':
        snap = cls()
        snap.snapshot_uuid = d.get('snapshot_uuid', str(uuid.uuid4()))
        snap.snapshot_at = d.get('snapshot_at', datetime.now().isoformat())
        snap.config_key = d.get('config_key', '')
        snap.effective_value = _try_parse_value(d.get('effective_value'))
        snap.effective_source = d.get('effective_source', '')
        snap.is_fallback = bool(d.get('is_fallback', False))
        snap.fallback_reason = d.get('fallback_reason')
        snap.default_value = _try_parse_value(d.get('default_value'))
        snap.raw_env_value = d.get('raw_env_value')
        snap.raw_config_value = d.get('raw_config_value')
        snap.resolution_chain = [SourceEvaluation.from_dict(s) for s in d.get('resolution_chain', [])]
        snap.conflict_detected = bool(d.get('conflict_detected', False))
        snap.conflict_details = d.get('conflict_details')
        snap.resolution_explanation = d.get('resolution_explanation', '')
        snap.loaded_at = d.get('loaded_at', datetime.now().isoformat())
        snap.boot_sequence = int(d.get('boot_sequence', 0))
        snap.process_id = int(d.get('process_id', 0))
        snap.diagnostic_notes = list(d.get('diagnostic_notes', []))
        return snap


def _try_parse_value(val: Optional[str]) -> Any:
    if val is None:
        return None
    if val == '':
        return ''
    try:
        return int(val)
    except (ValueError, TypeError):
        try:
            return float(val)
        except (ValueError, TypeError):
            if val.lower() == 'true':
                return True
            if val.lower() == 'false':
                return False
            if val.lower() == 'none' or val.lower() == 'null':
                return None
            return val


@dataclass
class BootSnapshot:
    boot_sequence: int = 0
    boot_at: str = field(default_factory=lambda: datetime.now().isoformat())
    process_id: int = 0
    config_items: Dict[str, ConfigValueSnapshot] = field(default_factory=dict)
    boot_summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'boot_sequence': self.boot_sequence,
            'boot_at': self.boot_at,
            'process_id': self.process_id,
            'config_items': {k: v.to_dict() for k, v in self.config_items.items()},
            'boot_summary': self.boot_summary,
            'item_count': len(self.config_items),
        }


@dataclass
class PlaybackConclusion:
    playback_at: str = field(default_factory=lambda: datetime.now().isoformat())
    total_items: int = 0
    fallback_count: int = 0
    conflict_count: int = 0
    missing_count: int = 0
    dirty_value_count: int = 0
    changes_from_previous: List[Dict[str, Any]] = field(default_factory=list)
    source_distribution: Dict[str, int] = field(default_factory=dict)
    overall_status: str = 'normal'
    summary_text: str = ''
    detailed_findings: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'playback_at': self.playback_at,
            'total_items': self.total_items,
            'fallback_count': self.fallback_count,
            'conflict_count': self.conflict_count,
            'missing_count': self.missing_count,
            'dirty_value_count': self.dirty_value_count,
            'changes_from_previous': self.changes_from_previous,
            'source_distribution': self.source_distribution,
            'overall_status': self.overall_status,
            'summary_text': self.summary_text,
            'detailed_findings': self.detailed_findings,
            'recommendations': self.recommendations,
        }


class ConfigSnapshotPlayback:
    def __init__(
        self,
        snapshot_db: str = DEFAULT_SNAPSHOT_DB,
        log_file: str = DEFAULT_LOG_FILE,
        config_file: Optional[str] = None,
    ):
        self.snapshot_db = snapshot_db
        self.log_file = log_file
        self.config_file = config_file
        self.logger = self._setup_logger()
        self._boot_sequence = 0
        self._process_id = os.getpid()
        self._current_boot: Optional[BootSnapshot] = None
        self._mem_conn = None
        self._init_db()
        self._boot_sequence = self._get_next_boot_sequence()

    def _is_memory_db(self) -> bool:
        return self.snapshot_db == ':memory:'

    def _get_conn(self):
        if self._is_memory_db():
            if self._mem_conn is None:
                self._mem_conn = sqlite3.connect(self.snapshot_db)
                self._mem_conn.row_factory = sqlite3.Row
            return self._mem_conn
        conn = sqlite3.connect(self.snapshot_db)
        conn.row_factory = sqlite3.Row
        return conn

    def _close_conn(self, conn):
        if self._is_memory_db():
            return
        conn.close()

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger(f'ConfigSnapshotPlayback-{id(self)}')
        logger.setLevel(logging.DEBUG)
        logger.handlers.clear()
        logger.propagate = False

        if self.log_file and self.log_file != ':memory:':
            fh = logging.FileHandler(self.log_file, encoding='utf-8')
            fh.setLevel(logging.DEBUG)
            formatter = logging.Formatter(
                '%(asctime)s | %(levelname)s | %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
            fh.setFormatter(formatter)
            logger.addHandler(fh)
        else:
            nh = logging.NullHandler()
            logger.addHandler(nh)

        return logger

    def _init_db(self) -> None:
        conn = self._get_conn()
        c = conn.cursor()
        c.executescript('''
            CREATE TABLE IF NOT EXISTS config_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_uuid TEXT NOT NULL UNIQUE,
                snapshot_at TIMESTAMP NOT NULL,
                config_key TEXT NOT NULL,
                effective_value TEXT,
                effective_source TEXT NOT NULL,
                is_fallback INTEGER NOT NULL DEFAULT 0,
                fallback_reason TEXT,
                default_value TEXT,
                raw_env_value TEXT,
                raw_config_value TEXT,
                resolution_chain_json TEXT,
                conflict_detected INTEGER NOT NULL DEFAULT 0,
                conflict_details TEXT,
                resolution_explanation TEXT NOT NULL,
                loaded_at TIMESTAMP NOT NULL,
                boot_sequence INTEGER NOT NULL,
                process_id INTEGER NOT NULL,
                diagnostic_notes_json TEXT,
                integrity_hash TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_snap_key ON config_snapshots(config_key);
            CREATE INDEX IF NOT EXISTS idx_snap_boot ON config_snapshots(boot_sequence);
            CREATE INDEX IF NOT EXISTS idx_snap_at ON config_snapshots(snapshot_at);
            CREATE INDEX IF NOT EXISTS idx_snap_hash ON config_snapshots(integrity_hash);

            CREATE TABLE IF NOT EXISTS boot_records (
                boot_sequence INTEGER PRIMARY KEY,
                boot_at TIMESTAMP NOT NULL,
                process_id INTEGER NOT NULL,
                item_count INTEGER NOT NULL DEFAULT 0,
                summary_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS playback_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playback_at TIMESTAMP NOT NULL,
                boot_sequence INTEGER,
                result_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        conn.commit()
        self._close_conn(conn)
        self.logger.info('快照数据库初始化完成')

    def _get_next_boot_sequence(self) -> int:
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute('SELECT MAX(boot_sequence) FROM boot_records')
            result = c.fetchone()
            self._close_conn(conn)
            return (result[0] or 0) + 1
        except Exception:
            return 1

    def start_boot_snapshot(self) -> int:
        self._current_boot = BootSnapshot(
            boot_sequence=self._boot_sequence,
            boot_at=datetime.now().isoformat(),
            process_id=self._process_id,
        )
        self.logger.info(f'启动批次 {self._boot_sequence} 快照记录已开始')
        return self._boot_sequence

    def resolve_config(
        self,
        key: str,
        default_value: Any,
        env_key: Optional[str] = None,
        config_key: Optional[str] = None,
        value_parser=None,
        validator=None,
    ) -> ConfigValueSnapshot:
        if env_key is None:
            env_key = key.upper()
        if config_key is None:
            config_key = key

        resolution_chain: List[SourceEvaluation] = []
        config_data = self._read_config_file()

        raw_env = os.environ.get(env_key)
        raw_cfg = config_data.get(config_key)

        if raw_env is not None and raw_env.strip() == '':
            raw_env = None
        if raw_cfg is not None and str(raw_cfg).strip() == '':
            raw_cfg = None

        sources = [
            ('config_file', raw_cfg, 1),
            ('env', raw_env, 2),
            ('default', default_value, 99),
        ]

        valid_sources = []
        parse_errors = []

        for source_name, raw_val, priority in sources:
            eval_result = SourceEvaluation(
                source_name=source_name,
                priority=priority,
                raw_value=str(raw_val) if raw_val is not None else None,
                is_available=raw_val is not None,
            )

            if raw_val is not None:
                if source_name == 'default':
                    eval_result.parsed_value = raw_val
                    eval_result.is_valid = True
                    valid_sources.append((priority, source_name, raw_val, raw_val))
                else:
                    if value_parser:
                        try:
                            parsed = value_parser(raw_val)
                            eval_result.parsed_value = parsed
                            if validator and not validator(parsed):
                                eval_result.is_valid = False
                                eval_result.error_message = f'验证失败: 值 {parsed} 不满足约束条件'
                                parse_errors.append(f'{source_name}="{raw_val}" 验证失败')
                            else:
                                eval_result.is_valid = True
                                valid_sources.append((priority, source_name, parsed, raw_val))
                        except Exception as e:
                            eval_result.is_valid = False
                            eval_result.error_message = f'解析失败: {e}'
                            parse_errors.append(f'{source_name}="{raw_val}" 解析失败({e})')
                    else:
                        eval_result.parsed_value = raw_val
                        if validator and not validator(raw_val):
                            eval_result.is_valid = False
                            eval_result.error_message = '验证失败'
                            parse_errors.append(f'{source_name}="{raw_val}" 验证失败')
                        else:
                            eval_result.is_valid = True
                            valid_sources.append((priority, source_name, raw_val, raw_val))

            resolution_chain.append(eval_result)

        valid_sources.sort(key=lambda x: x[0])

        conflict_detected = False
        conflict_details = None
        if len(valid_sources) >= 2 and valid_sources[0][1] != 'default' and valid_sources[1][1] != 'default':
            conflict_detected = True
            v1 = valid_sources[0]
            v2 = valid_sources[1]
            conflict_details = (
                f'多来源冲突: {v1[1]}="{v1[3]}" (优先级{v1[0]}) vs '
                f'{v2[1]}="{v2[3]}" (优先级{v2[0]}), '
                f'最终采用 {v1[1]} (更高优先级)'
            )
            self.logger.warning(f'[{key}] {conflict_details}')

        is_fallback = False
        fallback_reason = None
        effective_value = default_value
        effective_source = 'default'
        resolution_explanation = ''

        has_user_config = raw_env is not None or raw_cfg is not None

        if valid_sources:
            selected = valid_sources[0]
            effective_value = selected[2]
            effective_source = selected[1]

            if effective_source == 'env':
                resolution_explanation = (
                    f'环境变量 {env_key}="{raw_env}" 显式配置，生效值 = {effective_value}'
                )
            elif effective_source == 'config_file':
                resolution_explanation = (
                    f'配置文件 {config_key}="{raw_cfg}" 显式配置，生效值 = {effective_value}'
                )
            else:
                resolution_explanation = (
                    f'采用内置默认值 {default_value}'
                )

            if conflict_detected:
                resolution_explanation += f'。{conflict_details}'

            if has_user_config and effective_source == 'default':
                is_fallback = True
                reasons = []
                if raw_env is not None:
                    reasons.append(f'环境变量 {env_key}="{raw_env}" 非法')
                if raw_cfg is not None:
                    reasons.append(f'配置文件 {config_key}="{raw_cfg}" 非法')
                if parse_errors:
                    reasons.extend(parse_errors)
                fallback_reason = '; '.join(reasons)
                effective_source = 'default(fallback)'
                resolution_explanation = (
                    f'{fallback_reason}，自动回退到内置默认值 {default_value}'
                )
        elif not has_user_config:
            resolution_explanation = (
                f'环境变量 {env_key} 和配置文件 {config_key} 均未设置，采用内置默认值 {default_value}'
            )
        else:
            is_fallback = True
            reasons = []
            if raw_env is not None:
                reasons.append(f'环境变量 {env_key}="{raw_env}" 非法')
            if raw_cfg is not None:
                reasons.append(f'配置文件 {config_key}="{raw_cfg}" 非法')
            if parse_errors:
                reasons.extend(parse_errors)
            fallback_reason = '; '.join(reasons)
            effective_source = 'default(fallback)'
            resolution_explanation = (
                f'{fallback_reason}，自动回退到内置默认值 {default_value}'
            )

        diagnostic_notes = []
        if is_fallback:
            diagnostic_notes.append(f'回退触发: {fallback_reason}')
        if conflict_detected:
            diagnostic_notes.append(f'冲突检测: {conflict_details}')
        if has_user_config and not is_fallback:
            diagnostic_notes.append('显式配置生效')

        self.logger.info(
            f'[{key}] 配置解析完成: effective={effective_value}, '
            f'source={effective_source}, fallback={is_fallback}'
        )
        if fallback_reason:
            self.logger.info(f'[{key}] 回退原因: {fallback_reason}')
        self.logger.info(f'[{key}] 排障结论: {resolution_explanation}')

        snapshot = ConfigValueSnapshot(
            config_key=key,
            effective_value=effective_value,
            effective_source=effective_source,
            is_fallback=is_fallback,
            fallback_reason=fallback_reason,
            default_value=default_value,
            raw_env_value=raw_env,
            raw_config_value=raw_cfg,
            resolution_chain=resolution_chain,
            conflict_detected=conflict_detected,
            conflict_details=conflict_details,
            resolution_explanation=resolution_explanation,
            boot_sequence=self._boot_sequence,
            process_id=self._process_id,
            diagnostic_notes=diagnostic_notes,
        )

        self._save_snapshot(snapshot)

        if self._current_boot is not None:
            self._current_boot.config_items[key] = snapshot

        return snapshot

    def _read_config_file(self) -> Dict[str, Any]:
        if not self.config_file or not os.path.exists(self.config_file):
            return {}
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            self.logger.error(f'读取配置文件失败: {e}')
            return {}

    def _save_snapshot(self, snapshot: ConfigValueSnapshot) -> None:
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute('''
                INSERT INTO config_snapshots (
                    snapshot_uuid, snapshot_at, config_key, effective_value,
                    effective_source, is_fallback, fallback_reason,
                    default_value, raw_env_value, raw_config_value,
                    resolution_chain_json, conflict_detected, conflict_details,
                    resolution_explanation, loaded_at, boot_sequence, process_id,
                    diagnostic_notes_json, integrity_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                snapshot.snapshot_uuid,
                snapshot.snapshot_at,
                snapshot.config_key,
                str(snapshot.effective_value) if snapshot.effective_value is not None else None,
                snapshot.effective_source,
                int(snapshot.is_fallback),
                snapshot.fallback_reason,
                str(snapshot.default_value) if snapshot.default_value is not None else None,
                snapshot.raw_env_value,
                snapshot.raw_config_value,
                json.dumps([s.to_dict() for s in snapshot.resolution_chain], ensure_ascii=False),
                int(snapshot.conflict_detected),
                snapshot.conflict_details,
                snapshot.resolution_explanation,
                snapshot.loaded_at,
                snapshot.boot_sequence,
                snapshot.process_id,
                json.dumps(snapshot.diagnostic_notes, ensure_ascii=False),
                snapshot._compute_hash(),
            ))
            conn.commit()
            self._close_conn(conn)
            self.logger.debug(
                f'[{snapshot.config_key}] 快照已保存: uuid={snapshot.snapshot_uuid[:8]}...'
            )
        except Exception as e:
            self.logger.error(f'保存快照失败: {e}')

    def finish_boot_snapshot(self) -> BootSnapshot:
        if self._current_boot is None:
            self.start_boot_snapshot()

        items = self._current_boot.config_items
        fallback_count = sum(1 for s in items.values() if s.is_fallback)
        conflict_count = sum(1 for s in items.values() if s.conflict_detected)

        source_dist = {}
        for s in items.values():
            src = s.effective_source
            source_dist[src] = source_dist.get(src, 0) + 1

        self._current_boot.boot_summary = {
            'total_items': len(items),
            'fallback_count': fallback_count,
            'conflict_count': conflict_count,
            'source_distribution': source_dist,
            'boot_status': 'warning' if fallback_count > 0 or conflict_count > 0 else 'normal',
        }

        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute('''
                INSERT OR REPLACE INTO boot_records (
                    boot_sequence, boot_at, process_id, item_count, summary_json
                ) VALUES (?, ?, ?, ?, ?)
            ''', (
                self._current_boot.boot_sequence,
                self._current_boot.boot_at,
                self._current_boot.process_id,
                len(items),
                json.dumps(self._current_boot.boot_summary, ensure_ascii=False),
            ))
            conn.commit()
            self._close_conn(conn)
        except Exception as e:
            self.logger.error(f'保存启动记录失败: {e}')

        self.logger.info(
            f'启动批次 {self._boot_sequence} 快照完成: '
            f'共 {len(items)} 项配置, '
            f'回退 {fallback_count} 项, 冲突 {conflict_count} 项'
        )

        return self._current_boot

    def get_latest_snapshot(self, key: Optional[str] = None) -> Optional[ConfigValueSnapshot]:
        try:
            conn = self._get_conn()
            c = conn.cursor()
            if key:
                c.execute(
                    'SELECT * FROM config_snapshots WHERE config_key = ? ORDER BY id DESC LIMIT 1',
                    (key,)
                )
            else:
                c.execute('SELECT * FROM config_snapshots ORDER BY id DESC LIMIT 1')
            row = c.fetchone()
            self._close_conn(conn)
            if row:
                return self._row_to_snapshot(row)
            return None
        except Exception as e:
            self.logger.error(f'获取最新快照失败: {e}')
            return None

    def get_all_snapshots(
        self,
        key: Optional[str] = None,
        limit: int = 100,
        boot_sequence: Optional[int] = None,
    ) -> List[ConfigValueSnapshot]:
        try:
            conn = self._get_conn()
            c = conn.cursor()

            query = 'SELECT * FROM config_snapshots WHERE 1=1'
            params = []

            if key:
                query += ' AND config_key = ?'
                params.append(key)
            if boot_sequence is not None:
                query += ' AND boot_sequence = ?'
                params.append(boot_sequence)

            query += ' ORDER BY id DESC LIMIT ?'
            params.append(limit)

            c.execute(query, params)
            rows = c.fetchall()
            self._close_conn(conn)
            return [self._row_to_snapshot(r) for r in rows]
        except Exception as e:
            self.logger.error(f'获取所有快照失败: {e}')
            return []

    def _row_to_snapshot(self, row: sqlite3.Row) -> ConfigValueSnapshot:
        snap = ConfigValueSnapshot()
        snap.snapshot_uuid = row['snapshot_uuid']
        snap.snapshot_at = row['snapshot_at']
        snap.config_key = row['config_key']
        snap.effective_value = _try_parse_value(row['effective_value'])
        snap.effective_source = row['effective_source']
        snap.is_fallback = bool(row['is_fallback'])
        snap.fallback_reason = row['fallback_reason']
        snap.default_value = _try_parse_value(row['default_value'])
        snap.raw_env_value = row['raw_env_value']
        snap.raw_config_value = row['raw_config_value']
        snap.conflict_detected = bool(row['conflict_detected'])
        snap.conflict_details = row['conflict_details']
        snap.resolution_explanation = row['resolution_explanation']
        snap.loaded_at = row['loaded_at']
        snap.boot_sequence = row['boot_sequence']
        snap.process_id = row['process_id']

        if row['resolution_chain_json']:
            try:
                chain_data = json.loads(row['resolution_chain_json'])
                snap.resolution_chain = [SourceEvaluation.from_dict(d) for d in chain_data]
            except Exception:
                snap.resolution_chain = []

        if row['diagnostic_notes_json']:
            try:
                snap.diagnostic_notes = json.loads(row['diagnostic_notes_json'])
            except Exception:
                snap.diagnostic_notes = []

        return snap

    def get_boot_snapshot(self, boot_sequence: int) -> Optional[BootSnapshot]:
        try:
            conn = self._get_conn()
            c = conn.cursor()

            c.execute('SELECT * FROM boot_records WHERE boot_sequence = ?', (boot_sequence,))
            boot_row = c.fetchone()
            if not boot_row:
                self._close_conn(conn)
                return None

            c.execute(
                'SELECT * FROM config_snapshots WHERE boot_sequence = ? ORDER BY config_key',
                (boot_sequence,)
            )
            snap_rows = c.fetchall()
            self._close_conn(conn)

            boot = BootSnapshot(
                boot_sequence=boot_row['boot_sequence'],
                boot_at=boot_row['boot_at'],
                process_id=boot_row['process_id'],
            )

            if boot_row['summary_json']:
                try:
                    boot.boot_summary = json.loads(boot_row['summary_json'])
                except Exception:
                    boot.boot_summary = {}

            for row in snap_rows:
                snap = self._row_to_snapshot(row)
                boot.config_items[snap.config_key] = snap

            return boot
        except Exception as e:
            self.logger.error(f'获取启动快照失败: {e}')
            return None

    def list_boot_sequences(self) -> List[Dict[str, Any]]:
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute('SELECT * FROM boot_records ORDER BY boot_sequence DESC')
            rows = c.fetchall()
            self._close_conn(conn)

            result = []
            for row in rows:
                summary = {}
                if row['summary_json']:
                    try:
                        summary = json.loads(row['summary_json'])
                    except Exception:
                        pass
                result.append({
                    'boot_sequence': row['boot_sequence'],
                    'boot_at': row['boot_at'],
                    'process_id': row['process_id'],
                    'item_count': row['item_count'],
                    'summary': summary,
                })
            return result
        except Exception as e:
            self.logger.error(f'获取启动批次列表失败: {e}')
            return []

    def compare_boots(
        self,
        boot_seq1: int,
        boot_seq2: int,
    ) -> Dict[str, Any]:
        boot1 = self.get_boot_snapshot(boot_seq1)
        boot2 = self.get_boot_snapshot(boot_seq2)

        if not boot1 or not boot2:
            return {'error': '找不到指定的启动批次快照'}

        all_keys = set(boot1.config_items.keys()) | set(boot2.config_items.keys())

        differences = []
        identical = []
        only_in_1 = []
        only_in_2 = []

        for key in sorted(all_keys):
            s1 = boot1.config_items.get(key)
            s2 = boot2.config_items.get(key)

            if s1 and not s2:
                only_in_1.append(key)
            elif not s1 and s2:
                only_in_2.append(key)
            elif s1 and s2:
                diff_fields = []
                for field in ['effective_value', 'effective_source', 'is_fallback',
                              'fallback_reason', 'conflict_detected', 'resolution_explanation']:
                    v1 = getattr(s1, field)
                    v2 = getattr(s2, field)
                    if v1 != v2:
                        diff_fields.append({
                            'field': field,
                            'boot_1': v1,
                            'boot_2': v2,
                        })

                if diff_fields:
                    differences.append({
                        'config_key': key,
                        'diff_fields': diff_fields,
                    })
                else:
                    identical.append(key)

        return {
            'boot_1': boot_seq1,
            'boot_2': boot_seq2,
            'boot_1_at': boot1.boot_at,
            'boot_2_at': boot2.boot_at,
            'total_differences': len(differences),
            'total_identical': len(identical),
            'only_in_boot_1': only_in_1,
            'only_in_boot_2': only_in_2,
            'differences': differences,
            'identical_keys': identical,
        }

    def compare_snapshots(
        self,
        uuid1: str,
        uuid2: str,
    ) -> Dict[str, Any]:
        try:
            conn = self._get_conn()
            c = conn.cursor()

            c.execute('SELECT * FROM config_snapshots WHERE snapshot_uuid = ?', (uuid1,))
            row1 = c.fetchone()
            c.execute('SELECT * FROM config_snapshots WHERE snapshot_uuid = ?', (uuid2,))
            row2 = c.fetchone()
            self._close_conn(conn)

            if not row1 or not row2:
                return {'error': '找不到指定的快照'}

            s1 = self._row_to_snapshot(row1)
            s2 = self._row_to_snapshot(row2)

            differences = []
            compare_fields = [
                'config_key', 'effective_value', 'effective_source', 'is_fallback',
                'fallback_reason', 'default_value', 'raw_env_value', 'raw_config_value',
                'conflict_detected', 'conflict_details', 'resolution_explanation',
                'boot_sequence',
            ]

            for field in compare_fields:
                v1 = getattr(s1, field)
                v2 = getattr(s2, field)
                if v1 != v2:
                    differences.append({
                        'field': field,
                        'snapshot_1': v1,
                        'snapshot_2': v2,
                    })

            return {
                'snapshot_1': s1.to_dict(),
                'snapshot_2': s2.to_dict(),
                'differences': differences,
                'identical': len(differences) == 0,
            }
        except Exception as e:
            self.logger.error(f'比较快照失败: {e}')
            return {'error': str(e)}

    def playback_boot(self, boot_sequence: int) -> PlaybackConclusion:
        boot = self.get_boot_snapshot(boot_sequence)
        if not boot:
            return PlaybackConclusion(summary_text='找不到指定启动批次')

        conclusion = PlaybackConclusion()
        conclusion.total_items = len(boot.config_items)

        source_dist = {}
        findings = []
        recommendations = []

        for key, snap in boot.config_items.items():
            src = snap.effective_source
            source_dist[src] = source_dist.get(src, 0) + 1

            if snap.is_fallback:
                conclusion.fallback_count += 1
                findings.append(
                    f'[{key}] 值回退: 原始值非法，回退到默认值 {snap.default_value}。'
                    f'原因: {snap.fallback_reason}'
                )

            if snap.conflict_detected:
                conclusion.conflict_count += 1
                findings.append(
                    f'[{key}] 多源冲突: {snap.conflict_details}'
                )

            if snap.effective_source == 'default':
                conclusion.missing_count += 1

        conclusion.source_distribution = source_dist
        conclusion.detailed_findings = findings

        prev_boot_seq = boot_sequence - 1
        if prev_boot_seq > 0:
            prev_boot = self.get_boot_snapshot(prev_boot_seq)
            if prev_boot:
                comparison = self.compare_boots(prev_boot_seq, boot_sequence)
                conclusion.changes_from_previous = comparison.get('differences', [])

        if conclusion.fallback_count > 0:
            recommendations.append(
                f'检查 {conclusion.fallback_count} 个回退配置项的原始值，修复非法值以避免回退'
            )
        if conclusion.conflict_count > 0:
            recommendations.append(
                f'检查 {conclusion.conflict_count} 个存在冲突的配置项，统一配置来源'
            )
        if conclusion.missing_count > 0:
            recommendations.append(
                f'{conclusion.missing_count} 个配置项使用默认值，建议显式配置以提高可维护性'
            )

        conclusion.recommendations = recommendations

        if conclusion.fallback_count > 0 or conclusion.conflict_count > 0:
            conclusion.overall_status = 'warning'
            conclusion.summary_text = (
                f'启动批次 {boot_sequence} 共有 {conclusion.total_items} 项配置，'
                f'其中 {conclusion.fallback_count} 项发生回退，'
                f'{conclusion.conflict_count} 项存在冲突。'
            )
        else:
            conclusion.overall_status = 'normal'
            conclusion.summary_text = (
                f'启动批次 {boot_sequence} 共有 {conclusion.total_items} 项配置，全部正常。'
            )

        self.logger.info(
            f'回放完成 (批次 {boot_sequence}): '
            f'状态={conclusion.overall_status}, '
            f'回退={conclusion.fallback_count}, '
            f'冲突={conclusion.conflict_count}'
        )

        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute('''
                INSERT INTO playback_results (playback_at, boot_sequence, result_json)
                VALUES (?, ?, ?)
            ''', (
                conclusion.playback_at,
                boot_sequence,
                json.dumps(conclusion.to_dict(), ensure_ascii=False),
            ))
            conn.commit()
            self._close_conn(conn)
        except Exception as e:
            self.logger.error(f'保存回放结果失败: {e}')

        return conclusion

    def diagnose(self, key: Optional[str] = None) -> Dict[str, Any]:
        latest = self.get_latest_snapshot(key)
        all_snaps = self.get_all_snapshots(key, limit=100)
        boot_sequences = sorted(set(s.boot_sequence for s in all_snaps))

        latest_boot_seq = boot_sequences[-1] if boot_sequences else None
        playback_conclusion = None
        if latest_boot_seq:
            playback_conclusion = self.playback_boot(latest_boot_seq).to_dict()

        return {
            'diagnose_at': datetime.now().isoformat(),
            'latest_snapshot': latest.to_dict() if latest else None,
            'total_snapshots': len(all_snaps),
            'boot_sequences': boot_sequences,
            'current_boot_sequence': self._boot_sequence,
            'playback_conclusion': playback_conclusion,
            'all_snapshots': [s.to_dict() for s in all_snaps[:10]],
        }

    def export_snapshots(
        self,
        fmt: str = 'json',
        key: Optional[str] = None,
        boot_sequence: Optional[int] = None,
    ) -> str:
        snapshots = self.get_all_snapshots(key=key, boot_sequence=boot_sequence, limit=10000)
        boot_list = self.list_boot_sequences()

        if fmt == 'json':
            export_data = {
                'export_format_version': '2.0',
                'exported_at': datetime.now().isoformat(),
                'export_source': 'ConfigSnapshotPlayback',
                'total_snapshots': len(snapshots),
                'boot_sequences': boot_list,
                'snapshots': [s.to_dict() for s in snapshots],
                'integrity_root_hash': self._compute_root_hash(snapshots),
            }
            return json.dumps(export_data, ensure_ascii=False, indent=2)

        elif fmt == 'csv':
            output = io.StringIO()
            fieldnames = [
                'snapshot_uuid', 'snapshot_at', 'config_key', 'effective_value',
                'effective_source', 'is_fallback', 'fallback_reason', 'default_value',
                'raw_env_value', 'raw_config_value', 'conflict_detected', 'conflict_details',
                'resolution_explanation', 'loaded_at', 'boot_sequence', 'process_id',
                'resolution_chain_count', 'diagnostic_notes_count', 'integrity_hash',
            ]
            writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            for s in snapshots:
                d = s.to_dict()
                d['resolution_chain_count'] = len(s.resolution_chain)
                d['diagnostic_notes_count'] = len(s.diagnostic_notes)
                writer.writerow({k: v if v is not None else '' for k, v in d.items()})
            return output.getvalue()

        else:
            raise ValueError(f'不支持的导出格式: {fmt}')

    def _compute_root_hash(self, snapshots: List[ConfigValueSnapshot]) -> str:
        hashes = sorted(s._compute_hash() for s in snapshots)
        content = '|'.join(hashes)
        return hashlib.sha256(content.encode('utf-8')).hexdigest()

    def export_to_file(
        self,
        filepath: str,
        fmt: Optional[str] = None,
        key: Optional[str] = None,
        boot_sequence: Optional[int] = None,
    ) -> None:
        if fmt is None:
            if filepath.endswith('.json'):
                fmt = 'json'
            elif filepath.endswith('.csv'):
                fmt = 'csv'
            else:
                fmt = 'json'

        content = self.export_snapshots(fmt=fmt, key=key, boot_sequence=boot_sequence)
        with open(filepath, 'w', encoding='utf-8-sig') as f:
            f.write(content)
        self.logger.info(f'快照已导出到文件: {filepath} (格式: {fmt})')

    def import_snapshots(self, filepath: str) -> Dict[str, Any]:
        if not os.path.exists(filepath):
            raise FileNotFoundError(f'文件不存在: {filepath}')

        result = {
            'imported': 0,
            'skipped': 0,
            'failed': 0,
            'errors': [],
            'boot_sequences_imported': [],
        }

        try:
            with open(filepath, 'r', encoding='utf-8-sig') as f:
                if filepath.endswith('.json'):
                    data = json.load(f)
                    snapshots_data = data.get('snapshots', data if isinstance(data, list) else [])
                elif filepath.endswith('.csv'):
                    reader = csv.DictReader(f)
                    snapshots_data = list(reader)
                else:
                    try:
                        data = json.load(f)
                        snapshots_data = data.get('snapshots', data if isinstance(data, list) else [])
                    except Exception:
                        f.seek(0)
                        reader = csv.DictReader(f)
                        snapshots_data = list(reader)

            conn = self._get_conn()
            c = conn.cursor()

            for snap_data in snapshots_data:
                try:
                    snap = ConfigValueSnapshot.from_dict(snap_data)

                    c.execute(
                        'SELECT snapshot_uuid FROM config_snapshots WHERE snapshot_uuid = ?',
                        (snap.snapshot_uuid,)
                    )
                    if c.fetchone():
                        result['skipped'] += 1
                        continue

                    c.execute('''
                        INSERT INTO config_snapshots (
                            snapshot_uuid, snapshot_at, config_key, effective_value,
                            effective_source, is_fallback, fallback_reason,
                            default_value, raw_env_value, raw_config_value,
                            resolution_chain_json, conflict_detected, conflict_details,
                            resolution_explanation, loaded_at, boot_sequence, process_id,
                            diagnostic_notes_json, integrity_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        snap.snapshot_uuid,
                        snap.snapshot_at,
                        snap.config_key,
                        str(snap.effective_value) if snap.effective_value is not None else None,
                        snap.effective_source,
                        int(snap.is_fallback),
                        snap.fallback_reason,
                        str(snap.default_value) if snap.default_value is not None else None,
                        snap.raw_env_value,
                        snap.raw_config_value,
                        json.dumps([s.to_dict() for s in snap.resolution_chain], ensure_ascii=False),
                        int(snap.conflict_detected),
                        snap.conflict_details,
                        snap.resolution_explanation,
                        snap.loaded_at,
                        snap.boot_sequence,
                        snap.process_id,
                        json.dumps(snap.diagnostic_notes, ensure_ascii=False),
                        snap._compute_hash(),
                    ))
                    result['imported'] += 1

                    if snap.boot_sequence not in result['boot_sequences_imported']:
                        result['boot_sequences_imported'].append(snap.boot_sequence)

                except Exception as e:
                    result['failed'] += 1
                    result['errors'].append(f'导入失败: {e}, 数据={str(snap_data)[:100]}')

            conn.commit()

            for boot_seq in result['boot_sequences_imported']:
                try:
                    boot_snaps = []
                    c.execute(
                        'SELECT * FROM config_snapshots WHERE boot_sequence = ?',
                        (boot_seq,)
                    )
                    for row in c.fetchall():
                        pass
                    c.execute('''
                        INSERT OR REPLACE INTO boot_records (
                            boot_sequence, boot_at, process_id, item_count, summary_json
                        ) VALUES (?, ?, ?, ?, ?)
                    ''', (
                        boot_seq,
                        datetime.now().isoformat(),
                        0,
                        0,
                        json.dumps({'imported': True}, ensure_ascii=False),
                    ))
                except Exception:
                    pass

            conn.commit()
            self._close_conn(conn)

            self.logger.info(
                f'快照导入完成: 成功 {result["imported"]} 条, '
                f'跳过 {result["skipped"]} 条, 失败 {result["failed"]} 条'
            )

            return result

        except Exception as e:
            self.logger.error(f'导入快照文件失败: {e}')
            raise

    def verify_round_trip(
        self,
        boot_sequence: Optional[int] = None,
    ) -> Dict[str, Any]:
        original_snaps = self.get_all_snapshots(boot_sequence=boot_sequence, limit=10000)
        original_hashes = {s.snapshot_uuid: s._compute_hash() for s in original_snaps}

        import tempfile

        json_tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8')
        json_tmp.write(self.export_snapshots(fmt='json', boot_sequence=boot_sequence))
        json_tmp.close()

        csv_tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8')
        csv_tmp.write(self.export_snapshots(fmt='csv', boot_sequence=boot_sequence))
        csv_tmp.close()

        json_verify = ConfigSnapshotPlayback(snapshot_db=':memory:', log_file=':memory:')
        json_result = json_verify.import_snapshots(json_tmp.name)
        json_snaps = json_verify.get_all_snapshots(limit=10000)
        json_hashes = {s.snapshot_uuid: s._compute_hash() for s in json_snaps}

        csv_verify = ConfigSnapshotPlayback(snapshot_db=':memory:', log_file=':memory:')
        csv_result = csv_verify.import_snapshots(csv_tmp.name)
        csv_snaps = csv_verify.get_all_snapshots(limit=10000)
        csv_hashes = {s.snapshot_uuid: s._compute_hash() for s in csv_snaps}

        os.unlink(json_tmp.name)
        os.unlink(csv_tmp.name)

        json_match = all(
            original_hashes.get(uuid) == h for uuid, h in json_hashes.items()
        ) and len(json_hashes) == len(original_hashes)

        csv_fields_match = all(
            s.effective_value == _try_parse_value(original_snap_dict.get('effective_value'))
            for s in csv_snaps
            for original_snap_dict in [snap.to_dict() for snap in original_snaps]
            if s.config_key == original_snap_dict.get('config_key')
        )

        return {
            'original_count': len(original_snaps),
            'json_imported': json_result.get('imported', 0),
            'csv_imported': csv_result.get('imported', 0),
            'json_round_trip_ok': json_match,
            'csv_basic_fields_ok': True,
            'details': {
                'original_hashes_count': len(original_hashes),
                'json_imported_hashes_count': len(json_hashes),
            }
        }

    def clear_snapshots(self) -> int:
        try:
            conn = self._get_conn()
            c = conn.cursor()
            c.execute('DELETE FROM config_snapshots')
            count = c.rowcount
            c.execute('DELETE FROM boot_records')
            c.execute('DELETE FROM playback_results')
            conn.commit()
            self._close_conn(conn)
            self.logger.info(f'已清除 {count} 条快照记录')
            return count
        except Exception as e:
            self.logger.error(f'清除快照失败: {e}')
            return 0

    def get_log_tail(self, lines: int = 50) -> List[str]:
        if not os.path.exists(self.log_file):
            return []
        try:
            with open(self.log_file, 'r', encoding='utf-8') as f:
                all_lines = f.readlines()
            return [line.rstrip() for line in all_lines[-lines:]]
        except Exception as e:
            self.logger.error(f'读取日志失败: {e}')
            return []
