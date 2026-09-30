# 大型储能电池全生命周期协同平台

本项目是一套可离线运行的 Python 服务端平台，服务于大型储能电池从入库、状态评估、组件检测、场站调拨到退役处置的协同管理。平台将资产流转、评估协议、质量决定、幂等结果和审计事件保存在 SQLite 中，供运营、质量、维修和审计人员在单个 Linux 应用容器内使用。

## 目录

- `src/battery_logistics/`：储能场站、调拨走廊、资产批次、容量申请、分配与处置情景；
- `src/battery_assurance/`：电池资产、证据版本、评估协议、观测导入、排除复核、分析任务与准入决定；
- `src/component_quality/`：电芯组件批次、响应测量、统计分析、账号权限和质量审批；
- `src/battery_renovation/`：维修与翻新谱系——序列件登记、进场冻结、拆解处置（复用/维修/报废/隔离）、确定版本检测与维修动作、组包双签放行、换件返工与双向溯源；
- `fixtures/`：离线验收使用的评估协议与结构化观测；
- `tests/`：领域规则、错误边界、事务、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m battery_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m battery_assurance.acceptance --workspace .
PYTHONPATH=src python3 -m component_quality.acceptance
PYTHONPATH=src python3 -m battery_renovation.acceptance --workspace .
```

四条命令会在临时 SQLite 数据库中完成资产调拨、状态评估、组件质量和两个退役整包重组梯次利用设备的完整谱系流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m battery_logistics.api --database battery-logistics.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m battery_assurance.api --database battery-assurance.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m component_quality.api --database component-quality.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m battery_renovation.api --database battery-renovation.sqlite3 --host 127.0.0.1 --port 8083
```

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 维修与翻新谱系

`battery_renovation` 服务把纸单式重组纳入平台，关键规则：

- **冻结后才允许拆解**：拆解工单必须冻结进场配置（装配快照及 SHA-256）并挂接故障证据，之后才能开工；
- **四类处置分别落账**：每个拆出组件单独进入复用（available）、维修（repair）、报废（scrapped）或隔离（quarantined），旧整包全部处置完才退役；
- **一物一装**：装配关系为只追加的谱系边，部分唯一索引保证任一序列件同一时刻只属于一条生效装配、一个仓位只有一个件，数据库层杜绝一物多装；
- **确定版本引用**：检测按组件追加版本并带内容摘要，维修动作归属于确定的维修工单；组包清单逐行引用检测版本，有维修史的组件必须引用已完成维修工单中的维修动作；
- **双签放行**：技术确认（engineer）与质量放行（quality）必须由不同人员完成，出场后整包与全部组件配置不可变；
- **异常不留盲区**：撤销、换件以「旧关系 removed + 新关系 installed」记录，换件重置双签；部分工单失败时已拆件保留处置、未拆件留在原包，可开新单续拆；维修撤销转隔离并要求重新定级；
- **双向溯源**：`GET /items/{id}/lineage/up` 从新整包还原全部来源（含原拆解工单、进场快照、检测与维修动作），`GET /items/{id}/lineage/down` 从任一旧组件查到最终去向、未决动作与阻止交付的证据缺口，`GET /work_orders/{id}/blockers` 给出放行前的缺口清单。
