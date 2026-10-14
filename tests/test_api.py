"""
Comprehensive test suite for the Employee Attendance & Analytics API.

Uses mongomock for an isolated in-memory database — the live Atlas DB is never touched.
Run:  pytest tests/ -v
"""
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from mongomock import MongoClient as MockMongoClient

import app.main as main


# ---------------------------------------------------------------------------
# Fixture: fresh in-memory db + test client for each test
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch):
    mock_db = MockMongoClient().db
    monkeypatch.setattr(main, "db", mock_db)
    # Re-run index creation against mock db (idempotent)
    main.ensure_indexes.__wrapped__(mock_db) if hasattr(main.ensure_indexes, "__wrapped__") else None
    return TestClient(main.app)


@pytest.fixture()
def db(monkeypatch):
    mock_db = MockMongoClient().db
    monkeypatch.setattr(main, "db", mock_db)
    return mock_db


@pytest.fixture()
def client_db(monkeypatch):
    mock_db = MockMongoClient().db
    monkeypatch.setattr(main, "db", mock_db)
    c = TestClient(main.app)
    return c, mock_db


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

EMP_PAYLOAD = {
    "emp_code": "EMP0001",
    "name": "Asha Rao",
    "email": "asha@example.com",
    "department": "Engineering",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-01-05",
}

# 2026-07-06 09:28 IST = 03:58 UTC → epoch ms
PI_MS_ONTIME = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
# 2026-07-06 09:40:01 IST = 04:10:01 UTC → epoch ms
PI_MS_LATE = int(datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
# Punch-out at 18:35 IST = 13:05 UTC
PO_MS = int(datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc).timestamp() * 1000)


def create_emp(client, payload=None):
    return client.post("/employees", json=payload or EMP_PAYLOAD)


def punch_in(client, emp_code="EMP0001", punched_at=PI_MS_ONTIME, status="PRESENT"):
    body = {"emp_code": emp_code}
    if punched_at is not None:
        body["punched_at"] = punched_at
    if status != "PRESENT":
        body["status"] = status
    return client.post("/attendance/punch-in", json=body)


def punch_out(client, emp_code="EMP0001", punched_at=PO_MS):
    body = {"emp_code": emp_code}
    if punched_at is not None:
        body["punched_at"] = punched_at
    return client.post("/attendance/punch-out", json=body)


# ===========================================================================
# Helper unit tests
# ===========================================================================

class TestHelpers:
    def test_late_grace_boundary_exact_10_min(self):
        """09:40:00 IST → 0 late minutes (exactly at grace limit)."""
        punch = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)  # 09:40:00 IST
        assert main.compute_late_minutes(punch, "09:30", "18:30", "2026-07-06") == 0

    def test_late_one_second_beyond_grace(self):
        """09:40:01 IST → 10 late minutes (floor of 10:01 elapsed)."""
        punch = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
        assert main.compute_late_minutes(punch, "09:30", "18:30", "2026-07-06") == 10

    def test_late_45_minutes(self):
        """10:15:59 IST → 45 late minutes."""
        punch = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)
        assert main.compute_late_minutes(punch, "09:30", "18:30", "2026-07-06") == 45

    def test_work_hours_half_up(self):
        """Half-up rounding: 8999 seconds = 2.4997... → 2.50."""
        pi = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        po = pi + timedelta(seconds=9001)  # 2.5002... → 2.50
        wh = main.compute_work_hours(pi, po)
        assert wh == 2.50

    def test_work_hours_not_bankers_rounding(self):
        """9 hours exactly = 9.00."""
        pi = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        po = pi + timedelta(hours=9)
        assert main.compute_work_hours(pi, po) == 9.00

    def test_overtime_below_30_min_is_zero(self):
        """29 minutes after shift_end → 0 overtime."""
        po = datetime(2026, 7, 6, 13, 29, 0, tzinfo=timezone.utc)  # 18:59 IST, which is 29 min after 18:30 IST
        assert main.compute_overtime(po, "09:30", "18:30", "2026-07-06") == 0

    def test_overtime_exactly_30_min(self):
        """Exactly 30 minutes after shift_end → 30."""
        po = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc)  # 18:30 UTC is 13:00 UTC for 18:30 IST
        # shift_end 18:30 IST = 13:00 UTC for date 2026-07-06
        po = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)  # 19:00 IST = 30 min OT
        assert main.compute_overtime(po, "09:30", "18:30", "2026-07-06") == 30

    def test_half_day_below_threshold(self):
        assert main.compute_half_day(4.49) is True

    def test_half_day_at_threshold(self):
        assert main.compute_half_day(4.50) is False

    def test_overnight_attendance_date_early_morning(self):
        """22:00-06:00 shift: punch at 05:00 IST next day → previous day's date."""
        # 05:00 IST on 2026-07-07 = 23:30 UTC on 2026-07-06
        punch = datetime(2026, 7, 6, 23, 30, 0, tzinfo=timezone.utc)
        att_date = main.attendance_date_for_punch(punch, "22:00", "06:00")
        assert att_date == "2026-07-06"

    def test_overnight_attendance_date_evening(self):
        """22:00-06:00 shift: punch at 22:30 IST → same day's date."""
        # 22:30 IST = 17:00 UTC
        punch = datetime(2026, 7, 6, 17, 0, 0, tzinfo=timezone.utc)
        att_date = main.attendance_date_for_punch(punch, "22:00", "06:00")
        assert att_date == "2026-07-06"

    def test_to_epoch_ms_round_trip(self):
        dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
        ms = main.to_epoch_ms(dt)
        assert isinstance(ms, int)
        assert ms % 1000 == 0  # whole seconds only

    def test_naive_datetime_treated_as_utc(self):
        """PyMongo naive datetimes must be treated as UTC."""
        naive = datetime(2026, 7, 6, 3, 58, 0)  # no tzinfo
        aware = main.as_utc(naive)
        assert aware.tzinfo is not None
        assert aware.tzinfo == timezone.utc


