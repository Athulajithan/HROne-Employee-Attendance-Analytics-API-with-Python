"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (loaded from a local .env for convenience)
"""
import calendar
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

_MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
_MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(_MONGO_URI)
db = client[_MONGO_DB]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def ensure_indexes() -> None:
    """Create required indexes idempotently at startup."""
    # Unique employee code
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uniq_emp_code",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="idx_department_joined_on",
    )
    # Unique (employee, date) pair — prevents duplicate punch-ins atomically
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uniq_emp_code_date",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", ASCENDING)],
        name="idx_emp_punch_in",
    )
    # Support fast attendance list queries (date desc, emp asc sort + range filter)
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_date_desc_emp_asc",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("emp_code", ASCENDING)],
        name="idx_date_emp_asc",
    )
    # Support analytics aggregations on emp+date+status
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],
        name="idx_emp_date_status",
    )


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def to_utc_datetime(epoch_ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime, truncated to whole seconds."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).replace(microsecond=0)


def as_utc(dt: datetime) -> datetime:
    """Ensure a datetime is UTC-aware and truncated to whole seconds.

    PyMongo returns naive datetimes that are actually UTC; this normalises them.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0)


def to_epoch_ms(dt: datetime) -> int:
    """datetime -> epoch milliseconds (whole seconds * 1000, no sub-second jitter)."""
    return int(as_utc(dt).timestamp()) * 1000


