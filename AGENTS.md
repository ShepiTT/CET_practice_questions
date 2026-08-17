# AGENTS.md

## Cursor Cloud specific instructions

This is a single-service Flask app (CET-4 English practice system). See `README.md` for the product overview and standard run instructions.

- Cloud Agent installs put the Python 3.12 virtualenv at `/home/ubuntu/.venv`, not `/workspace/.venv`. A fresh git checkout of `/workspace` removes untracked files, so a workspace venv does not survive environment-build boots. Run Python via `/home/ubuntu/.venv/bin/python`.
- The install script also ensures `python3.12-venv` is present (`sudo apt-get install -y python3.12-venv`) before creating that venv.
- Run the dev server with `/home/ubuntu/.venv/bin/python app.py`. It serves on `http://127.0.0.1:5000` with `debug=True` (auto-reload enabled).
- On startup, `init_db()` scans `data/*.pdf` and syncs parsed questions into the SQLite DB at `instance/cet4_v2.db`. Files already present in the DB are skipped, so normal restarts are fast; the DB is committed to the repo so questions/answers are preloaded.
- There is no lint config or automated test suite in this repo. Use `/home/ubuntu/.venv/bin/python -m py_compile app.py models.py parser.py` as a basic syntax check.
- Core flow to smoke-test: open `/`, pick a paper (or use random mode), answer a question — `POST /submit_answer` returns `is_correct`, the `correct_answer`, and the explanation, and wrong answers are added to `/wrong_questions`.
