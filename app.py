import sqlite3
import json
import csv
import time
import threading
import os
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, g, Response

DATABASE = 'emergency_supply.db'
DEFAULT_RESERVATION_EXPIRE_MINUTES = 30

def load_reservation_expire_minutes():
    raw = os.environ.get('RESERVATION_EXPIRE_MINUTES')
    if raw is None or raw.strip() == '':
        source = 'default'
        explanation = f'环境变量 RESERVATION_EXPIRE_MINUTES 未设置，采用内置默认值 {DEFAULT_RESERVATION_EXPIRE_MINUTES} 分钟'
        print(f'[Config] {explanation} (source={source})', flush=True)
        return DEFAULT_RESERVATION_EXPIRE_MINUTES, source, False, raw, explanation
    try:
        val = int(raw)
        if val <= 0:
            source = 'default(fallback)'
            explanation = f'环境变量 RESERVATION_EXPIRE_MINUTES={raw!r} 为非正数，自动回退到内置默认值 {DEFAULT_RESERVATION_EXPIRE_MINUTES} 分钟'
            print(f'[Config] {explanation} (source={source})', flush=True)
            return DEFAULT_RESERVATION_EXPIRE_MINUTES, source, True, raw, explanation
        source = 'env'
        explanation = f'环境变量 RESERVATION_EXPIRE_MINUTES={raw!r} 显式配置，生效值 = {val} 分钟'
        print(f'[Config] {explanation} (source={source})', flush=True)
        return val, source, False, raw, explanation
    except (ValueError, TypeError):
        source = 'default(fallback)'
        explanation = f'环境变量 RESERVATION_EXPIRE_MINUTES={raw!r} 非法(非整数)，自动回退到内置默认值 {DEFAULT_RESERVATION_EXPIRE_MINUTES} 分钟'
        print(f'[Config] {explanation} (source={source})', flush=True)
        return DEFAULT_RESERVATION_EXPIRE_MINUTES, source, True, raw, explanation

RESERVATION_EXPIRE_MINUTES, CONFIG_SOURCE, CONFIG_FALLBACK, CONFIG_RAW_ENV, CONFIG_RESOLUTION = load_reservation_expire_minutes()

APP_CONFIG = {
    'reservation_expire_minutes': RESERVATION_EXPIRE_MINUTES,
    'default_reservation_expire_minutes': DEFAULT_RESERVATION_EXPIRE_MINUTES,
    'config_source': CONFIG_SOURCE,
    'config_fallback': CONFIG_FALLBACK,
    'raw_env_value': CONFIG_RAW_ENV,
    'loaded_at': datetime.now().isoformat(),
    'resolution_explanation': CONFIG_RESOLUTION
}

