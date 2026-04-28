# CLAUDE.md

## Limits (enforced by CI)
- Files: ≤300 lines
- Functions: ≤40 lines, ≤4 params, complexity ≤10
- Nesting: ≤4 levels
- If a limit blocks the task, stop and ask.

## Documentation
Update README and docs/ if needed. Keep alaways direct, concise, short. 

## Style
- Type hints on every signature. `from __future__ import annotations`.
- Dataclass/pydantic for 3+ related values. No dict-passing.
- Pure functions; I/O at module edges.
- Early returns, no nested ifs.
- No `utils.py`/`helpers.py` dumping grounds. Group by domain.
- `logging`, never `print`. No bare `except`.

## Definition of done
Run before declaring complete: `ruff check`, `ruff format --check`, `mypy`, `pytest`, `lizard -C 10 -L 40 -a 4`.
Then list: files changed + line counts, any limit violations, new deps.
Fix violations in the same session.

## New code
Write signature + docstring first. If a function passes 40 lines or a file passes 300 while writing, stop and split. No speculative code.

## Refactor passes (do in order, one per session, tests green between each)
0. **Safety net**: run `lizard` + `tokei`, save top offenders. Write characterization tests for anything untested before touching it.
1. **Split files** >300 lines by responsibility. Pure relocation, no body changes.
2. **Shrink functions** >40 lines or complexity >10. Extract named helpers. Early returns.
3. **Rename + dead code**. Run `vulture`/`ruff F401,F841`. Delete commented code.
4. **Dedupe**. Only lift real duplication, not coincidental similarity.
5. **Tighten types**. `mypy --strict` clean. Dataclasses for grouped values.

Don't bundle passes. Don't fix things outside the current pass — note them for later.