# /prp-plan

Write a PRP (Product Requirement Prompt) for a unit of work. No code changes
— this command only produces a spec file.

**Args:** `<gap-id> <one-line goal>`
- `gap-id` — short stable identifier for this work, e.g. `g3`. Used for the
  PRP filename and the `prp/<gap-id>` branch created later by `/prp-implement`.
- `one-line goal` — the rest of the args, a single sentence describing the
  outcome.

If either arg is missing, stop and ask for it. Do not guess a gap-id.

## Steps

1. **Read standing context.**
   - Read `CLAUDE.md` at the repo root (stack, run/test commands, branch
     rules). If it does not exist yet, note that and proceed with whatever
     conventions are discoverable from the repo itself.
   - List `PRPs/*.md` and skim any that look related to this gap-id or goal,
     so the new PRP doesn't duplicate or contradict an existing one.
   - Read `PRPs/templates/prp_base.md` — this is the template the new PRP
     must follow section-for-section.

2. **Research the current code (RAG, not guesswork).**
   - Grep for symbols, routes, components, or tables implied by the goal.
   - Read every file that will be touched or that establishes the pattern to
     follow. For each fact you plan to put in the PRP's Context section,
     have a concrete `path/to/file:line` to cite — don't assert something
     about the code without having just read it.
   - Identify existing tests covering the area, so the Task list can name
     the right test files to extend rather than inventing new ones
     needlessly.

3. **Draft the PRP** at `PRPs/<gap-id>-<slug>.md`, where `<slug>` is a short
   kebab-case version of the one-line goal. Fill in every section of
   `templates/prp_base.md`:
   - **Goal** — the one-line goal, expanded to 1-2 sentences of observable
     behavior.
   - **Non-goals** — what's explicitly excluded, to bound scope.
   - **Context** — file:line references gathered in step 2.
   - **Data model / API contracts** — concrete shapes, or "No data model or
     API changes."
   - **Task list** — ordered, each task names exact file(s), each
     implementation task preceded by the failing-test task that drives it.
   - **Validation gates** — exact, copy-pasteable shell commands (e.g.
     `cd backend && pytest -q`, `cd frontend && npm run build`). Pull these
     from `CLAUDE.md` if it documents them; otherwise discover the real
     commands from `backend/requirements.txt`, `frontend/package.json`, etc.
   - **Acceptance criteria** — checkable outcomes.
   - **Risks** — concrete failure modes for this specific change.
   - **Rollback** — exact steps to undo (branch to delete, migration to
     revert).

4. **Do not write or modify any code, test, or config file outside the new
   PRP file.** This command's only output is the PRP markdown file.

5. **Commit on a dedicated branch.**
   ```bash
   git checkout main
   git pull --ff-only          # skip if no remote
   git checkout -b prp/<gap-id>
   git add PRPs/<gap-id>-<slug>.md
   git commit -m "docs(prp): add PRP for <gap-id>"
   ```
   If `prp/<gap-id>` already exists (a previous planning pass), check it out
   instead of creating it, and amend or update the PRP file there rather
   than branching again.

6. Report the PRP file path and the branch name so `/prp-implement` can be
   run next.