app = Flask(__name__)

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('requester', 'approver', 'system')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS warehouses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            location TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS materials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            unit TEXT NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            warehouse_id INTEGER NOT NULL,
            material_id INTEGER NOT NULL,
            actual_quantity INTEGER NOT NULL DEFAULT 0,
            reserved_quantity INTEGER NOT NULL DEFAULT 0,
            safety_stock INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(warehouse_id, material_id),
            FOREIGN KEY (warehouse_id) REFERENCES warehouses(id),
            FOREIGN KEY (material_id) REFERENCES materials(id)
        );

        CREATE TABLE IF NOT EXISTS transfer_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN (
                'draft', 'submitted', 'reserved', 'approved',
                'completed', 'rejected', 'withdrawn', 'expired'
            )),
            source_warehouse_id INTEGER NOT NULL,
            target_warehouse_id INTEGER NOT NULL,
            material_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            requester_id INTEGER NOT NULL,
            approver_id INTEGER,
            reservation_id INTEGER,
            remark TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (source_warehouse_id) REFERENCES warehouses(id),
            FOREIGN KEY (target_warehouse_id) REFERENCES warehouses(id),
            FOREIGN KEY (material_id) REFERENCES materials(id),
            FOREIGN KEY (requester_id) REFERENCES users(id),
            FOREIGN KEY (approver_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER UNIQUE NOT NULL,
            warehouse_id INTEGER NOT NULL,
            material_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            is_released INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES transfer_orders(id)
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER,
            action TEXT NOT NULL,
            operator_id INTEGER NOT NULL,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES transfer_orders(id),
            FOREIGN KEY (operator_id) REFERENCES users(id)
        );

        CREATE INDEX IF NOT EXISTS idx_reservations_expires ON reservations(expires_at, is_released);
        CREATE INDEX IF NOT EXISTS idx_audit_order ON audit_logs(order_id);
        CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);

        CREATE TABLE IF NOT EXISTS config_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loaded_at TIMESTAMP NOT NULL,
            raw_env_value TEXT,
            effective_minutes INTEGER NOT NULL,
            default_minutes INTEGER NOT NULL,
            source TEXT NOT NULL,
            fallback INTEGER NOT NULL DEFAULT 0,
            resolution_explanation TEXT NOT NULL
        );
    ''')

    try:
        c.execute('ALTER TABLE reservations ADD COLUMN config_expire_minutes INTEGER')
    except Exception:
        pass

    c.execute('SELECT COUNT(*) FROM users')
    if c.fetchone()[0] == 0:
        c.execute("INSERT OR IGNORE INTO users (id, username, role) VALUES (0, 'system', 'system')")
        c.executemany('INSERT INTO users (username, role) VALUES (?, ?)', [
            ('requester1', 'requester'),
            ('requester2', 'requester'),
            ('approver1', 'approver'),
            ('approver2', 'approver'),
        ])
    else:
        c.execute("INSERT OR IGNORE INTO users (id, username, role) VALUES (0, 'system', 'system')")

    c.execute('SELECT COUNT(*) FROM warehouses')
    if c.fetchone()[0] == 0:
        c.executemany('INSERT INTO warehouses (name, location) VALUES (?, ?)', [
            ('中心仓库', '城市中心A区'),
            ('城东分仓', '城市东区B点'),
            ('城西分仓', '城市西区C点'),
        ])

    c.execute('SELECT COUNT(*) FROM materials')
    if c.fetchone()[0] == 0:
        c.executemany('INSERT INTO materials (name, unit, description) VALUES (?, ?, ?)', [
            ('医用口罩', '箱', '一次性医用外科口罩，每箱1000只'),
            ('防护服', '套', '一次性医用防护服'),
            ('消毒液', '桶', '含氯消毒液，每桶25L'),
            ('应急食品', '箱', '压缩饼干和饮用水套装'),
            ('急救包', '个', '标准急救医疗包'),
        ])

    c.execute('SELECT COUNT(*) FROM inventory')
    if c.fetchone()[0] == 0:
        c.executemany('''INSERT INTO inventory
            (warehouse_id, material_id, actual_quantity, reserved_quantity, safety_stock)
            VALUES (?, ?, ?, 0, ?)''', [
            (1, 1, 500, 50),
            (1, 2, 200, 30),
            (1, 3, 300, 40),
            (1, 4, 1000, 100),
            (1, 5, 150, 20),
            (2, 1, 200, 20),
            (2, 2, 80, 10),
            (3, 3, 100, 15),
            (3, 5, 50, 5),
        ])

    conn.commit()
    conn.close()

def generate_order_no():
    return f'TR{datetime.now().strftime("%Y%m%d%H%M%S")}{int(time.time()*1000) % 1000:03d}'

def row_to_dict(row):
    return {k: row[k] for k in row.keys()} if row else None

def log_audit(db, order_id, action, operator_id, details=None):
    db.execute('''INSERT INTO audit_logs (order_id, action, operator_id, details)
                  VALUES (?, ?, ?, ?)''',
               (order_id, action, operator_id, json.dumps(details, ensure_ascii=False) if details else None))

def insert_config_snapshot(db):
    db.execute('''INSERT INTO config_snapshots
                  (loaded_at, raw_env_value, effective_minutes, default_minutes, source, fallback, resolution_explanation)
                  VALUES (?, ?, ?, ?, ?, ?, ?)''',
               (APP_CONFIG['loaded_at'], APP_CONFIG['raw_env_value'],
                APP_CONFIG['reservation_expire_minutes'], APP_CONFIG['default_reservation_expire_minutes'],
                APP_CONFIG['config_source'], int(APP_CONFIG['config_fallback']),
                APP_CONFIG['resolution_explanation']))

def check_user_role(db, user_id, required_role):
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        return None, jsonify({'error': '用户不存在'}), 404
    if user['role'] != required_role:
        return None, jsonify({'error': f'需要{required_role}角色权限'}), 403
    return user, None, None

def get_available_quantity(db, warehouse_id, material_id):
    inv = db.execute('''SELECT actual_quantity, reserved_quantity, safety_stock
                        FROM inventory WHERE warehouse_id = ? AND material_id = ?''',
                     (warehouse_id, material_id)).fetchone()
    if not inv:
        return None
    return inv['actual_quantity'] - inv['reserved_quantity'] - inv['safety_stock']

def release_expired_reservations(db, reason='system'):
    now = datetime.now().isoformat()
    rows = db.execute('''SELECT r.* FROM reservations r
                         JOIN transfer_orders o ON r.order_id = o.id
                         WHERE r.expires_at <= ? AND r.is_released = 0
                         AND o.status = 'reserved' ''', (now,)).fetchall()

    count = 0
    for r in rows:
        cur = db.execute('UPDATE reservations SET is_released = 1 '
                         'WHERE id = ? AND is_released = 0',
                         (r['id'],))
        if cur.rowcount != 1:
            continue

        db.execute('UPDATE inventory SET reserved_quantity = reserved_quantity - ? '
                   'WHERE warehouse_id = ? AND material_id = ?',
                   (r['quantity'], r['warehouse_id'], r['material_id']))
        db.execute('UPDATE transfer_orders SET status = ?, reservation_id = NULL, updated_at = ? '
                   'WHERE id = ?', ('expired', datetime.now().isoformat(), r['order_id']))
        db.execute('''INSERT INTO audit_logs (order_id, action, operator_id, details)
                      VALUES (?, 'reservation_expired', 0, ?)''',
                   (r['order_id'], json.dumps({
                       'reservation_id': r['id'],
                       'quantity': r['quantity'],
                       'expired_at': r['expires_at'],
                       'released_at': now,
                       'reason': reason
                   }, ensure_ascii=False)))
        count += 1
    return count

def expire_reservations_worker():
    time.sleep(3)
    while True:
        try:
            conn = sqlite3.connect(DATABASE)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys = ON')
            release_expired_reservations(conn, reason='background_worker')
            conn.commit()
            conn.close()
        except Exception as e:
            print(f'Expire worker error: {e}')
        time.sleep(60)

@app.route('/api/warehouses', methods=['GET'])
def list_warehouses():
    db = get_db()
    rows = db.execute('SELECT * FROM warehouses ORDER BY id').fetchall()
    return jsonify([row_to_dict(r) for r in rows])

@app.route('/api/materials', methods=['GET'])
def list_materials():
    db = get_db()
    rows = db.execute('SELECT * FROM materials ORDER BY id').fetchall()
    return jsonify([row_to_dict(r) for r in rows])

@app.route('/api/users', methods=['GET'])
def list_users():
    db = get_db()
    rows = db.execute('SELECT id, username, role, created_at FROM users ORDER BY id').fetchall()
    return jsonify([row_to_dict(r) for r in rows])

@app.route('/api/inventory', methods=['GET'])
def list_inventory():
    db = get_db()
    rows = db.execute('''SELECT i.*, w.name as warehouse_name, m.name as material_name, m.unit
                         FROM inventory i
                         JOIN warehouses w ON i.warehouse_id = w.id
                         JOIN materials m ON i.material_id = m.id
                         ORDER BY i.id''').fetchall()
    result = []
    for r in rows:
        d = row_to_dict(r)
        d['available_quantity'] = d['actual_quantity'] - d['reserved_quantity'] - d['safety_stock']
        result.append(d)
    return jsonify(result)

@app.route('/api/orders', methods=['POST'])
def create_order():
    data = request.get_json()
    required = ['requester_id', 'source_warehouse_id', 'target_warehouse_id', 'material_id', 'quantity']
    for f in required:
        if f not in data:
            return jsonify({'error': f'缺少必填字段: {f}'}), 400

    if data['quantity'] <= 0:
        return jsonify({'error': '调拨数量必须大于0'}), 400

    if data['source_warehouse_id'] == data['target_warehouse_id']:
        return jsonify({'error': '调出和调入仓库不能相同'}), 400

    db = get_db()

    user, err, code = check_user_role(db, data['requester_id'], 'requester')
    if err:
        return err, code

    for wh in ['source_warehouse_id', 'target_warehouse_id']:
        if not db.execute('SELECT 1 FROM warehouses WHERE id = ?', (data[wh],)).fetchone():
            return jsonify({'error': f'{wh} 不存在'}), 404

    if not db.execute('SELECT 1 FROM materials WHERE id = ?', (data['material_id'],)).fetchone():
        return jsonify({'error': '物资不存在'}), 404

    order_no = generate_order_no()
    cur = db.execute('''INSERT INTO transfer_orders
        (order_no, status, source_warehouse_id, target_warehouse_id,
         material_id, quantity, requester_id, remark)
        VALUES (?, 'draft', ?, ?, ?, ?, ?, ?)''',
        (order_no, data['source_warehouse_id'], data['target_warehouse_id'],
         data['material_id'], data['quantity'], data['requester_id'],
         data.get('remark')))

    order_id = cur.lastrowid
    log_audit(db, order_id, 'create_draft', data['requester_id'], {'quantity': data['quantity']})
    db.commit()

    return jsonify({'id': order_id, 'order_no': order_no, 'status': 'draft'}), 201

@app.route('/api/orders/<int:order_id>/submit', methods=['POST'])
def submit_order(order_id):
    data = request.get_json() or {}
    operator_id = data.get('operator_id')
    if not operator_id:
        return jsonify({'error': '缺少 operator_id'}), 400

    db = get_db()
    order = db.execute('SELECT * FROM transfer_orders WHERE id = ?', (order_id,)).fetchone()
    if not order:
        return jsonify({'error': '调拨单不存在'}), 404

    if order['requester_id'] != operator_id:
        return jsonify({'error': '只能由申请人提交'}), 403

    if order['status'] not in ('draft', 'expired'):
        return jsonify({'error': f'当前状态 {order["status"]} 不能提交'}), 400

    available = get_available_quantity(db, order['source_warehouse_id'], order['material_id'])
    if available is None:
        return jsonify({'error': '源仓库无此物资库存记录'}), 400
    if available < order['quantity']:
        return jsonify({
            'error': '库存不足',
            'available': available,
            'requested': order['quantity']
        }), 400

    expires_at = (datetime.now() + timedelta(minutes=RESERVATION_EXPIRE_MINUTES)).isoformat()
    now_iso = datetime.now().isoformat()
    cur = db.execute('''INSERT INTO reservations
        (order_id, warehouse_id, material_id, quantity, expires_at, config_expire_minutes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (order_id, order['source_warehouse_id'], order['material_id'],
         order['quantity'], expires_at, RESERVATION_EXPIRE_MINUTES, now_iso))
    reservation_id = cur.lastrowid

    db.execute('UPDATE inventory SET reserved_quantity = reserved_quantity + ? '
               'WHERE warehouse_id = ? AND material_id = ?',
               (order['quantity'], order['source_warehouse_id'], order['material_id']))

    db.execute('''UPDATE transfer_orders
                  SET status = 'reserved', reservation_id = ?, updated_at = ?
                  WHERE id = ?''',
               (reservation_id, datetime.now().isoformat(), order_id))

    log_audit(db, order_id, 'submit_reserve', operator_id, {
        'reservation_id': reservation_id,
        'expires_at': expires_at,
        'quantity': order['quantity'],
        'config_expire_minutes': RESERVATION_EXPIRE_MINUTES,
        'config_source': CONFIG_SOURCE
    })
    db.commit()

    return jsonify({
        'id': order_id,
        'status': 'reserved',
        'reservation_id': reservation_id,
        'expires_at': expires_at,
        'config_expire_minutes_used': RESERVATION_EXPIRE_MINUTES
    })

