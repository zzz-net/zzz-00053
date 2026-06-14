import urllib.request
import urllib.parse
import json
import time

BASE = 'http://127.0.0.1:5000'

def request(path, method='GET', data=None):
    url = BASE + path
    headers = {'Content-Type': 'application/json'}
    body = json.dumps(data, ensure_ascii=False).encode('utf-8') if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.getcode(), json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))

def pprint(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))

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

def test_basic_queries():
    print('--- 查询仓库 ---')
    code, data = request('/api/warehouses')
    assert code == 200
    assert len(data) == 3
    pprint(data)

    print('--- 查询物资 ---')
    code, data = request('/api/materials')
    assert code == 200
    assert len(data) == 5
    pprint(data)

    print('--- 查询用户 ---')
    code, data = request('/api/users')
    assert code == 200
    assert len(data) == 4
    pprint(data)

    print('--- 查询库存 ---')
    code, data = request('/api/inventory')
    assert code == 200
    pprint(data)

    inv_map = {(i['warehouse_id'], i['material_id']): i for i in data}
    assert inv_map[(1, 1)]['actual_quantity'] == 500
    assert inv_map[(1, 1)]['safety_stock'] == 50
    assert inv_map[(1, 1)]['available_quantity'] == 450

def test_success_flow():
    print('--- 步骤1: 创建调拨草稿 ---')
    code, order = request('/api/orders', 'POST', {
        'requester_id': 1,
        'source_warehouse_id': 1,
        'target_warehouse_id': 2,
        'material_id': 1,
        'quantity': 100,
        'remark': '应急物资调拨'
    })
    assert code == 201
    assert order['status'] == 'draft'
    order_id = order['id']
    pprint(order)

    print('--- 步骤2: 提交并预占 ---')
    code, data = request(f'/api/orders/{order_id}/submit', 'POST', {'operator_id': 1})
    assert code == 200
    assert data['status'] == 'reserved'
    assert 'reservation_id' in data
    pprint(data)

    print('--- 验证: 预占后库存 ---')
    code, invs = request('/api/inventory')
    inv_map = {(i['warehouse_id'], i['material_id']): i for i in invs}
    assert inv_map[(1, 1)]['reserved_quantity'] == 100
    assert inv_map[(1, 1)]['available_quantity'] == 350
    print(f"中心仓库口罩: 实际={inv_map[(1,1)]['actual_quantity']}, 预占={inv_map[(1,1)]['reserved_quantity']}, 可用={inv_map[(1,1)]['available_quantity']}")

    print('--- 步骤3: 审批人批准 ---')
    code, data = request(f'/api/orders/{order_id}/approve', 'POST', {'operator_id': 3})
    assert code == 200
    assert data['status'] == 'approved'
    pprint(data)

    print('--- 步骤4: 执行出库 ---')
    code, data = request(f'/api/orders/{order_id}/outbound', 'POST', {'operator_id': 3})
    assert code == 200
    assert data['status'] == 'completed'
    pprint(data)

    print('--- 验证: 出库后库存 ---')
    code, invs = request('/api/inventory')
    inv_map = {(i['warehouse_id'], i['material_id']): i for i in invs}
    assert inv_map[(1, 1)]['actual_quantity'] == 400
    assert inv_map[(1, 1)]['reserved_quantity'] == 0
    assert inv_map[(1, 1)]['available_quantity'] == 350
    assert inv_map[(2, 1)]['actual_quantity'] == 300
    print(f"中心仓库口罩: 实际={inv_map[(1,1)]['actual_quantity']}, 预占={inv_map[(1,1)]['reserved_quantity']}")
    print(f"城东分仓口罩: 实际={inv_map[(2,1)]['actual_quantity']}")

    print('--- 步骤5: 导出审计日志 ---')
    req = urllib.request.Request(BASE + '/api/audit/export.json')
    with urllib.request.urlopen(req) as resp:
        audit_json = json.loads(resp.read().decode('utf-8'))
    print(f'JSON 审计日志条数: {len(audit_json)}')
    assert len(audit_json) >= 5

    req = urllib.request.Request(BASE + '/api/audit/export.csv')
    with urllib.request.urlopen(req) as resp:
        audit_csv = resp.read().decode('utf-8-sig')
    lines = audit_csv.strip().split('\n')
    print(f'CSV 审计日志行数: {len(lines)-1} 条数据')
    assert len(lines) >= 5

