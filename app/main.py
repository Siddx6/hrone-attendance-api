"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Conventions used throughout this file
  * Instants are BSON datetimes in MongoDB (read back as tz-aware UTC) and epoch milliseconds in the API.
  * Calendar dates / times of day (date, joined_on, shift_start, shift_end) are plain strings, IST.
  * Derived fields (late_minutes, work_hours, overtime_minutes, half_day) are always computed by `derive()`.
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from bson import json_util
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones, so we never mix the two.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
GRACE_SECONDS = 10 * 60


# --------------------------------------------------------------------------- #
# Startup: indexes (idempotent)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    # employees
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp")
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    # attendance_logs
    # natural key + per-employee queries (punch-in uniqueness, PATCH, punch-out, employee monthly)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date"
    )
    # date-led: global listing order (date desc, emp_code asc) and month/day range scans for analytics
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep the app up so /health can answer 503
        log.error("could not create indexes: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Validation types
# --------------------------------------------------------------------------- #
def _valid_date(v: str) -> str:
    try:
        Date.fromisoformat(v)
    except ValueError:
        raise ValueError("not a valid calendar date")
    return v


# strict int: floats, strings and bools are rejected; the range rejects seconds-looking values
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
DateStr = Annotated[
    str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(_valid_date)
]
HHMM = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]


def _valid_month(v: str) -> str:
    if not 1900 <= int(v[:4]) <= 2200:
        raise ValueError("year out of supported range")
    return v


MonthStr = Annotated[
    str, StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"), AfterValidator(_valid_month)
]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


def unprocessable(loc: str, msg: str):
    """422 in FastAPI's default validation-error shape ({"detail": [...]})."""
    raise RequestValidationError([{"type": "value_error", "loc": ("body", loc), "msg": msg, "input": None}])


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch millis -> tz-aware UTC datetime truncated to whole seconds (R1)."""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat a naive value as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_s() -> datetime:
    """Current time, UTC, whole seconds."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_ms() -> datetime:
    """Current time, UTC, millisecond precision (what BSON can store)."""
    n = datetime.now(timezone.utc)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


def parse_hhmm(s: str) -> tuple:
    h, m = s.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def ist_at(d: Date, hhmm: str, plus_days: int = 0) -> datetime:
    h, m = parse_hhmm(hhmm)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST) + timedelta(days=plus_days)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    """R1: the IST calendar date of the punch-in, except an overnight shift's early-morning punch-in
    (earlier than shift_end) belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    if is_overnight(shift_start, shift_end):
        h, m = parse_hhmm(shift_end)
        if local.hour * 3600 + local.minute * 60 + local.second < h * 3600 + m * 60:
            return local.date() - timedelta(days=1)
    return local.date()


def shift_end_dt(d: Date, shift_start: str, shift_end: str) -> datetime:
    """The instant the shift that started on attendance date `d` ends (next day for overnight shifts)."""
    return ist_at(d, shift_end, 1 if is_overnight(shift_start, shift_end) else 0)


# --------------------------------------------------------------------------- #
# Business rules R2 - R5
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, d: Date, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then whole minutes since shift_start."""
    seconds = (punch_in - ist_at(d, shift_start)).total_seconds()
    return int(seconds // 60) if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals, half-up (Decimal, not float round())."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, end: datetime) -> int:
    """R3: whole minutes from shift end to punch-out, counted only if at least 30."""
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def is_half_day(work_hours: float) -> bool:
    """R5: the rounded work_hours is below 4.50."""
    return Decimal(str(work_hours)) < Decimal("4.50")


def derive(emp: dict, date_str: str, status: str, punch_in, punch_out) -> dict:
    """All derived fields for a record, following the table in DATA_MODEL.md."""
    out = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return out
    d = Date.fromisoformat(date_str)
    out["late_minutes"] = compute_late_minutes(punch_in, d, emp["shift_start"])
    if punch_out is not None:
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, shift_end_dt(d, emp["shift_start"], emp["shift_end"]))
        out["half_day"] = is_half_day(out["work_hours"])
    return out


# --------------------------------------------------------------------------- #
# Serialisation (Mongo document -> API shape)
# --------------------------------------------------------------------------- #
def _api_value(v):
    return dt_to_ms(v) if isinstance(v, datetime) else v


def emp_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def history_out(entry: dict) -> dict:
    return {
        "at": dt_to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {
            k: {"from": _api_value(v.get("from")), "to": _api_value(v.get("to"))}
            for k, v in (entry.get("changes") or {}).items()
        },
    }


