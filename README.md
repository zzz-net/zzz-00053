# 应急物资调拨 JSON API

基于 Python + Flask + SQLite 的本地应急物资调拨管理系统。

## 功能特性

- 仓库、物资、库存管理（含安全库存）
- 调拨单完整状态流转：草稿 → 提交 → 预占 → 批准 → 出库 / 驳回 / 撤回
- 预占自动过期释放（默认 30 分钟）
- 审批、出库、释放预占全链路审计日志
- 审计日志导出为 JSON 或 CSV
- 库存争抢保护：预占 + 实际库存双层校验

## 快速启动

```bash
# 安装依赖
python -m pip install flask

# 启动服务（自动创建数据库和样例数据）
python app.py
```

服务地址：`http://127.0.0.1:5000`

## 预置数据

### 用户
| ID | 用户名 | 角色 |
|----|--------|------|
| 1 | requester1 | 申请人 |
| 2 | requester2 | 申请人 |
| 3 | approver1 | 审批人 |
| 4 | approver2 | 审批人 |

### 仓库
| ID | 名称 | 位置 |
|----|------|------|
| 1 | 中心仓库 | 城市中心A区 |
| 2 | 城东分仓 | 城市东区B点 |
| 3 | 城西分仓 | 城市西区C点 |

### 物资
| ID | 名称 | 单位 |
|----|------|------|
| 1 | 医用口罩 | 箱 |
| 2 | 防护服 | 套 |
| 3 | 消毒液 | 桶 |
| 4 | 应急食品 | 箱 |
| 5 | 急救包 | 个 |

### 初始库存（中心仓库 ID=1）
| 物资 | 实际库存 | 安全库存 | 可用库存 |
|------|----------|----------|----------|
| 医用口罩 | 500 | 50 | 450 |
| 防护服 | 200 | 30 | 170 |
| 消毒液 | 300 | 40 | 260 |
| 应急食品 | 1000 | 100 | 900 |
| 急救包 | 150 | 20 | 130 |

可用库存 = 实际库存 - 预占库存 - 安全库存

## API 接口

### 基础查询
```bash
# 查询仓库
curl http://127.0.0.1:5000/api/warehouses

# 查询物资
curl http://127.0.0.1:5000/api/materials

# 查询用户
curl http://127.0.0.1:5000/api/users

# 查询库存
curl http://127.0.0.1:5000/api/inventory

# 查询调拨单列表
curl http://127.0.0.1:5000/api/orders

# 查询指定调拨单
curl http://127.0.0.1:5000/api/orders/1

# 查询审计日志
curl http://127.0.0.1:5000/api/audit
curl http://127.0.0.1:5000/api/audit?order_id=1

# 查询统计
curl http://127.0.0.1:5000/api/stats
```

---

## 完整可复现 curl 链路

### 前置检查：查看初始库存

```bash
curl -s http://127.0.0.1:5000/api/inventory | python -m json.tool
```

预期：中心仓库(1) 医用口罩(1) 实际500，可用450

---

### 成功路径：完整调拨流程

#### 步骤 1：创建调拨草稿（申请人 requester1）

```bash
# 申请从中心仓库调100箱口罩到城东分仓
ORDER=$(curl -s -X POST http://127.0.0.1:5000/api/orders \
  -H "Content-Type: application/json" \
  -d '{
    "requester_id": 1,
    "source_warehouse_id": 1,
    "target_warehouse_id": 2,
    "material_id": 1,
    "quantity": 100,
    "remark": "应急物资调拨"
  }')

echo $ORDER | python -m json.tool
ORDER_ID=$(echo $ORDER | python -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "创建的调拨单 ID: $ORDER_ID"
```

#### 步骤 2：提交并预占库存（同一申请人）

```bash
curl -s -X POST http://127.0.0.1:5000/api/orders/$ORDER_ID/submit \
  -H "Content-Type: application/json" \
  -d '{"operator_id": 1}' | python -m json.tool
```

预期：状态变为 `reserved`，返回预占 ID 和过期时间

#### 验证：查看库存变化

```bash
curl -s http://127.0.0.1:5000/api/inventory | python -m json.tool
```

预期：中心仓库 医用口罩 `reserved_quantity=100`，可用库存变为 350

#### 步骤 3：审批人批准（approver1）

```bash
curl -s -X POST http://127.0.0.1:5000/api/orders/$ORDER_ID/approve \
  -H "Content-Type: application/json" \
  -d '{"operator_id": 3}' | python -m json.tool
```

预期：状态变为 `approved`

#### 步骤 4：执行出库（审批人操作）

```bash
curl -s -X POST http://127.0.0.1:5000/api/orders/$ORDER_ID/outbound \
  -H "Content-Type: application/json" \
  -d '{"operator_id": 3}' | python -m json.tool
```

