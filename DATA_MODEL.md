# DECISIONS.md

This file explains some of the main decisions I made while building the HROne Attendance & Analytics API and why I chose these approaches.

## 1. MongoDB indexes

I added indexes based on how the application accesses employee and attendance data. The unique index on `employees.emp_code` prevents duplicate employee codes, while the compound unique index on `attendance_logs(emp_code, date)` ensures that an employee cannot have multiple attendance records for the same day.

I also considered indexes for frequently used attendance lookups and department-based queries. I avoided adding too many indexes because every additional index takes up storage and adds overhead when records are inserted or updated.

The indexes created by the application should match the actual queries and the requirements in `DATA_MODEL.md`.

## 2. Preventing duplicate punch-ins

Two punch-in requests could arrive at almost the same time. If the application only checks whether a record exists before inserting, both requests might pass the check.

To prevent this, I rely on MongoDB's unique compound index as the final safeguard. When one request creates the attendance record, a second request for the same employee and date will fail with a duplicate-key error.

The API catches this error and returns HTTP `409 Conflict` instead of creating a duplicate record. This makes the database responsible for enforcing the uniqueness rule, even when requests arrive simultaneously.

## 3. Handling leaderboard ties

For the late-arrival leaderboard, I used competition ranking. Employees with the same total late minutes receive the same rank, and the next rank is skipped.

For example, if two employees share rank 1, the next employee receives rank 3.

The requested limit applies to the rank rather than strictly limiting the number of returned employees. Because of this, the response can contain more rows than the requested limit when employees are tied at the boundary. This keeps the ranking fair and consistent.

## 4. Calculating department headcount

I calculate department headcount from employee records rather than relying only on attendance logs.

This is important because an employee may belong to a department but have no attendance records for the reporting period. If I counted only attendance logs, that employee would be left out of the headcount.

The aggregation includes employees whose `joined_on` date is on or before the end of the reporting month, then matches the relevant attendance records. This allows employees with no attendance logs to be included in the headcount.

## 5. Improving performance in the future

For the current assignment, I focused on implementing the required functionality and using indexes to support the main queries.

If the application eventually handled around 100 times more data, repeatedly calculating monthly and department analytics from raw attendance records could become expensive.

At that point, I would consider maintaining daily summary collections so that frequently requested reports could use precomputed results instead of processing all attendance records each time.

Before introducing this change, I would measure query performance and inspect MongoDB execution plans. Precomputed summaries would also need to be updated whenever attendance records were corrected, so I would introduce them only when the performance benefits justified the extra complexity.

## Final note

My main goal was to keep the implementation straightforward while maintaining data integrity and producing consistent analytics. These decisions can be revisited as the application grows, based on actual performance measurements and changing requirements.