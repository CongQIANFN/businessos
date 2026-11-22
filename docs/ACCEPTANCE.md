# Acceptance / 验收记录

Verified locally on 2026-11-22 with Python, PostgreSQL and synthetic data.

| Entry point | Result |
|---|---|
| `tests/cli_acceptance.py` | 50 CLI checks passed |
| `sh run-demo.sh` | Original versioned contract/purchase demo passed |
| `sh experiments/procurement/run.sh` | 30 checks passed |
| `sh experiments/builder/run.sh` | 30 checks passed |
| `sh experiments/billing/run.sh` | 11 tests passed |
| Skill creator `quick_validate.py` | Builder and User skills valid |
| `sh run-cli-demo.sh` | Rule release, old contract, new contract and expense installation/use passed; persistent instance stopped with data retained |

CLI checks start fresh OS processes and verify CNY 40,000 / 40,001 boundaries, old-record rule versions, duplicate requests, changed-input rejection, publication permissions, validation invalidation, rejection of an inverted boundary, concurrent publication with one winning baseline, stale drafts, automatic discovery of a newly installed Action, expense input validation, and persistence after stopping and restarting PostgreSQL.

这些结果来自真实本地 PostgreSQL 和 CLI 调用，不涉及客户数据或生产系统。Skill 的结构检查及脚本验收不等于独立 Agent 的开放式行为评测。当前只覆盖已注册处理器、受限表达式和声明式新增记录；尚不支持任意上传代码。发布版本和能力描述原子生效，但版本提交后的变更回执保存仍是独立事务，跨该间隙的崩溃恢复需要继续完善。其他边界见 [architecture](ARCHITECTURE.md) 和 [CLI contract](CLI.md)。
