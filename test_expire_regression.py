import urllib.request
import json
import sqlite3
import time
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

def set_expired(reservation_id):
    conn = sqlite3.connect(DB_PATH)
    past_time = '2020-01-01T00:00:00.000000'
    conn.execute('UPDATE reservations SET expires_at = ? WHERE id = ?',
                 (past_time, reservation_id))
    conn.commit()
    conn.close()

def get_inventory(warehouse_id, material_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM inventory WHERE warehouse_id = ? AND material_id = ?',
                       (warehouse_id, material_id)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_order(order_id):
    code, data = api_request(f'/api/orders/{order_id}')
    return data

def count_audit_by_action(action):
    conn = sqlite3.connect(DB_PATH)
    c = conn.execute('SELECT COUNT(*) FROM audit_logs WHERE action = ?', (action,)).fetchone()
    conn.close()
    return c[0]

def test(name, func):
    print(f'\n{"="*60}')
    print(f'测试: {name}')
    print('='*60)
    try:
        func()
        print(f'OK  {name} - PASS')
    except AssertionError as e:
        print(f'FAIL {name} - FAIL: {e}')
        raise
    except Exception as e:
        print(f'ERR  {name} - ERROR: {e}')
        raise

def test_manual_cleanup_expired():
    print('--- 步骤1: 创建并提交调拨单（预占）---')
    code, order = api_request('/api/orders', 'POST', {
        'requester_id': 1,
        'source_warehouse_id': 1,
        'target_warehouse_id': 2,
        'material_id': 4,
        'quantity': 50,
        'remark': '过期测试订单'
    })
    assert code == 201, f'创建订单失败: {code}'
    order_id = order['id']
    print(f'订单 ID: {order_id}')

    code, result = api_request(f'/api/orders/{order_id}/submit', 'POST', {'operator_id': 1})
    assert code == 200, f'提交失败: {code}'
    assert result['status'] == 'reserved'
    reservation_id = result['reservation_id']
    print(f'预占 ID: {reservation_id}')

    inv_before = get_inventory(1, 4)
    print(f'预占后库存: actual={inv_before["actual_quantity"]}, reserved={inv_before["reserved_quantity"]}')
    assert inv_before['reserved_quantity'] == 50

    audit_before = count_audit_by_action('reservation_expired')
    print(f'过期释放审计数(清理前): {audit_before}')

    print('--- 步骤2: 把预占过期时间改成过去 ---')
    set_expired(reservation_id)
    print('已设置过期时间为 2020-01-01')

    print('--- 步骤3: 第一次手动清理 ---')
    code, result = api_request('/api/reservations/cleanup', 'POST')
    assert code == 200, f'清理接口返回 {code}, 预期 200'
    assert result['cleaned_count'] == 1, f'预期清理1条, 实际 {result["cleaned_count"]}'
    print(f'清理结果: {result}')

    print('--- 步骤4: 验证订单状态 ---')
    order_after = get_order(order_id)
    assert order_after['status'] == 'expired', f'订单状态应为 expired, 实际 {order_after["status"]}'
    assert order_after['reservation_id'] is None, 'reservation_id 应为空'
    print(f'订单状态: {order_after["status"]}')

    print('--- 步骤5: 验证库存恢复 ---')
    inv_after = get_inventory(1, 4)
    print(f'清理后库存: actual={inv_after["actual_quantity"]}, reserved={inv_after["reserved_quantity"]}')
    assert inv_after['reserved_quantity'] == 0, f'预占库存应归零, 实际 {inv_after["reserved_quantity"]}'
    assert inv_after['actual_quantity'] == 1000, '实际库存不应变化'

    print('--- 步骤6: 验证审计日志 ---')
    audit_after = count_audit_by_action('reservation_expired')
    print(f'过期释放审计数(清理后): {audit_after}')
    assert audit_after == audit_before + 1

    code, audits = api_request(f'/api/audit?order_id={order_id}')
    expired_audits = [a for a in audits if a['action'] == 'reservation_expired']
    assert len(expired_audits) == 1
    details = expired_audits[0]['details']
    assert details['reservation_id'] == reservation_id
    assert details['quantity'] == 50
    assert 'expired_at' in details
    assert 'released_at' in details
    assert details['reason'] == 'manual_cleanup'
    print('审计日志验证通过: 含 reservation_id、quantity、expired_at、released_at、reason')

    print('--- 步骤7: 第二次清理（幂等性验证）---')
    code, result2 = api_request('/api/reservations/cleanup', 'POST')
    assert code == 200
    assert result2['cleaned_count'] == 0, '重复清理应返回0'
    print(f'第二次清理结果: {result2}')

    inv_idempotent = get_inventory(1, 4)
    assert inv_idempotent['reserved_quantity'] == 0, '重复清理不应扣成负数'
    assert inv_idempotent['actual_quantity'] == 1000

    audit_idempotent = count_audit_by_action('reservation_expired')
    assert audit_idempotent == audit_after, '重复清理不应重复写审计'
    print(f'幂等性验证通过: 库存={inv_idempotent["reserved_quantity"]}, 审计数={audit_idempotent}')

def test_approve_expired():
    print('--- 创建预占订单 ---')
    code, order = api_request('/api/orders', 'POST', {
        'requester_id': 2,
        'source_warehouse_id': 1,
        'target_warehouse_id': 3,
        'material_id': 5,
        'quantity': 10
    })
    order_id = order['id']

    code, result = api_request(f'/api/orders/{order_id}/submit', 'POST', {'operator_id': 2})
    reservation_id = result['reservation_id']
    print(f'订单 {order_id}, 预占 {reservation_id}')

    print('--- 设置过期 ---')
    set_expired(reservation_id)

    print('--- 尝试审批过期订单 ---')
    code, result = api_request(f'/api/orders/{order_id}/approve', 'POST', {'operator_id': 3})
    assert code == 400, f'预期 400, 实际 {code}'
    assert '过期' in result['error'] or 'expired' in result['error'].lower() or '重新提交' in result['error']
    print(f'审批被拒绝: {result["error"]}')

    print('--- 验证订单已自动标记为 expired ---')
    order_after = get_order(order_id)
    assert order_after['status'] == 'expired'
    print(f'订单状态: {order_after["status"]}')

    print('--- 验证库存已释放 ---')
    inv = get_inventory(1, 5)
    assert inv['reserved_quantity'] == 0
    print(f'预占库存已释放: {inv["reserved_quantity"]}')

    audit_expired = count_audit_by_action('reservation_expired')
    print(f'累计过期释放审计: {audit_expired}')
    assert audit_expired >= 2

def test_json_csv_export_consistency():
    print('--- 导出 JSON ---')
    req = urllib.request.Request(BASE + '/api/audit/export.json')
    with urllib.request.urlopen(req) as resp:
        json_data = json.loads(resp.read().decode('utf-8'))
    print(f'JSON 条数: {len(json_data)}')

    print('--- 导出 CSV ---')
    req = urllib.request.Request(BASE + '/api/audit/export.csv')
    with urllib.request.urlopen(req) as resp:
        csv_text = resp.read().decode('utf-8-sig')
    csv_lines = csv_text.strip().split('\n')
    csv_data_count = len(csv_lines) - 1
    print(f'CSV 数据条数: {csv_data_count}')

    assert len(json_data) == csv_data_count, 'JSON 和 CSV 条数不一致'
    print('OK JSON 和 CSV 导出条数一致')

    expired_json = [a for a in json_data if a['action'] == 'reservation_expired']
    assert len(expired_json) >= 2
    print(f'JSON 中 reservation_expired 记录: {len(expired_json)} 条')

    for a in expired_json:
        assert isinstance(a['details'], dict)
        assert 'reservation_id' in a['details']
        assert 'quantity' in a['details']
        assert 'reason' in a['details']
    print('审计详情字段完整')

def save_reboot_state():
    print('\n--- 保存重启前状态 ---')
    req = urllib.request.Request(BASE + '/api/audit/export.json')
    with urllib.request.urlopen(req) as resp:
        audit_json = json.loads(resp.read().decode('utf-8'))
    with open('expire_test_before_audit.json', 'w', encoding='utf-8') as f:
        json.dump(audit_json, f, ensure_ascii=False, indent=2)

    inv_before = get_inventory(1, 4)
    with open('expire_test_before_inv.json', 'w', encoding='utf-8') as f:
        json.dump(inv_before, f, ensure_ascii=False, indent=2)

    stats_code, stats_data = api_request('/api/stats')
    with open('expire_test_before_stats.json', 'w', encoding='utf-8') as f:
        json.dump(stats_data, f, ensure_ascii=False, indent=2)

    expired_count_before = count_audit_by_action('reservation_expired')
    with open('expire_test_meta.json', 'w', encoding='utf-8') as f:
        json.dump({'expired_audit_count': expired_count_before}, f, ensure_ascii=False)

    print(f'过期释放审计数: {expired_count_before}')
    print('状态已保存')

if __name__ == '__main__':
    print('等待服务启动...')
    time.sleep(2)

    try:
        test('手动清理过期预占 + 幂等性', test_manual_cleanup_expired)
        test('批准前过期自动释放', test_approve_expired)
        test('JSON/CSV 审计导出一致性', test_json_csv_export_consistency)
        save_reboot_state()

        print('\n' + '='*60)
        print('过期预占回归测试通过！')
        print('='*60)
        print('\n请重启服务后运行 verify_expire_reboot.py 验证重启清理')
    except Exception as e:
        print(f'\n测试失败: {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)