@app.route('/api/orders/<int:order_id>/approve', methods=['POST'])
def approve_order(order_id):
    data = request.get_json() or {}
    operator_id = data.get('operator_id')
    if not operator_id:
        return jsonify({'error': '缺少 operator_id'}), 400

    db = get_db()

    approver, err, code = check_user_role(db, operator_id, 'approver')
    if err:
        return err, code

    order = db.execute('SELECT * FROM transfer_orders WHERE id = ?', (order_id,)).fetchone()
    if not order:
        return jsonify({'error': '调拨单不存在'}), 404

    if order['status'] == 'approved':
        return jsonify({'error': '调拨单已审批，请勿重复审批'}), 400
    if order['status'] == 'completed':
        return jsonify({'error': '调拨单已出库，无法审批'}), 400
    if order['status'] != 'reserved':
        return jsonify({'error': f'当前状态 {order["status"]} 不能审批'}), 400

    release_expired_reservations(db, reason='approve_check')

    order = db.execute('SELECT * FROM transfer_orders WHERE id = ?', (order_id,)).fetchone()
    if order['status'] == 'expired':
        db.commit()
        return jsonify({'error': '预占已过期，请重新提交'}), 400

    reservation = db.execute('SELECT * FROM reservations WHERE id = ? AND is_released = 0',
                             (order['reservation_id'],)).fetchone()
    if not reservation:
        db.commit()
        return jsonify({'error': '预占已失效，请重新提交'}), 400

    available = get_available_quantity(db, order['source_warehouse_id'], order['material_id'])
    if available < 0:
        db.commit()
        return jsonify({'error': '库存已不足，请重新确认'}), 400

    db.execute('''UPDATE transfer_orders
                  SET status = 'approved', approver_id = ?, updated_at = ?
                  WHERE id = ?''',
               (operator_id, datetime.now().isoformat(), order_id))

    log_audit(db, order_id, 'approve', operator_id, {
        'before_status': 'reserved',
        'after_status': 'approved'
    })
    db.commit()

    return jsonify({'id': order_id, 'status': 'approved', 'approver': approver['username']})