def half_up(value: Decimal, places: int) -> Decimal:
    """Round a Decimal half-up to the given number of decimal places."""
    quantizer = Decimal(10) ** -places
    return value.quantize(quantizer, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Attendance date and shift helpers (R1)
# ---------------------------------------------------------------------------


def attendance_date_for_punch(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """Return the IST attendance date string for a punch-in instant (R1).

    Overnight shifts (shift_end <= shift_start): a punch-in whose IST clock time
    is strictly earlier than shift_end belongs to the previous calendar day's shift.
    """
    local = as_utc(punch_in).astimezone(IST)
    start_t = time.fromisoformat(shift_start)
    end_t = time.fromisoformat(shift_end)

    att_day = local.date()

    if end_t <= start_t:  # overnight shift
        if local.time().replace(tzinfo=None) < end_t:
            att_day -= timedelta(days=1)

    return att_day.isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Return IST-aware (shift_start_dt, shift_end_dt) for the attendance day.

    For overnight shifts, shift_end is placed on date_str + 1 day.
    """
    att_day = date.fromisoformat(date_str)
    start_t = time.fromisoformat(shift_start)
    end_t = time.fromisoformat(shift_end)

    start_dt = datetime.combine(att_day, start_t, tzinfo=IST)
    end_dt = datetime.combine(att_day, end_t, tzinfo=IST)

    if end_t <= start_t:
        end_dt += timedelta(days=1)

    return start_dt, end_dt


# ---------------------------------------------------------------------------
# Derived-field calculators
# ---------------------------------------------------------------------------


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R2: 10-minute grace period; late_minutes = floor(elapsed minutes from shift_start)."""
    pi = as_utc(punch_in)
    start, _ = shift_bounds(date_str, shift_start, shift_end)
    elapsed = (pi - start.astimezone(timezone.utc)).total_seconds()
    if elapsed <= 600:  # <= 10 min: not late
        return 0
    return int(elapsed // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) / 3600, half-up to 2 dp."""
    elapsed = Decimal(str((as_utc(punch_out) - as_utc(punch_in)).total_seconds()))
    return float(half_up(elapsed / Decimal("3600"), 2))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: floor minutes after shift_end; zero when < 30 min."""
    _, end = shift_bounds(date_str, shift_start, shift_end)
    po = as_utc(punch_out)
    elapsed = (po - end.astimezone(timezone.utc)).total_seconds()
    if elapsed <= 0:
        return 0
    if elapsed < 1800:
        return 0
    return int(elapsed // 60)


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day when rounded work_hours < 4.50."""
    return work_hours < 4.50


# ---------------------------------------------------------------------------
# Response serialization
# ---------------------------------------------------------------------------


def _serialize_attendance(doc: dict) -> dict:
    """Convert a raw MongoDB attendance document to the API response shape."""
    doc.pop("_id", None)
    if doc.get("punch_in") is not None:
        doc["punch_in"] = to_epoch_ms(doc["punch_in"])
    if doc.get("punch_out") is not None:
        doc["punch_out"] = to_epoch_ms(doc["punch_out"])

    # Legacy records may omit these fields; defaults per DATA_MODEL.md
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("half_day", False)
    doc.setdefault("history", [])

    for entry in doc["history"]:
        if isinstance(entry.get("at"), datetime):
            entry["at"] = to_epoch_ms(entry["at"])
        for field, change in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                if isinstance(change.get("from"), datetime):
                    change["from"] = to_epoch_ms(change["from"])
                if isinstance(change.get("to"), datetime):
                    change["to"] = to_epoch_ms(change["to"])

    return doc


def _serialize_employee(doc: dict) -> dict:
    """Convert a raw MongoDB employee document to the API response shape."""
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = to_epoch_ms(doc["created_at"])
    return doc


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            parsed = date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        if parsed.isoformat() != v:
            raise ValueError("joined_on must use YYYY-MM-DD format")
        return v

    @model_validator(mode="after")
    def validate_shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(
        default=None, strict=True, ge=100_000_000_000, le=4_102_444_800_000
    )
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(
        default=None, strict=True, ge=100_000_000_000, le=4_102_444_800_000
    )


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(
        default=None, strict=True, ge=100_000_000_000, le=4_102_444_800_000
    )
    punch_out: Optional[int] = Field(
        default=None, strict=True, ge=100_000_000_000, le=4_102_444_800_000
    )
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# ---------------------------------------------------------------------------
# Month and working-day helpers
# ---------------------------------------------------------------------------


def _parse_month(month: str) -> tuple[date, date]:
    """Return (first_day, last_day) for a 'YYYY-MM' string."""
    year, mon = int(month[:4]), int(month[5:])
    first_day = date(year, mon, 1)
    last_day = date(year, mon, calendar.monthrange(year, mon)[1])
    return first_day, last_day


def _working_days_in_range(start: date, end: date) -> int:
    """Count Mon-Fri days from start to end inclusive."""
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def _round2(value) -> float:
    return float(half_up(Decimal(str(value)), 2))


def _round4(value) -> float:
    return float(half_up(Decimal(str(value)), 4))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


# ── Employees ────────────────────────────────────────────────────────────────


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)

    if db.employees.find_one({"emp_code": body.emp_code}) is not None:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return _serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query: dict = {}
    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size

    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    for emp in items:
        _serialize_employee(emp)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ── Punch-in ─────────────────────────────────────────────────────────────────


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts = to_utc_datetime(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    att_date = attendance_date_for_punch(ts, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(ts, emp["shift_start"], emp["shift_end"], att_date)

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": att_date}) is not None:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return _serialize_attendance(doc)


# ── Punch-out ────────────────────────────────────────────────────────────────


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        po_ts = to_utc_datetime(body.punched_at)
    else:
        po_ts = datetime.now(timezone.utc).replace(microsecond=0)

    # Most recent record with punch_in <= punch_out timestamp and still open
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": po_ts},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )

    if record is None:
        future_punch_in = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$gt": po_ts}},
            sort=[("punch_in", ASCENDING)],
        )
        if future_punch_in is not None:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")

        # Distinguish: already closed vs never punched in
        closed = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$lte": po_ts},
                "punch_out": {"$ne": None},
            },
            sort=[("punch_in", DESCENDING)],
        )
        if closed is not None:
            raise HTTPException(status_code=409, detail="Record already punched out")
        raise HTTPException(status_code=404, detail="No eligible punch-in found")

    pi_ts = as_utc(record["punch_in"])

    if po_ts <= pi_ts:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    if (po_ts - pi_ts).total_seconds() > 86400:
        raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    att_date = record["date"]
    wh = compute_work_hours(pi_ts, po_ts)
    ot = compute_overtime(po_ts, emp["shift_start"], emp["shift_end"], att_date)
    hd = compute_half_day(wh)

    # Atomic update: guard on punch_out still being None
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": po_ts,
                "work_hours": wh,
                "overtime_minutes": ot,
                "half_day": hd,
            }
        },
        return_document=True,
    )

    if result is None:
        raise HTTPException(status_code=409, detail="Record already punched out by concurrent request")

    return _serialize_attendance(result)


