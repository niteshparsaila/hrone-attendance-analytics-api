# Starter code review

| # | Where | What is wrong | How to notice | Fix |
|---|---|---|---|---|
| 1 | `health` | Returns 200 without contacting MongoDB. | Stop MongoDB and call `/health`. | Ping MongoDB and return 503 on failure. |
| 2 | `create_employee` | Check-then-insert is race-prone; creation time is naive and response format is wrong. | Parallel duplicate POSTs; inspect `created_at`. | Unique index, catch duplicate-key error, UTC BSON time and epoch-ms response. |
| 3 | `list_employees` | Offset is `page * page_size`; total ignores department filter; missing bounds/sort. | Page 1 skips first page; filtered total differs. | `(page-1)*page_size`, count filtered query, sort, validate bounds. |
| 4 | time helpers | Naive local timestamps and naive `round()` violate UTC/IST and half-up rules. | Test boundary times and half-cent rounding. | UTC-aware instants, IST calendar conversion, Decimal half-up. |
| 5 | `compute_late_minutes` | Uses a naive timestamp and can mis-handle day/shift boundaries. | Test grace boundary and overnight shift. | Compare aware instants to the attendance-date shift start; strictly greater than 10 minutes. |
| 6 | `compute_overtime` | Ignores overnight end date and the 30-minute minimum. | Overnight shift or 29/30-minute overtime. | Resolve shift end on the next day when overnight; apply minimum. |
| 7 | `punch_in` | Does not handle unknown employee, UTC/IST dates, invalid status/timestamp, or atomic duplicate protection; leaks an `id`. | Unknown code, parallel calls, seconds timestamp. | Validate input, compute IST attendance date, unique compound index, return contract fields only. |
| 8 | `list_attendance` | Loads all matches into memory, has unstable sort, returns internal IDs and no date-range validation. | Large dataset / equal dates / invalid range. | Mongo sort+skip+limit, count filtered query, stable sort, hide `_id`, validate dates. |
| 9 | Missing endpoints | Punch-out, regularization, analytics, and explain endpoints are absent. | Requests return 404. | Implemented endpoints in `app/main.py`. |

Not treated as a defect: using `emp_code` as the employee identifier and keeping attendance keyed by `(emp_code, date)` are explicitly required by the data model.