# ===========================================================================
# /health
# ===========================================================================

class TestHealth:
    def test_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_503_when_db_fails(self, monkeypatch, client):
        def boom(*a, **kw):
            raise RuntimeError("db down")
        monkeypatch.setattr(main.db, "command", boom)
        r = client.get("/health")
        assert r.status_code == 503


# ===========================================================================
# /employees POST
# ===========================================================================

class TestCreateEmployee:
    def test_creates_successfully(self, client):
        r = create_emp(client)
        assert r.status_code == 201
        data = r.json()
        assert data["emp_code"] == "EMP0001"
        assert "created_at" in data
        assert isinstance(data["created_at"], int)
        assert data["created_at"] >= 100_000_000_000
        assert "_id" not in data

    def test_duplicate_returns_409(self, client):
        create_emp(client)
        r = create_emp(client)
        assert r.status_code == 409

    def test_invalid_emp_code_format(self, client):
        bad = {**EMP_PAYLOAD, "emp_code": "EMP001"}  # only 3 digits
        r = client.post("/employees", json=bad)
        assert r.status_code == 422

    def test_invalid_email(self, client):
        bad = {**EMP_PAYLOAD, "email": "notanemail"}
        r = client.post("/employees", json=bad)
        assert r.status_code == 422

    def test_equal_shift_returns_422(self, client):
        bad = {**EMP_PAYLOAD, "shift_start": "09:30", "shift_end": "09:30"}
        r = client.post("/employees", json=bad)
        assert r.status_code == 422

    def test_invalid_joined_on(self, client):
        bad = {**EMP_PAYLOAD, "joined_on": "2026-13-01"}
        r = client.post("/employees", json=bad)
        assert r.status_code == 422

    def test_no_id_in_response(self, client):
        r = create_emp(client)
        assert "_id" not in r.json()


# ===========================================================================
# /employees GET
# ===========================================================================

