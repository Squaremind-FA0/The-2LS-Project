# PRPs — Product Requirement Prompts

This directory holds the specs that drive every feature change in this repo.
Nothing gets implemented without a PRP first. The pipeline is three slash
commands, used in order:

1. **`/prp-plan <gap-id> <one-line goal>`** — reads the codebase and writes
   `PRPs/<gap-id>-<slug>.md` from `templates/prp_base.md`. No code changes.
   Commits only the PRP file, on branch `prp/<gap-id>`.
2. **`/prp-implement PRPs/<gap-id>-<slug>.md`** — checks out (or creates)
   `prp/<gap-id>` from `main`, then executes the PRP's task list top to
   bottom, test-first, running the PRP's validation gates after every task.
   Commits once per task.
3. **`/prp-pr <gap-id>`** — rebases `prp/<gap-id>` on latest `main`, runs the
   full validation gates, and merges: opens a GitHub PR and squash-merges it
   if a `origin` remote exists, otherwise merges `prp/<gap-id>` into `main`
   locally with `--no-ff`.

See `.claude/commands/prp-plan.md`, `prp-implement.md`, `prp-pr.md` for the
exact steps each command follows.

## Naming

- `<gap-id>` — short stable identifier for the unit of work, e.g. `g3`,
  `auth-2`. Matches the branch name `prp/<gap-id>` and is reused across all
  three commands for a given piece of work.
- PRP file: `PRPs/<gap-id>-<slug>.md`, where `<slug>` is a short kebab-case
  version of the one-line goal, e.g. `PRPs/g3-user-preferences.md`.
- Branch: `prp/<gap-id>`, e.g. `prp/g3`.

## Why this exists

A PRP is the one artifact that carries full context (goal, non-goals, file
references, task breakdown, validation commands, acceptance criteria, risks,
rollback) from planning through implementation through merge, so that any
agent or human picking up the work at any stage has everything it needs
without re-deriving it from scratch.
