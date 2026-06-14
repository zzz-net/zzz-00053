import urllib.request
import json
import sys

BASE = 'http://127.0.0.1:5000'

def request(path):
    with urllib.request.urlopen(BASE + path) as resp:
        return resp.read().decode('utf-8')

def load_json(filename):
    with open(filename, 'r', encoding='utf-8') as f:
        return json.load(f)

def normalize_audit(audits):
    for a in audits:
        if 'created_at' in a:
            del a['created_at']
        if 'details' in a and isinstance(a['details'], str):
            try:
                a['details'] = json.loads(a['details'])
            except:
                pass
    return audits

def main():
    print('='*60)
    print('服务重启后数据一致性验证')
    print('='*60)

    try:
        after_inv = json.loads(request('/api/inventory'))
        after_stats = json.loads(request('/api/stats'))
        after_audit_raw = json.loads(request('/api/audit/export.json'))

        before_inv = load_json('before_reboot_inventory.json')
        before_stats = load_json('before_reboot_stats.json')
        before_audit_raw = load_json('before_reboot_audit.json')
    except FileNotFoundError as e:
        print(f'错误: 找不到前置文件 {e}')
        print('请先运行 test_all.py 生成快照')
        sys.exit(1)
    except Exception as e:
        print(f'错误: {e}')
        sys.exit(1)

    print('\n1. 验证库存一致性...')
    inv_match = True
    for i, (b, a) in enumerate(zip(before_inv, after_inv)):
        for key in ['warehouse_id', 'material_id', 'actual_quantity', 'reserved_quantity', 'safety_stock']:
            if b[key] != a[key]:
                print(f'  FAIL 库存项 {i} 不匹配: {key}: {b[key]} vs {a[key]}')
                inv_match = False
    if inv_match:
        print('  OK 库存数据完全一致')

    print('\n2. 验证统计一致性...')
    stats_match = True
    for key in before_stats:
        if before_stats[key] != after_stats[key]:
            print(f'  FAIL 统计 {key} 不匹配: {before_stats[key]} vs {after_stats[key]}')
            stats_match = False
    if stats_match:
        print('  OK 统计数据完全一致')

    print('\n3. 验证审计日志一致性...')
    before_audit = normalize_audit(before_audit_raw)
    after_audit = normalize_audit(after_audit_raw)

    if len(before_audit) != len(after_audit):
        print(f'  FAIL 审计日志条数不匹配: {len(before_audit)} vs {len(after_audit)}')
    else:
        audit_match = True
        for i, (b, a) in enumerate(zip(before_audit, after_audit)):
            for key in ['id', 'order_id', 'action', 'operator_id', 'details']:
                if b.get(key) != a.get(key):
                    print(f'  FAIL 审计 {i} 不匹配: {key}: {b.get(key)} vs {a.get(key)}')
                    audit_match = False
        if audit_match:
            print(f'  OK {len(after_audit)} 条审计日志完全一致')

    print('\n4. 验证预占状态...')
    reserved_total_before = sum(i['reserved_quantity'] for i in before_inv)
    reserved_total_after = sum(i['reserved_quantity'] for i in after_inv)
    if reserved_total_before == reserved_total_after:
        print(f'  OK 总预占数量一致: {reserved_total_before}')
    else:
        print(f'  FAIL 总预占数量不一致: {reserved_total_before} vs {reserved_total_after}')

    print('\n' + '='*60)
    if inv_match and stats_match and audit_match and reserved_total_before == reserved_total_after:
        print('OK 所有验证通过！服务重启后数据完全一致。')
    else:
        print('FAIL 部分验证失败，请检查上述错误。')
        sys.exit(1)

if __name__ == '__main__':
    main()
