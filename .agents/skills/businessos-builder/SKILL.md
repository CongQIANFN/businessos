---
name: businessos-builder
description: Develop, install, test and publish BusinessOS enterprise modules through the bos CLI. Use for ERP schema, capability, workflow and rule changes; ordinary business execution belongs to businessos-user.
---
# BusinessOS Builder

Use a coding agent to edit module artifacts, and the `bos` CLI to operate the enterprise. Find the checkout containing `bos` and `businessos/service.py`; read its `AGENTS.md` and `docs/CLI.md`. Run `./bos --help` for the installed interface. Do not assume the old experimental Builder wrapper is the operational interface.

## Establish context

Run `./bos context show`, `./bos module list`, and `./bos catalog export --output <file>` against the requested `--home` instance. Keep that instance argument on subsequent commands. The current preview defaults to a synthetic local enterprise. Its identity selection is a fixture facility, not production authentication. Do not initialize over an existing instance or redirect a request to a different enterprise without saying so.

Derive business expectations from the request, including boundaries, current permissions and existing-record behavior. Reuse existing Object and capability names. A Query is read-only; writes use the owning module's Action. Task/approval records are ordinary Objects; Process owns durable steps.

## Develop an artifact

For edits, start from the exported current definition. Modify the candidate JSON or restricted expression locally; never edit the running database or stored release. For a new module, produce an additive package with `objects`, `queries`, `actions`, and `modules`, or export the `expenses` starter with `./bos template export expenses --output <file>`.

Every public Action/Query must declare `description`, `roles`, `input`, and `output`. Inputs use the supported schema subset described in `docs/CLI.md`. A new capability must carry enough description for the User agent to discover and use it. The current runtime supports registered built-in handlers and declarative `data.create`; arbitrary new Python implementations require a runtime code change and regression checks. Do not claim arbitrary code upload is supported.

Write a separate cases JSON derived from the requested business behavior. Include boundary and rejection cases and `before` records when version compatibility matters. Existing synthetic fixture cases do not prove compatibility with a customer's real data. Use `expect` or `error` assertions, not just successful setup calls.

## Operate through CLI

- Edit: `./bos change create --candidate <file> --cases <file> --request '<business requirement>'`.
- Install: `./bos module install <package> --cases <file> --request '<business requirement>'`. This creates a draft; it does not activate it.
- Inspect: `./bos change inspect <id>` and `./bos change diff <id>`.
- Verify: `./bos change test <id>`. Review the evidence and correct mismatches against the request.
- Amend: `./bos change update <id> --candidate <file> --cases <file>`. This invalidates previous validation.
- Publish when authorized: `./bos change publish <id>`. Publishing reruns tests and checks the base version under the release lock. A stale draft must be rebuilt from the current catalog and retested.

Read JSON `ok`, error code/message, and nonzero exit status. Do not bypass rejected operations with SQL or `Runtime.publish`. Use the returned change/release IDs as evidence. Publishing a module makes its implementation and capability descriptions visible together.

## Finish as a User

Check `./bos action list` / `describe` and query discovery with the intended User identity, then execute meaningful synthetic cases through those same CLI entry points. Verify that the new capability can be understood without editing the User skill. For writes use explicit stable `--key` values and reuse them on retries.

Report the installed instance, behavior change, release, actual tests and remaining limitations. Run `experiments/procurement/.venv/bin/python tests/cli_acceptance.py` for changes to the CLI/release path; use the relevant legacy experiments for runtime changes. Do not present a passed local preview as a deployed production service.