@app.route('/api/orders/<int:order_id>/reject', methods=['POST'])
def reject_order(order_id):
    data = request.get_json() or {}
    operator_id = data.get('operator_id')
    reason = data.get('reason', '')
    if not operator_id:
        return jsonify({'error': '缺少 operator_id'}), 400

    db = get_db()

    approver, err, code = check_user_role(db, operator_id, 'approver')
    if err:
        return err, code

    order = db.execute('SELECT * FROM transfer_orders WHERE id = ?', (order_id,)).fetchone()
    if not order:
        return jsonify({'error': '调拨单不存在'}), 404

    if order['status'] not in ('reserved', 'approved'):
        return jsonify({'error': f'当前状态 {order["status"]} 不能驳回'}), 400

    if order['reservation_id']:
        db.execute('UPDATE inventory SET reserved_quantity = reserved_quantity - ? '
                   'WHERE warehouse_id = ? AND material_id = ?',
                   (order['quantity'], order['source_warehouse_id'], order['material_id']))
        db.execute('UPDATE reservations SET is_released = 1 WHERE id = ?',
                   (order['reservation_id'],))
        log_audit(db, order_id, 'release_reservation', operator_id, {
            'reservation_id': order['reservation_id'],
            'reason': '驳回释放'
        })

    db.execute('''UPDATE transfer_orders
                  SET status = 'rejected', approver_id = ?, reservation_id = NULL, updated_at = ?
                  WHERE id = ?''',
               (operator_id, datetime.now().isoformat(), order_id))

    log_audit(db, order_id, 'reject', operator_id, {'reason': reason})
    db.commit()

    return jsonify({'id': order_id, 'status': 'rejected'})

