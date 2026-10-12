# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the HROne engineering assignment.

## Run locally

Requires Python 3.11+ and MongoDB 6.0+.

```bash
python -m venv .venv
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` as environment variables before starting the server. The application also loads a local `.env` for convenience; environment variables already set by the shell take precedence. Do not commit `.env` or credentials.

```bash
uvicorn app.main:app --port 8000
```

Check `GET http://127.0.0.1:8000/health` and open `/docs` for the interactive API documentation. Indexes are created idempotently at startup.

## Sample data

Use the provided `sample_seed.py` and sample JSON files from the assignment kit, with `MONGO_URI` and `MONGO_DB` configured. **Seeding deletes existing documents** in the two target collections, so use a disposable development database.

## Notes

- Instants at the API boundary are epoch milliseconds; MongoDB stores UTC BSON dates.
- Attendance dates and shift times use IST calendar rules.
- `REVIEW.md` records starter defects; `DECISIONS.md` explains the key choices.
- The sample dataset is only a smoke test. Run contract and concurrency tests against MongoDB 6/7 before submitting. No `.env`, secrets, data dumps, or Dockerfile should be committed.