# ── Attendance list ───────────────────────────────────────────────────────────


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")

    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size

    docs = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": [_serialize_attendance(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ── Manual regularization ────────────────────────────────────────────────────


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    # 1. Verify employee and record exist
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    if body.status is None and body.punch_in is None and body.punch_out is None:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    # 2. Resolve intended final status
    new_status = body.status if body.status is not None else record["status"]
    is_absent_leave = new_status in ("ABSENT", "LEAVE")

    # 3. Validate: ABSENT/LEAVE cannot have punch times
    if is_absent_leave and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(
            status_code=422,
            detail="punch_in and punch_out must not be supplied with ABSENT or LEAVE",
        )

    # 4. Resolve punch_in
    if is_absent_leave:
        new_pi: Optional[datetime] = None
    elif body.punch_in is not None:
        new_pi = to_utc_datetime(body.punch_in)
    else:
        raw = record.get("punch_in")
        new_pi = as_utc(raw) if raw is not None else None

    # 5. Presence status requires punch_in
    if not is_absent_leave and new_pi is None:
        raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

    # 6. Validate punch_in attendance date (R1)
    if new_pi is not None:
        pi_att_date = attendance_date_for_punch(new_pi, emp["shift_start"], emp["shift_end"])
        if pi_att_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in does not belong to this attendance date (R1)",
            )

    # 7. Resolve punch_out
    if is_absent_leave:
        new_po: Optional[datetime] = None
    elif body.punch_out is not None:
        new_po = to_utc_datetime(body.punch_out)
    else:
        raw_po = record.get("punch_out")
        new_po = as_utc(raw_po) if raw_po is not None else None

    # 8. Validate punch_out
    if new_po is not None:
        if new_pi is None:
            raise HTTPException(status_code=422, detail="punch_out requires punch_in")
        if new_po <= new_pi:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if (new_po - new_pi).total_seconds() > 86400:
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    # 9. Recompute derived fields
    if is_absent_leave:
        new_late = 0
        new_wh: Optional[float] = None
        new_ot = 0
        new_hd = False
    else:
        new_late = compute_late_minutes(new_pi, emp["shift_start"], emp["shift_end"], date) if new_pi else 0
        if new_pi and new_po:
            new_wh = compute_work_hours(new_pi, new_po)
            new_ot = compute_overtime(new_po, emp["shift_start"], emp["shift_end"], date)
            new_hd = compute_half_day(new_wh)
        else:
            new_wh = None
            new_ot = record.get("overtime_minutes", 0)
            new_hd = record.get("half_day", False)

    # 10. Detect what actually changed
    old_pi = as_utc(record["punch_in"]) if record.get("punch_in") is not None else None
    old_po = as_utc(record["punch_out"]) if record.get("punch_out") is not None else None
    old_status = record["status"]
    old_late = record.get("late_minutes", 0)
    old_wh = record.get("work_hours")
    old_ot = record.get("overtime_minutes", 0)
    old_hd = record.get("half_day", False)

    changes: dict = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_pi != old_pi:
        changes["punch_in"] = {
            "from": to_epoch_ms(old_pi) if old_pi else None,
            "to": to_epoch_ms(new_pi) if new_pi else None,
        }
    if new_po != old_po:
        changes["punch_out"] = {
            "from": to_epoch_ms(old_po) if old_po else None,
            "to": to_epoch_ms(new_po) if new_po else None,
        }
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_wh != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": new_wh}
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}
    if new_hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": new_hd}

    # 11. No-op check
    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    # 12. Build history entry — store punch datetimes as BSON dates in MongoDB
    history_changes_db: dict = {}
    for field, change in changes.items():
        if field in ("punch_in", "punch_out"):
            history_changes_db[field] = {
                "from": to_utc_datetime(change["from"]) if change["from"] is not None else None,
                "to": to_utc_datetime(change["to"]) if change["to"] is not None else None,
            }
        else:
            history_changes_db[field] = change

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": history_changes_db,
    }

    # 13. Atomic update — guard on history length to detect concurrent edits
    expected_len = len(record.get("history", []))
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, expected_len]},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_pi,
                "punch_out": new_po,
                "work_hours": new_wh,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )

    if result is None:
        raise HTTPException(status_code=409, detail="Concurrent modification; please retry")

    return _serialize_attendance(result)


