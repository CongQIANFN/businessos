# BusinessOS CLI contract / 命令约定

`bos` is the standard local operational interface. A coding agent edits artifact files; it reads and changes the running enterprise through CLI commands. The CLI calls `businessos/service.py`, which delegates transactions, identity checks and business-version execution to the shared runtime.

`bos` 是本地操作入口。Agent 可以编辑开发产物文件；读取和修改企业运行状态统一经过 CLI。Skill 指导操作，服务层执行约束。

## Start / 启动

Run `sh run-demo.sh` once to prepare Python dependencies and check PostgreSQL tools. Then:

```sh
./bos instance init
./bos context show
./bos action list
./bos action describe contract.create
```

The default home is `.businessos` in the working directory. Use an absolute `--home` (or `BOS_HOME`) for a stable instance location. These global flags precede commands:

```sh
./bos --home /absolute/path/to/instance --identity employee action list
```

This is a synthetic local enterprise with owner, employee, agent, delivery and service identities. Host files contain development credentials. `--identity` is a fixture switch, not production sign-in. The Postgres cluster listens only on a local Unix socket; it uses local trust authentication and disables fsync. Keep this preview on a trusted development host.

本版本为合成本地企业，身份切换用于验证权限。尚未提供远程服务、正式认证或生产数据库配置。实例目录不可提交到 Git。

## Commands / 命令

| Command | Behavior / 行为 |
|---|---|
| `instance init/start/stop` | Initialize once; stop/start preserves data. 初始化、停止、恢复实例。 |
| `context show` | Current enterprise, identity, role, release. 当前上下文。 |
| `module list` | Installed module descriptions. 已安装模块。 |
| `template list` / `template export NAME --output FILE` | Discover/export additive starter package. 导出模板。 |
| `catalog export --output FILE` | Owner exports current full definition. 导出当前定义。 |
| `action list/describe NAME` | Discover permitted public Actions and their schemas. 发现可用动作。 |
| `query list/describe NAME` | Discover permitted Queries. 发现查询。 |
| `action run NAME --input FILE --key KEY` | Execute with stable retry key. 执行动作。 |
| `query run NAME --input FILE` | Read-only query. 只读查询。 |
| `process inspect ID` / `process tick` | Inspect / authorized advancement. 查看或推进流程。 |
| `change create --candidate FILE --cases FILE --request TEXT` | Persist a draft bound to current release. 创建变更。 |
| `module install PACKAGE --cases FILE --request TEXT` | Merge additive module package into a draft. 安装先产生草案。 |
| `change inspect/diff/test/publish ID` | Inspect, compare, test, activate. 检查、验证、发布。 |
| `change update ID --candidate FILE --cases FILE` | Replace artifact; invalidate validation. 更新并作废旧验证。 |
| `release list` / `audit list` | Owner inspects versions and successful action receipts. 查看版本、成功动作记录。 |

All commands except help return JSON: `{"ok":true,"result":...}` with exit 0, or `{"ok":false,"error":{"code":"...","message":"..."}}` with exit 1. File inputs accept either `path.json` or `@path.json`. `--key` is mandatory for public Action calls; use the same key/input/identity for a retry.

## First business operation / 第一笔业务

```sh
printf '{"amount":50000}\n' > /tmp/contract-input.json
./bos action run contract.create --input /tmp/contract-input.json --key first-contract
# Put the returned result.id in {"id":"..."}, then call contract.sign.
# 把返回的 result.id 放入 {"id":"..."}，再调用 contract.sign。
printf '{"object":"contract"}\n' > /tmp/contract-query.json
./bos query run records --input /tmp/contract-query.json
```

Only the synthetic initializer seeds `sample-project` for purchase scenarios. It does not call an external supplier.

## Add a module / 新增模块

```sh
./bos template export expenses --output /tmp/expenses.json
./bos module install /tmp/expenses.json --cases examples/expense-cases.json --request 'Add simple expense submission'
# Use the returned change id / 使用返回的变更 ID：
./bos change diff CHANGE_ID
./bos change test CHANGE_ID
./bos change publish CHANGE_ID
./bos --identity employee action describe expense.submit
```

Write `{"description":"Train ticket","amount":120}` to an input file and execute `expense.submit`. The new capability is discoverable without changing the CLI or User skill. The starter supports submission only, not approval/reimbursement.

新增模块与能力描述同版本发布，User 无需更新 Skill 即可发现。模板目前只演示报销提交，不包含完整报销审批和付款。

## Definitions / 定义

`catalog export` returns the installed full definition. Edit this for an existing module. Additive packages contain `modules`, `objects`, `actions`, `queries`. Objects use `columns` with runtime-supported PostgreSQL types. Changes preserve old column names/types. Public capabilities require `description`, `roles`, `input`, `output`. Internal Actions do not appear in discovery.

The current input schema subset is an object with `properties`, `required`, scalar types `string`, `integer`, `boolean`, or nested `object` (opaque), and integer `minimum`. Unknown top-level fields are rejected. Output schemas are descriptive; full JSON Schema validation is not implemented. Amounts in the starter are whole CNY, not decimal money.

`data.create` is the declarative create handler: declare an `object`, allowed input fields and optional `defaults`. Other handlers must already be registered in the runtime. Queries currently use registered handlers, including `record.list`. Complex new Python handlers require runtime development; arbitrary module code upload and robust code isolation are not shipped.

For a contract gate change, export the current catalog, set `policy.cost_threshold` to the requested integer, `policy.cost_gate` to true, and `extensions["contract.sign"]` to `{'requires_cost': amount > threshold}`. The candidate's release ID is assigned by the service when the draft is created. Write expectations independently from the requested threshold; do not derive expected outcomes from the candidate expression.

## Cases / 验收案例

```json
{
  "before": [
    {"name":"contract.create","input":{"amount":80000},"save":"old"}
  ],
  "after": [
    {"name":"contract.sign","input":{"id":"$old.id"},"expect":{"status":"signed"}},
    {"name":"contract.create","input":{"amount":40001},"save":"high","expect":{}},
    {"name":"contract.sign","input":{"id":"$high.id"},"error":"cost assessment required"}
  ]
}
```

This example assumes a base release without a cost gate. Adapt expectations to the actual exported base. `before` runs on the base definition; then the candidate is published in a disposable PostgreSQL fixture; `after` exercises it. `$name.field` references a saved result. `identity` defaults to owner; `kind` defaults to action (or query). `expect` compares top-level result fields; `error` expects a rejection containing that message. Empty `expect` is useful for setup but is not a semantic assertion. An operation cannot pass an expected-error case by failing on an unrelated error. A case set has at most 200 steps.

Tests use reconstructed synthetic data, not a copy of the live database. Publishing requires a prior passing result, reruns the cases in the operation layer, checks the artifact hash, and compares the base under the same release lock used to activate the definition. Editing a draft invalidates previous validation; concurrent stale drafts are rejected. Existing records keep their recorded rules. The local host and Python runtime APIs remain trusted; this is not a remote adversarial security boundary.

规则测试已经不再固定为三万元。案例预期由需求明确，服务层运行案例、重新验证发布产物，并在发布锁内检查基线。旧记录保留原业务版本；当前执行权限仍即时检查。

## Verification / 验收

```sh
experiments/procurement/.venv/bin/python tests/cli_acceptance.py
sh run-demo.sh
```

The CLI acceptance uses fresh OS processes and disposable PostgreSQL to verify rule boundaries, old records, permissions, idempotency, invalidated tests, stale releases, module discovery, and restart persistence. Legacy experiment scripts remain regression tools rather than the Builder's normal operating interface.
