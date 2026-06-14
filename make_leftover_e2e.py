import sqlite3
import os

DB = 'emergency_supply.db'

def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    for ono in ('TREXPIRE01', 'TREXPIRE02', 'TREXPIRE03'):
        c.execute("DELETE FROM audit_logs WHERE order_id IN (SELECT id FROM transfer_orders WHERE order_no=?)", (ono,))
        c.execute("DELETE FROM reservations WHERE order_id IN (SELECT id FROM transfer_orders WHERE order_no=?)", (ono,))
        c.execute("DELETE FROM transfer_orders WHERE order_no=?", (ono,))
    conn.commit()

    materials = c.execute('SELECT * FROM materials').fetchall()
    mat_by_name = {m['name']: m['id'] for m in materials}
    mat_by_id = {m['id']: m['name'] for m in materials}
    print(f'物资: {mat_by_name}')

    past = '2020-01-01T00:00:00'
    order_specs = [
        ('医用口罩', 15, 'TREXPIRE01'),
        ('防护服', 10, 'TREXPIRE02'),
        ('消毒液', 25, 'TREXPIRE03'),
    ]

    print('\n创建 3 条遗留过期预占:')
    for mat_name, qty, order_no in order_specs:
        mid = mat_by_name[mat_name]
        inv = c.execute('''SELECT * FROM inventory
            WHERE warehouse_id = 1 AND material_id = ?''', (mid,)).fetchone()
        print(f'  {mat_name}: inv_before actual={inv["actual_quantity"]}, reserved_before={inv["reserved_quantity"]}, qty_to_reserve={qty}')
        c.execute('''UPDATE inventory SET reserved_quantity = reserved_quantity + ?
            WHERE id = ?''', (qty, inv['id']))
        c.execute('''INSERT INTO transfer_orders
            (order_no, status, source_warehouse_id, target_warehouse_id,
             material_id, quantity, requester_id, created_at, updated_at)
            VALUES (?, ?, 1, 2, ?, ?, 1, ?, ?)''',
            (order_no, 'reserved', mid, qty, past, past))
        oid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.execute('''INSERT INTO reservations
            (order_id, warehouse_id, material_id, quantity, expires_at, is_released, created_at)
            VALUES (?, 1, ?, ?, ?, 0, ?)''',
            (oid, mid, qty, past, past))
        rid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.execute('UPDATE transfer_orders SET reservation_id = ? WHERE id = ?', (rid, oid))
        print(f'    -> order#{oid} reservation#{rid}')

    conn.commit()

    print('\n插完后查询 (模拟服务未启动清理前状态):')
    for inv in c.execute('SELECT * FROM inventory WHERE warehouse_id=1').fetchall():
        mat_name = mat_by_id[inv['material_id']]
        print(f'  {mat_name}: reserved={inv["reserved_quantity"]}')
    for o in c.execute("SELECT id, order_no, status, reservation_id FROM transfer_orders WHERE status='reserved'").fetchall():
        print(f'  order#{o["id"]}: {o["order_no"]} status={o["status"]} reservation_id={o["reservation_id"]}')
    for r in c.execute('SELECT id, order_id, quantity, expires_at, is_released FROM reservations').fetchall():
        print(f'  reservation#{r["id"]}: order#{r["order_id"]} qty={r["quantity"]} expires_at={r["expires_at"]} released={r["is_released"]}')

    conn.close()
    print('\n遗留过期预占创建完成。接下来请重启服务查看启动清理日志。')

if __name__ == '__main__':
    main()
