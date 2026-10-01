# 协同事务平台

协同事务平台为需要多机构参与的业务提供统一的机构、授权、案件、证据、审批、资源预约、资金分录、消息归并、通知、定时任务和历史回放能力。项目只使用 Python 标准库与 SQLite，适合在单个 Linux 应用容器内运行。

## 素材采用记录

在平台能力之上，`civicflow.governance.Governance` 提供素材治理：

- `assets`（素材登记）：记录文件指纹、来源、训练与再利用许可、署名条件、模型或工具版本、适用产品、地域、发布渠道和授权到期时间。相同指纹再次上传返回原素材；指纹一致但许可陈述冲突时转入复核并通知。合作方只能读取获授字段，来源与提交人被裁剪。
- `adoptions`（采用记录）：送展或产品采用时提交组合，批准人不得是素材提交人；批准时校验许可范围与有效期，并封存素材当时的可见版本作为送审依据。已发布组合通过替换记录和补充声明说明后续处置。
- 授权撤回、品牌限制更新和授权到期都通过定时任务传播，只阻止尚未发布的组合，已发布的组合收到后续处置通知；进程恢复后再次调用 `run_due_jobs` 即可继续处理到期授权、待审组合和撤回传播。
- `provenance(work_id)` 从作品反查每份素材的来历、采用时有效的许可、批准人以及受影响的后续发布。

涉及的权限：`write:assets`、`transition:assets`、`read:assets`、`history:assets`、`write:adoptions`、`approve:adoptions`、`publish:adoptions`、`read:adoptions`、`history:adoptions`。

## 目录

- `src/civicflow/`：领域服务、SQLite 持久化、权限和命令行入口。
- `tests/`：核心流程、边界条件和异常路径测试。
- `examples/`：本地演示输入。

## 配置

通过 `CIVICFLOW_DB` 指定 SQLite 文件路径；不设置时命令行使用当前目录下的 `civicflow.sqlite3`。所有时间使用带时区的 ISO 8601 字符串。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 编译或构建

```bash
PYTHONPATH=src python3 -m compileall -q src
```

## 使用

初始化数据库并运行离线演示：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 demo
```

查看当前案件：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 list-cases
```
