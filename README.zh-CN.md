# BusinessOS

[English](README.md) · 简体中文

BusinessOS 是一个轻量的业务运行底座，供人和 AI Agent 共同使用。这个预览版演示怎样发布一条新规则，同时让已经开始的业务沿原版本继续。

## 运行 Demo

```sh
git clone https://github.com/CongQIANFN/businessos.git
cd businessos
sh run-demo.sh
```

请以普通用户运行。需要 Python 3.10+、`venv` 模块、PostgreSQL 服务端工具和 libpq。确认 `initdb` 和 `pg_ctl` 在 `PATH` 中。如果系统把服务端工具装在单独的目录，可以将 PostgreSQL 的 `bin` 目录加入 `PATH`。例如，在 `pg_config` 指向正确安装的前提下，执行 `export PATH="$(pg_config --bindir):$PATH"`。

脚本会创建 Python 虚拟环境并安装 psycopg，启动一个临时的本地 PostgreSQL 实例，加载合成数据，退出时清理实例。Demo 使用预先准备的规则变更，无需模型 API key。

1. 在 `v1` 下保存一份合同，并提交一笔采购申请。
2. 验证并启用一条新规则。金额超过 ¥30,000 的新合同，签约前必须完成成本评估。
3. 核对规则边界。旧合同沿用原规则，¥30,000 的新合同可直接签约，¥30,001 的新合同须先评估。
4. 批准尚在等待的采购申请。采购沿 `v1` 继续，并在原项目上占用 ¥15,000 预算。

每一步都会调用真实运行时并检查数据库状态。实际结果与预期不符时，脚本会报错退出。

## 核心模型

| 概念 | 职责 |
| --- | --- |
| **Object** | 一张业务表及其字段和引用关系。 |
| **Query** | 在授权范围内执行只读查询。 |
| **Action** | 检查权限和业务状态后执行操作。 |
| **Process** | 按版本组织动作，并基于持久化状态等待和继续。 |

模块组织这些定义。审批单是一条业务记录，流程进度由运行时单独保存。每次执行业务操作时，运行时都会检查当前权限。可选的 `businessos-builder` Skill 为代码 Agent 提供准备和验证变更的项目约定。Demo 可以直接在终端运行。

## 运行实验

```sh
sh experiments/procurement/run.sh
sh experiments/builder/run.sh
sh experiments/billing/run.sh
```

采购实验验证预算竞争、中断后的事务处理、流程恢复和本地供应商模拟器。Builder 实验验证规则检查和版本变更。计费实验复用同一运行时，覆盖用量去重、费率生效版本和冻结账单调整。先运行一次 Demo 或采购实验来准备共享的 Python 环境。

这是一个本地工程预览。Builder 的校验入口验证示例合同变更。正式使用前，需要把验证结果和预期基线版本的检查纳入受信任的发布事务。表达式只支持有限的语法，受信任的模块处理器仍持有数据库游标。编辑和测试约定见 [AGENTS.md](AGENTS.md)。

## 许可

[MIT](LICENSE)
