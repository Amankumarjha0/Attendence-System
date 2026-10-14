# DECISIONS.md

1. **Indexes.** I create a unique index on `employees.emp_code` to enforce the customer code’s uniqueness and a unique compound index on `attendance_logs` for `(emp_code, date)` so only one record can exist per daily attendance key. I also add supporting indexes on `department`, `date`, and `status` plus a compound date/employee sort index to keep the list and analytics queries efficient. I did not keep a broad, expensive index on every field because the assignment’s workload is dominated by date and employee lookups and adding more indexes would add write cost without a clear win.

2. **Punch-in race.** The endpoint first looks up the employee and then tries to insert a document with the same `(emp_code, date)` key. Because the collection has a unique compound index, the first insert wins and returns 201; the second request hits a duplicate-key error and is translated to 409. This is exactly the contract requirement: no check-then-insert race and no silent overwrite.

3. **Ties.** The leaderboard ranks by total late minutes descending, then by `emp_code` ascending. For tied values, the code assigns the same competition rank to both employees and returns every employee whose rank is less than or equal to the limit. So if the cutoff is 10 and two employees tie at rank 9, both appear even if that makes the result length exceed `limit`.

4. **Headcount.** The department summary starts from employees whose `joined_on` is on or before the month end, then aggregates attendance records only for those employees. That includes employees with no logs at all because they are still counted in the headcount and are simply absent from the record set; the department only disappears if its headcount is zero.

5. **One thing to change at 100x the data.** I would move the aggregate calculations into a precomputed reporting structure or a materialized monthly summary collection so that the analytics endpoints become mostly read-only lookups instead of running large pipelines over hundreds of thousands of daily rows on every request.
