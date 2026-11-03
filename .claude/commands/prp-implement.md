# /prp-implement

Execute a PRP's task list, test-first, one commit per task.

**Args:** `<path to PRP file>` — e.g. `PRPs/g3-user-preferences.md`.

If the arg is missing or the file doesn't exist, stop and ask.

## Steps

1. **Load the PRP.** Read the full file. Extract: `gap-id` (from the
   filename, `<gap-id>-<slug>.md`), the Task list, the Validation gates, and
   the Acceptance criteria. If any of these sections are missing or empty,
   stop and report the PRP as incomplete — do not improvise a task list.

2. **Get on the right branch.**
   ```bash
   git fetch origin 2>/dev/null || true
   git checkout prp/<gap-id> 2>/dev/null || git checkout -b prp/<gap-id> main
   ```
   If `prp/<gap-id>` already exists locally (e.g. from `/prp-plan`), check it
   out as-is — do not recreate it from `main`, that would drop the PRP
   commit. Only branch fresh from `main` when `prp/<gap-id>` doesn't exist
   anywhere yet.

3. **Execute the Task list in order.** For each task:
   a. If the task is a test (per the PRP's TDD pairing — a failing test
      precedes the implementation it drives), write that test now. Run the
      relevant validation gate and confirm it **fails** (RED) for the
      expected reason — a test that passes before the implementation exists
      is testing the wrong thing.
   b. If the task is an implementation step, write the minimal code to make
      the preceding failing test pass. Never weaken, skip, delete, or
      loosen an assertion in an existing or newly-added test to force a
      pass — if a test seems wrong, stop and report it instead of editing
      it into compliance.
   c. Run every command listed in the PRP's **Validation gates** section.
      - All green → commit this task:
        ```bash
        git add <files for this task>
        git commit -m "<type>(<scope>): <task description>"
        ```
        Use a conventional-commit type matching the task (`test`, `feat`,
        `fix`, `refactor`, `docs`, `chore`).
      - Any red → attempt a fix, then rerun the gates. Repeat up to **3 fix
        loops total** for this task. If still red after 3 attempts, stop
        work on this PRP, leave the tree as-is (uncommitted), and report
        the task, the failing gate, and the last error as **blocked** —
        do not commit broken state and do not move to the next task.

4. **After the last task's commit**, re-run every validation gate once more
   against the full branch (not just the last task's diff) to confirm
   nothing earlier regressed.

5. **Check acceptance criteria** from the PRP against what was built. Report
   any criterion that isn't actually met rather than marking it done.

6. Report: branch name, commits made (one line each), final validation gate
   output, and whether the PRP is fully implemented or blocked (and where).

## Rules

- Test-first, always: no implementation task runs before its failing test
  exists and has been observed to fail.
- Never skip or weaken a test to get to green.
- Max 3 fix loops per task before reporting blocked — don't loop forever.
- One commit per task, not one commit for the whole PRP.
- Don't touch files outside what the task names unless a validation gate
  failure requires a one-line fix elsewhere — note any such fix in the
  commit body.
