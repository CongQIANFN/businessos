# BusinessOS

BusinessOS is a small, versioned business runtime for people and AI agents. This preview demonstrates a rule change while existing business continues on the version it started with.

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

Modules group these definitions. An approval is a business record. A process persists its progress separately. The runtime checks current permission when it executes a business operation. The optional `businessos-builder` Skill gives a coding agent project-specific instructions for preparing and testing a change. The demo runs directly from the shell.

## Explore the experiments

```sh
sh experiments/procurement/run.sh
sh experiments/builder/run.sh
sh experiments/billing/run.sh
```

The procurement suite exercises budget contention, interrupted transactions, process recovery, and a local mock supplier. The Builder suite exercises validation and versioned changes. The billing suite uses the same runtime for usage deduplication, effective rate versions, and adjustments to frozen invoices. Run the demo or procurement suite once to prepare the shared Python environment.

This is a local engineering preview. The publish checks in the Builder wrapper verify the sample contract change. Moving those checks and the expected base version into the trusted release transaction is necessary for production use. The expression evaluator implements a restricted subset, and trusted module handlers still hold database cursors. See [AGENTS.md](AGENTS.md) for editing and testing conventions.

## License

[MIT](LICENSE)
