# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | The starter logic only checks for a difference from the shift start and ignores the 10-minute grace-period rule correctly; it also fails to account for IST-aware datetime logic consistently. | A punch at 09:40:01 on a 09:30 shift should produce 10 late minutes, but the starter path can mis-handle timezone-aware values and naive values. | Reworked the time calculations around UTC->IST conversion and strict comparison with the grace threshold. |
| 2 | `compute_overtime` | It derives the shift end incorrectly for overnight shifts and for a date string that is not the attendance date. | A 22:00-06:00 overnight shift produces a wrong overtime period when the punch-out happens after midnight. | Added a proper `shift_end_datetime_for_record` helper that respects overnight shifts and the attendance date. |
| 3 | `list_employees` | The pagination offset is wrong: it uses `page * page_size` instead of `(page - 1) * page_size`, so the first page is skipped. | Requesting `/employees?page=1&page_size=20` starts at item 21 instead of item 1. | Switched to the contract-correct offset calculation. |
| 4 | `punch_in` | The endpoint writes a record using the machine-local date instead of the IST attendance date required by `R1`, and it does not enforce the `status` whitelist. | Punches around midnight or overnight shifts produce the wrong `date` and wrong late metrics. | Added `attendance_date_for_punch_in` and validated only `PRESENT`, `WFH`, and `ON_DUTY` for punch-in. |
| 5 | `/attendance/punch-out` | The route does not reliably choose the correct open punch for the employee and does not validate the punch-out / punch-in boundary correctly across midnight. | A late punch-out after a previous record can close the wrong day or reject a valid overnight shift. | Selected the most recent eligible record whose `punch_in` is at or before the punch-out time and enforced the 24-hour window. |
| 6 | `regularize_attendance` | The starter version has no real validation of a regularization request and can silently accept useless edits or mutate the wrong fields. | A request with no effective change or a status transition that violates the rules returns an incorrect 200/201 instead of a 422. | Added validation for changed fields, required `history` append semantics, and proper concurrency-safe updates. |
| 7 | `/health` | The endpoint only returns `{"status":"ok"}` and never checks MongoDB availability. | The assignment requires readiness to fail with 503 when MongoDB is not answering. | Added a real MongoDB `ping` check and a 503 response on failure. |
| 8 | Indexing and searchability | The starter app does not create the required indexes at startup, which is a contract requirement and a performance risk at ~100k records. | `admin/explain` on the hidden dataset would show table scans rather than `IXSCAN`. | Added startup index creation for employee uniqueness and attendance query patterns. |

Also note anything you looked at and decided was **not** a defect, and why.

- `/employees` response shape is fine in principle; the contract only requires `emp_code` not MongoDB `_id`, and the fixed implementation keeps the API response focused on business identifiers.
- The use of `datetime.now()` for server-side timestamps is acceptable because the assignment requires an instant in UTC when persisting `created_at`, but the timestamp must then be converted to epoch milliseconds at the API boundary.
- The sample data files were not treated as a source of truth for hidden test logic; the assignment explicitly says the grader dataset is larger and contains edge cases not shown in the sample.