def record_out(doc: dict) -> dict:
    """Never exposes _id; fills the documented defaults for legacy records."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: Annotated[str, StringConstraints(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    email: Annotated[str, StringConstraints(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def _shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: Annotated[str, StringConstraints(min_length=1)]
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: Annotated[str, StringConstraints(min_length=1)]
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: Annotated[str, StringConstraints(min_length=5, max_length=200)]
    regularized_by: Annotated[str, StringConstraints(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def _check(self):
        # the schema is not nullable: an explicit null is a validation error, omitting the field is fine
        for f in ("status", "punch_in", "punch_out"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} must not be null")
        if self.status in ("ABSENT", "LEAVE") and (self.punch_in is not None or self.punch_out is not None):
            raise ValueError("ABSENT / LEAVE clear the punch times; do not send punch_in or punch_out")
        return self


# --------------------------------------------------------------------------- #
# System / employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_ms()
    try:
        db.employees.insert_one(doc)  # the unique index on emp_code is the real guard (no check-then-insert)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return emp_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [emp_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_s()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(emp, d.isoformat(), body.status, ts, None),
        "history": [],
    }
    try:
        # unique index (emp_code, date): of N simultaneous identical requests exactly one insert succeeds
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    out_ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_s()
    # most recent record whose punch_in is at or before the punch-out (works across midnight)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": out_ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found for this employee")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    in_ts = rec["punch_in"]
    if out_ts <= in_ts:
        unprocessable("punched_at", "punched_at must be after the punch-in")
    if out_ts - in_ts > timedelta(hours=24):
        unprocessable("punched_at", "punched_at is more than 24 hours after the punch-in")
    fields = derive(emp, rec["date"], rec["status"], in_ts, out_ts)
    # conditional update: only succeeds while the record is still open and unchanged -> one winner
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": rec["date"], "punch_in": in_ts, "punch_out": None},
        {"$set": {"punch_out": out_ts, **fields}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was already punched out or changed concurrently")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    check_date_order(date_from, date_to)
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = (
        db.attendance_logs.find(q)
        .sort(list(ATTENDANCE_SORT.items()))
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# date descending, then emp_code ascending; shared with /admin/explain so both run the same query
ATTENDANCE_SORT = {"date": DESCENDING, "emp_code": ASCENDING}


def check_date_order(date_from: Optional[str], date_to: Optional[str]) -> None:
    if date_from and date_to and date_from > date_to:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", "date_from"), "msg": "date_from is after date_to", "input": date_from}]
        )


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


CHANGE_ORDER = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = get_employee(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for this date")

    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or rec["status"]
    if new_status in ("ABSENT", "LEAVE"):
        new_in = new_out = None  # these statuses clear both punch times
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            unprocessable("punch_in", "a presence status requires a punch_in")
        if body.punch_in is not None:
            if attendance_date(new_in, emp["shift_start"], emp["shift_end"]).isoformat() != date:
                unprocessable("punch_in", "punch_in must stay on the record's attendance date")
        if (body.punch_in is not None or body.punch_out is not None) and new_out is not None:
            if new_out <= new_in:
                unprocessable("punch_out", "punch_out must be after punch_in")
            if new_out - new_in > timedelta(hours=24):
                unprocessable("punch_out", "punch_out must be within 24 hours of punch_in")

    old = {
        "status": rec["status"],
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day")),
    }
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out}
    new.update(derive(emp, date, new_status, new_in, new_out))

    # "changes nothing" is judged on what the client can set; derived fields follow from those
    if all(old[k] == new[k] for k in ("status", "punch_in", "punch_out")):
        unprocessable("status", "the request does not change anything")

    changes = {k: {"from": old[k], "to": new[k]} for k in CHANGE_ORDER if old[k] != new[k]}
    entry = {"at": now_ms(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    fields = {k: new[k] for k in CHANGE_ORDER}

    # optimistic concurrency: the update only applies if the record is exactly as we read it, so two
    # simultaneous corrections can never overwrite each other's history (the loser gets 409).
    guard = {"emp_code": emp_code, "date": date, "status": old["status"], "punch_in": old_in, "punch_out": old_out}
    history = rec.get("history")
    if history is None:  # legacy record without history
        guard["history"] = None
        update = {"$set": {**fields, "history": [entry]}}
    else:
        guard["$expr"] = {"$eq": [{"$size": "$history"}, len(history)]}
        update = {"$set": fields, "$push": {"history": entry}}
    updated = db.attendance_logs.find_one_and_update(guard, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "the record was modified concurrently; retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics
#
# Every endpoint is a MongoDB aggregation that reads the STORED derived values (late_minutes, work_hours,
# half_day, ...) - nothing is recomputed from punch times. Python only builds the pipeline and rounds the
# final numbers half-up (Mongo's $round is round-half-even, which R8 forbids).
# The pipeline builders are separate functions so /admin/explain can run explain on exactly the same pipeline.
# --------------------------------------------------------------------------- #
def q2(x) -> float:
    """R8: 2 decimals, half-up."""
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def q4(x) -> float:
    """R8: rates, 4 decimals, half-up."""
    return float(Decimal(str(x)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def month_bounds(month: str) -> tuple:
    """First and last calendar day of a 'YYYY-MM' month, as dates."""
    y, m = int(month[:4]), int(month[5:])
    first, last = Date(y, m, 1), Date(y, m, calendar.monthrange(y, m)[1])
    return first, last


def count_weekdays(a: Date, b: Date) -> int:
    """Mon-Fri days in [a, b], inclusive (R7: no holiday calendar)."""
    if b < a:
        return 0
    full, rest = divmod((b - a).days + 1, 7)
    n = full * 5
    for i in range(rest):
        if (a.weekday() + i) % 7 < 5:
            n += 1
    return n


# reusable aggregation expressions over one attendance document
LATE = {"$ifNull": ["$late_minutes", 0]}
IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}  # R6
# R5: a half day counts 0.5, a full present day 1, a non-present record 0 (missing half_day = false)
PRESENT_VALUE = {
    "$cond": [IS_PRESENT, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]
}
IS_LATE = {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}
IS_WEEKDAY = {  # R7: the `date` string is a plain IST calendar date; Mon=1 .. Sun=7
    "$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]
}
HAS_WORK_HOURS = {"$and": [IS_PRESENT, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
# work_hours in whole hundredths so the department average can be summed exactly (no float drift)
WORK_CENTS = {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}


def monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        # index uq_emp_date: equality on emp_code + range on date
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": {"$cond": [IS_WEEKDAY, PRESENT_VALUE, 0]}},  # Mon-Fri only
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": IS_LATE},
                "total_late_minutes": {"$sum": LATE},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthStr):
    emp = get_employee(emp_code)
    first, last = month_bounds(month)
    agg = next(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month)), {})
    # R7: working days run from joined_on for someone who joined mid-month (0 if joined after the month)
    working_days = count_weekdays(max(first, Date.fromisoformat(emp["joined_on"])), last)
    present_days = agg.get("present_days", 0)
    pct = None
    if working_days > 0:
        pct = float(
            (Decimal(str(present_days)) * 100 / Decimal(working_days)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": q2(present_days),
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


def summary_pipeline(month: str, department: Optional[str]) -> list:
    """Starts from employees, so people with zero logs still count (R9); a log-first pipeline would lose them."""
    first, last = month_bounds(month)
    emp_match = {"joined_on": {"$lte": last.isoformat()}}  # R9: joined on or before the month's last day
    if department is not None:
        emp_match["department"] = department
    return [
        {"$match": emp_match},
        {
            # equality join on emp_code uses uq_emp_date; the pipeline then reduces that employee's month to one doc
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {"$sum": {"$cond": [IS_WEEKDAY, PRESENT_VALUE, 0]}},
                            "late_count": {"$sum": IS_LATE},
                            "total_late_minutes": {"$sum": LATE},
                            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                            "wh_cents": {"$sum": {"$cond": [HAS_WORK_HOURS, WORK_CENTS, 0]}},
                            "wh_count": {"$sum": {"$cond": [HAS_WORK_HOURS, 1, 0]}},
                        }
                    },
                ],
                "as": "agg",
            }
        },
        {"$set": {"agg": {"$ifNull": [{"$arrayElemAt": ["$agg", 0]}, {}]}}},  # no logs -> empty doc -> zeros
        {
            # one row per department; headcount counts employees, the rest sums their month totals
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": ["$agg.present_days", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$agg.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$agg.total_late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$agg.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$agg.on_duty_count", 0]}},
                "wh_cents": {"$sum": {"$ifNull": ["$agg.wh_cents", 0]}},
                "wh_count": {"$sum": {"$ifNull": ["$agg.wh_count", 0]}},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthStr, department: Optional[str] = None):
    items = []
    for r in db.employees.aggregate(summary_pipeline(month, department)):
        wh_count = int(r["wh_count"])
        avg = None  # average over RECORDS (sum of all hundredths / number of records), not per-employee averages
        if wh_count > 0:
            avg = float(
                (Decimal(int(r["wh_cents"])) / Decimal(wh_count) / Decimal(100)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )
        items.append(
            {
                "department": r["_id"],
                "headcount": int(r["headcount"]),
                "present_days": q2(r["present_days"]),
                "avg_work_hours": avg,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline = [
        # index date_emp: range on date; only late records can contribute to a total > 0
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs whose employee does not exist drop out here
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})  # BEFORE ranking: ranks are within the department
    pipeline += [
        # $rank = standard competition ranking: ties share a rank and the next rank is skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking, so ties at the cutoff are all kept
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$e.name",
                "department": "$e.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthStr,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    rows = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


MAX_TREND_DAYS = 92


def trend_pipeline(department: str, frm: Date, to: Date, emp_codes: list) -> list:
    """One row per calendar day, generated inside MongoDB ($range + $unwind), so weekends and days without
    any record are still present. The 7-day moving average is a $setWindowFields over those rows."""
    ndays = (to - frm).days + 1
    start = datetime(frm.year, frm.month, frm.day, tzinfo=timezone.utc)
    return [
        {"$match": {"department": department}},  # index dept_emp
        {"$group": {"_id": None, "joined": {"$push": "$joined_on"}}},  # every joined_on, for headcount (R9)
        {"$project": {"_id": 0, "joined": 1, "i": {"$range": [0, ndays]}}},
        {"$unwind": "$i"},  # one doc per day of the range
        {
            "$set": {
                "d": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$i"}},
                    }
                }
            }
        },
        {
            # equality join on `date` uses index date_emp; reduce that day's department records to one doc
            "$lookup": {
                "from": "attendance_logs",
                "localField": "d",
                "foreignField": "date",
                "pipeline": [
                    {"$match": {"emp_code": {"$in": emp_codes}}},
                    {"$group": {"_id": None, "present": {"$sum": PRESENT_VALUE}, "late": {"$sum": IS_LATE}}},
                ],
                "as": "agg",
            }
        },
        {"$set": {"agg": {"$ifNull": [{"$arrayElemAt": ["$agg", 0]}, {}]}}},
        {
            "$project": {
                "_id": 0,
                "i": 1,
                "d": 1,
                "headcount": {"$size": {"$filter": {"input": "$joined", "as": "j", "cond": {"$lte": ["$$j", "$d"]}}}},
                "present_count": {"$ifNull": ["$agg.present", 0]},
                "late_count": {"$ifNull": ["$agg.late", 0]},
                "is_working_day": {
                    "$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$d", "format": "%Y-%m-%d"}}}, 5]
                },
            }
        },
        {
            "$set": {
                "rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            # current row + up to 6 earlier rows of the requested range; $avg skips null rates and is null if all are
            "$setWindowFields": {
                "sortBy": {"i": 1},
                "output": {"moving": {"$avg": "$rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {"$sort": {"i": 1}},
    ]


def validate_trend_range(frm: Date, to: Date) -> None:
    if to < frm:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", "to"), "msg": "to is before from", "input": str(to)}]
        )
    if (to - frm).days + 1 > MAX_TREND_DAYS:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", "to"), "msg": f"range exceeds {MAX_TREND_DAYS} days", "input": str(to)}]
        )


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[DateStr, Query(alias="from")], to: DateStr):
    frm, to_d = Date.fromisoformat(from_), Date.fromisoformat(to)
    validate_trend_range(frm, to_d)
    emp_codes = db.employees.distinct("emp_code", {"department": department})
    if not emp_codes:
        raise HTTPException(404, "unknown department")
    items = []
    for r in db.employees.aggregate(trend_pipeline(department, frm, to_d, emp_codes)):
        items.append(
            {
                "date": r["d"],
                "is_working_day": bool(r["is_working_day"]),
                "headcount": int(r["headcount"]),
                "present_count": q2(r["present_count"]),
                "late_count": int(r["late_count"]),
                "attendance_rate": None if r.get("rate") is None else q4(r["rate"]),
                "moving_avg_7d": None if r.get("moving") is None else q4(r["moving"]),
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Part C: explain
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def missing_params(*names: str):
    raise RequestValidationError(
        [{"type": "missing", "loc": ("query", n), "msg": "Field required", "input": None} for n in names]
    )


def run_explain(command: dict) -> dict:
    """explain with executionStats verbosity, converted to plain JSON (dates, timestamps, ... made serialisable)."""
    raw = db.command("explain", command, verbosity="executionStats")
    return json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Optional[DateStr] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """Explains the main query of an endpoint. The query/pipeline comes from the SAME builder the endpoint uses."""
    if endpoint == "attendance_list":
        check_date_order(date_from, date_to)
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": ATTENDANCE_SORT,
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            if emp_code is None or month is None:
                missing_params(*[n for n, v in (("emp_code", emp_code), ("month", month)) if v is None])
            collection, pipeline = "attendance_logs", monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            if month is None:
                missing_params("month")
            collection, pipeline = "employees", summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            if month is None:
                missing_params("month")
            collection, pipeline = "attendance_logs", leaderboard_pipeline(month, limit, department)
        else:  # department_trend
            needed = (("department", department), ("from", from_), ("to", to))
            if any(v is None for _, v in needed):
                missing_params(*[n for n, v in needed if v is None])
            frm, to_d = Date.fromisoformat(from_), Date.fromisoformat(to)
            validate_trend_range(frm, to_d)
            emp_codes = db.employees.distinct("emp_code", {"department": department})
            if not emp_codes:
                raise HTTPException(404, "unknown department")
            collection, pipeline = "employees", trend_pipeline(department, frm, to_d, emp_codes)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": run_explain(command)}
