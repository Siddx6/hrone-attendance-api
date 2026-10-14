# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity and join key), `(department, emp_code)` for filtered lists and the trend, `joined_on` for headcount. `attendance_logs`: unique `(emp_code, date)`, which enforces one record per day and serves punch-in, punch-out, PATCH, the monthly report and joins; and `(date desc, emp_code)` for the list order and month scans. I rejected an index on `status`: too few distinct values to help.

2. **Punch-in race.** Both requests are validated and both call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept one; the other gets `DuplicateKeyError`, which I return as 409. I never check first and insert later.

3. **Ties.** `limit` applies after ranking, so everyone tied at the cutoff is returned and the list can be longer than `limit`. With `limit=1` and two people on 75 minutes, both come back as rank 1.

4. **Headcount.** The summary starts from `employees` (joined on or before month end) and looks up each person's logs. No logs gives an empty lookup, which adds zeros but the person is still counted.

5. **100x the data.** I would stop recomputing monthly figures from raw logs and keep a small per-employee, per-month rollup, updated on each punch or correction.