def test_negative_quantity():
    print('--- 测试零数量 ---')
    code, data = request('/api/orders', 'POST', {
        'requester_id': 1,
        'source_warehouse_id': 1,
        'target_warehouse_id': 2,
        'material_id': 1,
        'quantity': 0
    })
    assert code == 400
    assert '大于0' in data['error']
    pprint(data)

    print('--- 测试负数 ---')
    code, data = request('/api/orders', 'POST', {
        'requester_id': 1,
        'source_warehouse_id': 1,
        'target_warehouse_id': 2,
        'material_id': 1,
        'quantity': -10
    })
    assert code == 400
    assert '大于0' in data['error']
    pprint(data)

def test_insufficient_stock():
    print('--- 创建大数量订单 ---')
    code, order = request('/api/orders', 'POST', {
        'requester_id': 1,
        'source_warehouse_id': 1,
        'target_warehouse_id': 2,
        'material_id': 1,
        'quantity': 1000
    })
    assert code == 201
    order_id = order['id']

    print('--- 提交时库存不足 ---')
    code, data = request(f'/api/orders/{order_id}/submit', 'POST', {'operator_id': 1})
    assert code == 400
    assert data['error'] == '库存不足'
    assert data['available'] < data['requested']
    pprint(data)

def test_no_permission_approve():
    print('--- 创建订单并提交 ---')
    code, order = request('/api/orders', 'POST', {
        'requester_id': 1,
        'source_warehouse_id': 1,
        'target_warehouse_id': 2,
        'material_id': 2,
        'quantity': 10
    })
    order_id = order['id']
    request(f'/api/orders/{order_id}/submit', 'POST', {'operator_id': 1})

    print('--- 申请人尝试审批（无权限）---')
    code, data = request(f'/api/orders/{order_id}/approve', 'POST', {'operator_id': 1})
    assert code == 403
    assert 'approver' in data['error']
    pprint(data)

    return order_id

def test_duplicate_approve(order_id):
    print('--- 第一次审批（成功）---')
    code, data = request(f'/api/orders/{order_id}/approve', 'POST', {'operator_id': 3})
    assert code == 200
    pprint(data)

    print('--- 第二次审批（重复）---')
    code, data = request(f'/api/orders/{order_id}/approve', 'POST', {'operator_id': 3})
    assert code == 400
    assert '重复审批' in data['error']
    pprint(data)

def test_contention():
    print('--- 查看消毒液初始库存 ---')
    code, invs = request('/api/inventory')
    inv_map = {(i['warehouse_id'], i['material_id']): i for i in invs}
    print(f"消毒液: 实际={inv_map[(1,3)]['actual_quantity']}, 安全={inv_map[(1,3)]['safety_stock']}, 可用={inv_map[(1,3)]['available_quantity']}")
    assert inv_map[(1, 3)]['available_quantity'] == 260

    print('--- 申请人1 创建订单A (250桶) ---')
    code, order_a = request('/api/orders', 'POST', {
        'requester_id': 1,
        'source_warehouse_id': 1,
        'target_warehouse_id': 3,
        'material_id': 3,
        'quantity': 250
    })
    order_a_id = order_a['id']
    print(f'订单A ID: {order_a_id}')

    print('--- 申请人2 创建订单B (250桶) ---')
    code, order_b = request('/api/orders', 'POST', {
        'requester_id': 2,
        'source_warehouse_id': 1,
        'target_warehouse_id': 2,
        'material_id': 3,
        'quantity': 250
    })
    order_b_id = order_b['id']
    print(f'订单B ID: {order_b_id}')

    print('--- 申请人1 提交订单A (成功) ---')
    code, data = request(f'/api/orders/{order_a_id}/submit', 'POST', {'operator_id': 1})
    assert code == 200
    assert data['status'] == 'reserved'
    pprint(data)

    print('--- 验证: 订单A预占后库存 ---')
    code, invs = request('/api/inventory')
    inv_map = {(i['warehouse_id'], i['material_id']): i for i in invs}
    assert inv_map[(1, 3)]['reserved_quantity'] == 250
    assert inv_map[(1, 3)]['available_quantity'] == 10
    print(f"消毒液: 实际={inv_map[(1,3)]['actual_quantity']}, 预占={inv_map[(1,3)]['reserved_quantity']}, 可用={inv_map[(1,3)]['available_quantity']}")

    print('--- 申请人2 提交订单B (失败，库存不足) ---')
    code, data = request(f'/api/orders/{order_b_id}/submit', 'POST', {'operator_id': 2})
    assert code == 400
    assert data['error'] == '库存不足'
    assert data['available'] == 10
    pprint(data)

    print('--- 验证: 库存未超扣 ---')
    code, invs = request('/api/inventory')
    inv_map = {(i['warehouse_id'], i['material_id']): i for i in invs}
    assert inv_map[(1, 3)]['actual_quantity'] == 300
    assert inv_map[(1, 3)]['reserved_quantity'] == 250
    print('OK 库存正确，未被超扣')

    print('--- 撤回订单A，释放预占 ---')
    code, data = request(f'/api/orders/{order_a_id}/withdraw', 'POST', {'operator_id': 1})
    assert code == 200
    assert data['status'] == 'withdrawn'
    pprint(data)

    print('--- 验证: 撤回后库存恢复 ---')
    code, invs = request('/api/inventory')
    inv_map = {(i['warehouse_id'], i['material_id']): i for i in invs}
    assert inv_map[(1, 3)]['reserved_quantity'] == 0
    assert inv_map[(1, 3)]['available_quantity'] == 260
    print(f"消毒液: 实际={inv_map[(1,3)]['actual_quantity']}, 预占={inv_map[(1,3)]['reserved_quantity']}, 可用={inv_map[(1,3)]['available_quantity']}")

    print('--- 订单B现在可以提交了 ---')
    code, data = request(f'/api/orders/{order_b_id}/submit', 'POST', {'operator_id': 2})
    assert code == 200
    assert data['status'] == 'reserved'
    pprint(data)

    print('--- 驳回订单B ---')
    code, data = request(f'/api/orders/{order_b_id}/reject', 'POST', {
        'operator_id': 3,
        'reason': '申请数量过大'
    })
    assert code == 200
    assert data['status'] == 'rejected'
    pprint(data)

    print('--- 验证: 驳回后库存释放 ---')
    code, invs = request('/api/inventory')
    inv_map = {(i['warehouse_id'], i['material_id']): i for i in invs}
    assert inv_map[(1, 3)]['reserved_quantity'] == 0
    assert inv_map[(1, 3)]['available_quantity'] == 260
    print(f"消毒液: 实际={inv_map[(1,3)]['actual_quantity']}, 预占={inv_map[(1,3)]['reserved_quantity']}, 可用={inv_map[(1,3)]['available_quantity']}")