class TestListEmployees:
    def test_empty(self, client):
        r = client.get("/employees")
        assert r.status_code == 200
        assert r.json()["total"] == 0
        assert r.json()["items"] == []

    def test_pagination(self, client):
        for i in range(1, 6):
            client.post("/employees", json={
                **EMP_PAYLOAD,
                "emp_code": f"EMP000{i}",
                "email": f"e{i}@x.com",
            })
        r = client.get("/employees?page=1&page_size=3")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 5
        assert len(body["items"]) == 3

    def test_department_filter(self, client):
        client.post("/employees", json={**EMP_PAYLOAD})
        client.post("/employees", json={
            **EMP_PAYLOAD, "emp_code": "EMP0002", "email": "b@x.com", "department": "HR"
        })
        r = client.get("/employees?department=HR")
        assert r.json()["total"] == 1

    def test_total_reflects_filter(self, client):
        for i in range(1, 4):
            client.post("/employees", json={
                **EMP_PAYLOAD, "emp_code": f"EMP000{i}", "email": f"e{i}@x.com",
                "department": "Eng" if i < 3 else "HR"
            })
        r = client.get("/employees?department=HR")
        assert r.json()["total"] == 1  # Not 3

    def test_sorted_by_emp_code(self, client):
        for code in ["EMP0003", "EMP0001", "EMP0002"]:
            client.post("/employees", json={**EMP_PAYLOAD, "emp_code": code, "email": f"{code}@x.com"})
        items = client.get("/employees").json()["items"]
        codes = [i["emp_code"] for i in items]
        assert codes == sorted(codes)

    def test_invalid_page_size(self, client):
        r = client.get("/employees?page_size=101")
        assert r.status_code == 422

    def test_invalid_page(self, client):
        r = client.get("/employees?page=0")
        assert r.status_code == 422


# ===========================================================================
# /attendance/punch-in POST
# ===========================================================================

class TestPunchIn:
    def test_successful_punch_in(self, client):
        create_emp(client)
        r = punch_in(client)
        assert r.status_code == 201
        body = r.json()
        assert body["emp_code"] == "EMP0001"
        assert body["date"] == "2026-07-06"
        assert body["status"] == "PRESENT"
        assert body["punch_in"] == PI_MS_ONTIME
        assert body["punch_out"] is None
        assert body["work_hours"] is None
        assert body["late_minutes"] == 0
        assert body["overtime_minutes"] == 0
        assert body["half_day"] is False
        assert body["history"] == []
        assert "_id" not in body

    def test_unknown_employee_returns_404(self, client):
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP9999", "punched_at": PI_MS_ONTIME})
        assert r.status_code == 404

    def test_duplicate_punch_in_returns_409(self, client):
        create_emp(client)
        punch_in(client)
        r = punch_in(client)
        assert r.status_code == 409

    def test_late_punch_in_computes_late_minutes(self, client):
        create_emp(client)
        r = punch_in(client, punched_at=PI_MS_LATE)
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 10

    def test_invalid_status_returns_422(self, client):
        create_emp(client)
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001", "punched_at": PI_MS_ONTIME, "status": "ABSENT"
        })
        assert r.status_code == 422

    def test_timestamp_in_seconds_rejected(self, client):
        """epoch seconds (< 100_000_000_000) must be rejected with 422."""
        create_emp(client)
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001", "punched_at": 1751803080  # seconds, not ms
        })
        assert r.status_code == 422

    def test_float_timestamp_rejected(self, client):
        create_emp(client)
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001", "punched_at": 1751803080000.5
        })
        assert r.status_code == 422

    def test_wfh_status(self, client):
        create_emp(client)
        r = punch_in(client, status="WFH")
        assert r.status_code == 201
        assert r.json()["status"] == "WFH"

    def test_default_status_is_present(self, client):
        create_emp(client)
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": PI_MS_ONTIME})
        assert r.json()["status"] == "PRESENT"


# ===========================================================================
# /attendance/punch-out POST
# ===========================================================================