@app.route('/api/orders/<int:order_id>/withdraw', methods=['POST'])
def withdraw_order(order_id):
    data = request.get_json() or {}
    operator_id = data.get('operator_id')
    if not operator_id:
        return jsonify({'error': '缺少 operator_id'}), 400

    db = get_db()
    order = db.execute('SELECT * FROM transfer_orders WHERE id = ?', (order_id,)).fetchone()
    if not order:
        return jsonify({'error': '调拨单不存在'}), 404

    if order['requester_id'] != operator_id:
        return jsonify({'error': '只能由申请人撤回'}), 403

    if order['status'] in ('completed', 'rejected', 'withdrawn'):
        return jsonify({'error': f'当前状态 {order["status"]} 不能撤回'}), 400

    if order['reservation_id']:
        db.execute('UPDATE inventory SET reserved_quantity = reserved_quantity - ? '
                   'WHERE warehouse_id = ? AND material_id = ?',
                   (order['quantity'], order['source_warehouse_id'], order['material_id']))
        db.execute('UPDATE reservations SET is_released = 1 WHERE id = ?',
                   (order['reservation_id'],))
        log_audit(db, order_id, 'release_reservation', operator_id, {
            'reservation_id': order['reservation_id'],
            'reason': '撤回释放'
        })

    db.execute('''UPDATE transfer_orders
                  SET status = 'withdrawn', reservation_id = NULL, updated_at = ?
                  WHERE id = ?''',
               (datetime.now().isoformat(), order_id))

    log_audit(db, order_id, 'withdraw', operator_id)
    db.commit()

    return jsonify({'id': order_id, 'status': 'withdrawn'})

