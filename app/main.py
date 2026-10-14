"""Employee Attendance & Analytics API.

Run:
  uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import calendar
import os
from datetime import date as date_cls, datetime, time as time_cls, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
VALID_ENDPOINTS = [
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ----------------------------
# Shared validation / helpers
# ----------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def normalize_to_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def epoch_ms_to_datetime(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(microsecond=0)


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    value = normalize_to_utc(value)
    return int(value.astimezone(timezone.utc).replace(microsecond=0).timestamp() * 1000)


def date_string_to_datetime(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=IST)


def parse_hhmm(value: str) -> tuple[int, int]:
    hours, minutes = value.split(":")
    return int(hours), int(minutes)


def time_string_from_dt(value: datetime) -> str:
    return value.astimezone(IST).strftime("%H:%M")


def shift_day_start_date(date_str: str, shift_start: str) -> datetime:
    return datetime.strptime(f"{date_str} {shift_start}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)


def shift_end_datetime_for_record(date_str: str, shift_start: str, shift_end: str) -> datetime:
    start_h, _ = parse_hhmm(shift_start)
    end_h, _ = parse_hhmm(shift_end)
    end_date = datetime.strptime(f"{date_str} {shift_end}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    if end_h <= start_h:
        end_date += timedelta(days=1)
    return end_date


def attendance_date_for_punch_in(emp: dict[str, Any], punch_in_utc: datetime) -> str:
    local_dt = punch_in_utc.astimezone(IST)
    shift_end_hhmm = emp["shift_end"]
    end_h, end_m = parse_hhmm(shift_end_hhmm)
    candidate_date = local_dt.date().isoformat()
    if end_h <= parse_hhmm(emp["shift_start"])[0]:
        if local_dt.time() < time_cls(end_h, end_m):
            candidate_date = (local_dt.date() - timedelta(days=1)).isoformat()
    return candidate_date


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    punch_in_utc = normalize_to_utc(punch_in_utc)
    local_punch = punch_in_utc.astimezone(IST)
    shift_start_dt = datetime.strptime(f"{attendance_date} {shift_start}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    delta_seconds = (local_punch - shift_start_dt).total_seconds()
    if delta_seconds <= 600:
        return 0
    return int(delta_seconds // 60)


def quantize_hours(value: Decimal) -> Decimal:
    q = Decimal("0.01")
    return value.quantize(q, rounding=ROUND_HALF_UP)


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    punch_in_utc = normalize_to_utc(punch_in_utc)
    punch_out_utc = normalize_to_utc(punch_out_utc)
    total_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    hours = Decimal(str(total_seconds)) / Decimal("3600")
    return float(quantize_hours(hours))


def compute_overtime_minutes(punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    punch_out_utc = normalize_to_utc(punch_out_utc)
    shift_end_dt = shift_end_datetime_for_record(attendance_date, shift_start, shift_end)
    delta_seconds = (punch_out_utc.astimezone(IST) - shift_end_dt.astimezone(IST)).total_seconds()
    if delta_seconds < 1800:
        return 0
    return int(delta_seconds // 60)


def compute_half_day(work_hours_value: Optional[float]) -> bool:
    if work_hours_value is None:
        return False
    rounded = Decimal(str(work_hours_value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return rounded < Decimal("4.50")


def normalize_isoweekday_weekday(date_str: str) -> bool:
    return datetime.strptime(date_str, "%Y-%m-%d").weekday() < 5


def round_half_up_decimal(value: Decimal, places: int = 2) -> Decimal:
    q = Decimal("1").scaleb(-places)
    return value.quantize(q, rounding=ROUND_HALF_UP)


def decimal_percentage(numerator: Decimal, denominator: Decimal) -> Optional[Decimal]:
    if denominator == 0:
        return None
    return round_half_up_decimal((numerator / denominator) * Decimal("100"), 2)


def round_float(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    quant = Decimal("1").scaleb(-digits)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc["created_at"]),
    }


def serialize_history_entry(entry: dict[str, Any]) -> dict[str, Any]:
    changes = {}
    for key, value in entry.get("changes", {}).items():
        if key in {"punch_in", "punch_out"}:
            changes[key] = {"from": datetime_to_epoch_ms(value.get("from")), "to": datetime_to_epoch_ms(value.get("to"))}
        else:
            changes[key] = {"from": value.get("from"), "to": value.get("to")}
    return {
        "at": datetime_to_epoch_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes,
    }


def serialize_attendance_record(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0)),
        "overtime_minutes": int(doc.get("overtime_minutes", 0)),
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history_entry(item) for item in doc.get("history", [])],
    }


def sanitize_explain(obj: Any) -> Any:
    """Recursively convert all non-JSON-serializable BSON types in explain() output."""
    if isinstance(obj, dict):
        return {k: sanitize_explain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_explain(v) for v in obj]
    if isinstance(obj, (bool, int, float, str)) or obj is None:
        return obj
    if isinstance(obj, datetime):
        return obj.isoformat()
    # Catch all remaining BSON / non-serializable types (ObjectId, Timestamp, Decimal128, etc.)
    return str(obj)


def compute_derived_fields(emp: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    status = record.get("status")
    punch_in = record.get("punch_in")
    punch_out = record.get("punch_out")
    if status in {"ABSENT", "LEAVE"}:
        return {
            "status": status,
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    if status not in PRESENT_STATUSES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unsupported status: {status}")
    if punch_in is None:
        return {
            "status": status,
            "punch_in": None,
            "punch_out": punch_out,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    late_minutes = compute_late_minutes(punch_in, emp["shift_start"], record["date"])
    if punch_out is None:
        return {
            "status": status,
            "punch_in": punch_in,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": late_minutes,
            "overtime_minutes": 0,
            "half_day": False,
        }
    work_hours = compute_work_hours(punch_in, punch_out)
    overtime_minutes = compute_overtime_minutes(punch_out, emp["shift_start"], emp["shift_end"], record["date"])
    return {
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": compute_half_day(work_hours),
    }


def create_history_entry(record: dict[str, Any], by: str, reason: str, changes: dict[str, Any]) -> dict[str, Any]:
    return {
        "at": utcnow(),
        "by": by,
        "reason": reason,
        "changes": changes,
    }


# ----------------------------
# Request models
# ----------------------------
class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value: str) -> str:
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError as exc:
            raise ValueError("Time must be HH:MM") from exc
        return value

    @model_validator(mode="after")
    def ensure_shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            date_cls.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("joined_on must be YYYY-MM-DD") from exc
        return value


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: str = Field(default="PRESENT")

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in PRESENT_STATUSES:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[str] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}:
            raise ValueError("status must be one of PRESENT, WFH, ON_DUTY, ABSENT, LEAVE")
        return value


# ----------------------------
# Index creation and startup
# ----------------------------
@app.on_event("startup")
def startup_indexes() -> None:
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index("date")
    db.attendance_logs.create_index("emp_code")
    db.attendance_logs.create_index("status")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])


# ----------------------------
# Endpoints
# ----------------------------
@app.get("/health")
def health() -> dict[str, str]:
    try:
        client.admin.command("ping")
    except Exception as exc:  # pragma: no cover - exercised by deployment checks
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "MongoDB unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeCreate) -> dict[str, Any]:
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(status.HTTP_409_CONFLICT, "emp_code already exists")
    payload = body.model_dump()
    payload["created_at"] = utcnow()
    try:
        db.employees.insert_one(payload)
    except DuplicateKeyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "emp_code already exists") from exc
    return serialize_employee(payload)


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if department is not None:
        query["department"] = department
    total = db.employees.count_documents(query)
    items = list(db.employees.find(query, {"_id": 0}).sort([("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_employee(item) for item in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInRequest) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown employee")
    punch_ts = epoch_ms_to_datetime(body.punched_at) or utcnow()
    punch_ts = punch_ts.replace(microsecond=0)
    record_date = attendance_date_for_punch_in(employee, punch_ts)
    try:
        doc = {
            "emp_code": body.emp_code,
            "date": record_date,
            "status": body.status,
            "punch_in": punch_ts,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": compute_late_minutes(punch_ts, employee["shift_start"], record_date),
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "already punched in for this date") from exc
    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown employee")
    punch_out_dt = epoch_ms_to_datetime(body.punched_at) or utcnow()
    punch_out_dt = punch_out_dt.replace(microsecond=0)
    open_record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_out": None}, sort=[("punch_in", -1)])
    if open_record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no punch-in found")
    if open_record["punch_in"] is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no punch-in found")
    open_punch_in = normalize_to_utc(open_record["punch_in"])
    if punch_out_dt <= open_punch_in:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "punch_out must be after punch_in")
    delta_hours = (punch_out_dt - open_punch_in).total_seconds() / 3600
    if delta_hours > 24:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "punch_out must be within 24 hours of punch_in")
    work_hours = compute_work_hours(open_punch_in, punch_out_dt)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": open_record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": punch_out_dt,
            "work_hours": work_hours,
            "overtime_minutes": compute_overtime_minutes(punch_out_dt, employee["shift_start"], employee["shift_end"], open_record["date"]),
            "half_day": compute_half_day(work_hours),
        }},
        return_document=True,
    )
    if updated is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "that record is already punched out")
    updated["late_minutes"] = compute_late_minutes(open_punch_in, employee["shift_start"], open_record["date"])
    updated["status"] = open_record["status"]
    return serialize_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "date_from must be <= date_to")
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    items = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [serialize_attendance_record(item) for item in items], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, payload: RegularizeRequest) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown employee")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no record for this date")

    original_history = list(record.get("history", []))
    current = dict(record)
    changes: dict[str, Any] = {}
    new_status = payload.status if payload.status is not None else current.get("status")
    if new_status in {"ABSENT", "LEAVE"} and payload.punch_in is not None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "ABSENT or LEAVE records cannot have punch_in")
    if new_status in {"ABSENT", "LEAVE"} and payload.punch_out is not None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "ABSENT or LEAVE records cannot have punch_out")

    if payload.punch_in is not None:
        punch_in_dt = epoch_ms_to_datetime(payload.punch_in)
        if punch_in_dt is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid punch_in")
        punch_in_dt = normalize_to_utc(punch_in_dt)
        punch_in_date = attendance_date_for_punch_in(employee, punch_in_dt)
        if punch_in_date != date:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "punch_in must stay on the attendance date")
        current["punch_in"] = punch_in_dt
        if "punch_in" not in changes:
            changes["punch_in"] = {"from": record.get("punch_in"), "to": punch_in_dt}
        if current["punch_out"] is not None and punch_in_dt >= normalize_to_utc(current["punch_out"]):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "punch_out must be after punch_in")

    if payload.punch_out is not None:
        punch_out_dt = epoch_ms_to_datetime(payload.punch_out)
        if punch_out_dt is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid punch_out")
        punch_out_dt = normalize_to_utc(punch_out_dt)
        if current.get("punch_in") is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "presence status requires a punch_in")
        if punch_out_dt <= normalize_to_utc(current["punch_in"]):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "punch_out must be after punch_in")
        max_delta = normalize_to_utc(current["punch_in"]) + timedelta(hours=24)
        if punch_out_dt > max_delta:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "punch_out must be within 24 hours of punch_in")
        current["punch_out"] = punch_out_dt
        if "punch_out" not in changes:
            changes["punch_out"] = {"from": record.get("punch_out"), "to": punch_out_dt}

    if payload.status is not None:
        current["status"] = payload.status
        if payload.status in {"ABSENT", "LEAVE"}:
            current["punch_in"] = None
            current["punch_out"] = None
            if record.get("punch_in") is not None or record.get("punch_out") is not None:
                changes.setdefault("punch_in", {"from": record.get("punch_in"), "to": None})
                changes.setdefault("punch_out", {"from": record.get("punch_out"), "to": None})
        else:
            if current.get("punch_in") is None:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "presence status requires a punch_in")
        if payload.status != record.get("status"):
            changes["status"] = {"from": record.get("status"), "to": payload.status}

    if payload.punch_in is None and payload.punch_out is None and payload.status is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "request changes nothing")

    if current.get("status") in {"ABSENT", "LEAVE"}:
        current["work_hours"] = None
        current["late_minutes"] = 0
        current["overtime_minutes"] = 0
        current["half_day"] = False
    elif current.get("punch_in") is not None:
        current["late_minutes"] = compute_late_minutes(current["punch_in"], employee["shift_start"], date)
        if current.get("punch_out") is not None:
            work_hours = compute_work_hours(current["punch_in"], current["punch_out"])
            current["work_hours"] = work_hours
            current["overtime_minutes"] = compute_overtime_minutes(current["punch_out"], employee["shift_start"], employee["shift_end"], date)
            current["half_day"] = compute_half_day(work_hours)
        else:
            current["work_hours"] = None
            current["overtime_minutes"] = 0
            current["half_day"] = False
    else:
        current["work_hours"] = None
        current["late_minutes"] = 0
        current["overtime_minutes"] = 0
        current["half_day"] = False

    for field in ["work_hours", "late_minutes", "overtime_minutes", "half_day"]:
        if record.get(field) != current.get(field):
            changes.setdefault(field, {"from": record.get(field), "to": current.get(field)})

    if not changes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "request changes nothing")

    new_history = list(original_history)
    new_history.append({
        "at": utcnow(),
        "by": payload.regularized_by,
        "reason": payload.reason,
        "changes": {k: v for k, v in changes.items() if k in {"status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"}},
    })
    current["history"] = new_history

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": original_history},
        {"$set": {
            "status": current["status"],
            "punch_in": current.get("punch_in"),
            "punch_out": current.get("punch_out"),
            "work_hours": current.get("work_hours"),
            "late_minutes": current.get("late_minutes"),
            "overtime_minutes": current.get("overtime_minutes"),
            "half_day": current.get("half_day"),
            "history": new_history,
        }},
        return_document=True,
    )
    if updated is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "record changed concurrently")
    return serialize_attendance_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown employee")
    year, month_num = map(int, month.split("-"))
    first_day = datetime(year, month_num, 1, tzinfo=IST)
    last_day_number = calendar.monthrange(year, month_num)[1]
    last_day = datetime(year, month_num, last_day_number, tzinfo=IST)
    joined_on = datetime.strptime(employee["joined_on"], "%Y-%m-%d").replace(tzinfo=IST)
    working_days = 0
    present_days = Decimal("0")
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0
    records = list(db.attendance_logs.find({"emp_code": emp_code, "date": {"$gte": first_day.strftime("%Y-%m-%d"), "$lte": last_day.strftime("%Y-%m-%d")}}))
    for record in records:
        day = record["date"]
        record_date = datetime.strptime(day, "%Y-%m-%d").date()
        if record_date >= joined_on.date():
            if record_date.weekday() < 5:
                working_days += 1
        if record.get("status") in PRESENT_STATUSES and record_date.weekday() < 5:
            present_days += Decimal("1") if not record.get("half_day", False) else Decimal("0.5")
        if record.get("status") == "LEAVE":
            leave_days += 1
        if record.get("late_minutes", 0) > 0:
            late_count += 1
            total_late_minutes += int(record.get("late_minutes", 0))
        total_overtime_minutes += int(record.get("overtime_minutes", 0))

    attendance_pct = None
    if working_days:
        attendance_pct = float(round_half_up_decimal((present_days / Decimal(working_days)) * Decimal("100"), 2))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present_days),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    year, month_num = map(int, month.split("-"))
    start_date = f"{year}-{month_num:02d}-01"
    last_day = calendar.monthrange(year, month_num)[1]
    end_date = f"{year}-{month_num:02d}-{last_day:02d}"

    employee_match: dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department is not None:
        employee_match["department"] = department
    employees = list(db.employees.find(employee_match, {"_id": 0, "emp_code": 1, "department": 1, "joined_on": 1}))
    if not employees:
        return {"month": month, "items": []}
    department_to_employees = {}
    for emp in employees:
        dept = emp["department"]
        department_to_employees.setdefault(dept, []).append(emp)

    results = []
    for dept in sorted(department_to_employees):
        emp_codes = [item["emp_code"] for item in department_to_employees[dept]]
        records = list(db.attendance_logs.find({"emp_code": {"$in": emp_codes}, "date": {"$gte": start_date, "$lte": end_date}}))
        headcount = len(emp_codes)
        present_days = Decimal("0")
        total_work_hours = Decimal("0")
        work_hours_count = 0
        late_count = 0
        total_late_minutes = 0
        leave_count = 0
        on_duty_count = 0

        for record in records:
            day = record.get("date")
            if day is None:
                continue
            if record.get("status") in PRESENT_STATUSES and datetime.strptime(day, "%Y-%m-%d").weekday() < 5:
                present_days += Decimal("1") if not record.get("half_day", False) else Decimal("0.5")
            if record.get("status") == "LEAVE":
                leave_count += 1
            if record.get("status") == "ON_DUTY":
                on_duty_count += 1
            if record.get("late_minutes", 0) > 0:
                late_count += 1
                total_late_minutes += int(record.get("late_minutes", 0))
            if record.get("status") in PRESENT_STATUSES and record.get("work_hours") is not None:
                total_work_hours += Decimal(str(record["work_hours"]))
                work_hours_count += 1

        avg_work_hours = None
        if work_hours_count:
            avg_work_hours = float(round_half_up_decimal(total_work_hours / Decimal(work_hours_count), 2))
        results.append({
            "department": dept,
            "headcount": headcount,
            "present_days": float(present_days),
            "avg_work_hours": avg_work_hours,
            "late_count": late_count,
            "total_late_minutes": total_late_minutes,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })
    return {"month": month, "items": results}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    year, month_num = map(int, month.split("-"))
    first_day = f"{year}-{month_num:02d}-01"
    last_day = calendar.monthrange(year, month_num)[1]
    last_day_str = f"{year}-{month_num:02d}-{last_day:02d}"

    employee_match: dict[str, Any] = {}
    if department is not None:
        employee_match["department"] = department
    employees = list(db.employees.find(employee_match, {"_id": 0, "emp_code": 1, "name": 1, "department": 1}))
    employee_lookup = {emp["emp_code"]: emp for emp in employees}
    records = list(db.attendance_logs.find({"emp_code": {"$in": list(employee_lookup)}, "date": {"$gte": first_day, "$lte": last_day_str}}))
    totals: dict[str, dict[str, Any]] = {}
    for record in records:
        emp_code = record["emp_code"]
        if emp_code not in employee_lookup:
            continue
        total = totals.setdefault(emp_code, {"emp_code": emp_code, "name": employee_lookup[emp_code]["name"], "department": employee_lookup[emp_code]["department"], "total_late_minutes": 0, "late_count": 0})
        minutes = int(record.get("late_minutes", 0))
        if minutes > 0:
            total["total_late_minutes"] += minutes
            total["late_count"] += 1
    rows = [
        {"emp_code": data["emp_code"], "name": data["name"], "department": data["department"], "total_late_minutes": data["total_late_minutes"], "late_count": data["late_count"]}
        for data in totals.values()
        if data["total_late_minutes"] > 0
    ]
    rows.sort(key=lambda item: (-item["total_late_minutes"], item["emp_code"]))
    if not rows:
        return {"month": month, "items": []}
    ranked = []
    prev_total = None
    prev_rank = 0
    for index, row in enumerate(rows, start=1):
        if prev_total is None or row["total_late_minutes"] != prev_total:
            prev_rank = index
            prev_total = row["total_late_minutes"]
        row["rank"] = prev_rank
        ranked.append(row)
    filtered = [row for row in ranked if row["rank"] <= limit]
    return {"month": month, "items": [{"rank": item["rank"], "emp_code": item["emp_code"], "name": item["name"], "department": item["department"], "total_late_minutes": item["total_late_minutes"], "late_count": item["late_count"]} for item in filtered]}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(alias="from"),
    to_date: str = Query(alias="to"),
) -> dict[str, Any]:
    if to_date < from_date:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "to must be >= from")
    start_dt = datetime.strptime(from_date, "%Y-%m-%d")
    end_dt = datetime.strptime(to_date, "%Y-%m-%d")
    if (end_dt - start_dt).days + 1 > 92:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "range cannot exceed 92 days")
    employee = db.employees.find_one({"department": department})
    if employee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown department")
    day_range = []
    current = start_dt
    while current <= end_dt:
        day_range.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    records = list(db.attendance_logs.find({"emp_code": {"$in": [item["emp_code"] for item in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]}, "date": {"$gte": from_date, "$lte": to_date}}))
    by_date: dict[str, dict[str, Any]] = {}
    for record in records:
        by_date.setdefault(record["date"], {"record_count": 0, "present": Decimal("0"), "late_count": 0})
        day_item = by_date[record["date"]]
        day_item["record_count"] += 1
        day_item["late_count"] += 1 if int(record.get("late_minutes", 0)) > 0 else 0
        if record.get("status") in PRESENT_STATUSES:
            day_item["present"] += Decimal("1") if not record.get("half_day", False) else Decimal("0.5")
    rows = []
    employee_dates = list(db.employees.find({"department": department}, {"_id": 0, "joined_on": 1, "emp_code": 1}))
    for day in day_range:
        headcount = sum(1 for item in employee_dates if item["joined_on"] <= day)
        present_count = float(by_date.get(day, {}).get("present", Decimal("0")))
        late_count = by_date.get(day, {}).get("late_count", 0)
        if datetime.strptime(day, "%Y-%m-%d").weekday() < 5:
            attendance_rate = None
            if headcount > 0:
                attendance_rate = float(round_half_up_decimal((Decimal(str(present_count)) / Decimal(headcount)) * Decimal("100"), 4) / Decimal("100"))
        else:
            attendance_rate = None
        rows.append({
            "date": day,
            "is_working_day": datetime.strptime(day, "%Y-%m-%d").weekday() < 5,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
        })
    window = []
    for index, row in enumerate(rows):
        if row["attendance_rate"] is not None:
            window.append(row["attendance_rate"])
            if len(window) > 7:
                window.pop(0)
        else:
            window = window[-6:]
        if window:
            row["moving_avg_7d"] = float(round_half_up_decimal(sum(Decimal(str(v)) for v in window) / Decimal(len(window)), 4))
        else:
            row["moving_avg_7d"] = None
    return {"department": department, "items": rows}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = Query(default=None),
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None, ge=1, le=50),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    if endpoint == "attendance_list":
        query: dict[str, Any] = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if date_from or date_to:
            if date_from and date_to and date_from > date_to:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "date_from must be <= date_to")
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        if status is not None:
            query["status"] = status
        collection = db.attendance_logs
        explain_doc = collection.find(query).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_explain(explain_doc)}
    if endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "emp_code and month are required")
        collection = db.attendance_logs
        explain_doc = collection.find({"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_explain(explain_doc)}
    if endpoint == "department_summary":
        if month is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "month is required")
        collection = db.attendance_logs
        explain_doc = collection.find({"date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_explain(explain_doc)}
    if endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "month is required")
        collection = db.attendance_logs
        explain_doc = collection.find({"date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}, "late_minutes": {"$gt": 0}}).sort([("late_minutes", -1), ("emp_code", 1)]).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_explain(explain_doc)}
    if endpoint == "department_trend":
        if department is None or from_date is None or to_date is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "department, from and to are required")
        collection = db.attendance_logs
        explain_doc = collection.find({"emp_code": {"$in": [item["emp_code"] for item in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]}, "date": {"$gte": from_date, "$lte": to_date}}).sort([("date", 1)]).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_explain(explain_doc)}
    raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown endpoint")
