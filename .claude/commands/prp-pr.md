# /prp-pr

Rebase a finished PRP branch on latest `main`, run full validation gates,
and merge it.

**Args:** `<gap-id>` — e.g. `g3`. Operates on branch `prp/<gap-id>`.

If the arg is missing, or branch `prp/<gap-id>` doesn't exist, stop and ask.

## Steps

1. **Rebase on latest main.**
   ```bash
   git fetch origin 2>/dev/null || true
   git checkout prp/<gap-id>
   git rebase main          # or `git rebase origin/main` if a remote exists
   ```
   - If the rebase conflicts, resolve the conflicts in the diff itself
     (don't abort-and-ignore). Re-run the PRP's own task-level tests as you
     resolve each file to make sure the resolution didn't reintroduce a bug
     the PRP's tests were guarding against.
   - **Alembic multi-head case:** if the rebase leaves two or more Alembic
     heads (`cd backend && alembic heads` shows more than one), do not pick
     one arbitrarily. Add a merge revision instead:
     ```bash
     cd backend && alembic merge -m "merge heads for <gap-id>" <rev1> <rev2>
     ```
     Commit the generated merge revision file as its own commit
     (`fix(db): merge alembic heads for <gap-id>`).

2. **Run full validation gates** (not just the PRP's own gates — the whole
   stack):
   ```bash
   cd backend && pytest -q
   cd backend && ruff check .
   cd frontend && npm run build
   cd frontend && npm test        # once a frontend test runner exists
   ```
   All must pass. If anything is red, fix it on this branch (small, targeted
   fixes only — this is not the place to add new scope) and rerun until
   green. If you cannot get to green, stop and report the failure instead of
   merging.

3. **Build the PR body from the PRP file** (`PRPs/<gap-id>-*.md`):
   - Title: the PRP's Goal, one line.
   - Body sections: Goal, Non-goals, what changed (summarize the commits on
     `prp/<gap-id>`), test evidence (paste the green output from step 2),
     and the PRP's Acceptance criteria as a checklist.

4. **Merge — branch on whether a remote exists:**
   ```bash
   git remote get-url origin >/dev/null 2>&1 && echo has-remote || echo no-remote
   ```
   - **Has remote:** push the branch, open a PR, and squash-merge only once
     checks are green:
     ```bash
     git push -u origin prp/<gap-id>
     gh pr create --base main --head prp/<gap-id> --title "<title>" --body "<body>"
     gh pr merge --squash --auto   # or without --auto once CI confirms green
     ```
   - **No remote (true for this repo today):** merge locally into `main`
     with `--no-ff`, using the PR body as the merge commit message so the
     same context is preserved in history:
     ```bash
     git checkout main
     git merge --no-ff prp/<gap-id> -m "<title>" -m "<body>"
     ```
   Never force-push `main` in either case.

5. **Report**: final gate output, PR URL (remote case) or merge commit SHA
   (local case), and confirm `prp/<gap-id>` can now be deleted (but don't
   delete it yourself — leave that for the human/owner unless explicitly
   told to clean up branches).