@app.route('/api/orders/<int:order_id>/outbound', methods=['POST'])
def outbound_order(order_id):
    data = request.get_json() or {}
    operator_id = data.get('operator_id')
    if not operator_id:
        return jsonify({'error': '缺少 operator_id'}), 400

    db = get_db()

    approver, err, code = check_user_role(db, operator_id, 'approver')
    if err:
        return err, code

    order = db.execute('SELECT * FROM transfer_orders WHERE id = ?', (order_id,)).fetchone()
    if not order:
        return jsonify({'error': '调拨单不存在'}), 404

    if order['status'] != 'approved':
        return jsonify({'error': f'当前状态 {order["status"]} 不能出库'}), 400

    db.execute('UPDATE inventory SET actual_quantity = actual_quantity - ?, '
               'reserved_quantity = reserved_quantity - ? '
               'WHERE warehouse_id = ? AND material_id = ?',
               (order['quantity'], order['quantity'],
                order['source_warehouse_id'], order['material_id']))

    db.execute('UPDATE inventory SET actual_quantity = actual_quantity + ? '
               'WHERE warehouse_id = ? AND material_id = ?',
               (order['quantity'], order['target_warehouse_id'], order['material_id']))

    db.execute('UPDATE reservations SET is_released = 1 WHERE id = ?',
               (order['reservation_id'],))

    log_audit(db, order_id, 'release_reservation', operator_id, {
        'reservation_id': order['reservation_id'],
        'reason': '出库释放'
    })

    db.execute('''UPDATE transfer_orders
                  SET status = 'completed', updated_at = ?
                  WHERE id = ?''',
               (datetime.now().isoformat(), order_id))

    log_audit(db, order_id, 'outbound', operator_id, {
        'quantity': order['quantity'],
        'source_warehouse_id': order['source_warehouse_id'],
        'target_warehouse_id': order['target_warehouse_id']
    })
    db.commit()

    return jsonify({'id': order_id, 'status': 'completed'})

@app.route('/api/orders', methods=['GET'])
def list_orders():
    db = get_db()
    status = request.args.get('status')
    query = '''SELECT o.*,
                      w1.name as source_warehouse_name,
                      w2.name as target_warehouse_name,
                      m.name as material_name, m.unit,
                      u1.username as requester_name,
                      u2.username as approver_name
               FROM transfer_orders o
               JOIN warehouses w1 ON o.source_warehouse_id = w1.id
               JOIN warehouses w2 ON o.target_warehouse_id = w2.id
               JOIN materials m ON o.material_id = m.id
               JOIN users u1 ON o.requester_id = u1.id
               LEFT JOIN users u2 ON o.approver_id = u2.id'''
    params = []
    if status:
        query += ' WHERE o.status = ?'
        params.append(status)
    query += ' ORDER BY o.id DESC'
    rows = db.execute(query, params).fetchall()
    return jsonify([row_to_dict(r) for r in rows])