预期：状态变为 `completed`

#### 验证：出库后库存

```bash
curl -s http://127.0.0.1:5000/api/inventory | python -m json.tool
```

预期：
- 中心仓库(1) 医用口罩：实际库存 500-100=400，预占清零，可用 400-50=350
- 城东分仓(2) 医用口罩：实际库存 200+100=300

#### 步骤 5：导出审计日志

```bash
# JSON 格式
curl -s http://127.0.0.1:5000/api/audit/export.json > audit_logs.json
python -m json.tool audit_logs.json

# CSV 格式
curl -s http://127.0.0.1:5000/api/audit/export.csv > audit_logs.csv
cat audit_logs.csv
```

---

### 失败路径覆盖

#### 1. 零数量或负数

```bash
# 零数量
curl -s -X POST http://127.0.0.1:5000/api/orders \
  -H "Content-Type: application/json" \
  -d '{
    "requester_id": 1,
    "source_warehouse_id": 1,
    "target_warehouse_id": 2,
    "material_id": 1,
    "quantity": 0
  }' | python -m json.tool

# 负数
curl -s -X POST http://127.0.0.1:5000/api/orders \
  -H "Content-Type: application/json" \
  -d '{
    "requester_id": 1,
    "source_warehouse_id": 1,
    "target_warehouse_id": 2,
    "material_id": 1,
    "quantity": -10
  }' | python -m json.tool
```

预期：都返回 400 错误 `调拨数量必须大于0`

#### 2. 库存不足

```bash
# 申请调 1000 箱，远超可用 350
curl -s -X POST http://127.0.0.1:5000/api/orders \
  -H "Content-Type: application/json" \
  -d '{
    "requester_id": 1,
    "source_warehouse_id": 1,
    "target_warehouse_id": 2,
    "material_id": 1,
    "quantity": 1000
  }' > /tmp/order_big.json

BIG_ORDER_ID=$(cat /tmp/order_big.json | python -c "import sys,json; print(json.load(sys.stdin)['id'])")

# 提交时失败
curl -s -X POST http://127.0.0.1:5000/api/orders/$BIG_ORDER_ID/submit \
  -H "Content-Type: application/json" \
  -d '{"operator_id": 1}' | python -m json.tool
```

预期：返回 400 错误，显示可用量和申请量

#### 3. 无权限审批

```bash
# 创建并提交一个调拨单
TEST_ORDER=$(curl -s -X POST http://127.0.0.1:5000/api/orders \
  -H "Content-Type: application/json" \
  -d '{
    "requester_id": 1,
    "source_warehouse_id": 1,
    "target_warehouse_id": 2,
    "material_id": 2,
    "quantity": 10
  }')
TEST_ORDER_ID=$(echo $TEST_ORDER | python -c "import sys,json; print(json.load(sys.stdin)['id'])")

curl -s -X POST http://127.0.0.1:5000/api/orders/$TEST_ORDER_ID/submit \
  -H "Content-Type: application/json" \
  -d '{"operator_id": 1}' > /dev/null

# 用申请人角色去审批（应该失败）
curl -s -X POST http://127.0.0.1:5000/api/orders/$TEST_ORDER_ID/approve \
  -H "Content-Type: application/json" \
  -d '{"operator_id": 1}' | python -m json.tool
```

预期：返回 403 错误 `需要approver角色权限`

#### 4. 重复审批

```bash
# 先正常审批一次
curl -s -X POST http://127.0.0.1:5000/api/orders/$TEST_ORDER_ID/approve \
  -H "Content-Type: application/json" \
  -d '{"operator_id": 3}' | python -m json.tool

# 再次审批（失败）
curl -s -X POST http://127.0.0.1:5000/api/orders/$TEST_ORDER_ID/approve \
  -H "Content-Type: application/json" \
  -d '{"operator_id": 3}' | python -m json.tool
```

预期第二次返回：`调拨单已审批，请勿重复审批`

#### 5. 两个申请争抢最后库存

