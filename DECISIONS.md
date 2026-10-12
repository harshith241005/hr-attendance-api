# DECISIONS.md

1. **Indexes.**
   - `employees`: `{ emp_code: 1 }` (unique) enforces identifier uniqueness; `{ department: 1, joined_on: 1 }` backs department headcount and trend lookups; `{ joined_on: 1 }` supports period filters.
   - `attendance_logs`: `{ emp_code: 1, date: 1 }` (unique) guarantees natural key uniqueness and race-free punch-ins; `{ date: -1, emp_code: 1 }` accelerates attendance list pagination; `{ emp_code: 1, punch_in: -1 }` speeds punch-out search; `{ date: 1, late_minutes: 1 }` backs leaderboard aggregation.
   - *Rejected:* `{ punch_out: 1 }`, since queries never filter across collections on punch-out status alone.

2. **Punch-in race.**
   Both requests execute `insert_one()`. Because of the unique compound index on `(emp_code, date)`, MongoDB atomically commits the first write and fails the second with `DuplicateKeyError` (code 11000). The exception handler catches this and returns HTTP 409 Conflict, yielding exactly one 201 without check-then-insert race conditions.

3. **Ties.**
   The pipeline ranks via `$rank` in `$setWindowFields` (standard competition ranking: 1, 2, 2, 4) and matches `rank <= limit`. If employees tie at the cutoff, all tied rows are returned (potentially exceeding `limit`), ordered by `total_late_minutes` descending and `emp_code` ascending as the contract requires.

4. **Headcount.**
   The aggregation pipeline originates on `employees` filtered by `joined_on <= period_end`, grouping by department with `{ $sum: 1 }`. A `$lookup` joins `attendance_logs`. Employees without logs produce empty arrays, counting toward headcount while contributing zero present days.

5. **One thing you would change for 100x data.**
   Maintain pre-aggregated daily/monthly department summaries updated incrementally via Change Streams or write events, avoiding full cross-collection pipeline scans across millions of log documents.