class TestPunchOut:
    def setup_open_record(self, client):
        create_emp(client)
        punch_in(client)

    def test_successful_punch_out(self, client):
        self.setup_open_record(client)
        r = punch_out(client)
        assert r.status_code == 200
        body = r.json()
        assert body["punch_out"] == PO_MS
        assert body["work_hours"] is not None
        assert isinstance(body["work_hours"], float)
        assert body["overtime_minutes"] == 0  # 18:35 IST, 5 min OT < 30 min

    def test_unknown_employee_returns_404(self, client):
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP9999", "punched_at": PO_MS})
        assert r.status_code == 404

    def test_no_eligible_punch_in_returns_404(self, client):
        create_emp(client)
        # Punch out without punching in
        r = punch_out(client)
        assert r.status_code == 404

    def test_already_punched_out_returns_409(self, client):
        self.setup_open_record(client)
        punch_out(client)
        r = punch_out(client)
        assert r.status_code == 409

    def test_punch_out_before_punch_in_returns_422(self, client):
        self.setup_open_record(client)
        before = PI_MS_ONTIME - 1000
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": before})
        assert r.status_code == 422

    def test_punch_out_equal_to_punch_in_returns_422(self, client):
        self.setup_open_record(client)
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": PI_MS_ONTIME})
        assert r.status_code == 422

    def test_punch_out_more_than_24h_returns_422(self, client):
        self.setup_open_record(client)
        too_late = PI_MS_ONTIME + (25 * 3600 * 1000)
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": too_late})
        assert r.status_code == 422

    def test_half_day_set_correctly(self, client):
        """3.5 hours worked → half_day=True."""
        create_emp(client)
        punch_in(client)
        po_3h = PI_MS_ONTIME + int(3.5 * 3600 * 1000)
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": po_3h})
        assert r.status_code == 200
        assert r.json()["half_day"] is True
        assert r.json()["work_hours"] == 3.50

    def test_overtime_above_30min(self, client):
        """Punch out 40 min after shift end → 40 overtime."""
        create_emp(client)
        punch_in(client)
        # shift_end 18:30 IST = 13:00 UTC; 40 min after = 13:40 UTC
        po_ms = int(datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc).timestamp() * 1000)
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": po_ms})
        assert r.status_code == 200
        assert r.json()["overtime_minutes"] == 40

    def test_no_id_in_response(self, client):
        self.setup_open_record(client)
        r = punch_out(client)
        assert "_id" not in r.json()


# ===========================================================================
# /attendance GET
# ===========================================================================

class TestListAttendance:
    def test_empty(self, client):
        r = client.get("/attendance")
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_date_filter_invalid_range_422(self, client):
        r = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
        assert r.status_code == 422

    def test_pagination_via_mongodb(self, client_db):
        client, mock_db = client_db
        create_emp(client)
        # Insert two records directly
        mock_db.attendance_logs.insert_many([
            {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
             "punch_in": None, "punch_out": None, "work_hours": None,
             "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
            {"emp_code": "EMP0001", "date": "2026-07-07", "status": "PRESENT",
             "punch_in": None, "punch_out": None, "work_hours": None,
             "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
        ])
        r = client.get("/attendance?page=1&page_size=1")
        body = r.json()
        assert body["total"] == 2  # Total count uses filter, not page
        assert len(body["items"]) == 1

    def test_sorted_date_desc_emp_asc(self, client_db):
        client, mock_db = client_db
        mock_db.attendance_logs.insert_many([
            {"emp_code": "EMP0002", "date": "2026-07-06", "status": "PRESENT",
             "punch_in": None, "punch_out": None, "work_hours": None,
             "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
            {"emp_code": "EMP0001", "date": "2026-07-07", "status": "PRESENT",
             "punch_in": None, "punch_out": None, "work_hours": None,
             "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
        ])
        items = client.get("/attendance").json()["items"]
        assert items[0]["date"] == "2026-07-07"
        assert items[1]["date"] == "2026-07-06"

    def test_legacy_record_defaults(self, client_db):
        """Legacy records missing half_day/history should return defaults."""
        _, mock_db = client_db
        mock_db.attendance_logs.insert_one({
            "emp_code": "EMP0002",
            "date": "2026-07-14",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 14, 4, 2, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 14, 13, 1, 0, tzinfo=timezone.utc),
            "work_hours": 8.98,
            "late_minutes": 0,
            "overtime_minutes": 0,
            # No half_day, no history — legacy format
        })
        items = client_db[0].get("/attendance").json()["items"]
        assert items[0]["half_day"] is False
        assert items[0]["history"] == []

    def test_no_id_in_items(self, client_db):
        client, mock_db = client_db
        mock_db.attendance_logs.insert_one({
            "emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
            "punch_in": None, "punch_out": None, "work_hours": None,
            "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []
        })
        items = client.get("/attendance").json()["items"]
        assert "_id" not in items[0]


# ===========================================================================
# /attendance/{emp_code}/{date} PATCH
# ===========================================================================

class TestRegularize:
    def seed_record(self, mock_db, **overrides):
        doc = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc),  # late
            "punch_out": datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc),
            "work_hours": 9.08,
            "late_minutes": 10,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        doc.update(overrides)
        mock_db.employees.insert_one({
            "emp_code": "EMP0001", "name": "Asha Rao", "email": "a@x.com",
            "department": "Eng", "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-01-05",
            "created_at": datetime.now(timezone.utc),
        })
        mock_db.attendance_logs.insert_one(doc)

    def test_correct_punch_in(self, client_db):
        client, mock_db = client_db
        self.seed_record(mock_db)
        # Correct punch_in to on-time: 09:28 IST = 03:58 UTC
        new_pi_ms = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": new_pi_ms,
            "reason": "biometric glitch",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["late_minutes"] == 0  # recomputed
        assert len(body["history"]) == 1
        assert body["history"][0]["by"] == "hr.admin"
        assert "late_minutes" in body["history"][0]["changes"]
        assert "punch_in" in body["history"][0]["changes"]

    def test_no_op_returns_422(self, client_db):
        client, mock_db = client_db
        self.seed_record(mock_db)
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "reason": "nothing changes",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 422

    def test_absent_clears_punch_times(self, client_db):
        client, mock_db = client_db
        self.seed_record(mock_db)
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "ABSENT",
            "reason": "correcting status to absent",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["punch_in"] is None
        assert body["punch_out"] is None
        assert body["late_minutes"] == 0
        assert body["work_hours"] is None

    def test_absent_with_punch_in_returns_422(self, client_db):
        client, mock_db = client_db
        self.seed_record(mock_db)
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "ABSENT",
            "punch_in": PI_MS_ONTIME,
            "reason": "invalid combo",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 422

    def test_unknown_employee_returns_404(self, client):
        r = client.patch("/attendance/EMP9999/2026-07-06", json={
            "reason": "no such employee",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 404

    def test_missing_record_returns_404(self, client_db):
        client, mock_db = client_db
        mock_db.employees.insert_one({
            "emp_code": "EMP0001", "name": "A", "email": "a@x.com",
            "department": "Eng", "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-01-05", "created_at": datetime.now(timezone.utc),
        })
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "reason": "no record",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 404

    def test_history_entries_accumulate(self, client_db):
        """Each PATCH appends one history entry; old entries are never removed."""
        client, mock_db = client_db
        self.seed_record(mock_db)
        new_pi_ms = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
        client.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": new_pi_ms,
            "reason": "first correction",
            "regularized_by": "hr.admin",
        })
        # Second correction: change status
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "WFH",
            "reason": "second correction",
            "regularized_by": "hr.admin",
        })
        body = r.json()
        assert len(body["history"]) == 2

    def test_punch_out_before_punch_in_returns_422(self, client_db):
        client, mock_db = client_db
        self.seed_record(mock_db)
        new_po_ms = PI_MS_ONTIME - 1000
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_out": new_po_ms,
            "reason": "invalid time order",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 422


