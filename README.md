# 管网隔离方案培训演示（Isolation Training Demo）

面向工艺培训的**纯演示系统**：在一张固定的模拟管网上，计算隔离目标设备所需关闭的
阀门候选集合。**不连接任何真实控制系统，计算结果不代表真实检修已满足安全隔离条件。**

- 前端：Angular 19 + Cytoscape.js（拓扑、管段方向、旁路、阀门锁定、方案与残余路径高亮、联合计划区域/共享路径高亮）
- 后端：FastAPI + NetworkX（无向物理连通图上的候选阀门集合枚举与约束校验、多区域联合隔离求解）
- 存储：PostgreSQL（节点连接、阀门开闭/锁定、必要供给点、联合隔离计划与审计快照；本地无 PG 时自动回退 SQLite）

## 演示拓扑

```
SRC ─V0─ N1 ─V1─ N2 ─V_TIN─ [T] ─V_TOUT─ N3
          │            ╲  旁路 N2─V_BP_IN─BP─V_BP_OUT─N3 ╱
         V_P1           环网联络 N1─V_LK1─N5─V_LK2─N3
          ↓                          N3 ─V_P2─ P2
          P1
                          [T] ─V_TU─ [U] ─V_UP3─ P3   （第二个目标设备区域，盲端枝路）
```

- 每条管段显式保存名义方向（upstream→downstream，图上箭头标注）；隔离按**无向物理连通**计算。
- 旁路（紫虚线）与目标设备 T 并联；P1、P2 为**必要供给点**（任何方案不得断供）。
- U 为第二个可登记的目标设备区域，P3 为其下游**非必要**用户（默认不要求保供）。
- 阀门与管段 1:1；初始全部打开。锁定 = 禁止关闭（保持现状），可在页面勾选后重新计算。

## 三个培训样例

| 样例 | 锁定 | 结果 |
| --- | --- | --- |
| 1 旁路绕回 | 无 | 关 `V_TIN`+`V_TOUT` 即隔离 T；旁路保持打开，P2 经 `N2→BP→N3` 绕回不断供 |
| 2 锁定入口阀 | `V_TIN` | 最小集合升为 3 阀：`V_TOUT`+`V1`+旁路一只（`V_BP_IN`/`V_BP_OUT` 两个等价方案）；旁路被封，P2 改由环网 `N1→N5→N3` 供料 |
| 3 不应断供的支路 | `V_TOUT` | **无可行方案**：任何切法都不可避免断供 P2（P1 可保住）；页面显示仍连通的残余路径 `SRC→N1→N2→T`，以及经锁定阀的见证路径 `…→N3→T`（含 `V_TOUT`） |

> 仅关 T 一侧阀门时隔离不成立：例如只关 `V_TIN`，介质仍可经旁路绕回 N3 再回到 T
> （`N2→BP→N3→T`），或经环网绕回。这正是样例 1 必须两侧同关的原因。

## 联合隔离计划（多区域同检）

一次检修常同时隔离多个设备区域；分别求出的单目标方案放在一起会**共享阀门、
相互断供或被错误提前复位**。系统支持登记“联合隔离计划”：

- 一次登记多个目标区域及**各自仍要求保供的节点**（留空 = 全部必要供给点 P1/P2），
  在**同一张物理连通图**上求一个去重后的联合关阀集合，须同时满足：
  1. 每个目标都与所有来源断开；2. 每个区域声明的全部保供节点仍可达来源。
- 结果包含：去重后的关阀集合、**共享阀门**（被多个区域隔离边界共同见证的阀）、
  每个区域的隔离证据（目标断源 + 边界阀）、每个供给点的保供路径。
- **计划状态管理**：`prepared`（求解完成、未动阀）→ `executing`（一次性按集合关阀，
  各区域可分别释放）→ `released`（最后一个区域释放后按计划恢复，转为只读历史快照）。
  释放一个区域时，仍被其他未释放区域依赖的**共享阀保持关闭**。
- **无解不部分应用**：锁定关键阀（如 `V_TOUT`）或区域间需求矛盾（一个区域的隔离
  目标恰是另一区域的保供节点）导致联合约束无解时，不应用任何区域的单目标方案；
  接口逐区域返回残余连通路径、经锁定阀的见证路径，以及“任何联合切法都保不住”
  的保供点见证。不可行计划同样持久化留痕，但禁止执行。