def test_stats_and_final_audit():
    print('--- 查看统计 ---')
    code, stats = request('/api/stats')
    pprint(stats)

    print('--- 查看所有审计日志 ---')
    code, audits = request('/api/audit')
    print(f'总审计日志数: {len(audits)}')
    for a in audits[:5]:
        print(f"  [{a['id']}] {a['action']} by {a['username']}({a['role']})")

    print('\n--- 导出最终审计 JSON ---')
    req = urllib.request.Request(BASE + '/api/audit/export.json')
    with urllib.request.urlopen(req) as resp:
        audit_data = json.loads(resp.read().decode('utf-8'))
    with open('final_audit.json', 'w', encoding='utf-8') as f:
        json.dump(audit_data, f, ensure_ascii=False, indent=2)
    print(f'已导出 {len(audit_data)} 条审计日志到 final_audit.json')

    print('\n--- 导出最终审计 CSV ---')
    req = urllib.request.Request(BASE + '/api/audit/export.csv')
    with urllib.request.urlopen(req) as resp:
        csv_data = resp.read().decode('utf-8-sig')
    with open('final_audit.csv', 'w', encoding='utf-8-sig') as f:
        f.write(csv_data)
    print(f'已导出 CSV 到 final_audit.csv')

    return audit_data

def save_state():
    print('\n--- 保存重启前状态快照 ---')
    code, invs = request('/api/inventory')
    with open('before_reboot_inventory.json', 'w', encoding='utf-8') as f:
        json.dump(invs, f, ensure_ascii=False, indent=2)

    code, stats = request('/api/stats')
    with open('before_reboot_stats.json', 'w', encoding='utf-8') as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    req = urllib.request.Request(BASE + '/api/audit/export.json')
    with urllib.request.urlopen(req) as resp:
        audits = json.loads(resp.read().decode('utf-8'))
    with open('before_reboot_audit.json', 'w', encoding='utf-8') as f:
        json.dump(audits, f, ensure_ascii=False, indent=2)

    print('已保存: before_reboot_inventory.json, before_reboot_stats.json, before_reboot_audit.json')

if __name__ == '__main__':
    print('等待服务启动...')
    time.sleep(2)

    try:
        test('基础查询', test_basic_queries)
        test('成功路径: 完整调拨流程', test_success_flow)
        test('失败路径: 零数量/负数', test_negative_quantity)
        test('失败路径: 库存不足', test_insufficient_stock)
        order_id = test_no_permission_approve()
        test('失败路径: 重复审批', lambda: test_duplicate_approve(order_id))
        test('库存争抢测试', test_contention)
        test('导出审计日志', test_stats_and_final_audit)
        save_state()

        print('\n' + '='*60)
        print('所有测试通过！')
        print('='*60)
        print('\n请手动重启服务后运行 verify_reboot.py 验证数据一致性')
    except Exception as e:
        print(f'\n测试中断: {e}')
        import traceback
        traceback.print_exc()