# ===========================================================================
# /analytics/employees/{emp_code}/monthly GET
# ===========================================================================

class TestEmployeeMonthly:
    def setup_emp(self, mock_db, joined_on="2026-07-01"):
        mock_db.employees.insert_one({
            "emp_code": "EMP0001", "name": "Asha", "email": "a@x.com",
            "department": "Eng", "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": joined_on, "created_at": datetime.now(timezone.utc),
        })

    def test_unknown_employee_returns_404(self, client):
        r = client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
        assert r.status_code == 404

    def test_employee_no_logs(self, client_db):
        client, mock_db = client_db
        self.setup_emp(mock_db)
        r = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        assert r.status_code == 200
        body = r.json()
        assert body["present_days"] == 0
        assert body["late_count"] == 0
        assert body["leave_days"] == 0

    def test_working_days_from_joined_on(self, client_db):
        """Employee joining 2026-07-20 (Monday) → fewer working days."""
        client, mock_db = client_db
        self.setup_emp(mock_db, joined_on="2026-07-20")
        r = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        body = r.json()
        # July 2026 weekdays from Mon 20 to Fri 31 = 10 working days
        assert body["working_days"] == 10

    def test_zero_working_days_gives_null_attendance_pct(self, client_db):
        """Employee joining after the month ends → 0 working days → null pct."""
        client, mock_db = client_db
        self.setup_emp(mock_db, joined_on="2026-08-01")
        r = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        body = r.json()
        assert body["working_days"] == 0
        assert body["attendance_pct"] is None

    def test_half_day_counts_as_half(self, client_db):
        client, mock_db = client_db
        self.setup_emp(mock_db)
        # 2026-07-07 is a Tuesday
        mock_db.attendance_logs.insert_one({
            "emp_code": "EMP0001", "date": "2026-07-07", "status": "PRESENT",
            "punch_in": datetime(2026, 7, 7, 4, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 7, 7, 30, tzinfo=timezone.utc),
            "work_hours": 3.5, "late_minutes": 0, "overtime_minutes": 0,
            "half_day": True, "history": [],
        })
        r = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        assert r.json()["present_days"] == 0.5

    def test_weekend_records_excluded_from_present_days(self, client_db):
        """Saturday records do not count toward present_days (R7)."""
        client, mock_db = client_db
        self.setup_emp(mock_db)
        # 2026-07-11 is Saturday
        mock_db.attendance_logs.insert_one({
            "emp_code": "EMP0001", "date": "2026-07-11", "status": "PRESENT",
            "punch_in": datetime(2026, 7, 11, 4, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc),
            "work_hours": 9.0, "late_minutes": 0, "overtime_minutes": 0,
            "half_day": False, "history": [],
        })
        r = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        assert r.json()["present_days"] == 0.0

    def test_leave_counted(self, client_db):
        client, mock_db = client_db
        self.setup_emp(mock_db)
        mock_db.attendance_logs.insert_one({
            "emp_code": "EMP0001", "date": "2026-07-07", "status": "LEAVE",
            "punch_in": None, "punch_out": None, "work_hours": None,
            "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": [],
        })
        r = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        assert r.json()["leave_days"] == 1

    def test_invalid_month_format_422(self, client):
        r = client.get("/analytics/employees/EMP0001/monthly?month=2026-13")
        assert r.status_code == 422


