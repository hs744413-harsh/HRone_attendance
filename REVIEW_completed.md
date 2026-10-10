# REVIEW.md

This review records defects identified in the starter `app/main.py`, the symptoms that expose them, and the corresponding fixes.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | The starter computes lateness by replacing clock fields on the punch-in datetime and can use the wrong date/time context, especially with IST and overnight shifts. | Test punches around the 10-minute grace threshold and shifts crossing midnight. | Normalize the instant and compute lateness relative to the employee's shift start on the attendance date. |
| 2 | `compute_work_hours` | Built-in `round()` does not guarantee the contract's decimal half-up rounding. | Test elapsed seconds at a rounding boundary. | Use `Decimal` with `ROUND_HALF_UP` and return two decimal places as a float. |
| 3 | `compute_overtime` | The starter does not correctly model overnight shift ends and the required 30-minute threshold. | Test overtime at 29 minutes, exactly 30 minutes, and after an overnight shift. | Compute the correct shift-end instant and count whole overtime minutes only when the threshold is met. |
| 4 | Timestamp conversion in punch-in and response helpers | Local-time conversion and loose timestamp handling can misinterpret epoch milliseconds or accept invalid values. | Submit seconds-looking timestamps, strings, floats, booleans, and out-of-range values; compare IST dates. | Centralize conversion, require strict integer epoch milliseconds in range, and normalize stored instants to UTC. |
| 5 | `EmployeeIn` / request models | The starter models do not enforce the required employee-code, email, date, shift-time, status, and timestamp constraints. | Submit malformed codes/emails/dates/times, unsupported statuses, or non-integer timestamps. | Add Pydantic field constraints, validators, and strict request types. |
| 6 | `create_employee` | A read-before-insert duplicate check alone is vulnerable to concurrent requests; creation time can also be naive/local. | Send parallel creates with the same `emp_code` and inspect `created_at`. | Create a unique `emp_code` index, handle `DuplicateKeyError` as 409, and store/return UTC creation time as epoch milliseconds. |
| 7 | `health` | The starter can report healthy even when MongoDB is unavailable. | Stop or disconnect MongoDB and call `/health`. | Ping MongoDB and return 503 if the ping fails. |
| 8 | `list_employees` | The starter pagination offset skips the first page, `total` ignores the department filter, and results are not sorted by `emp_code`. | Compare the first page, later pages, and filtered totals. | Use `(page - 1) * page_size`, count using the active filter, and sort ascending by `emp_code`. |
| 9 | `punch_in` | The starter does not safely handle an unknown employee and relies on a check-then-insert pattern to prevent duplicate daily punches. | Punch in for an unknown employee and send simultaneous duplicate punch-ins. | Return 404 when the employee is unknown and enforce uniqueness on `(emp_code, date)` in MongoDB; map duplicate-key conflicts to 409. |
| 10 | `punch_in` response | The starter exposes an identifier derived from MongoDB's internal `_id`, which is not part of the API contract. | Inspect a successful punch-in response for extra ID fields. | Remove internal IDs and serialize records through the response helper. |
| 11 | `list_attendance` | The starter fetches all matching records and sorts/paginates them in Python, which scales poorly on a large collection. | Test against a large attendance collection and inspect the work performed in the application process. | Apply filtering, sorting, skip, and limit in MongoDB and create supporting indexes at startup. |
| 12 | MongoDB setup / indexes | The starter does not create the indexes required for uniqueness and the expected list/analytics query patterns. | Inspect indexes after startup and use `explain` on representative queries. | Create idempotent employee and attendance indexes at application startup. |

## Reviewed and considered not defects

- `emp_code` is the public employee identifier; MongoDB `_id` is internal and must not be exposed.
- `date` and `joined_on` are calendar-date strings, while instants are stored as UTC datetimes and exposed as epoch milliseconds. These different representations are intentional in the data model.
- A shift whose end time is earlier than or equal to its start time represents an overnight shift; the comparison itself is not a defect.

## Additional endpoints implemented

The starter did not contain implementations for punch-out, attendance regularization, monthly employee analytics, department summary, the late leaderboard, department trend, and the explain endpoint. These endpoints were added to meet the API contract.
