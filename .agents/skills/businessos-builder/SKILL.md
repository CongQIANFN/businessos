---
name: businessos-builder
description: Prepare and verify a reviewable business rule or module change in the BusinessOS repository. Use for changes to Objects, Queries, Actions, Processes, permissions, or installed modules.
---

# BusinessOS Builder

Read the project `AGENTS.md`, `README.md`, and the affected definitions in `experiments/`. Use `demo.py` for an example of reading a release, drafting a change, validating it, and activating it in a disposable fixture.

1. Read the target tenant's current definition with `BuilderPlatform.catalog()` and inspect the relevant actions and records. Reuse stable object and action identifiers. Clarify ambiguity about business conditions, access, or existing records.
2. Prepare a change set bound to the tenant and base release. Include the rule or code change, affected dependencies, and how existing records and waiting processes continue. A simple rule can use the existing restricted expression. More complex changes may require a runtime capability or an owned module handler.
3. Verify against a disposable local PostgreSQL fixture. Test the boundary, a case that should be rejected, and any affected existing records. The existing Builder validator contains expectations for the CNY 30,000 sample. Derive independent assertions from the requested behavior for a different rule.
4. Report the diff, evidence, and applicable versions. Local fixture activation and real enterprise release are separate operations. The current preview uses the local validation path. A production release requires trusted server checks for authorization, validated artifact, and base release.
