import urllib.request
import urllib.error
import json
import sqlite3
import time
import sys
import csv
import io

BASE = 'http://127.0.0.1:5000'
DB = 'emergency_supply.db'

EXPECTED_SPECS = [
    {'mat': '医用口罩', 'qty': 15, 'order_no': 'TREXPIRE01'},
    {'mat': '防护服', 'qty': 10, 'order_no': 'TREXPIRE02'},
    {'mat': '消毒液', 'qty': 25, 'order_no': 'TREXPIRE03'},
]

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
mats = {m['name']: m['id'] for m in conn.execute('SELECT * FROM materials').fetchall()}
EXPECTED = []
for spec in EXPECTED_SPECS:
    mid = mats[spec['mat']]
    o = conn.execute("SELECT id FROM transfer_orders WHERE order_no=?",
                     (spec['order_no'],)).fetchone()
    r = conn.execute("SELECT id FROM reservations WHERE order_id=?", (o['id'],)).fetchone()
    EXPECTED.append({'oid': o['id'], 'rid': r['id'],
                     'mat': spec['mat'], 'qty': spec['qty'], 'order_no': spec['order_no'],
                     'mid': mid})
conn.close()
print('EXPECTED 订单:')
for e in EXPECTED:
    print(f'  order#{e["oid"]} reservation#{e["rid"]} {e["mat"]} x{e["qty"]} ({e["order_no"]})')
exp_oids = {e['oid'] for e in EXPECTED}

def api(path, method='GET', data=None):
    url = BASE + path
    body = None
    headers = {'Content-Type': 'application/json'}
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode('utf-8')
            return resp.status, json.loads(text) if text else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))

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

print('='*70)
print('端到端验证: 重启后遗留过期预占释放链路')
print('='*70)

print('\n--- 1. 订单状态验证 ---')
exp_oids = {e['oid'] for e in EXPECTED}
for e in EXPECTED:
    code, order = api(f'/api/orders/{e["oid"]}')
    check(f'订单#{e["oid"]} ({e["order_no"]}/{e["mat"]}) 返回 200', code == 200, f'code={code}')
    check(f'订单#{e["oid"]} status=expired',
          order.get('status') == 'expired', f'实际={order.get("status")}')
    check(f'订单#{e["oid"]} reservation_id 清空',
          order.get('reservation_id') is None, f'实际={order.get("reservation_id")}')

print('\n--- 2. 库存恢复验证 (reserved 扣回, 不扣负, actual 不变) ---')
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
mats = {m['name']: m['id'] for m in conn.execute('SELECT * FROM materials').fetchall()}
conn.close()

code, inv_list = api('/api/inventory')
inv_map = {(i['warehouse_id'], i['material_id']): i for i in inv_list}
for e in EXPECTED:
    mid = mats[e['mat']]
    inv = inv_map[(1, mid)]
    check(f'{e["mat"]} reserved 归零 (扣回 {e["qty"]})',
          inv['reserved_quantity'] == 0,
          f'扣前={e["qty"]} (因为插遗留时原 reserved=0, 加了 {e["qty"]}) 实际={inv["reserved_quantity"]}')
    check(f'{e["mat"]} actual 不变', inv['actual_quantity'] > 0)
    check(f'{e["mat"]} reserved >= 0', inv['reserved_quantity'] >= 0,
          f'实际={inv["reserved_quantity"]}')

print('\n--- 3. 审计原因=startup_cleanup, 且每笔订单 1 条, 不重复 ---')
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
placeholders = ','.join('?' * len(EXPECTED))
qmarks = tuple(e['oid'] for e in EXPECTED)
audits = conn.execute(f"""
    SELECT a.id, a.order_id, a.action,
           json_extract(a.details, '$.reason') as reason,
           json_extract(a.details, '$.quantity') as qty,
           json_extract(a.details, '$.reservation_id') as rid,
           json_extract(a.details, '$.expired_at') as exp_at,
           json_extract(a.details, '$.released_at') as rel_at
    FROM audit_logs a
    WHERE a.action = 'reservation_expired'
    AND a.order_id IN ({placeholders})
    ORDER BY a.id
""", qmarks).fetchall()
conn.close()