@app.route('/api/orders/<int:order_id>', methods=['GET'])
def get_order(order_id):
    db = get_db()
    row = db.execute('''SELECT o.*,
                               w1.name as source_warehouse_name,
                               w2.name as target_warehouse_name,
                               m.name as material_name, m.unit,
                               u1.username as requester_name,
                               u2.username as approver_name,
                               r.expires_at as reservation_expires_at
                        FROM transfer_orders o
                        JOIN warehouses w1 ON o.source_warehouse_id = w1.id
                        JOIN warehouses w2 ON o.target_warehouse_id = w2.id
                        JOIN materials m ON o.material_id = m.id
                        JOIN users u1 ON o.requester_id = u1.id
                        LEFT JOIN users u2 ON o.approver_id = u2.id
                        LEFT JOIN reservations r ON o.reservation_id = r.id
                        WHERE o.id = ?''', (order_id,)).fetchone()
    if not row:
        return jsonify({'error': '调拨单不存在'}), 404
    result = row_to_dict(row)
    result['config'] = APP_CONFIG
    return jsonify(result)

@app.route('/api/audit', methods=['GET'])
def list_audit():
    db = get_db()
    order_id = request.args.get('order_id', type=int)
    query = '''SELECT a.*, o.order_no, COALESCE(u.username, 'system') as username, COALESCE(u.role, 'system') as role
               FROM audit_logs a
               LEFT JOIN users u ON a.operator_id = u.id
               LEFT JOIN transfer_orders o ON a.order_id = o.id'''
    params = []
    if order_id:
        query += ' WHERE a.order_id = ?'
        params.append(order_id)
    query += ' ORDER BY a.id DESC'
    rows = db.execute(query, params).fetchall()
    result = []
    for r in rows:
        d = row_to_dict(r)
        if d.get('details'):
            try:
                d['details'] = json.loads(d['details'])
            except:
                pass
        result.append(d)
    return jsonify(result)

@app.route('/api/audit/export.<fmt>', methods=['GET'])
def export_audit(fmt):
    if fmt not in ('json', 'csv'):
        return jsonify({'error': '仅支持 json 或 csv 格式'}), 400

    db = get_db()
    rows = db.execute('''SELECT a.*, o.order_no, COALESCE(u.username, 'system') as username, COALESCE(u.role, 'system') as role
                         FROM audit_logs a
                         LEFT JOIN users u ON a.operator_id = u.id
                         LEFT JOIN transfer_orders o ON a.order_id = o.id
                         ORDER BY a.id''').fetchall()

    data = []
    for r in rows:
        d = row_to_dict(r)
        if d.get('details'):
            try:
                d['details'] = json.loads(d['details'])
            except:
                pass
        data.append(d)

    if fmt == 'json':
        return Response(
            json.dumps(data, ensure_ascii=False, indent=2),
            mimetype='application/json',
            headers={'Content-Disposition': 'attachment; filename=audit_logs.json'}
        )
    else:
        output = []
        headers = ['id', 'order_id', 'order_no', 'action', 'operator_id', 'username', 'role', 'details', 'created_at']
        output.append(','.join(headers))
        for d in data:
            line = []
            for h in headers:
                v = d.get(h, '')
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False)
                v = str(v).replace('"', '""')
                line.append(f'"{v}"')
            output.append(','.join(line))

        return Response(
            '\n'.join(output),
            mimetype='text/csv; charset=utf-8-sig',
            headers={'Content-Disposition': 'attachment; filename=audit_logs.csv'}
        )

@app.route('/api/reservations/cleanup', methods=['POST'])
def manual_cleanup_expired():
    db = get_db()
    count = release_expired_reservations(db, reason='manual_cleanup')
    db.commit()
    return jsonify({'cleaned_count': count})

@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(APP_CONFIG)

