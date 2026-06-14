import json
import sqlite3
import urllib.request
import sys

BASE = 'http://127.0.0.1:5000'
DB_PATH = 'emergency_supply.db'

def api_request(path, method='GET', data=None):
    url = BASE + path
    headers = {'Content-Type': 'application/json'}
    body = json.dumps(data, ensure_ascii=False).encode('utf-8') if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.getcode(), json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))

def get_inventory(warehouse_id, material_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM inventory WHERE warehouse_id = ? AND material_id = ?',
                       (warehouse_id, material_id)).fetchone()
    conn.close()
    return dict(row) if row else None

def count_audit_by_action(action):
    conn = sqlite3.connect(DB_PATH)
    c = conn.execute('SELECT COUNT(*) FROM audit_logs WHERE action = ?', (action,)).fetchone()
    conn.close()
    return c[0]

def main():
    print('='*60)
    print('重启后过期预占处理验证')
    print('='*60)

    print('\n1. 验证防护服订单状态（应该已过期）...')
    code, order3 = api_request('/api/orders/3')
    assert code == 200
    print(f'  订单3状态: {order3["status"]}')
    assert order3['status'] == 'expired', f'订单3应为 expired, 实际 {order3["status"]}'
    assert order3['reservation_id'] is None
    print('  OK 订单已自动过期')

    print('\n2. 验证库存恢复（中心仓库防护服）...')
    inv = get_inventory(1, 2)
    print(f'  防护服库存: actual={inv["actual_quantity"]}, reserved={inv["reserved_quantity"]}')
    assert inv['reserved_quantity'] == 0, '预占应已释放'
    assert inv['actual_quantity'] == 200, '实际库存不应变化'
    print('  OK 库存已恢复')

    print('\n3. 验证启动清理产生的审计日志...')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    startup_audits = conn.execute(
        "SELECT * FROM audit_logs WHERE action = 'reservation_expired' "
        "AND details LIKE '%startup_cleanup%' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchall()
    conn.close()
    print(f'  startup_cleanup 审计数: {len(startup_audits)}')
    assert len(startup_audits) >= 1
    details = json.loads(startup_audits[0]['details'])
    print(f'  详情: reservation_id={details["reservation_id"]}, '
          f'quantity={details["quantity"]}, reason={details["reason"]}')
    assert details['reservation_id'] == 3
    assert details['quantity'] == 10
    assert details['reason'] == 'startup_cleanup'
    assert 'expired_at' in details
    assert 'released_at' in details
    print('  OK 启动清理审计完整')

    print('\n4. 验证所有过期预占都已释放...')
    conn = sqlite3.connect(DB_PATH)
    unreleased = conn.execute(
        "SELECT COUNT(*) FROM reservations WHERE is_released = 0"
    ).fetchone()[0]
    total_reservations = conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
    conn.close()
    print(f'  总预占数: {total_reservations}, 未释放: {unreleased}')
    assert unreleased == 0
    print('  OK 所有预占已释放')

    print('\n5. 验证手动清理幂等性（重启后再清理）...')
    code, result = api_request('/api/reservations/cleanup', 'POST')
    assert code == 200
    print(f'  手动清理结果: cleaned_count={result["cleaned_count"]}')
    assert result['cleaned_count'] == 0

    after_count = count_audit_by_action('reservation_expired')
    print(f'  过期释放审计总数: {after_count}')
    assert after_count == 3  # manual_cleanup + approve_check + startup_cleanup
    print('  OK 幂等性验证通过')

    print('\n6. 验证 JSON 导出...')
    req = urllib.request.Request(BASE + '/api/audit/export.json')
    with urllib.request.urlopen(req) as resp:
        json_data = json.loads(resp.read().decode('utf-8'))
    print(f'  JSON 总条数: {len(json_data)}')
    expired_records = [a for a in json_data if a['action'] == 'reservation_expired']
    print(f'  reservation_expired 记录: {len(expired_records)} 条')
    for r in expired_records:
        assert isinstance(r['details'], dict)
        assert 'reservation_id' in r['details']
        assert 'quantity' in r['details']
        assert 'reason' in r['details']
    print('  OK JSON 导出正确')

    print('\n7. 验证 CSV 导出...')
    req = urllib.request.Request(BASE + '/api/audit/export.csv')
    with urllib.request.urlopen(req) as resp:
        csv_text = resp.read().decode('utf-8-sig')
    csv_lines = csv_text.strip().split('\n')
    csv_count = len(csv_lines) - 1
    print(f'  CSV 数据条数: {csv_count}')
    assert csv_count == len(json_data)
    print('  OK CSV 与 JSON 一致')

    print('\n8. 验证可用库存计算正确...')
    code, invs = api_request('/api/inventory')
    for inv in invs:
        expected = inv['actual_quantity'] - inv['reserved_quantity'] - inv['safety_stock']
        assert inv['available_quantity'] == expected, \
            f"{inv['material_name']} 可用库存计算错误"
    print('  OK 可用库存计算正确')

    print('\n' + '='*60)
    print('OK 重启后过期预占处理验证全部通过！')
    print('='*60)
    print()
    print('三条过期释放链路验证:')
    print('  1. manual_cleanup   - 手动清理接口 ✓')
    print('  2. approve_check     - 批准前检查 ✓')
    print('  3. startup_cleanup   - 启动时清理 ✓')
    print('  (后台 background_worker 每分钟自动清理)')

if __name__ == '__main__':
    main()
