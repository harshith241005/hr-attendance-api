"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import math
import os
import re
from typing import Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument, errors

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Indexes setup
# --------------------------------------------------------------------------- #
def init_indexes():
    """Ensure all required indexes exist idempotently."""
    def safe_create_index(collection, keys, **kwargs):
        key_list = list(keys)
        info = collection.index_information()
        for existing_name, existing_info in info.items():
            if existing_info.get("key") == key_list:
                # Matching index keys already present
                if kwargs.get("unique", False) == existing_info.get("unique", False):
                    return
        try:
            collection.create_index(keys, **kwargs)
        except errors.OperationFailure as e:
            if e.code in (85, 86):  # IndexOptionsConflict / IndexKeySpecsConflict
                pass
            else:
                raise

    # Employees
    safe_create_index(db.employees, [("emp_code", 1)], unique=True)
    safe_create_index(db.employees, [("department", 1), ("joined_on", 1)])
    safe_create_index(db.employees, [("joined_on", 1)])

    # Attendance logs
    safe_create_index(db.attendance_logs, [("emp_code", 1), ("date", 1)], unique=True)
    safe_create_index(db.attendance_logs, [("date", -1), ("emp_code", 1)])
    safe_create_index(db.attendance_logs, [("emp_code", 1), ("punch_in", -1)])
    safe_create_index(db.attendance_logs, [("date", 1), ("late_minutes", 1)])
    safe_create_index(db.attendance_logs, [("status", 1), ("date", -1), ("emp_code", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """R8: 2 decimals half-up (rates: 4 decimals half-up)."""
    if val is None:
        return None
    d = Decimal(str(val))
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def truncate_ms(ts_ms: int) -> int:
    """R1: Instants are truncated to whole seconds."""
    return (ts_ms // 1000) * 1000


def ms_to_utc_dt(ts_ms: int) -> datetime:
    ts_sec = truncate_ms(ts_ms) / 1000.0
    return datetime.fromtimestamp(ts_sec, tz=UTC)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in.
    Overnight shifts (shift_end <= shift_start): a punch-in earlier than shift_end
    belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        if (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second) < (end_h, end_m, 0):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if the punch-in is strictly more than 10:00
    after shift_start; then late_minutes = whole minutes (floor) since shift_start,
    not since the grace period ended.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    att_date = date.fromisoformat(attendance_date)
    shift_start_ist = datetime(att_date.year, att_date.month, att_date.day, start_h, start_m, 0, 0, tzinfo=IST)

    diff_sec = (punch_in_ist - shift_start_ist).total_seconds()
    if diff_sec > 600:  # strictly more than 10 minutes
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    return round_half_up(diff_sec / 3600.0, 2)


def compute_overtime(punch_out_dt: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if that is
    at least 30; otherwise 0.
    """
    punch_out_ist = punch_out_dt.astimezone(IST)
    end_h, end_m = map(int, shift_end.split(":"))
    att_date = date.fromisoformat(attendance_date)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_date = att_date + timedelta(days=1)
    else:
        end_date = att_date

    shift_end_ist = datetime(end_date.year, end_date.month, end_date.day, end_h, end_m, 0, 0, tzinfo=IST)
    diff_sec = (punch_out_ist - shift_end_ist).total_seconds()
    diff_min = int(diff_sec // 60)
    if diff_min >= 30:
        return diff_min
    return 0


def compute_working_days(joined_on_str: str, month_str: str) -> int:
    """R7: Monday - Friday, no holiday calendar; counted from joined_on for mid-month joiners."""
    year, month = map(int, month_str.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    month_start = f"{month_str}-01"
    month_end = f"{month_str}-{last_day:02d}"

    if joined_on_str > month_end:
        return 0

    start_str = max(joined_on_str, month_start)
    cur = date.fromisoformat(start_str)
    end = date.fromisoformat(month_end)

    working_days = 0
    while cur <= end:
        if cur.weekday() < 5:  # Monday to Friday
            working_days += 1
        cur += timedelta(days=1)
    return working_days


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc["created_at"]),
    }


def format_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    history_out = []
    for h in doc.get("history", []):
        changes_out = {}
        for k, v in h.get("changes", {}).items():
            if k in ("punch_in", "punch_out"):
                changes_out[k] = {
                    "from": dt_to_epoch_ms(v.get("from")) if isinstance(v.get("from"), datetime) else v.get("from"),
                    "to": dt_to_epoch_ms(v.get("to")) if isinstance(v.get("to"), datetime) else v.get("to"),
                }
            else:
                changes_out[k] = v

        history_out.append({
            "at": dt_to_epoch_ms(h["at"]) if isinstance(h["at"], datetime) else h["at"],
            "by": h["by"],
            "reason": h["reason"],
            "changes": changes_out,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_out,
    }


# --------------------------------------------------------------------------- #
# Request Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("shift_end")
    @classmethod
    def check_shifts(cls, v: str, info):
        shift_start = info.data.get("shift_start")
        if shift_start and v == shift_start:
            raise ValueError("shift_start must differ from shift_end")
        return v

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, v: str):
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a valid date")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    created_dt = datetime.now(timezone.utc).replace(microsecond=0)
    doc["created_at"] = created_dt

    try:
        db.employees.insert_one(doc)
    except errors.DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts_dt = ms_to_utc_dt(body.punched_at)
    else:
        ts_dt = datetime.now(timezone.utc).replace(microsecond=0)

    d = get_attendance_date(ts_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(ts_dt, emp["shift_start"], emp["shift_end"], d)

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except errors.DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = ms_to_utc_dt(body.punched_at)
    else:
        punch_out_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # Closes employee's most recent record whose punch_in <= punched_at
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not rec:
        raise HTTPException(status_code=404, detail="no punch-in found")

    punch_in_dt = rec["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    if punch_out_dt <= punch_in_dt or (punch_out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be strictly after punch_in and within 24 hours",
        )

    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="that record is already punched out")

    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    ot_min = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], rec["date"])
    half = bool(work_hrs < 4.50)

    # Concurrency safe: exactly one 200, rest 409
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": half,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="that record is already punched out")

    return format_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_record(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found for date")

    # Determine prospective fields
    new_status = body.status if body.status is not None else rec["status"]

    if body.status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="supplying punch_in or punch_out with ABSENT or LEAVE is not allowed",
            )
        new_punch_in_ms = None
        new_punch_out_ms = None
    else:
        new_punch_in_ms = body.punch_in if body.punch_in is not None else dt_to_epoch_ms(rec.get("punch_in"))
        new_punch_out_ms = body.punch_out if body.punch_out is not None else dt_to_epoch_ms(rec.get("punch_out"))

    if new_status in ("PRESENT", "WFH", "ON_DUTY") and new_punch_in_ms is None:
        raise HTTPException(status_code=422, detail="A presence status requires a punch_in")

    new_punch_in_dt = ms_to_utc_dt(new_punch_in_ms) if new_punch_in_ms is not None else None
    new_punch_out_dt = ms_to_utc_dt(new_punch_out_ms) if new_punch_out_ms is not None else None

    if new_punch_in_dt:
        computed_date = get_attendance_date(new_punch_in_dt, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in must stay on the record's attendance date ({date}), got {computed_date}",
            )

    if new_punch_out_dt:
        if not new_punch_in_dt:
            raise HTTPException(status_code=422, detail="punch_out requires punch_in")
        if new_punch_out_dt <= new_punch_in_dt or (new_punch_out_dt - new_punch_in_dt).total_seconds() > 24 * 3600:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in and within 24 hours",
            )

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in_dt, emp["shift_start"], emp["shift_end"], date)
        if new_punch_out_dt:
            new_work_hours = compute_work_hours(new_punch_in_dt, new_punch_out_dt)
            new_overtime_minutes = compute_overtime(new_punch_out_dt, emp["shift_start"], emp["shift_end"], date)
            new_half_day = bool(new_work_hours < 4.50)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Calculate actual changes
    old_status = rec["status"]
    old_punch_in_ms = dt_to_epoch_ms(rec.get("punch_in"))
    old_punch_out_ms = dt_to_epoch_ms(rec.get("punch_out"))
    old_work_hours = rec.get("work_hours")
    old_late_minutes = rec.get("late_minutes", 0)
    old_overtime_minutes = rec.get("overtime_minutes", 0)
    old_half_day = rec.get("half_day", False)

    changes_api = {}
    changes_db = {}

    if old_status != new_status:
        changes_api["status"] = {"from": old_status, "to": new_status}
        changes_db["status"] = {"from": old_status, "to": new_status}

    if old_punch_in_ms != new_punch_in_ms:
        changes_api["punch_in"] = {"from": old_punch_in_ms, "to": new_punch_in_ms}
        changes_db["punch_in"] = {"from": rec.get("punch_in"), "to": new_punch_in_dt}

    if old_punch_out_ms != new_punch_out_ms:
        changes_api["punch_out"] = {"from": old_punch_out_ms, "to": new_punch_out_ms}
        changes_db["punch_out"] = {"from": rec.get("punch_out"), "to": new_punch_out_dt}

    if old_work_hours != new_work_hours:
        changes_api["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
        changes_db["work_hours"] = {"from": old_work_hours, "to": new_work_hours}

    if old_late_minutes != new_late_minutes:
        changes_api["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
        changes_db["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}

    if old_overtime_minutes != new_overtime_minutes:
        changes_api["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
        changes_db["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}

    if old_half_day != new_half_day:
        changes_api["half_day"] = {"from": old_half_day, "to": new_half_day}
        changes_db["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes_api:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    # Optimistic concurrency check: match history array length
    old_history_len = len(rec.get("history", []))
    update_res = db.attendance_logs.update_one(
        {"_id": rec["_id"], "history": {"$size": old_history_len}},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in_dt,
                "punch_out": new_punch_out_dt,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
    )

    if update_res.modified_count == 0:
        raise HTTPException(status_code=409, detail="Concurrent edit conflict")

    updated_rec = db.attendance_logs.find_one({"_id": rec["_id"]})
    return format_attendance_record(updated_rec)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str):
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-{last_day:02d}"},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {"dateString": "$date"}
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = compute_working_days(emp["joined_on"], month)
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = list(db.attendance_logs.aggregate(pipeline))

    if res:
        agg = res[0]
        present_days = float(agg.get("present_days", 0.0))
        leave_days = int(agg.get("leave_days", 0))
        late_count = int(agg.get("late_count", 0))
        total_late = int(agg.get("total_late_minutes", 0))
        total_ot = int(agg.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        att_pct = round_half_up((present_days / working_days) * 100.0, 2)
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None):
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    match_stage = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {"dateString": "$$l.date"}
                                                        }
                                                    },
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                        ]
                                    },
                                }
                            },
                            "as": "item",
                            "in": {"$cond": [{"$eq": ["$$item.half_day", True]}, 0.5, 1.0]},
                        }
                    }
                },
                "work_hours_list": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {
                                    "$and": [
                                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$$l.work_hours", None]},
                                    ]
                                },
                            }
                        },
                        "as": "wh",
                        "in": "$$wh.work_hours",
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {"$avg": "$work_hours_list"},
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": float(r["present_days"]),
            "avg_work_hours": round_half_up(r.get("avg_work_hours"), 2),
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


def build_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None):
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_trend_pipeline(department: str, from_date: str, to_date: str):
    from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc) + timedelta(days=1)

    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"date_dt": {"$literal": from_dt}}},
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt],
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date_dt",
                        "timezone": "+00:00",
                    }
                },
                "dayOfWeek": {
                    "$dayOfWeek": {
                        "date": "$date_dt",
                        "timezone": "+00:00",
                    }
                },
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "dept_emps",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"cur_date": "$date", "emp_codes": "$dept_emps.emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$cur_date"]},
                                    {"$in": ["$emp_code", "$$emp_codes"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "day_logs",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": {"$size": "$dept_emps"},
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$day_logs",
                                    "as": "l",
                                    "cond": {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                }
                            },
                            "as": "item",
                            "in": {"$cond": [{"$eq": ["$$item.half_day", True]}, 0.5, 1.0]},
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$day_logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        },
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date must not be earlier than 'from' date")

    days_diff = (d_to - d_from).days + 1
    if days_diff > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

    pipeline = build_trend_pipeline(department, from_date, to_date)
    rows = list(db.employees.aggregate(pipeline))

    items = []
    for r in rows:
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": float(r["present_count"]),
            "late_count": r["late_count"],
            "attendance_rate": round_half_up(r.get("attendance_rate"), 4),
            "moving_avg_7d": round_half_up(r.get("moving_avg_7d"), 4),
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
def serialize_bson_doc(obj: Any) -> Any:
    """Safely convert MongoDB / BSON types to JSON-serializable types."""
    return json.loads(json_util.dumps(obj))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        exp = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return serialize_bson_doc(exp)

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        exp = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return serialize_bson_doc(exp)

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = build_department_summary_pipeline(month, department)
        exp = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return serialize_bson_doc(exp)

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = build_leaderboard_pipeline(month, limit or 10, department)
        exp = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return serialize_bson_doc(exp)

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        try:
            d_from = date.fromisoformat(from_date)
            d_to = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format")
        if d_to < d_from:
            raise HTTPException(status_code=422, detail="'to' date must not be earlier than 'from' date")
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

        pipeline = build_trend_pipeline(department, from_date, to_date)
        exp = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return serialize_bson_doc(exp)

    raise HTTPException(status_code=404, detail="Endpoint not found")