- **幂等与快照**：重复提交同一组（区域 + 保供要求）返回同一计划，不新增第二套
  阀门、不重复审计；重复执行/释放请求幂等；请求、求解结论与全部状态变迁都保留
  在计划快照与审计时间线中，释放后仍可查看。

联合样例：

| 登记 | 结果 |
| --- | --- |
| 区域 T + 区域 U（默认保供 P1/P2） | 联合最小集合 `{V_TIN, V_TOUT}`，两只阀均为共享；U 的单目标阀 `V_TU` 被去重；P1/P2 经旁路保供 |
| 锁定 `V_TOUT` 后登记 T+U | 整体无解：两区域各自给出经 `V_TOUT` 的见证路径，且任何联合切法都不可避免断供 P2 |
| 区域 T 要求保供 U、区域 U 要求隔离 U | 结构性矛盾，直接返回矛盾见证 |
| 区域 U 显式要求保供 P3 | P3 是 U 下游唯一枝路，任何切法都保不住，返回不可避免断供见证 |

联合计划 API：

- `POST /api/joint-plans`：body `{ "regions": [{"target_id": "T", "supply_node_ids": null}, {"target_id": "U"}] }`
  （幂等：重复登记返回同一计划，`idempotent_hit=true`）
- `GET /api/joint-plans` / `GET /api/joint-plans/{id}`：列表 / 详情（含结果与审计快照）
- `POST /api/joint-plans/{id}/regions`：向 prepared 计划追加区域并重新联合求解（重复目标幂等）
- `POST /api/joint-plans/{id}/execute`：按去重集合一次性关阀（不可行计划返回 409，不部分关阀）
- `POST /api/joint-plans/{id}/regions/{target_id}/release`：释放一个区域；共享阀待最后一个区域释放才恢复
- `GET /api/topology` 的 `active_plans` 字段携带未释放计划摘要，供拓扑界面联动高亮

## 本地运行

### 后端（无 PostgreSQL 时自动用 SQLite 文件）

```bash
cd backend
python3 -m pip install -r requirements.txt
python3 -m uvicorn app.main:app --reload --port 8000
# API: http://127.0.0.1:8000/api/topology, /api/isolation, /api/valves/{id}/lock, /api/reset
# 测试: python3 -m pytest tests/ -q
```

指定 PostgreSQL：

```bash
export DATABASE_URL=postgresql+psycopg://isolation:isolation@localhost:5432/isolation_demo
docker compose up -d db        # 或使用任意已有 PG 实例
```

### 前端

```bash
cd frontend
npm install
npm start                      # http://localhost:4200 （/api 代理到 8000）
# 或产物构建后由后端直接托管: npx ng build  → http://127.0.0.1:8000/
```

### 一键（含 PG）

```bash
cd frontend && npm ci && npx ng build && cd ..
docker compose up --build
```

## API 摘要

- `GET /api/topology`：节点 / 有向管段 / 阀门（含 `is_open`、`locked`、`is_bypass`）/ `active_plans`
- `POST /api/isolation`：body `{ "target_id": "T", "locks": {"V_TIN": true} }`
  - 可行：`best_solution`（最少阀门）、等价方案、方案后每个必要供给点的来源路径
  - 不可行：`residual_path`（仍连通的一条残余路径）、`locked_witness_path`（经锁定阀的见证路径）、
    `unconstrained_best.unavoidable_essentials`（任何切法都无法保住的供给点）
- `POST /api/valves/{valve_id}/lock`：持久化单只阀门锁定状态
- `POST /api/reset`：全部阀门恢复打开、未锁定，并清空联合计划与审计
- 联合隔离计划：`/api/joint-plans`（登记/列表/详情/追加区域/执行/逐区域释放，见上节）

## 安全边界声明

系统仅对**给定拓扑与阀门模型**做枚举演示：假设阀门与管段 1:1、关阀即断边、
无背压/泄漏/盲板/双阀双断等真实工况要素。任何输出**不得**作为真实检修隔离
（LOTO）的安全依据。