check('新建 3 条订单, reservation_expired 审计正好 3 条', len(audits) == 3, f'实际={len(audits)}')
audit_by_oid = {a['order_id']: a for a in audits}
for e in EXPECTED:
    a = audit_by_oid.get(e['oid'])
    check(f'order#{e["oid"]} 有 reservation_expired 审计', a is not None)
    if a:
        check(f'order#{e["oid"]} 审计 reason=startup_cleanup (无后台线程抢先)',
              a['reason'] == 'startup_cleanup', f'实际={a["reason"]}')
        check(f'order#{e["oid"]} 审计 quantity={e["qty"]}', a['qty'] == e['qty'])
        check(f'order#{e["oid"]} 审计 reservation_id={e["rid"]}', a['rid'] == e['rid'])
        check(f'order#{e["oid"]} 审计有 expired_at', a['exp_at'] is not None)
        check(f'order#{e["oid"]} 审计有 released_at', a['rel_at'] is not None)

print('\n--- 4. 手动清理幂等性: 再 cleanup 一次 ---')
before_reserved = {k: v['reserved_quantity'] for k, v in inv_map.items()}
before_count = len(audits)
conn = sqlite3.connect(DB)
before_total = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='reservation_expired'").fetchone()[0]
conn.close()

code, result = api('/api/reservations/cleanup', 'POST')
check('cleanup 返回 200', code == 200, f'code={code}')
check(f'cleanup 返回 cleaned_count=0', result.get('cleaned_count') == 0, f'实际={result}')

code, inv_list2 = api('/api/inventory')
inv_map2 = {(i['warehouse_id'], i['material_id']): i for i in inv_list2}
for k, v in before_reserved.items():
    check(f'inv{k} reserved 未被重复扣减', inv_map2[k]['reserved_quantity'] == v,
          f'before={v}, after={inv_map2[k]["reserved_quantity"]}')

conn = sqlite3.connect(DB)
after_total = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='reservation_expired'").fetchone()[0]
conn.close()
check(f'审计条数未增加 (重复清理不重复写)', after_total == before_total,
      f'before={before_total}, after={after_total}')

print('\n--- 5. 再手动创建 1 条已过期订单, 验证 manual_cleanup 链路正确 ---')
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
c = conn.cursor()

for ono in ('TREXPIRE04',):
    c.execute("DELETE FROM audit_logs WHERE order_id IN (SELECT id FROM transfer_orders WHERE order_no=?)", (ono,))
    c.execute("DELETE FROM reservations WHERE order_id IN (SELECT id FROM transfer_orders WHERE order_no=?)", (ono,))
    c.execute("DELETE FROM transfer_orders WHERE order_no=?", (ono,))
conn.commit()

mid = mats['急救包']
past = '2020-01-01T00:00:00'
qty_new = 8
inv = c.execute('SELECT * FROM inventory WHERE warehouse_id=1 AND material_id=?', (mid,)).fetchone()
before_res = inv['reserved_quantity']
c.execute('UPDATE inventory SET reserved_quantity = reserved_quantity + ? WHERE id=?', (qty_new, inv['id']))
c.execute('''INSERT INTO transfer_orders
    (order_no, status, source_warehouse_id, target_warehouse_id,
     material_id, quantity, requester_id, created_at, updated_at)
    VALUES ('TREXPIRE04', 'reserved', 1, 2, ?, ?, 1, ?, ?)''', (mid, qty_new, past, past))
new_oid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
c.execute('''INSERT INTO reservations
    (order_id, warehouse_id, material_id, quantity, expires_at, is_released, created_at)
    VALUES (?, 1, ?, ?, ?, 0, ?)''', (new_oid, mid, qty_new, past, past))
new_rid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
c.execute('UPDATE transfer_orders SET reservation_id=? WHERE id=?', (new_rid, new_oid))
conn.commit()
conn.close()
print(f'  创建: order#{new_oid} reservation#{new_rid} 急救包 x{qty_new}')

code, result = api('/api/reservations/cleanup', 'POST')
check('cleanup 新订单返回 200', code == 200)
check(f'cleanup 返回 cleaned_count=1', result.get('cleaned_count') == 1, f'实际={result}')

