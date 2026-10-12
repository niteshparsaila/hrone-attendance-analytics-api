"""HROne Employee Attendance & Analytics API (contract v2.0)."""
import os
import re
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure
from bson import json_util

load_dotenv(override=False)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, tz_aware=True)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    """Create indexes idempotently; unique indexes enforce concurrency safety."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_employee")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="attendance_date_status_employee")


# ------------------------------- Helpers ----------------------------------
def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def epoch_dt(value: int) -> datetime:
    if type(value) is not int or value < 100_000_000_000 or value > 4_102_444_800_000:
        raise HTTPException(422, "timestamps must be integer epoch milliseconds")
    try:
        return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(422, "timestamp out of range")


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def to_epoch(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(as_utc(value).timestamp() * 1000)


def halfup(value, places=2):
    return float(Decimal(str(value)).quantize(Decimal("1." + "0" * places), rounding=ROUND_HALF_UP))


def validate_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise HTTPException(422, "date must be YYYY-MM-DD")


def month_bounds(month: str):
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must be YYYY-MM")
    year, mon = map(int, month.split("-"))
    last_day = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last_day:02d}", date(year, mon, 1), date(year, mon, last_day)


def working_days(start: date, end: date) -> int:
    if start > end:
        return 0
    days = (end - start).days + 1
    weeks, rem = divmod(days, 7)
    count = weeks * 5
    for i in range(rem):
        if (start.weekday() + i) % 7 < 5:
            count += 1
    return count


def validate_shift(value: str) -> str:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("time must be HH:MM")
    return value


def attendance_date(punch: datetime, emp: dict) -> str:
    local = as_utc(punch).astimezone(IST)
    day = local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.strftime("%H:%M") < emp["shift_end"]:
        day -= timedelta(days=1)
    return day.isoformat()


def shift_start_dt(att_date: str, shift_start: str) -> datetime:
    h, m = map(int, shift_start.split(":"))
    d = date.fromisoformat(att_date)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST).astimezone(UTC)


def shift_end_dt(att_date: str, emp: dict) -> datetime:
    h, m = map(int, emp["shift_end"].split(":"))
    d = date.fromisoformat(att_date)
    if emp["shift_end"] <= emp["shift_start"]:
        d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST).astimezone(UTC)


def derived(punch_in, punch_out, att_date, emp):
    late = 0
    if punch_in:
        delta = (as_utc(punch_in) - shift_start_dt(att_date, emp["shift_start"])).total_seconds()
        late = int(delta // 60) if delta > 600 else 0
    if punch_in is None or punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    hours = halfup(Decimal(seconds) / Decimal(3600), 2)
    overtime = int((as_utc(punch_out) - shift_end_dt(att_date, emp)).total_seconds() // 60)
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": overtime if overtime >= 30 else 0, "half_day": hours < 4.5}


def public_employee(doc):
    result = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in result:
        result["created_at"] = to_epoch(result["created_at"])
    return result


def public_record(doc):
    history = []
    for item in doc.get("history", []):
        changes = {}
        for key, change in item.get("changes", {}).items():
            changes[key] = {
                "from": to_epoch(change.get("from")) if key in {"punch_in", "punch_out"} else change.get("from"),
                "to": to_epoch(change.get("to")) if key in {"punch_in", "punch_out"} else change.get("to"),
            }
        history.append({"at": to_epoch(item.get("at")), "by": item.get("by"), "reason": item.get("reason"), "changes": changes})
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": to_epoch(doc.get("punch_in")), "punch_out": to_epoch(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0), "half_day": doc.get("half_day", False),
        "history": history,
    }


def status_presence_expr(field="$status"):
    return {"$in": [field, sorted(PRESENCE)]}


def attendance_filter(emp_code=None, date_from=None, date_to=None, status=None):
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if status is not None:
        if status not in STATUSES:
            raise HTTPException(422, "invalid status")
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            validate_date(date_from)
            q["date"]["$gte"] = date_from
        if date_to:
            validate_date(date_to)
            q["date"]["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not exceed date_to")
    return q


def trend_pipeline(department: str, from_value: str, to_value: str):
    start = validate_date(from_value)
    end = validate_date(to_value)
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(422, "invalid date range (maximum 92 days)")
    # Group only dates that have logs, then densify in MongoDB to fill every date.
    return [
        {"$match": {"date": {"$gte": from_value, "$lte": to_value}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department, "employee.joined_on": {"$lte": to_value}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": {"$cond": [status_presence_expr(), {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$project": {"_id": 0, "date": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [datetime(start.year, start.month, start.day), datetime(end.year, end.month, end.day + timedelta(days=1))]}}},
        {"$set": {"present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$sort": {"date": 1}},
    ]


# -------------------------------- Models ----------------------------------
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def shift_format(cls, value):
        return validate_shift(value)

    @field_validator("joined_on")
    @classmethod
    def joined_format(cls, value):
        validate_date(value)
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def timestamp(cls, value):
        if value is not None:
            epoch_dt(value)
        return value


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def timestamp(cls, value):
        if value is not None:
            epoch_dt(value)
        return value


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def timestamps(cls, value):
        if value is not None:
            epoch_dt(value)
        return value


# -------------------------------- Routes ----------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return public_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [public_employee(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    ts = (epoch_dt(body.punched_at) if body.punched_at is not None else now_utc()).replace(microsecond=0)
    day = attendance_date(ts, emp)
    values = derived(ts, None, day, emp)
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts, "punch_out": None,
           "work_hours": None, "late_minutes": values["late_minutes"], "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return public_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    ts = (epoch_dt(body.punched_at) if body.punched_at is not None else now_utc()).replace(microsecond=0)
    doc = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not doc:
        raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= as_utc(doc["punch_in"]) or ts > as_utc(doc["punch_in"]) + timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    values = derived(doc["punch_in"], ts, doc["date"], emp)
    updated = db.attendance_logs.find_one_and_update({"_id": doc["_id"], "punch_out": None}, {"$set": {"punch_out": ts, **values}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "already punched out")
    return public_record(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    docs = db.attendance_logs.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [public_record(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    validate_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(404, "attendance record not found")
    fields = body.model_fields_set
    pi = epoch_dt(body.punch_in) if "punch_in" in fields and body.punch_in is not None else (None if "punch_in" in fields else old.get("punch_in"))
    po = epoch_dt(body.punch_out) if "punch_out" in fields and body.punch_out is not None else (None if "punch_out" in fields else old.get("punch_out"))
    status = body.status if "status" in fields and body.status is not None else old["status"]
    if status in {"ABSENT", "LEAVE"}:
        if ("punch_in" in fields and body.punch_in is not None) or ("punch_out" in fields and body.punch_out is not None):
            raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")
        pi = po = None
    elif pi is None:
        raise HTTPException(422, "presence status requires punch_in")
    if pi is not None and attendance_date(pi, emp) != date:
        raise HTTPException(422, "punch_in must belong to attendance date")
    if po is not None and (pi is None or po <= as_utc(pi) or po > as_utc(pi) + timedelta(hours=24)):
        raise HTTPException(422, "invalid punch_out")
    values = derived(pi, po, date, emp) if status in PRESENCE else {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    new_values = {"status": status, "punch_in": pi, "punch_out": po, **values}
    changes = {}
    defaults = {"half_day": False, "late_minutes": 0, "overtime_minutes": 0, "work_hours": None}
    for key, value in new_values.items():
        before = old.get(key, defaults.get(key))
        if before != value:
            changes[key] = {"from": before, "to": value}
    if not changes:
        raise HTTPException(422, "request makes no changes")
    history_entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    old_history = old.get("history", [])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": old["_id"], "$or": [{"history": old_history}, {"history": {"$exists": False}}]},
        {"$set": new_values, "$push": {"history": history_entry}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record changed concurrently; retry")
    return public_record(updated)


# ----------------------------- Aggregations -------------------------------
def monthly_employee_pipeline(emp_code: str, first: str, last: str):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$set": {"_parsed_date": {"$dateFromString": {"dateString": "$date"}}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [status_presence_expr(), {"$and": [{"$gte": [{"$dayOfWeek": "$_parsed_date"}, 2]}, {"$lte": [{"$dayOfWeek": "$_parsed_date"}, 6]}]}]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(first: str, last: str, department: Optional[str]):
    employee_match = {"joined_on": {"$lte": last}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"date": {"$gte": first, "$lte": last}, "$expr": {"$eq": ["$emp_code", "$$code"]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "employees": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [status_presence_expr("$logs.status"), {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", first]}}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", first]}}}}, 6]}]}]}, {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
            "hours": {"$push": {"$cond": [{"$and": [status_presence_expr("$logs.status"), {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", "$$REMOVE"]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$employees"}, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": [{"$size": "$hours"}, 0]}, {"$avg": "$hours"}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(first: str, last: str, limit: int, department: Optional[str]):
    pipe = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipe.append({"$match": {"employee.department": department}})
    pipe += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return pipe


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if not emp:
        raise HTTPException(404, "unknown employee")
    first, last, start, end = month_bounds(month)
    joined = max(start, validate_date(emp["joined_on"]))
    days = working_days(joined, end) if joined <= end else 0
    row = next(iter(db.attendance_logs.aggregate(monthly_employee_pipeline(emp_code, first, last))), {})
    present_days = row.get("present_days", 0)
    return {"emp_code": emp_code, "month": month, "working_days": days, "present_days": halfup(present_days, 2),
            "leave_days": row.get("leave_days", 0), "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0), "total_overtime_minutes": row.get("total_overtime_minutes", 0),
            "attendance_pct": halfup(present_days / days * 100, 2) if days else None}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first, last, _, _ = month_bounds(month)
    items = []
    for item in db.employees.aggregate(department_summary_pipeline(first, last, department)):
        if item["headcount"] > 0:
            item["present_days"] = halfup(item["present_days"], 2)
            item["avg_work_hours"] = halfup(item["avg_work_hours"], 2) if item["avg_work_hours"] is not None else None
            items.append(item)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first, last, _, _ = month_bounds(month)
    return {"month": month, "items": list(db.attendance_logs.aggregate(leaderboard_pipeline(first, last, limit, department)))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    start = validate_date(from_)
    end = validate_date(to)
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(422, "invalid date range (maximum 92 days)")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "unknown department")
    pipe = trend_pipeline(department, from_, to)
    # Window stage computes the rolling average in MongoDB over rows in the requested range.
    pipe += [
        {"$lookup": {"from": "employees", "let": {"day": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d"}}}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}}, {"$count": "count"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]}, "_weekday": {"$dayOfWeek": "$date"}}},
        {"$set": {"is_working_day": {"$and": [{"$gte": ["$_weekday", 2]}, {"$lte": ["$_weekday", 6]}]},
                   "attendance_rate": {"$cond": [{"$and": [{"$gte": ["$_weekday", 2]}, {"$lte": ["$_weekday", 6]}, {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]} }},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d"}}, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]
    items = []
    for row in db.attendance_logs.aggregate(pipe):
        row["present_count"] = halfup(row["present_count"], 2)
        row["attendance_rate"] = halfup(row["attendance_rate"], 4) if row["attendance_rate"] is not None else None
        row["moving_avg_7d"] = halfup(row["moving_avg_7d"], 4) if row["moving_avg_7d"] is not None else None
        items.append(row)
    return {"department": department, "items": items}


# ---------------------------- Explain endpoint ----------------------------
def explain_pipeline(endpoint: str, emp_code, month, department, limit, date_from, date_to, status, from_value, to_value, page, page_size):
    if endpoint == "attendance_list":
        query = attendance_filter(emp_code, date_from, date_to, status)
        cmd = {"find": "attendance_logs", "filter": query, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}
        return "attendance_logs", client.admin.command("explain", cmd, verbosity="executionStats")
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        first, last, _, _ = month_bounds(month)
        coll, pipe = "attendance_logs", monthly_employee_pipeline(emp_code, first, last)
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        first, last, _, _ = month_bounds(month)
        coll, pipe = "employees", department_summary_pipeline(first, last, department)
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        first, last, _, _ = month_bounds(month)
        coll, pipe = "attendance_logs", leaderboard_pipeline(first, last, limit, department)
    elif endpoint == "department_trend":
        if not department or not from_value or not to_value:
            raise HTTPException(422, "department, from and to are required")
        if not db.employees.find_one({"department": department}):
            raise HTTPException(404, "unknown department")
        coll, pipe = "attendance_logs", trend_pipeline(department, from_value, to_value)
    else:
        raise HTTPException(422, "unknown endpoint")
    explain = client.admin.command("explain", {"aggregate": coll, "pipeline": pipe, "cursor": {}}, verbosity="executionStats")
    return coll, explain


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                     date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None,
                     from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(422, "unknown endpoint")
    coll, explain = explain_pipeline(endpoint, emp_code, month, department, limit, date_from, date_to, status, from_, to, page, page_size)
    return {"endpoint": endpoint, "collection": coll, "explain": json_util.loads(json_util.dumps(explain))}
