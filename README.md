# Polaris 防漂移系统

Polaris 个性防漂移系统——为硅基智能体建立人格基线，检测并纠正人格漂移。

M1 SaaS 化版本（v0.1.0）：独立仓库 + SaaS 服务层 + 审计/告警/订阅/用量表。

## 目录结构

```
polaris/
├── anti_drift/               # 核心检测引擎
│   ├── soul_baseline.py      # L0 灵魂基线提炼
│   ├── scene_tagger.py       # L0.5 场景标签
│   ├── sampler.py            # L1 采样
│   ├── detector.py           # L1.5+L2 漂移检测
│   ├── prescription_engine.py# L3 处方引擎
│   ├── archive.py            # L4 归档判定
│   ├── models.py             # 存量 4 表 ORM
│   ├── models_saas.py        # M1 新增 6 表 + M3 billing_checkouts ORM
│   ├── api_v2.py             # REST API v1
│   ├── billing.py            # M3 订阅计费引擎（状态机/用量聚合/模拟支付）
│   └── billing_api.py        # M3 订阅计费 API（/api/v1/billing/*）
├── polaris/                  # SaaS 服务层
│   ├── saas_server.py        # 启动入口（:5052）
│   └── soul_baseline_api.py  # Soul Baseline API（DID 鉴权）
├── common/                   # 内部公共依赖（config_manager / logger）
├── web/                      # Web 控制台
├── tests/                    # pytest 测试
├── docs/
├── data/                     # 本地运行数据（不入库）
├── config.yaml               # 根统一配置（阈值权威来源）
└── requirements.txt
```

## 快速开始

```bash
pip install -r requirements.txt
# MemGuard 依赖（M1 决策：依赖引入，不复制源码）：
#   待 memguard 发布 pip 包后：pip install memguard
#   当前开发期：设置 MEMGUARD_SOURCE_PATH=<memguard源码父目录> 或放入同父目录

# 启动服务（:5052）
python -m polaris.saas_server

# 运行测试
pytest tests/ -v
```

## 配置（P-05：阈值已统一）

检测阈值权威配置在根 `config.yaml`（与 `detector.py` 代码默认一致）：

```yaml
anti_drift:
  thresholds:
    green: 0.15   # < 0.15 正常
    gray: 0.25    # 0.15~0.25 过渡
    yellow: 0.30
    red: 0.30     # > 0.30 显著偏离
```

## 数据库

- 开发：SQLite（`data/polaris_saas.db`，create_all 幂等建表）
- 生产（v0.3）：PostgreSQL + Alembic 迁移
- 存量 4 表 + M1 新增 6 表（见 `anti_drift/models_saas.py` 与 `docs/schema.md`）

## 验收状态（P-CODE-001 / P-CODE-002 / P-CODE-003）

### M1 v0.1.0（P-CODE-001）

- [x] P-01 saas_server.py 引用 soul_baseline_api 修复（服务可启动）
- [x] P-02 models.py User.__repr__ 字段修复
- [x] P-03 update_baseline 截断防护（String(128) + 长度校验）
- [x] P-04 .gitignore 策略（data/ 与 DB 不入库）
- [x] P-05 阈值统一 0.15/0.25/0.30
- [x] 6 张新表（models_saas.py）
- [x] pytest 全绿
- [x] tag v0.1.0

### M2 v0.2.0（P-CODE-002）

- [x] 告警引擎（alerts / alert_webhooks）+ Webhook 推送（接口先行）
- [x] 告警 API + 控制台告警中心（ack / resolve）
- [x] pytest 全绿（含新增用例）
- [x] tag v0.2.0

### M3 v0.3.0（P-CODE-003）订阅计费

- [x] 订阅 API + 状态机（trial / active / canceled / expired + 非法迁移防护）
- [x] 用量事件记录 + 月周期聚合（月对齐 + over_limit flag）
- [x] 支付 checkout + webhook 回调闭环（HMAC-SHA256 模拟签名可验证）
- [x] 控制台订阅页（套餐 / 用量 / 账单 / 升级降级 / 模拟续费）
- [x] pytest 全绿（21 新增用例，标准套件合计 45 用例）
- [x] tag v0.3.0

## M3 订阅计费说明（v0.3.0）

- 订阅状态机：`trial → active → canceled → expired`，非法迁移（如 active→trial）一律拒绝；
  周期结束惰性过期（查询时自动转 expired）。
- 套餐与限额：config.yaml `billing.plans.*` 为权威来源（free/pro/enterprise 价格 + 用量限额），
  环境变量 `POLARIS_BILLING_*` 可逐项覆盖。
- 用量聚合：按自然月对齐（UTC 当月 1 号 ~ 下月 1 号），订阅周期优先；超限打 over_limit flag。
- 模拟支付：`POST /api/v1/billing/checkout` 创建会话 → `POST /api/v1/billing/mock-pay/<id>`
  模拟支付成功；webhook 端点（`POST /api/v1/billing/webhook`）要求
  `X-Polaris-Signature: t=<ts>,v1=<hmac-sha256>` 头（secret 在 config billing.webhook_secret），
  篡改 payload / 过期时间戳一律 400。无需真实 Stripe 密钥。
- 账单：当前周期 = 套餐费 + 超额用量 × overage_rate；历史账单 = 已支付 checkout 记录。
- 数据库：新增 billing_checkouts 表（v0.3），存量 4 表 + M1/M2 新表未动。