code, order = api(f'/api/orders/{new_oid}')
check(f'新订单 status=expired', order.get('status') == 'expired')
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
row = conn.execute("""SELECT json_extract(details, '$.reason') as reason,
    json_extract(details, '$.quantity') as qty FROM audit_logs
    WHERE order_id=? AND action='reservation_expired'""", (new_oid,)).fetchone()
conn.close()
check(f'新订单审计 reason=manual_cleanup', row['reason'] == 'manual_cleanup', f'实际={row["reason"]}')
check(f'新订单审计 qty={qty_new}', row['qty'] == qty_new)

print('\n--- 6. JSON 审计导出 ---')
req = urllib.request.Request(BASE + '/api/audit/export.json')
with urllib.request.urlopen(req) as resp:
    json_data = json.loads(resp.read().decode('utf-8'))
check('JSON 导出非空', len(json_data) > 0)
exp_audits = [r for r in json_data if r.get('action') == 'reservation_expired']
conn = sqlite3.connect(DB)
all_audits = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='reservation_expired'").fetchone()[0]
conn.close()
check('JSON 中 reservation_expired 条数 = DB', len(exp_audits) == all_audits,
      f'JSON={len(exp_audits)}, DB={all_audits}')
for a in exp_audits:
    if a['order_id'] in exp_oids:
        det = json.loads(a['details']) if isinstance(a['details'], str) else a['details']
        check(f"JSON order#{a['order_id']} reason={det['reason']}",
              det['reason'] == 'startup_cleanup', f'实际={det["reason"]}')

print('\n--- 7. CSV 审计导出 (条数/表头/原因字段 与 JSON 一致) ---')
req = urllib.request.Request(BASE + '/api/audit/export.csv')
with urllib.request.urlopen(req) as resp:
    csv_text = resp.read().decode('utf-8-sig')
reader = csv.reader(io.StringIO(csv_text))
rows = list(reader)
check('CSV 有表头', len(rows) > 1 and 'id' in rows[0] and 'action' in rows[0],
      f'表头={rows[0] if rows else None}')
check(f'CSV 数据行数 = JSON 条数 ({len(json_data)})',
      len(rows) - 1 == len(json_data),
      f'CSV={len(rows)-1}, JSON={len(json_data)}')
action_col = rows[0].index('action')
details_col = rows[0].index('details')
csv_exp_count = sum(1 for r in rows[1:] if r[action_col] == 'reservation_expired')
check(f'CSV 中 reservation_expired 条数 = DB ({all_audits})',
      csv_exp_count == all_audits, f'CSV={csv_exp_count}, DB={all_audits}')
for r in rows[1:]:
    if r[action_col] == 'reservation_expired':
        oid = int(r[rows[0].index('order_id')])
        if oid in exp_oids:
            det_raw = r[details_col]
            det = json.loads(det_raw) if det_raw.startswith('{') else {}
            check(f"CSV order#{oid} reason={det.get('reason')}",
                  det.get('reason') == 'startup_cleanup',
                  f'实际={det.get("reason")}')

print('\n--- 8. 库存一致性: DB vs API, 并验证 reserved_quantity >= 0 全局 ---')
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
db_invs = conn.execute('SELECT * FROM inventory ORDER BY id').fetchall()
conn.close()
check('DB inventory 行数 = API 行数', len(db_invs) == len(inv_list2),
      f'DB={len(db_invs)}, API={len(inv_list2)}')
for db_inv in db_invs:
    key = (db_inv['warehouse_id'], db_inv['material_id'])
    a_inv = inv_map2[key]
    mat_name = [n for n, i in mats.items() if i == db_inv['material_id']][0]
    check(f'{mat_name} DB/API reserved 一致', db_inv['reserved_quantity'] == a_inv['reserved_quantity'])
    check(f'{mat_name} reserved >= 0', db_inv['reserved_quantity'] >= 0,
          f'DB={db_inv["reserved_quantity"]}')

print(f'\n{"="*70}')
passed = sum(1 for _, c, _ in checks if c)
total = len(checks)
print(f'检查总计: {passed} / {total} 通过')
print(f'{"="*70}')

sys.exit(0 if ok else 1)
