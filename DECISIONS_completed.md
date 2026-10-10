# DECISIONS.md

1. **Indexes.** The unique `employees.emp_code` index enforces employee identity. `(department, emp_code)` supports filtered employee listings; `(joined_on)` and `(department, joined_on)` support headcount queries. On attendance, unique `(emp_code, date)` prevents duplicate daily records. `(date, emp_code)` supports date-based listing, `(status, date, emp_code)` supports status-filtered listing, and `(emp_code, punch_in)` helps find recent punches. I avoided one oversized index for every optional filter because index order matters and extra indexes increase storage and write cost.

2. **Punch-in race.** Two requests may both pass the employee lookup, but the unique `(emp_code, date)` index permits only one insert. The winner returns 201; the duplicate-key error for the other request becomes 409. The pre-check is not the concurrency guarantee.

3. **Ties.** The leaderboard uses `$rank` on total late minutes. Everyone tied at the cutoff rank is returned, then rows are ordered by late minutes and `emp_code`.

4. **Headcount.** Department summary starts from employees who joined by month-end and looks up attendance. Employees with no logs still count and contribute zero metrics.

5. **100x scale.** I would benchmark query plans and workload, then consider pre-aggregated summaries and targeted index changes based on measured bottlenecks.
