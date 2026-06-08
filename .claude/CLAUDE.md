# Clean code requirements

## How to write a code?

- Avoid over-engineering. Only make changes that are directly requested. Don't add features, refactor code, or make improvements beyond what was asked. Just suggest what can be improved in the end of plan.
- Before writing new code, check if existing code already does this (or almost does) and reuse/refactor it instead.
- Keep code simple, human readable, easy to extend, easy to refactor for human in the future.
- Prefer DELETING and SIMPLIFYING code over adding new code for fixing old code.
- When fixing a bug, find and fix the root cause. Do NOT add workarounds, wrappers.
- Do not keep old code "just in case".
- Do not add defensive try/except or null checks to silence errors — fix the reason the error occurs.

## How to automate e2e tests?

- Follow BDD (given, when, then) while writing manual test case.
- Follow all steps from manual test cases. Every assert must have failaure message.
- Statuses of test cases: automated, not-automated, partially-automated.
- Add allure steps, prioritiy, tags, markers.
- Follow best practices of writing clean end-to-end tests. Tests should be read like a novel.
- Follow AAA (arrange, act, assert) for every test. Follow BDD (given, when, then).

## Documentation

- Update README.md, it should be human readable, don't use difficult termins/words.
- REFERENCES.md is used as a refence to backend, android code and ios code.
- Keep commetns simple for human to understand.

## Limits

- Files: <300 lines, tests files can be <1000 lines.
- Functions: <40 lines, <=4 params, complexity <10. pytest tests can be longer <100.
- Nesting: <=4 levels

## Style

- Write signature + docstring first. Add docstring for file, class, method.
- Follow KISS, YAGNI to write simple, human redable code.
- Follow SOLID to make code extendible and easy to refactor code.
- Type hints on every signature. `from __future__ import annotations`.
- Dataclass/pydantic for 3+ related values. No dict-passing.
- Pure functions; I/O at module edges.
- Early returns, no nested ifs.
- `logging`, never `print`. No bare `except`.

## Definition of done

Run before declaring complete: `ruff check`, `ruff format --check`, `mypy`, `pytest`, `lizard -C 10 -L 40 -a 4`.
Then list: files changed + line counts, any limit violations, new deps.
Fix violations in the same session.
Run tests and make sure you didn't break anything.

## Refactor passes (do in order, one per session, tests green between each)

0. **Safety net**: run `lizard` + `tokei`, save top offenders. Write characterization tests for anything untested before touching it.
1. **Split files** >300 lines by responsibility. Pure relocation, no body changes.
2. **Shrink functions** >40 lines or complexity >10. Extract named helpers. Early returns.
3. **Rename + dead code**. Run `vulture`/`ruff F401,F841`. Delete commented code.
4. **Dedupe**. Only lift real duplication, not coincidental similarity.
5. **Tighten types**. `mypy --strict` clean. Dataclasses for grouped values.
