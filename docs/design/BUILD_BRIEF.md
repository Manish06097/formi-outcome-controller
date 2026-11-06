# Build brief for implementation agents

Read before writing code: `CLAUDE.md` (hard rules), `docs/design/DESIGN.md` (the spec; section 17
overrides earlier sections), `notes/UNDERSTANDING.md` (simulator mechanics with line references),
`docs/SIMULATOR.md` (the API contract). Where the design is ambiguous, choose the option that is
simplest to defend, write the choice into your ADR, and list it in your report.

## Environment
- Python 3.11. Create your own venv in your working copy: `uv venv --python 3.11 .venv` then
  `uv pip install --python .venv/bin/python -r requirements.txt -r requirements-controller.txt`
  (the second file exists after M0).
- Run tests with `.venv/bin/python -m pytest -q <path>`.
- Start a simulator for tests as a subprocess:
  `.venv/bin/python -m sim serve --scenario <yaml> --run-dir <tmp dir> --port <your port> --force`
  and poll `GET /health` until it answers. Kill it when done.
- **Ports and names per track** (so parallel tracks never collide):
  M0 8700 / pg 55432 / compose `formi_m0`; T1 8711 / 55441 / `formi_t1`; T2 8712 / 55442 / `formi_t2`;
  T3 8713 / 55443 / `formi_t3`; T4 8714 / 55444 / `formi_t4`; T5 8715 / 55445 / `formi_t5`.
  If a track needs Postgres without the full compose stack:
  `docker run -d --rm --name <compose name>_pg -e POSTGRES_PASSWORD=ctl -p 127.0.0.1:<pg port>:5432 postgres:16`.
  Stop what you start.

## Ownership
- M0 owns `controller/types.py`, `controller/settings.py`, `controller/store/migrations/`,
  `requirements-controller.txt`, `pyproject.toml`, and every `__init__.py` of the package skeleton.
  After M0, **no track edits these files**. If you need a contract change (a new field, table,
  column or setting), implement against a local extension inside your own module where you can,
  and list the exact change under "Contract changes requested" in your report. The lead merges them.
- Each track writes only the modules, tests and docs listed for it.

## Quality bar
- Code a teammate can own: small functions, type hints, a module docstring stating the module's
  job and which DESIGN.md section it implements, every `[T]` threshold read from `settings.py`.
- Controller code never imports `sim`, reads `runs/`, reads chunk files from disk, or parses
  `scenarios/*.yaml`. Tests and `scripts/` may.
- Pure statistics and policy code get unit tests with hand-computed fixtures. Product behaviour gets
  end-to-end tests against a real simulator process over HTTP (`tests/e2e/`), marked
  `@pytest.mark.e2e`.
- Every test you add must pass before you report. Report honestly what does not work.
- No em dashes anywhere (use a hyphen or rewrite).
- Write the ADRs assigned to your track in `docs/decisions/NNNN-title.md`
  (Status, Context, Decision, Alternatives, Consequences), reasoning a reviewer can follow.

## Commits
- Commit in small, coherent steps on your current branch with clear messages that say why, ending
  with the line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never squash or rewrite
  history. If committing is blocked, leave the work uncommitted and say so in your report.

## Report (your final message)
1. Branch name (`git rev-parse --abbrev-ref HEAD`) and the list of commits.
2. What you built, mapped to DESIGN.md sections.
3. Test results: the exact commands and their pass or fail summary.
4. Contract changes requested (exact diffs).
5. Deviations from the design and why.
6. Known gaps and risks.
