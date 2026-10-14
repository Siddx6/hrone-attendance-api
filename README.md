# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run

```
pip install -r requirements.txt
set MONGO_URI=mongodb://localhost:27017      (PowerShell: $env:MONGO_URI = "...")
set MONGO_DB=attendance_db                   (PowerShell: $env:MONGO_DB = "...")
uvicorn app.main:app --port 8000
```

`MONGO_URI` / `MONGO_DB` are read from the environment (a local `.env` is optional and never overrides real
variables). Needs Python 3.11+ and MongoDB 6.0+. Indexes are created at startup; nothing else to run.

## Status

Everything in the contract is implemented: health, employees, punch-in, punch-out, list, PATCH (regularize), the four
analytics endpoints and `/admin/explain/{endpoint}`. `REVIEW.md` and `DECISIONS.md` are filled in.

## Choices where the contract left room

* `moving_avg_7d` averages the **unrounded** daily rates; only the final value is rounded (4 decimals, half-up).
* `avg_work_hours` (department summary) averages over all present-status records with a `work_hours` value, weekends
  included; `present_days` counts Monday to Friday records only.
* A punch-in on a date that already has a record (even a seeded `ABSENT` / `LEAVE`) is a 409.
* Of several simultaneous PATCH requests on one record, the one that wins gets 200; the others get 409 (or 422 if
  they no longer change anything). No history entry is ever lost.
* Punch-out closes the record with the latest `punch_in` at or before `punched_at`; if that record is already closed
  the answer is 409.

## Testing

I tested with local helper scripts (not committed): parallel-request checks for the race conditions, a seeded dataset
of awkward cases compared against a brute-force calculation, and a 100,000-record dataset for `explain` and timings.
