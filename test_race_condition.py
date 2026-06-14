import sqlite3
import json
import sys
from datetime import datetime

DB_PATH = 'emergency_supply_race.db'
import os

def clean_db():
    for suffix in ['', '-wal', '-shm', '-journal']:
        p = DB_PATH + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except:
                pass

def init_fresh_db():
    clean_db()
    conn = sqlite3.connect(DB_PATH)
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
            UNIQUE(warehouse_id, material_id)
        );
        CREATE TABLE IF NOT EXISTS transfer_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            source_warehouse_id INTEGER NOT NULL,
            target_warehouse_id INTEGER NOT NULL,
            material_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            requester_id INTEGER NOT NULL,
            approver_id INTEGER,
            reservation_id INTEGER,
            remark TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER UNIQUE NOT NULL,
            warehouse_id INTEGER NOT NULL,
            material_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            is_released INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER,
            action TEXT NOT NULL,
            operator_id INTEGER NOT NULL,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    c.execute("INSERT OR IGNORE INTO users (id, username, role) VALUES (0, 'system', 'system')")
    c.executemany('INSERT INTO users (username, role) VALUES (?, ?)', [
        ('requester1', 'requester'), ('approver1', 'approver')])
    c.execute("INSERT INTO warehouses (name, location) VALUES (?, ?)", ('中心仓库', 'A'))
    c.execute("INSERT INTO materials (name, unit, description) VALUES (?, ?, ?)", ('口罩', '箱', 'test'))
    c.execute('''INSERT INTO inventory
        (warehouse_id, material_id, actual_quantity, reserved_quantity, safety_stock)
        VALUES (1, 1, 100, 20, 10)''')
    c.execute('''INSERT INTO transfer_orders
        (order_no, status, source_warehouse_id, target_warehouse_id,
         material_id, quantity, requester_id, reservation_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        ('TRTEST001', 'reserved', 1, 2, 1, 20, 1, 1))
    past_time = '2020-01-01T00:00:00.000000'
    c.execute('''INSERT INTO reservations
        (order_id, warehouse_id, material_id, quantity, expires_at, is_released)
        VALUES (?, ?, ?, ?, ?, ?)''',
        (1, 1, 1, 20, past_time, 0))
    conn.commit()
    conn.close()

def select_expired(db):
    now = datetime.now().isoformat()
    return db.execute('''SELECT r.* FROM reservations r
                         JOIN transfer_orders o ON r.order_id = o.id
                         WHERE r.expires_at <= ? AND r.is_released = 0
                         AND o.status = 'reserved' ''', (now,)).fetchall()

def bad_update(db, r, reason):
    now = datetime.now().isoformat()
    db.execute('UPDATE inventory SET reserved_quantity = reserved_quantity - ? '
               'WHERE warehouse_id = ? AND material_id = ?',
               (r['quantity'], r['warehouse_id'], r['material_id']))
    db.execute('UPDATE reservations SET is_released = 1 WHERE id = ?', (r['id'],))
    db.execute('UPDATE transfer_orders SET status = ?, reservation_id = NULL '
               'WHERE id = ?', ('expired', r['order_id']))
    db.execute('''INSERT INTO audit_logs (order_id, action, operator_id, details)
                  VALUES (?, 'reservation_expired', 0, ?)''',
               (r['order_id'], json.dumps({
                   'reservation_id': r['id'], 'quantity': r['quantity'],
                   'reason': reason
               }, ensure_ascii=False)))
    return 1

def good_update(db, r, reason):
    now = datetime.now().isoformat()
    cur = db.execute('UPDATE reservations SET is_released = 1 '
                     'WHERE id = ? AND is_released = 0', (r['id'],))
    if cur.rowcount != 1:
        return 0
    db.execute('UPDATE inventory SET reserved_quantity = reserved_quantity - ? '
               'WHERE warehouse_id = ? AND material_id = ?',
               (r['quantity'], r['warehouse_id'], r['material_id']))
    db.execute('UPDATE transfer_orders SET status = ?, reservation_id = NULL '
               'WHERE id = ?', ('expired', r['order_id']))
    db.execute('''INSERT INTO audit_logs (order_id, action, operator_id, details)
                  VALUES (?, 'reservation_expired', 0, ?)''',
               (r['order_id'], json.dumps({
                   'reservation_id': r['id'], 'quantity': r['quantity'],
                   'reason': reason
               }, ensure_ascii=False)))
    return 1

def simulate_toctou(update_fn, label, mode_name):
    init_fresh_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows_A = select_expired(conn)
    assert len(rows_A) == 1, '应该查到 1 条过期预占'
    r_A = rows_A[0]

    rows_B = select_expired(conn)
    assert len(rows_B) == 1, '线程 B 在 A 执行前也查到了同一批'
    r_B = rows_B[0]

    assert r_A['id'] == r_B['id'], '两条线程看到的是同一笔预占'

    count_A = update_fn(conn, r_A, 'startup_cleanup')
    count_B = update_fn(conn, r_B, 'background_worker')
    conn.commit()

    inv = conn.execute('SELECT * FROM inventory WHERE id = 1').fetchone()
    audit_count = conn.execute('SELECT COUNT(*) FROM audit_logs WHERE action = ?',
                               ('reservation_expired',)).fetchone()[0]
    audit_rows = conn.execute("SELECT id, json_extract(details, '$.reason') as reason, "
                               "json_extract(details, '$.quantity') as qty "
                               "FROM audit_logs WHERE action = 'reservation_expired'").fetchall()
    order = conn.execute('SELECT status, reservation_id FROM transfer_orders WHERE id = 1').fetchone()
    reservation = conn.execute('SELECT is_released FROM reservations WHERE id = 1').fetchone()
    conn.close()

    print(f'\n{"="*60}')
    print(f'  {label}')
    print(f'{"="*60}')
    print(f'  场景: A(startup) 和 B(background) 都 SELECT 到同一条过期预占')
    print(f'  时序: A SELECT -> B SELECT -> A UPDATE -> B UPDATE  (TOCTOU)')
    print(f'  A(startup)   count = {count_A}')
    print(f'  B(background) count = {count_B}')
    print(f'  sum = {count_A + count_B}')
    print(f'  inventory: actual={inv["actual_quantity"]}, reserved={inv["reserved_quantity"]}, safety={inv["safety_stock"]}')
    print(f'  order status={order["status"]}, reservation_id={order["reservation_id"]}')
    print(f'  reservation is_released={reservation["is_released"]}')
    print(f'  audit count={audit_count}')
    for ar in audit_rows:
        print(f'    audit[{ar["id"]}]: reason={ar["reason"]}, qty={ar["qty"]}')

    ok = True
    if inv["reserved_quantity"] < 0:
        print(f'  FAIL: reserved_quantity 被扣成负数! (实际={inv["reserved_quantity"]})')
        ok = False
    if mode_name == 'good':
        if inv["reserved_quantity"] != 0:
            print(f'  FAIL: reserved_quantity 应为 0, 实际 {inv["reserved_quantity"]}')
            ok = False
        if audit_count != 1:
            print(f'  FAIL: 审计应为 1 条, 实际 {audit_count} 条')
            ok = False
        if count_A + count_B != 1:
            print(f'  FAIL: 两次 count 之和应为 1, 实际 {count_A + count_B}')
            ok = False
        if ok:
            print('  PASS: 只有一次抢占成功, 结果完全正确')
    else:
        if inv["reserved_quantity"] == 0 and audit_count == 1:
            print('  (此模式为旧代码, 预期会出错, 若未出错可能是 SQLite 单连接序列化的副作用)')
    return ok

if __name__ == '__main__':
    print('='*60)
    print('TOCTOU 复现: SELECT 后交替 UPDATE 模拟')
    print('='*60)

    simulate_toctou(bad_update,
                    '旧代码 (UPDATE reservations 不带 is_released 条件, 不判 rowcount)',
                    mode_name='bad')

    ok = simulate_toctou(good_update,
                         '新代码 (UPDATE reservations 带 AND is_released=0, 判 rowcount)',
                         mode_name='good')

    clean_db()
    if not ok:
        print('\n新代码仍有问题!')
        sys.exit(1)
    print('\n竞争条件修复验证完成, PASS。')