```bash
# 先查看消毒液库存：中心仓库实际300，安全40，可用260
curl -s http://127.0.0.1:5000/api/inventory | python -m json.tool | grep -A10 '"material_id": 3'

# 申请人1 申请 250 桶消毒液
ORDER_A=$(curl -s -X POST http://127.0.0.1:5000/api/orders \
  -H "Content-Type: application/json" \
  -d '{
    "requester_id": 1,
    "source_warehouse_id": 1,
    "target_warehouse_id": 3,
    "material_id": 3,
    "quantity": 250
  }')
ORDER_A_ID=$(echo $ORDER_A | python -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "订单A ID: $ORDER_A_ID"

# 申请人2 也申请 250 桶消毒液
ORDER_B=$(curl -s -X POST http://127.0.0.1:5000/api/orders \
  -H "Content-Type: application/json" \
  -d '{
    "requester_id": 2,
    "source_warehouse_id": 1,
    "target_warehouse_id": 2,
    "material_id": 3,
    "quantity": 250
  }')
ORDER_B_ID=$(echo $ORDER_B | python -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "订单B ID: $ORDER_B_ID"

# 申请人1 先提交预占 - 成功
echo "=== 订单A提交 ==="
curl -s -X POST http://127.0.0.1:5000/api/orders/$ORDER_A_ID/submit \
  -H "Content-Type: application/json" \
  -d '{"operator_id": 1}' | python -m json.tool

# 申请人2 后提交 - 失败（只剩 10 可用）
echo "=== 订单B提交 ==="
curl -s -X POST http://127.0.0.1:5000/api/orders/$ORDER_B_ID/submit \
  -H "Content-Type: application/json" \
  -d '{"operator_id": 2}' | python -m json.tool

# 验证库存
curl -s http://127.0.0.1:5000/api/inventory | python -m json.tool | grep -A10 '"material_id": 3'
```

预期：订单A成功预占250，订单B失败（只剩10可用），库存不会超扣

#### 6. 撤回已预占的订单

```bash
# 撤回订单A，释放预占
curl -s -X POST http://127.0.0.1:5000/api/orders/$ORDER_A_ID/withdraw \
  -H "Content-Type: application/json" \
  -d '{"operator_id": 1}' | python -m json.tool

# 验证预占释放，库存恢复
curl -s http://127.0.0.1:5000/api/inventory | python -m json.tool | grep -A10 '"material_id": 3'

# 现在订单B再提交应该成功
echo "=== 订单B再次提交 ==="
curl -s -X POST http://127.0.0.1:5000/api/orders/$ORDER_B_ID/submit \
  -H "Content-Type: application/json" \
  -d '{"operator_id": 2}' | python -m json.tool
```

预期：撤回后预占释放，订单B可以成功预占

#### 7. 驳回已预占订单

```bash
# 驳回订单B
curl -s -X POST http://127.0.0.1:5000/api/orders/$ORDER_B_ID/reject \
  -H "Content-Type: application/json" \
  -d '{"operator_id": 3, "reason": "申请数量过大，需重新评估"}' | python -m json.tool

# 验证预占释放
curl -s http://127.0.0.1:5000/api/inventory | python -m json.tool | grep -A10 '"material_id": 3'
```

---

### 预占过期测试

```bash
# 先手动把预占过期时间改短（可选：通过修改数据库）
# 或者使用清理接口触发
curl -s -X POST http://127.0.0.1:5000/api/reservations/cleanup | python -m json.tool

# 查看过期订单
curl -s "http://127.0.0.1:5000/api/orders?status=expired" | python -m json.tool
```

---

## 服务重启数据一致性验证

### 重启前记录状态

```bash
# 记录库存快照
curl -s http://127.0.0.1:5000/api/inventory > before_inventory.json

# 记录审计日志条数
curl -s http://127.0.0.1:5000/api/stats > before_stats.json

# 导出审计
curl -s http://127.0.0.1:5000/api/audit/export.json > before_audit.json
```

### 重启服务

```bash
# Ctrl+C 停止服务，然后重新启动
python app.py
```

### 重启后验证

```bash
# 对比库存
curl -s http://127.0.0.1:5000/api/inventory > after_inventory.json
diff before_inventory.json after_inventory.json

# 对比统计
curl -s http://127.0.0.1:5000/api/stats > after_stats.json
diff before_stats.json after_stats.json

# 对比审计日志
curl -s http://127.0.0.1:5000/api/audit/export.json > after_audit.json
diff before_audit.json after_audit.json

# 检查预占状态是否正确
curl -s http://127.0.0.1:5000/api/inventory | python -m json.tool
```

预期：所有数据完全一致，预占、库存、审计日志无错乱

---

## 状态流转图

```
draft(草稿)
  │
  └─ submit → reserved(预占)
               │
               ├─ approve → approved(批准) → outbound → completed(完成)
               │
               ├─ reject → rejected(驳回)
               │
               ├─ withdraw → withdrawn(撤回)
               │
               └─ [过期] → expired(已过期)
```

所有关键操作（create_draft、submit_reserve、approve、reject、withdraw、outbound、release_reservation、reservation_expired）均写入审计日志。

---

## 数据库表结构

- **users**: 用户表（申请人/审批人角色）
- **warehouses**: 仓库表
- **materials**: 物资表
- **inventory**: 库存表（实际/预占/安全库存）
- **transfer_orders**: 调拨单表（8种状态）
- **reservations**: 预占表（过期时间管理）
- **audit_logs**: 审计日志表