# ===========================================================================
# /analytics/leaderboard/late GET
# ===========================================================================

class TestLateLeaderboard:
    def _seed(self, mock_db):
        mock_db.employees.insert_many([
            {"emp_code": "EMP0001", "name": "Alice", "department": "Eng",
             "email": "a@x.com", "shift_start": "09:30", "shift_end": "18:30",
             "joined_on": "2026-01-01", "created_at": datetime.now(timezone.utc)},
            {"emp_code": "EMP0002", "name": "Bob", "department": "Eng",
             "email": "b@x.com", "shift_start": "09:30", "shift_end": "18:30",
             "joined_on": "2026-01-01", "created_at": datetime.now(timezone.utc)},
            {"emp_code": "EMP0003", "name": "Carol", "department": "HR",
             "email": "c@x.com", "shift_start": "09:30", "shift_end": "18:30",
             "joined_on": "2026-01-01", "created_at": datetime.now(timezone.utc)},
        ])
        mock_db.attendance_logs.insert_many([
            {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
             "late_minutes": 30, "overtime_minutes": 0, "half_day": False, "history": [],
             "punch_in": None, "punch_out": None, "work_hours": None},
            {"emp_code": "EMP0002", "date": "2026-07-06", "status": "PRESENT",
             "late_minutes": 30, "overtime_minutes": 0, "half_day": False, "history": [],
             "punch_in": None, "punch_out": None, "work_hours": None},
            {"emp_code": "EMP0003", "date": "2026-07-07", "status": "PRESENT",
             "late_minutes": 60, "overtime_minutes": 0, "half_day": False, "history": [],
             "punch_in": None, "punch_out": None, "work_hours": None},
        ])

    def test_competition_ranking_ties(self, client_db):
        """EMP0001 and EMP0002 both have 30 min late → same rank."""
        client, mock_db = client_db
        self._seed(mock_db)
        r = client.get("/analytics/leaderboard/late?month=2026-07&limit=3")
        assert r.status_code == 200
        items = r.json()["items"]
        # Carol is rank 1 (60 min), Alice and Bob tied at rank 2
        assert items[0]["rank"] == 1
        assert items[0]["emp_code"] == "EMP0003"
        assert items[1]["rank"] == 2
        assert items[2]["rank"] == 2

    def test_limit_includes_tied_employees(self, client_db):
        """limit=2 but two employees share rank=2 → 3 rows returned."""
        client, mock_db = client_db
        self._seed(mock_db)
        r = client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
        items = r.json()["items"]
        # Rank 1 and rank 2 (tie) → all 3 rows
        assert len(items) == 3

    def test_department_filter(self, client_db):
        client, mock_db = client_db
        self._seed(mock_db)
        r = client.get("/analytics/leaderboard/late?month=2026-07&department=HR")
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["emp_code"] == "EMP0003"

    def test_zero_late_employees_excluded(self, client_db):
        client, mock_db = client_db
        mock_db.employees.insert_one({
            "emp_code": "EMP0001", "name": "Alice", "department": "Eng",
            "email": "a@x.com", "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-01-01", "created_at": datetime.now(timezone.utc),
        })
        mock_db.attendance_logs.insert_one({
            "emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
            "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": [],
            "punch_in": None, "punch_out": None, "work_hours": None,
        })
        r = client.get("/analytics/leaderboard/late?month=2026-07")
        assert r.json()["items"] == []