# ---------------------------------------------------------------------------
# Analytics: employee monthly
# ---------------------------------------------------------------------------


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    first_day, last_day = _parse_month(month)
    first_str, last_str = first_day.isoformat(), last_day.isoformat()

    # R7: working days from max(first_day, joined_on) to last_day
    joined = date.fromisoformat(emp["joined_on"])
    work_start = max(first_day, joined)
    working_days = _working_days_in_range(work_start, last_day) if work_start <= last_day else 0

    rows = list(
        db.attendance_logs.find(
            {"emp_code": emp_code, "date": {"$gte": first_str, "$lte": last_str}},
            {"_id": 0, "status": 1, "date": 1, "late_minutes": 1, "overtime_minutes": 1, "half_day": 1},
        )
    )

    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late = 0
    total_ot = 0

    for row in rows:
        status = row.get("status")
        date_value = row.get("date")
        if not date_value:
            continue

        weekday = datetime.strptime(date_value, "%Y-%m-%d").weekday() + 2
        if status in {"PRESENT", "WFH", "ON_DUTY"} and 2 <= weekday <= 6:
            present_days += 0.5 if row.get("half_day") else 1.0

        if status == "LEAVE":
            leave_days += 1

        late_minutes = int(row.get("late_minutes") or 0)
        if late_minutes > 0:
            late_count += 1
            total_late += late_minutes

        total_ot += int(row.get("overtime_minutes") or 0)

    attendance_pct = (
        None if working_days == 0 else _round4(present_days / working_days * 100)
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": _round2(present_days),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


# ---------------------------------------------------------------------------
# Analytics: department summary
# ---------------------------------------------------------------------------


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first_day, last_day = _parse_month(month)
    first_str, last_str = first_day.isoformat(), last_day.isoformat()

    emp_match: dict = {"joined_on": {"$lte": last_str}}
    if department:
        emp_match["department"] = department

    emp_docs = list(
        db.employees.find(
            emp_match,
            {"_id": 0, "emp_code": 1, "department": 1},
        ).sort("department", ASCENDING)
    )
    logs = list(
        db.attendance_logs.find(
            {"date": {"$gte": first_str, "$lte": last_str}},
            {
                "_id": 0,
                "emp_code": 1,
                "status": 1,
                "late_minutes": 1,
                "half_day": 1,
                "work_hours": 1,
                "date": 1,
            },
        )
    )

    # Map persisted attendance to employees so the result remains department-based
    emp_by_code = {doc["emp_code"]: doc["department"] for doc in emp_docs}
    grouped: dict[str, dict[str, Any]] = {}
    for doc in emp_docs:
        dept = doc["department"]
        grouped.setdefault(dept, {
            "department": dept,
            "headcount": 0,
            "present_days": 0.0,
            "work_hours_total": 0.0,
            "work_hours_count": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "leave_count": 0,
            "on_duty_count": 0,
        })
        grouped[dept]["headcount"] += 1

    for record in logs:
        dept = emp_by_code.get(record.get("emp_code"))
        if dept is None:
            continue
        if record.get("status") in {"PRESENT", "WFH", "ON_DUTY"}:
            weekday = datetime.strptime(record["date"], "%Y-%m-%d").weekday() + 2
            if 2 <= weekday <= 6:
                if record.get("half_day", False):
                    grouped[dept]["present_days"] += 0.5
                else:
                    grouped[dept]["present_days"] += 1.0
        if record.get("status") == "LEAVE":
            grouped[dept]["leave_count"] += 1
        if record.get("status") == "ON_DUTY":
            grouped[dept]["on_duty_count"] += 1
        if record.get("late_minutes", 0) or 0 > 0:
            grouped[dept]["late_count"] += 1
            grouped[dept]["total_late_minutes"] += int(record.get("late_minutes", 0) or 0)
        if record.get("status") in {"PRESENT", "WFH", "ON_DUTY"} and record.get("work_hours") is not None:
            grouped[dept]["work_hours_total"] += float(record["work_hours"])
            grouped[dept]["work_hours_count"] += 1

    items = []
    for dept in sorted(grouped):
        stats = grouped[dept]
        avg_wh = None
        if stats["work_hours_count"]:
            avg_wh = _round2(stats["work_hours_total"] / stats["work_hours_count"])
        items.append({
            "department": dept,
            "headcount": stats["headcount"],
            "present_days": _round2(stats["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": stats["late_count"],
            "total_late_minutes": stats["total_late_minutes"],
            "leave_count": stats["leave_count"],
            "on_duty_count": stats["on_duty_count"],
        })

    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# Analytics: late leaderboard
# ---------------------------------------------------------------------------


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    first_day, last_day = _parse_month(month)
    first_str, last_str = first_day.isoformat(), last_day.isoformat()

    emp_match: dict = {}
    if department:
        emp_match["department"] = department

    emp_docs = list(
        db.employees.find(emp_match, {"_id": 0, "emp_code": 1, "name": 1, "department": 1}).sort("emp_code", ASCENDING)
    )
    late_logs = list(
        db.attendance_logs.find(
            {"date": {"$gte": first_str, "$lte": last_str}, "late_minutes": {"$gt": 0}},
            {"_id": 0, "emp_code": 1, "late_minutes": 1},
        )
    )

    late_by_emp: dict[str, dict[str, Any]] = {}
    for record in late_logs:
        emp_code = record["emp_code"]
        bucket = late_by_emp.setdefault(emp_code, {"total_late_minutes": 0, "late_count": 0})
        bucket["total_late_minutes"] += int(record.get("late_minutes", 0) or 0)
        bucket["late_count"] += 1

    ranked = []
    for employee in emp_docs:
        emp_code = employee["emp_code"]
        stats = late_by_emp.get(emp_code)
        if stats is None:
            continue
        ranked.append({
            "emp_code": emp_code,
            "name": employee["name"],
            "department": employee["department"],
            "total_late_minutes": stats["total_late_minutes"],
            "late_count": stats["late_count"],
        })

    ranked.sort(key=lambda item: (-item["total_late_minutes"], item["emp_code"]))

    items = []
    prev_total = None
    current_rank = 0
    for index, row in enumerate(ranked, start=1):
        if prev_total is None or row["total_late_minutes"] != prev_total:
            current_rank = index
            prev_total = row["total_late_minutes"]
        if current_rank > limit:
            continue
        items.append({
            "rank": current_rank,
            "emp_code": row["emp_code"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": row["total_late_minutes"],
            "late_count": row["late_count"],
        })

    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# Analytics: department trend
# ---------------------------------------------------------------------------


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    try:
        from_date = date.fromisoformat(from_)
        to_date = date.fromisoformat(to)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to must not be before from")

    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")

    if db.employees.find_one({"department": department}) is None:
        raise HTTPException(status_code=404, detail="Department not found")

    all_emps = list(
        db.employees.find(
            {"department": department},
            {"emp_code": 1, "joined_on": 1, "_id": 0},
        )
    )
    emp_codes = [e["emp_code"] for e in all_emps]
    joined_dates = [date.fromisoformat(e["joined_on"]) for e in all_emps]

    from_str, to_str = from_date.isoformat(), to_date.isoformat()
    from_dt = datetime.combine(from_date, time.min, tzinfo=timezone.utc)
    to_dt = datetime.combine(to_date, time.max, tzinfo=timezone.utc)

    log_pipeline: list = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_str, "$lte": to_str},
            }
        },
        {
            "$project": {
                "day": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
                "present_count_raw": {
                    "$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                        0,
                    ]
                },
                "late_count": {
                    "$cond": [
                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                        1,
                        0,
                    ]
                },
            }
        },
        {"$group": {"_id": "$day", "present_count_raw": {"$sum": "$present_count_raw"}, "late_count": {"$sum": "$late_count"}}},
        {"$sort": {"_id": 1}},
        {
            "$densify": {
                "field": "_id",
                "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt]},
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                "present_count_raw": {"$ifNull": ["$present_count_raw", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
    ]

    try:
        log_rows = {row["date"]: row for row in db.attendance_logs.aggregate(log_pipeline)}
    except NotImplementedError:
        log_rows = {}
        current = from_date
        while current <= to_date:
            d_str = current.isoformat()
            log_rows[d_str] = {"date": d_str, "present_count_raw": 0, "late_count": 0}
            current += timedelta(days=1)

        all_logs = list(db.attendance_logs.find({"emp_code": {"$in": emp_codes}, "date": {"$gte": from_str, "$lte": to_str}}, {"_id": 0, "date": 1, "status": 1, "late_minutes": 1, "half_day": 1}))
        for record in all_logs:
            d_str = record["date"]
            if d_str not in log_rows:
                log_rows[d_str] = {"date": d_str, "present_count_raw": 0, "late_count": 0}
            if record.get("status") in {"PRESENT", "WFH", "ON_DUTY"}:
                log_rows[d_str]["present_count_raw"] += 0.5 if record.get("half_day", False) else 1.0
            if int(record.get("late_minutes") or 0) > 0:
                log_rows[d_str]["late_count"] += 1

    items = []
    rate_history: list = []
    current = from_date
    while current <= to_date:
        d_str = current.isoformat()
        is_wd = current.weekday() < 5
        headcount = sum(1 for jd in joined_dates if jd <= current)
        log = log_rows.get(d_str, {"present_count_raw": 0, "late_count": 0})
        present_count = float(log.get("present_count_raw", 0))
        late_count = int(log.get("late_count", 0))

        rate = _round4(present_count / headcount) if is_wd and headcount > 0 else None
        rate_history.append(rate)
        window = rate_history[-7:]
        non_null = [r for r in window if r is not None]
        moving_avg = _round4(sum(non_null) / len(non_null)) if non_null else None

        items.append({
            "date": d_str,
            "is_working_day": is_wd,
            "headcount": headcount,
            "present_count": _round2(present_count),
            "late_count": late_count,
            "attendance_rate": rate,
            "moving_avg_7d": moving_avg,
        })
        current += timedelta(days=1)

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Admin: explain
# ---------------------------------------------------------------------------


_VALID_EXPLAIN_ENDPOINTS = {
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
}


def _json_safe(obj: Any) -> Any:
    """Recursively make MongoDB explain output JSON-serializable."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(i) for i in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint not in _VALID_EXPLAIN_ENDPOINTS:
        raise HTTPException(
            status_code=422,
            detail=f"endpoint must be one of: {', '.join(sorted(_VALID_EXPLAIN_ENDPOINTS))}",
        )

    collection = "attendance_logs"
    explain_doc: Any

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must not be after date_to")
        query: dict = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        if status:
            query["status"] = status
        skip = (page - 1) * page_size
        explain_doc = (
            db.attendance_logs.find(query)
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip(skip)
            .limit(page_size)
            .explain("executionStats")
        )

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        first_day, last_day = _parse_month(month)
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()},
                }
            }
        ]
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        _, last_day = _parse_month(month)
        emp_q: dict = {"joined_on": {"$lte": last_day.isoformat()}}
        if department:
            emp_q["department"] = department
        collection = "employees"
        explain_doc = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": [{"$match": emp_q}], "cursor": {}},
            verbosity="executionStats",
        )

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        emp_q = {}
        if department:
            emp_q["department"] = department
        collection = "employees"
        explain_doc = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": [{"$match": emp_q}], "cursor": {}},
            verbosity="executionStats",
        )

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        ec_list = [
            e["emp_code"]
            for e in db.employees.find({"department": department}, {"emp_code": 1, "_id": 0})
        ]
        query = {"emp_code": {"$in": ec_list}, "date": {"$gte": from_, "$lte": to}}
        explain_doc = db.command(
            "explain",
            {"find": "attendance_logs", "filter": query},
            verbosity="executionStats",
        )

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": _json_safe(explain_doc),
    }
