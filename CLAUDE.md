# CLAUDE.md: Ryan-MCP

## What this project is

A read-only semantic glossary API (FastAPI) plus a thin MCP server. It returns **approved business meanings** (definition, authoritative source, rule, relationships, governance metadata) for terms such as "member", so AI agents stop guessing between competing definitions. It is a proof of concept for a client, using synthetic data only.

- Spec and source of truth: `BUILD_BRIEF.md`. Read it fully at the start of every session.
- Running log: `PROGRESS.md`, which you create and maintain (status, decisions, deviations, open questions).

This is a **greenfield build**. The repo starts with only this file and the brief.

## Hard rules

Never break these. If the brief seems to require breaking one, stop and ask.

- No LLM calls anywhere in the service or MCP server. Resolution is deterministic.
- No `eval()`, `exec()`, `pickle` or dynamic code execution on rule or request content.
- Metadata only: the service never returns member rows or runs queries against data.
- Read-only service: no create, update or delete endpoints for concepts.
- No error path returns HTTP 200. No input may cause a 500. Error body: `{"error": {"code", "message", "details"}}`.
- A missing fact never yields a boolean result.
- Never pick a definition when more than one fits; return candidates and a clarifying question.
- The MCP server is a thin wrapper with no business logic and at most 3 tools.
- No real data, secrets, API keys or absolute local paths in the repo. Never log or echo an API key.

## How to work

- The developer gives you three build prompts. Each covers several milestones from `BUILD_BRIEF.md` §11. Work through them in order, commit after each milestone and tag it `m<N>-done`.
- Stop only at a **DESIGN PAUSE** (post the design, wait for "go") or at the end of the prompt. Never start work from a later prompt.
- For anything non-trivial, write the decision, plus the alternative you rejected, in `PROGRESS.md` **before** coding.
- Make the smallest change that meets the acceptance criteria. No unrequested features, dependencies, config options or abstractions.
- If you think the brief is wrong or incomplete, say so and propose a fix. Do not silently deviate. Record every deviation.
- Tests use exact assertions (ids, statuses, counts, status codes), never `>= 1`. Every error path and every bug fix gets a test. Show that new tests can fail: break the code, watch the test fail, restore.
- Never claim something works without running it. Paste real output.
- Comments explain *why*, not *what*. Each module starts with 2–4 lines of design intent.
- Never push to a remote. The developer pushes.
- Never amend a commit or move a tag once created. Fix forward with a new commit, and a new tag if needed (e.g. `m5-fix1`).
- Run mutation checks only on committed code, with a clean `git status` before and after each one.
- After editing `PROGRESS.md`, search for the inserted text to confirm it landed; at the end of each milestone, list the decision IDs and confirm there are no gaps or duplicates.
- Record every documented behaviour or limitation in `PROGRESS.md` as you go.
- Before reporting the end of a prompt, **audit your own work as an independent reviewer would**: re-run everything from a clean state, verify each acceptance criterion with evidence you produced, scan for hard-rule violations and scope creep, and check that your claims in `PROGRESS.md` match the code.

## Environment

- The developer works on **Windows (PowerShell)**. Everything must also work on Linux/macOS: use `python -m ...`, `pathlib`, and no bash-only scripts.
- Target Python 3.11. A local virtualenv, if used, is `.venv` and is git-ignored.
- Commands: `python -m pytest -q` · `uvicorn app.main:app --reload` · `python -m evals.run` (after M6).

## Standard report (end of every prompt)

1. **Summary** (3–5 lines).
2. **Acceptance criteria**: a table of criterion → evidence (command output or test names).
3. **Decisions and deviations** from the brief, with reasons.
4. **Verification output**: pasted, not paraphrased.
5. **Risks and open questions** (max 5).
6. **Things the developer must be able to explain**: 3–6 items with `file:line` pointers.

Finish with: "Stopped at the end of this prompt; not starting the next one."
