# CLAUDE.md

Project: Loura support ticket classifier (engineering take-home for Rakan Aldosari).

## Always
- `DESIGN.md` is the single source of truth. Read it fully at the start of every session before doing anything.
- Follow section 0 of `DESIGN.md` (rules of engagement) at all times.
- Work one chunk at a time (section 14). Never start the next chunk until Rakan says so.
- Ask, never assume. Build only what `DESIGN.md` describes.
- Never commit API keys or secrets.
- GitHub: the project lives in a private GitHub repo named `loura-ticket-classifier`, created in Chunk 0 with `gh` and connected as `origin`. Before the first push, check `.gitignore` excludes `.venv`, `.env`, `data/*.db` and cache folders, and that no file contains a secret.
- End of every chunk: after the chunk report and the commit, ask Rakan "Push chunk N to GitHub?" and push only after he says yes. Don't start the next chunk until the push is done, or until he says to skip it.

## Progress
Update this list at the end of every chunk, so a new session knows where things stand.

| Chunk | Status | Notes |
|---|---|---|
| 0: confirm and set up | done (23 Nov 2026) | All 9 approval items approved as proposed. Skeleton, git repo, first commit done. |
| 1: config and database | done (23 Nov 2026), reviewed | `config.py` + `db.py`. 60 tests in `tests/test_db.py` (added to the section 13 tree). Both SQL guards mutation-tested. Rakan's review applied: `insert_ticket` returns nothing, setting range checks, strong phrase `you are now` tightened to `you are now a`/`an`. |
| 2: the front door (API) | done (23 Nov 2026) | `schemas.py`, `api.py`, minimal lifespan in `main.py` (init_db only). 139 tests. Fixed a clock-dependent flaky ordering test in Chunk 1's suite. |
| 3: suspicion scoring | done (23 Nov 2026) | 3a `scoring.py` phrase lists. 3b trained detector: `injection_detector.py`, `scripts/train_injection_detector.py`, data+model in `data/injection/`. Cut-offs recalibrated 0.50/0.90 -> 0.65/0.85 after measuring (Rakan approved). 221 tests. |
| 4: prompt and output validation | done (23 Nov 2026) | `classifier.py`: L2 prompt + L3 shape checks. 84 tests, 307 total. A test compares the prompt with DESIGN 10.2 word for word. Open finding: a realistic poisoned summary scores 1-2, so it is flagged not rejected (see Chunk 4 report). |
| 5: model interface and fake model | done (23 Nov 2026) | `models/base.py` (ModelClient Protocol), `models/fake.py` (rules + 7 broken replies + scripted mode), `models/__init__.py` (get_model_client). Startup key check wired into `main.py`. Timeout decided: enforced by the worker. 38 tests, 354 total. |
| 6: workers | not started | |
| 7: samples end to end | not started | |
| 8: README | not started | |
| 9: real provider (only if Rakan says so) | not started | |
| 10: optional extra (only if Rakan says so) | not started | |

## Commands
Python on this machine: system Python is 3.9, which is too old. Use Homebrew's `python3.12`
(`/opt/homebrew/bin/python3.12`) to create the venv. Once `.venv` exists and is activated,
plain `python` and `pytest` are the 3.12 ones.

- First-time setup: `/opt/homebrew/bin/python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
- Run the service: `source .venv/bin/activate && uvicorn app.main:app` (docs at http://127.0.0.1:8000/docs)
- Load the samples: `source .venv/bin/activate && python scripts/load_samples.py` (second terminal, from Chunk 7)
- Run the tests: `source .venv/bin/activate && python -m pytest`
- Retrain the injection detector: `source .venv/bin/activate && python scripts/train_injection_detector.py` (optional; the model file is committed) (plain `pytest` cannot import `app`; see DESIGN.md section 12)
