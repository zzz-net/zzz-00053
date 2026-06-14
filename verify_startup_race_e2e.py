import urllib.request
import urllib.error
import json
import sqlite3
import time
import sys
import os
import subprocess
import signal

BASE = 'http://127.0.0.1:5000'
DB = 'emergency_supply.db'

def api(path, method='GET', data=None, username=None, password=None):
    url = BASE + path
    body = None
    headers = {'Content-Type': 'application/json'}
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))

def wait_for_server(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, _ = api('/api/health')
            if code == 200:
                return True
        except Exception as e:
            pass
        time.sleep(0.5)
    return False

def create_leftover_expired():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    materials = c.execute('SELECT * FROM materials').fetchall()
    mat_by_name = {m['name']: m['id'] for m in materials}

    past = '2020-01-01T00:00:00'
    order_specs = [
        ('医用口罩', 15, 'TREXPIRE01'),
        ('防护服', 10, 'TREXPIRE02'),
        ('消毒液', 25, 'TREXPIRE03'),
    ]

    created = []
    for mat_name, qty, order_no in order_specs:
        mid = mat_by_name[mat_name]
        inv = c.execute('''SELECT * FROM inventory
            WHERE warehouse_id = 1 AND material_id = ?''', (mid,)).fetchone()
        c.execute('''UPDATE inventory SET reserved_quantity = reserved_quantity + ?
            WHERE id = ?''', (qty, inv['id']))
        c.execute('''INSERT INTO transfer_orders
            (order_no, status, source_warehouse_id, target_warehouse_id,
             material_id, quantity, requester_id, created_at, updated_at)
            VALUES (?, 'reserved', 1, 2, ?, ?, 1, ?, ?)''',
            (order_no, mid, qty, past, past))
        oid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.execute('''INSERT INTO reservations
            (order_id, warehouse_id, material_id, quantity, expires_at, is_released, created_at)
            VALUES (?, 1, ?, ?, ?, 0, ?)''',
            (oid, mid, qty, past, past))
        rid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.execute('UPDATE transfer_orders SET reservation_id = ? WHERE id = ?', (rid, oid))
        created.append({'oid': oid, 'rid': rid, 'mat': mat_name, 'qty': qty,
                        'before_reserved': inv['reserved_quantity'] + qty})
    conn.commit()
    conn.close()
    return created

def check(label, cond, detail=''):
    status = 'PASS' if cond else 'FAIL'
    msg = f'  [{status}] {label}'
    if detail:
        msg += f' ({detail})'
    print(msg)
    return cond

def main():
    if not os.path.exists(DB):
        print('数据库不存在, 先启动 app.py 初始化一次...')
        sys.exit(1)

    print('='*60)
    print('端到端: 重启后遗留过期预占清理验证')
    print('='*60)

    print('\n[步骤1] 在数据库中插入 3 条遗留过期预占 (订单状态=reserved, expires_at=2020年)...')
    leftovers = create_leftover_expired()
    for l in leftovers:
        print(f'  - order#{l["oid"]} {l["mat"]} x{l["qty"]}, reservation#{l["rid"]}')

    print('\n[步骤2] 确认服务已停止, 然后启动 app.py, 抓取启动日志...')
    print('(请在 terminal 1 启动服务, 等 3 秒后按回车)')
    input('回车继续...')

    if not wait_for_server():
        print('服务未启动!')
        sys.exit(1)

    time.sleep(2)

    print('\n[步骤3] 验证启动清理结果:')

    all_ok = True

    for l in leftovers:
        code, order = api(f'/api/orders/{l["oid"]}')
        all_ok &= check(f'订单#{l["oid"]} ({l["mat"]}) 状态=expired',
                        order.get('status') == 'expired',
                        f'实际={order.get("status")}')
        all_ok &= check(f'订单#{l["oid"]} reservation_id 已清空',
                        order.get('reservation_id') is None,
                        f'实际={order.get("reservation_id")}')

    print('\n[步骤4] 验证库存恢复 + 不扣负:')
    code, inv_list = api('/api/inventory')
    inv_map = {(i['warehouse_id'], i['material_id']): i for i in inv_list}
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    mats = {m['id']: m['name'] for m in conn.execute('SELECT * FROM materials').fetchall()}
    conn.close()

    for l in leftovers:
        inv = inv_map[(1, [mid for mid, nm in mats.items() if nm == l['mat']][0])]
        reserved_after = inv['reserved_quantity']
        expected_before = l['before_reserved']
        expected_after = expected_before - l['qty']
        all_ok &= check(f'{l["mat"]} reserved_quantity 正确扣回',
                        reserved_after == expected_after,
                        f'扣前={expected_before}, 应扣={l["qty"]}, 扣后={reserved_after}, 期望={expected_after}')
        all_ok &= check(f'{l["mat"]} reserved_quantity >= 0',
                        reserved_after >= 0, f'实际={reserved_after}')

    print('\n[步骤5] 验证审计原因一律为 startup_cleanup, 没有 background_worker:')
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    audit_rows = conn.execute("""
        SELECT a.id, a.order_id, json_extract(a.details, '$.reason') as reason,
               json_extract(a.details, '$.quantity') as qty,
               json_extract(a.details, '$.reservation_id') as rid
        FROM audit_logs a
        WHERE a.action = 'reservation_expired'
        ORDER BY a.id
    """).fetchall()

    leftovers_oids = {l['oid'] for l in leftovers}
    target_audits = [a for a in audit_rows if a['order_id'] in leftovers_oids]

    all_ok &= check('3 笔遗留订单各有且只有 1 条 reservation_expired 审计',
                    len(target_audits) == 3,
                    f'实际={len(target_audits)} 条')
    for a in target_audits:
        all_ok &= check(f'order#{a["order_id"]} 审计 reason=startup_cleanup',
                        a['reason'] == 'startup_cleanup',
                        f'实际={a["reason"]}')
        all_ok &= check(f'order#{a["order_id"]} 审计 quantity 正确',
                        a['qty'] == next(l['qty'] for l in leftovers if l['oid'] == a['order_id']))
        all_ok &= check(f'order#{a["order_id"]} 审计有 reservation_id',
                        a['rid'] is not None)

    conn.close()

    print('\n[步骤6] 手动清理幂等性验证 (再调一次 cleanup):')
    before_reserved = {k: v['reserved_quantity'] for k, v in inv_map.items()}
    before_audit_count = len(target_audits)
    code, result = api('/api/reservations/cleanup', 'POST')
    all_ok &= check('cleanup 返回 200', code == 200)
    all_ok &= check('cleanup 返回 cleaned=0', result.get('cleaned') == 0,
                    f'实际={result.get("cleaned")}')

    code, inv_list2 = api('/api/inventory')
    inv_map2 = {(i['warehouse_id'], i['material_id']): i for i in inv_list2}
    for k, v in before_reserved.items():
        all_ok &= check(f'inventory{k} reserved_quantity 未变',
                        inv_map2[k]['reserved_quantity'] == v)

    conn = sqlite3.connect(DB)
    cnt_after = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='reservation_expired'").fetchone()[0]
    conn.close()
    all_ok &= check('审计条数未增加', cnt_after == before_audit_count + (len(audit_rows) - len(target_audits)),
                    f'清理前 reservation_expired 总数={len(audit_rows)}, 清理后={cnt_after}')

    print('\n[步骤7] JSON/CSV 审计导出一致性:')
    req = urllib.request.Request(BASE + '/api/audit/export.json')
    with urllib.request.urlopen(req) as resp:
        json_text = resp.read().decode('utf-8')
        json_data = json.loads(json_text)
    exp_count_json = sum(1 for r in json_data if r.get('action') == 'reservation_expired')
    all_ok &= check(f'JSON 导出 reservation_expired 条数',
                    exp_count_json == len(audit_rows),
                    f'JSON={exp_count_json}, DB={len(audit_rows)}')

    req = urllib.request.Request(BASE + '/api/audit/export.csv')
    with urllib.request.urlopen(req) as resp:
        csv_text = resp.read().decode('utf-8-sig')
    csv_lines = [line for line in csv_text.strip().split('\n') if line.strip()]
    csv_count = len(csv_lines) - 1
    all_ok &= check(f'CSV 条数与 JSON 一致', csv_count == len(json_data),
                    f'CSV={csv_count}, JSON={len(json_data)}')
    all_ok &= check('CSV 有表头', 'action' in csv_lines[0] and 'id' in csv_lines[0])
    exp_count_csv = sum(1 for line in csv_lines[1:] if 'reservation_expired' in line)
    all_ok &= check(f'CSV 中 reservation_expired 条数正确',
                    exp_count_csv == len(audit_rows),
                    f'CSV={exp_count_csv}, DB={len(audit_rows)}')

    print(f'\n{"="*60}')
    print('结果: ' + ('全部通过' if all_ok else '有失败, 请查看上方 FAIL 项'))
    print(f'{"="*60}')
    return 0 if all_ok else 1

if __name__ == '__main__':
    sys.exit(main())
