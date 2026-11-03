# PRP: <gap-id> — <one-line goal>

## Goal
What this PRP delivers, in one or two sentences. State the end-user or system
behavior that exists after this PRP lands that does not exist today.

## Non-goals
Explicitly out of scope for this PRP. List things a reader might assume are
included but are not — this is what keeps the task list bounded.

## Context
Everything an implementer needs to read before writing code. Every claim here
must cite a real location.
- Relevant files and the lines that matter: `path/to/file.py:12-40`
- Existing patterns to follow (and why): `path/to/other.py:5`
- External docs / RFCs only if the repo already references them.

## Data model / API contracts
Request/response shapes, DB schema changes, function signatures. Use real
types, not prose. If nothing changes here, write "No data model or API
changes."

```
# example
POST /api/<resource>
  request:  { field: str }
  response: { id: str, field: str }
```

## Task list
Ordered, each task names the exact file(s) it touches and is small enough to
commit on its own. Each implementation task must be preceded by its failing
test per the TDD rule in `prp-implement.md`.

1. `path/to/test_file.py` — write failing test for X
2. `path/to/file.py` — implement X to make the test in (1) pass
3. ...

## Validation gates
Exact, copy-pasteable commands. `prp-implement.md` runs these after every
task; `prp-pr.md` runs them again before merge.
```bash
cd backend && pytest -q
cd backend && ruff check .
cd frontend && npm run build
```

## Acceptance criteria
Bullet list of observable, checkable outcomes. Each one should be verifiable
by running a validation gate or inspecting a specific file/endpoint.

## Risks
What could go wrong, and what to watch for while implementing (e.g. migration
conflicts, breaking an existing contract, perf regressions).

## Rollback
How to undo this change if it ships broken: branch to delete, migration to
revert, flag to flip. Be specific.
