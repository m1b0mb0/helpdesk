# Repository guidelines for Codex

## Context

This repository implements the take-home assignment described in `docs/ASSIGNMENT.md`. The original employer-provided assignment takes precedence if there is any discrepancy. Keep the solution proportional to a small, time-boxed backend exercise.

The selected stack is Django, PostgreSQL, Django Admin for operator review, Celery for background processing, and Redis as the task broker and shared rate-limiting backend. PostgreSQL remains the source of truth for tickets, drafts, moderation decisions, and delivery state.

Consider Django's built-in capabilities and established libraries before implementing custom functionality. Prefer existing solutions when they meet project requirements and simplify implementation without introducing unnecessary dependencies or complexity. Avoid forcing existing abstractions when a custom solution is more appropriate. Discuss significant architectural trade-offs before implementation and document relevant operational limitations in `README.md`.

## Working agreement

- I set priorities and approve scope and architectural decisions. You may propose alternatives, explain trade-offs, and implement an approved task. Do not assume that a proposal has been accepted.
- Work on the current requested task only. Before making substantial changes, briefly identify the intended approach and affected areas.
- Keep changes focused and understandable. Prefer straightforward code over unnecessary abstractions.
- Preserve the assignment's reliability constraints; do not silently weaken them for simplicity. Flag ambiguity or a limitation explicitly.
- Place business rules in testable code rather than only in views or Django Admin actions.
- Never add credentials or local environment files to Git. Do not overwrite unrelated user changes.

## Verification and reporting

- Add tests for the behavior you change, particularly concurrency, idempotency, failure recovery, and external API failures where relevant.
- Run appropriate checks when available. Distinguish commands actually run from recommended commands; never report unverified behavior as passing.
- At the end of a task, summarize what changed, the checks performed and their results, and any remaining limitations or decisions requiring my input.
- Do not claim that I reviewed or approved a change unless I explicitly did so.

## Git workflow

I manage the Git history manually. Do not run `git add`, `git commit`, `git push`, history rewrites, or destructive Git commands unless I explicitly request them.

After a completed task, inspect `git status --short` and recommend a cohesive commit:
- List the exact changed files to stage, excluding unrelated files and secrets.
- Provide the explicit `git add <paths>` command and a concise Conventional Commit message in English.
- Include `git diff --cached --check` and `git diff --cached --stat` as review steps.
- If relevant tests have not run or a blocker remains, mark the commit recommendation as conditional rather than implying the work is complete.

Respond to me in Ukrainian; use English for code, filenames, and commit messages.