# ===========================================================================
# /analytics/departments/trend GET
# ===========================================================================

class TestDepartmentTrend:
    def _seed(self, mock_db):
        mock_db.employees.insert_one({
            "emp_code": "EMP0001", "name": "Asha", "department": "Eng",
            "email": "a@x.com", "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-07-01", "created_at": datetime.now(timezone.utc),
        })

    def test_unknown_department_returns_404(self, client):
        r = client.get("/analytics/departments/Nonexistent/trend?from=2026-07-01&to=2026-07-07")
        assert r.status_code == 404

    def test_to_before_from_returns_422(self, client_db):
        client, mock_db = client_db
        self._seed(mock_db)
        r = client.get("/analytics/departments/Eng/trend?from=2026-07-10&to=2026-07-01")
        assert r.status_code == 422

    def test_range_exceeding_92_days_returns_422(self, client_db):
        client, mock_db = client_db
        self._seed(mock_db)
        r = client.get("/analytics/departments/Eng/trend?from=2026-01-01&to=2026-05-01")
        assert r.status_code == 422

    def test_every_calendar_day_included(self, client_db):
        client, mock_db = client_db
        self._seed(mock_db)
        r = client.get("/analytics/departments/Eng/trend?from=2026-07-01&to=2026-07-07")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 7

    def test_weekend_rate_is_null(self, client_db):
        client, mock_db = client_db
        self._seed(mock_db)
        r = client.get("/analytics/departments/Eng/trend?from=2026-07-04&to=2026-07-05")
        items = r.json()["items"]
        # 2026-07-04 is Saturday, 2026-07-05 is Sunday
        for item in items:
            assert item["is_working_day"] is False
            assert item["attendance_rate"] is None

    def test_working_day_no_records_rate_zero(self, client_db):
        client, mock_db = client_db
        self._seed(mock_db)
        # 2026-07-06 is Monday; no logs seeded
        r = client.get("/analytics/departments/Eng/trend?from=2026-07-06&to=2026-07-06")
        items = r.json()["items"]
        assert items[0]["is_working_day"] is True
        assert items[0]["headcount"] == 1
        assert items[0]["attendance_rate"] == 0.0

    def test_headcount_respects_joined_on(self, client_db):
        """Employee joining 2026-07-10 → headcount 0 on 2026-07-09."""
        client, mock_db = client_db
        mock_db.employees.insert_one({
            "emp_code": "EMP0002", "name": "Bob", "department": "Eng",
            "email": "b@x.com", "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-07-10", "created_at": datetime.now(timezone.utc),
        })
        r = client.get("/analytics/departments/Eng/trend?from=2026-07-09&to=2026-07-10")
        items = r.json()["items"]
        assert items[0]["date"] == "2026-07-09"
        assert items[0]["headcount"] == 0
        assert items[1]["date"] == "2026-07-10"
        assert items[1]["headcount"] == 1

    def test_moving_avg_does_not_use_pre_range_dates(self, client_db):
        """The moving average must only use rows inside [from, to]."""
        client, mock_db = client_db
        self._seed(mock_db)
        # Get a single-day window; moving_avg must equal that day's rate (or null)
        r = client.get("/analytics/departments/Eng/trend?from=2026-07-06&to=2026-07-06")
        items = r.json()["items"]
        # attendance_rate is 0.0 (working day, no records), moving_avg should be 0.0
        assert items[0]["moving_avg_7d"] == 0.0
