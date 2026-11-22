---
name: businessos-user
description: Use a BusinessOS enterprise ERP through the bos CLI to discover capabilities, query records, perform business actions and inspect workflows. Use for business operations; module development and rule changes belong to businessos-builder.
---
# BusinessOS User

Locate the BusinessOS checkout containing `bos`. Read `docs/CLI.md` when command details are needed. Keep the requested `--home` instance and identity throughout the task. `./bos context show` identifies the enterprise, actor, role, current release and whether it is a fixture. The local preview includes synthetic identities; changing identity is not a way to bypass an authorization failure.

## Discover before acting

Run `./bos module list`, `./bos action list`, and `./bos query list`. Use `action describe <name>` or `query describe <name>` for the current description, input schema, output and allowed roles. Do not assume a fixed list of modules: Builder releases can add capabilities without changing this skill.

Use read-only queries to find the records needed for the request. The built-in `records` query accepts `{"object":"<declared object>"}`; it is not arbitrary SQL. Inspect process records with `./bos process inspect <id>`.

## Execute

Write the input JSON to a local file, then run:

```sh
./bos query run <name> --input @<file>
./bos action run <name> --input @<file> --key <stable-operation-key>
```

Validate the requested target and parameters from descriptions and query results. Pass business data only; enterprise, role and record business version are controlled by the runtime. Reuse the same key and unchanged input when retrying the same Action. A changed intention requires a new key. Check JSON `ok`, returned IDs/status and exit status; a failed operation is not completion.

A rule rejection such as `cost assessment required` means inspect the required prerequisite. Perform it only when authorized by the user's request and the current identity. Report missing authority or a genuine business decision; do not switch identity, rewrite rules, or alter tables to force completion. A new rule request belongs to Builder.

For a process that is waiting, explain what it is waiting for. `process tick` is for an authorized service operator, not an automatic remedy for every wait. External supplier dispatch is still a separate legacy mock connector experiment; the CLI does not complete real external orders.

Verify the resulting state with the returned receipt or a query and report the business result. Do not expose local instance configuration, identity tokens or connection strings. Audit/release inspection requires owner access. This skill does not grant permissions or authorize unrelated business operations.
