# BusinessOS

English · [简体中文](README.zh-CN.md)

BusinessOS is a small, versioned business runtime for people and AI agents. This preview demonstrates a rule change while existing business continues on the version it started with.

## Agent + CLI + Skills

The local preview now has one CLI for business operations and module development, plus two skills:

- **User** discovers capabilities, queries records, runs Actions and inspects Processes.
- **Builder** exports definitions, edits artifacts, creates changes, tests and publishes. Newly published capabilities appear in the same CLI automatically.

After preparing dependencies with `sh run-demo.sh`, run the persistent walkthrough in a fresh instance directory:

```sh
sh run-cli-demo.sh
./bos instance start
./bos context show
./bos --identity employee action list
./bos instance stop
```

The walkthrough publishes a CNY 40,000 contract assessment rule, verifies old record behavior, installs expense submission and uses it through the User interface. It leaves the instance stopped with data preserved in `.businessos/`. Use `BOS_HOME=/absolute/new/path` to select another fresh instance; initialization never overwrites an existing instance.

For manual use, start with `./bos instance init` instead of the walkthrough. See the [CLI contract](docs/CLI.md) and [architecture](docs/ARCHITECTURE.md). The repository ships [Builder](.agents/skills/businessos-builder/SKILL.md) and [User](.agents/skills/businessos-user/SKILL.md) skills. Copy the desired skill folders into your agent's skill directory if it does not discover repository-local skills.

Run black-box acceptance with `experiments/procurement/.venv/bin/python tests/cli_acceptance.py`.

## Run the demo

```sh
git clone https://github.com/CongQIANFN/businessos.git
cd businessos
sh run-demo.sh
```

Run as a regular user with Python 3.10+, the `venv` module, PostgreSQL server tools (`initdb` and `pg_ctl` on `PATH`), and libpq. On systems that place server tools outside `PATH`, add the PostgreSQL `bin` directory. For example, use `export PATH="$(pg_config --bindir):$PATH"` when `pg_config` points to the installed server tools.

The script creates a virtual environment for psycopg and starts a temporary local PostgreSQL instance. It loads synthetic data and removes the instance on exit. The demo uses a prepared rule change and requires no model API key.

1. Save a contract and submit a purchase on `v1`.
2. Validate and activate a new rule that requires a cost assessment on new contracts above CNY 30,000.
3. Show that the old contract keeps its rule, exactly CNY 30,000 can sign directly, and CNY 30,001 requires assessment.
4. Approve the previously waiting purchase. It resumes on `v1` and reserves CNY 15,000 against its existing project.

Each step calls the actual runtime and verifies database state. A mismatch exits with an error.

## The model

| Concept | Responsibility |
| --- | --- |
| **Object** | A business table, its fields, and its references. |
| **Query** | A scoped, read-only operation. |
| **Action** | A business operation with authorization and state checks. |
| **Process** | A versioned definition for calling actions and waiting on durable state. |

Modules group these definitions. An approval is a business record. A process persists its progress separately. The runtime checks current permission when it executes a business operation. Builder and User skills guide coding agents through the unified CLI. The demo runs directly from the shell.

## Explore the experiments

```sh
sh experiments/procurement/run.sh
sh experiments/builder/run.sh
sh experiments/billing/run.sh
```

The procurement suite exercises budget contention, interrupted transactions, process recovery, and a local mock supplier. The Builder suite exercises validation and versioned changes. The billing suite uses the same runtime for usage deduplication, effective rate versions, and adjustments to frozen invoices. Run the demo or procurement suite once to prepare the shared Python environment.

This is a local engineering preview with synthetic identities. The CLI publish path reruns artifact-bound cases and checks the expected base under the activation lock. The older experimental Python Builder wrapper remains a sample-specific regression fixture. Direct Python handlers and host access are trusted; remote authentication, robust code isolation and arbitrary uploaded handlers are not implemented. See [AGENTS.md](AGENTS.md) for conventions.

## License

[MIT](LICENSE)
