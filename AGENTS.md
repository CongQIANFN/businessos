# BusinessOS

BusinessOS is a local preview of a business runtime shared by people and agents. It uses PostgreSQL for business records and process progress. The shell demo exercises a versioned contract rule change while a purchase is waiting for approval.

## Map

- `demo.py` and `run-demo.sh` provide the runnable demo.
- `experiments/procurement/` contains the shared runtime, database fixture, purchase process, and mock supplier.
- `experiments/builder/` contains catalog, draft, validation, and local publish operations.
- `experiments/billing/` contains usage and invoice operations on the same runtime.
- `.agents/skills/businessos-builder/` contains the optional coding-agent workflow.

## Conventions

- An Object corresponds to one business table. Queries read, Actions enforce permissions and change records, and Processes coordinate actions and durable waits. Approvals are business records.
- Reuse object and action identifiers when extending existing modules. Cross-module writes go through the owning module's declared action. Keep Query read-only.
- Bind existing business to its version and recheck current authorization on execution. Add acceptance cases for rule boundaries and waiting records when they are affected.
- Use synthetic fixtures and disposable PostgreSQL instances. Run `sh run-demo.sh` for the core walkthrough and `sh experiments/<name>/run.sh` for the affected experiment.
- Commit runnable source, dependency declarations, and reconstructible fixtures. Keep local credentials, private deployments, customer data, and generated runtime state outside tracked files.
