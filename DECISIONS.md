# Design Decisions — HROne Attendance & Analytics API

This document explains the main technical decisions made while implementing the attendance and analytics API, along with the reasoning behind each choice.

## 1. MongoDB indexes

I added unique indexes on `employees.emp_code` and the compound key `attendance_logs(emp_code, date)` to prevent duplicate employee codes and duplicate attendance records for the same employee and date.

I also considered the application's query patterns when selecting supporting indexes. Indexes on attendance employee/punch-in fields, attendance date/employee fields, and employee department/joined-date fields can help with attendance lookups and analytics filtering.

I avoided creating a large compound index covering every analytics field. Although indexes can improve read performance, each additional index increases storage usage and the cost of inserts and updates. The goal was to support the main query patterns without adding unnecessary write overhead.

The index definitions in this document should match the indexes actually created by the application.

## 2. Handling concurrent punch-in requests

A simple check-then-insert approach can cause problems when two identical punch-in requests arrive at nearly the same time. Both requests might check for an existing record before either has inserted one.

To avoid relying on that check alone, I use MongoDB's unique compound index on `attendance_logs(emp_code, date)` as the final safeguard.

If one request inserts the record successfully, a competing request with the same employee and date violates the unique constraint. The resulting `DuplicateKeyError` is handled and translated into HTTP `409 Conflict`.

This lets MongoDB enforce the uniqueness rule at the database level, even when requests overlap.

## 3. Leaderboard ranking and ties

The late-arrival leaderboard uses **competition ranking**. Employees with the same `total_late_minutes` receive the same rank, and the next rank is skipped.

For example:

| Employee | Total late minutes | Rank |
|---|---:|---:|
| Employee A | 120 | 1 |
| Employee B | 120 | 1 |
| Employee C | 90 | 3 |

The API returns employees whose rank is less than or equal to the requested limit. Because employees can share a rank, the response may contain more rows than the limit when a tie occurs at the boundary.

This approach preserves the ranking semantics rather than arbitrarily excluding employees who have the same result.

## 4. Department headcount

Department headcount should not depend only on attendance records. An employee may belong to a department during the reporting period without having any attendance logs.

For the department summary, I start with the employee collection and apply the `joined_on <= month_end` condition. The aggregation then joins the attendance records that match the reporting month.

This allows employees with no attendance records to remain part of the headcount, instead of disappearing from the summary simply because they have no attendance data.

The implementation should follow any additional employment-status or reporting-period rules specified in the assignment.

## 5. Scaling analytics in the future

The current approach is intended for the scope of this assignment. If the application grew to handle roughly 100 times the current data volume, repeatedly calculating complex analytics from raw attendance records could become a performance bottleneck.

At that scale, I would evaluate maintaining daily summary collections or materialized reporting data. Frequently requested monthly and departmental metrics could then be calculated from these summaries instead of repeatedly processing all the underlying attendance records.

Before making that change, I would measure actual query performance, inspect MongoDB execution plans, and identify which aggregations are most expensive. Precomputed summaries introduce additional complexity because they must remain consistent when attendance records are corrected.

For that reason, I would introduce them only when workload measurements justify the trade-off.

## Summary

These decisions focus on database-level data integrity, predictable API behavior, meaningful analytics results, and a clear path for future performance improvements. The implementation and its documentation should remain aligned, particularly for index definitions, ranking behavior, and aggregation logic.