@app.route('/api/config/diagnose', methods=['GET'])
def diagnose_config():
    db = get_db()
    now_iso = datetime.now().isoformat()

    latest_snapshot_row = db.execute(
        'SELECT * FROM config_snapshots ORDER BY id DESC LIMIT 1'
    ).fetchone()
    latest_snapshot = row_to_dict(latest_snapshot_row) if latest_snapshot_row else None

    all_snapshots_rows = db.execute(
        'SELECT * FROM config_snapshots ORDER BY id'
    ).fetchall()
    all_snapshots = [row_to_dict(r) for r in all_snapshots_rows]

    active_reservations = db.execute('''
        SELECT r.id, r.order_id, r.expires_at, r.config_expire_minutes, r.created_at,
               o.order_no, o.status as order_status
        FROM reservations r
        JOIN transfer_orders o ON r.order_id = o.id
        WHERE r.is_released = 0
        ORDER BY r.id
    ''').fetchall()

    alignment_details = []
    aligned_count = 0
    misaligned_count = 0
    for r in active_reservations:
        d = row_to_dict(r)
        cfg_min = d.get('config_expire_minutes')
        if cfg_min is not None and d.get('created_at') and d.get('expires_at'):
            try:
                created = datetime.fromisoformat(d['created_at'])
                expires = datetime.fromisoformat(d['expires_at'])
                actual_delta_minutes = (expires - created).total_seconds() / 60.0
                d['actual_delta_minutes'] = round(actual_delta_minutes, 2)
                d['aligned'] = abs(actual_delta_minutes - cfg_min) < 1.0
                if d['aligned']:
                    aligned_count += 1
                else:
                    misaligned_count += 1
            except Exception:
                d['actual_delta_minutes'] = None
                d['aligned'] = None
        else:
            d['actual_delta_minutes'] = None
            d['aligned'] = None
        alignment_details.append(d)

    expired_unreleased = db.execute('''
        SELECT r.id, r.order_id, r.expires_at, r.config_expire_minutes, o.order_no
        FROM reservations r
        JOIN transfer_orders o ON r.order_id = o.id
        WHERE r.is_released = 0 AND r.expires_at <= ? AND o.status = 'reserved'
    ''', (now_iso,)).fetchall()

    return jsonify({
        'current_config': APP_CONFIG,
        'latest_snapshot': latest_snapshot,
        'all_snapshots': all_snapshots,
        'reservation_alignment': {
            'total_active': len(active_reservations),
            'aligned_with_config': aligned_count,
            'misaligned': misaligned_count,
            'details': alignment_details
        },
        'expired_unreleased': [row_to_dict(r) for r in expired_unreleased],
        'diagnose_at': now_iso
    })

@app.route('/api/stats', methods=['GET'])
def get_stats():
    db = get_db()
    stats = {}
    for status in ['draft', 'submitted', 'reserved', 'approved', 'completed', 'rejected', 'withdrawn', 'expired']:
        c = db.execute('SELECT COUNT(*) FROM transfer_orders WHERE status = ?', (status,)).fetchone()
        stats[f'{status}_count'] = c[0]
    c = db.execute('SELECT COUNT(*) FROM audit_logs').fetchone()
    stats['audit_log_count'] = c[0]
    stats['config'] = APP_CONFIG
    return jsonify(stats)

if __name__ == '__main__':
    init_db()
    startup_conn = sqlite3.connect(DATABASE)
    startup_conn.row_factory = sqlite3.Row
    startup_conn.execute('PRAGMA foreign_keys = ON')
    insert_config_snapshot(startup_conn)
    startup_conn.commit()
    print(f'[Config] 配置快照已写入 config_snapshots 表 (loaded_at={APP_CONFIG["loaded_at"]})', flush=True)
    before_count = startup_conn.execute(
        "SELECT COUNT(*) FROM reservations r JOIN transfer_orders o ON r.order_id = o.id "
        "WHERE r.is_released = 0 AND o.status = 'reserved'"
    ).fetchone()[0]
    print(f'Startup: found {before_count} reserved (unreleased) reservations')
    startup_cleaned = release_expired_reservations(startup_conn, reason='startup_cleanup')
    startup_conn.commit()
    startup_conn.close()
    if startup_cleaned > 0:
        print(f'Startup: cleaned {startup_cleaned} expired reservation(s)')
    else:
        print('Startup: no expired reservations to clean')
    t = threading.Thread(target=expire_reservations_worker, daemon=True)
    t.start()
    app.run(host='127.0.0.1', port=5000, debug=False)
