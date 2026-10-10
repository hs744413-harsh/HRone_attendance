"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from typing import Literal
from pydantic import (
    BaseModel,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson.decimal128 import Decimal128
import json
from bson import json_util

load_dotenv()


UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=UTC,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]




app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def create_indexes():
    # Employees
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employee_code",
    )
    db.employees.create_index(
        [("department", 1), ("emp_code", 1)],
        name="idx_employee_department_code",
    )
    db.employees.create_index(
        [("joined_on", 1)],
        name="idx_employee_joined_on",
    )
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)],
        name="idx_employee_department_joined",
    )

    # Attendance
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="idx_attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1), ("emp_code", 1)],
        name="idx_attendance_status_date",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)],
        name="idx_attendance_employee_punchin",
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from fastapi import HTTPException


MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000


def truncate_to_seconds(value: datetime) -> datetime:
    """Normalize an instant to UTC and discard fractional seconds."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    return value.astimezone(UTC).replace(microsecond=0)


def epoch_ms_to_datetime(value: Optional[int]) -> datetime:
    """Convert epoch milliseconds to a UTC datetime."""
    if value is None:
        return truncate_to_seconds(datetime.now(UTC))

    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(
            status_code=422,
            detail="Timestamp must be an integer in epoch milliseconds",
        )

    if not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(
            status_code=422,
            detail="Timestamp is outside the permitted range",
        )

    return datetime.fromtimestamp(value // 1000, tz=UTC)


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    """Convert a UTC instant into epoch milliseconds."""
    if value is None:
        return None

    value = truncate_to_seconds(value)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = value - epoch

    return (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )


def to_ist(value: datetime) -> datetime:
    """Convert an instant to Indian Standard Time."""
    return truncate_to_seconds(value).astimezone(IST)


def compute_attendance_date(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> str:
    """Return the attendance date in IST, including overnight shifts."""
    local = to_ist(punch_in)

    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))

    overnight = (end_hour, end_minute) <= (
        start_hour,
        start_minute,
    )

    punch_time = (
        local.hour,
        local.minute,
        local.second,
    )

    shift_end_time = (end_hour, end_minute, 0)

    if overnight and punch_time < shift_end_time:
        return (local.date() - timedelta(days=1)).isoformat()

    return local.date().isoformat()


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_date: str,
) -> int:
    """Count whole late minutes only when lateness exceeds 10 minutes."""
    local = to_ist(punch_in)
    shift_date = date.fromisoformat(attendance_date)

    hour, minute = map(int, shift_start.split(":"))
    start = datetime.combine(
        shift_date,
        time(hour, minute),
        tzinfo=IST,
    )

    elapsed_seconds = int((local - start).total_seconds())

    if elapsed_seconds <= 600:
        return 0

    return elapsed_seconds // 60


def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    """Calculate hours using two-decimal half-up rounding."""
    start = truncate_to_seconds(punch_in)
    end = truncate_to_seconds(punch_out)

    elapsed_seconds = int((end - start).total_seconds())

    hours = (
        Decimal(elapsed_seconds) / Decimal(3600)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return float(hours)


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    """Count overtime only when at least 30 whole minutes are worked."""
    shift_date = date.fromisoformat(attendance_date)

    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))

    overnight = (end_hour, end_minute) <= (
        start_hour,
        start_minute,
    )

    end_date = shift_date + timedelta(days=1) if overnight else shift_date

    shift_end_dt = datetime.combine(
        end_date,
        time(end_hour, end_minute),
        tzinfo=IST,
    )

    elapsed_seconds = int(
        (to_ist(punch_out) - shift_end_dt).total_seconds()
    )

    if elapsed_seconds < 1800:
        return 0

    return elapsed_seconds // 60

def attendance_to_response(doc: dict) -> dict:
    result = dict(doc)

    # MongoDB's internal ID must never appear in the API.
    result.pop("_id", None)

    # Convert top-level instants to epoch milliseconds.
    for field in ("punch_in", "punch_out"):
        result[field] = datetime_to_epoch_ms(
            result.get(field)
        )

    # Support older seeded documents.
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)

    converted_history = []

    for original_entry in result.get("history", []):
        entry = dict(original_entry)

        if entry.get("at") is not None:
            entry["at"] = datetime_to_epoch_ms(entry["at"])

        converted_changes = {}

        for field, original_change in entry.get(
            "changes", {}
        ).items():
            change = dict(original_change)

            if field in ("punch_in", "punch_out"):
                for direction in ("from", "to"):
                    if change.get(direction) is not None:
                        change[direction] = datetime_to_epoch_ms(
                            change[direction]
                        )

            converted_changes[field] = change

        entry["changes"] = converted_changes
        converted_history.append(entry)

    result["history"] = converted_history

    return result


ALLOWED_ATTENDANCE_STATUSES = {
    "PRESENT",
    "WFH",
    "ON_DUTY",
    "ABSENT",
    "LEAVE",
}


def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[str] = None,
) -> dict:
    """Build the exact MongoDB filter used by attendance listing."""

    if status is not None and status not in ALLOWED_ATTENDANCE_STATUSES:
        raise HTTPException(
            status_code=422,
            detail="Invalid attendance status",
        )

    if date_from is not None and date_to is not None:
        if date_from > date_to:
            raise HTTPException(
                status_code=422,
                detail="date_from must not be after date_to",
            )

    query = {}

    if emp_code is not None:
        query["emp_code"] = emp_code

    if date_from is not None or date_to is not None:
        query["date"] = {}

        if date_from is not None:
            query["date"]["$gte"] = date_from.isoformat()

        if date_to is not None:
            query["date"]["$lte"] = date_to.isoformat()

    if status is not None:
        query["status"] = status

    return query




def build_employee_monthly_pipeline(
    emp_code: str,
    start_str: str,
    end_str: str,
) -> list:
    """Build the MongoDB pipeline shared by monthly analytics and explain."""

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": start_str,
                    "$lt": end_str,
                },
            }
        },
        {
            "$set": {
                "_parsed_date": {
                    "$dateFromString": {
                        "dateString": "$date",
                    }
                }
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
                                    {
                                        "$in": [
                                            "$status",
                                            [
                                                "PRESENT",
                                                "WFH",
                                                "ON_DUTY",
                                            ],
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            {
                                                "$isoDayOfWeek":
                                                    "$_parsed_date"
                                            },
                                            5,
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {
                                        "$eq": [
                                            {
                                                "$ifNull": [
                                                    "$half_day",
                                                    False,
                                                ]
                                            },
                                            True,
                                        ]
                                    },
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    {
                                        "$ifNull": [
                                            "$late_minutes",
                                            0,
                                        ]
                                    },
                                    0,
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$ifNull": ["$late_minutes", 0]
                    }
                },
                "total_overtime_minutes": {
                    "$sum": {
                        "$ifNull": ["$overtime_minutes", 0]
                    }
                },
            }
        },
    ]
    
    

def build_department_summary_pipeline(
    start_str: str,
    end_str: str,
    next_month_str: str,
    department: Optional[str] = None,
) -> list:
    """Build the shared aggregation pipeline for department summary."""

    employee_match = {
        "joined_on": {"$lte": end_str}
    }

    if department is not None:
        employee_match["department"] = department

    presence_statuses = ["PRESENT", "WFH", "ON_DUTY"]

    zero_metrics = {
        "present_days": 0,
        "work_hours_cents_sum": 0,
        "work_hours_count": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "leave_count": 0,
        "on_duty_count": 0,
    }

    pipeline = [
        {
            "$match": employee_match
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$emp_code",
                                            "$$employee_code",
                                        ]
                                    },
                                    {"$gte": ["$date", start_str]},
                                    {"$lt": ["$date", next_month_str]},
                                ]
                            }
                        }
                    },
                    {
                        "$set": {
                            "_is_weekday": {
                                "$lte": [
                                    {
                                        "$isoDayOfWeek": {
                                            "$dateFromString": {
                                                "dateString": "$date"
                                            }
                                        }
                                    },
                                    5,
                                ]
                            }
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
                                                {
                                                    "$in": [
                                                        "$status",
                                                        presence_statuses,
                                                    ]
                                                },
                                                "$_is_weekday",
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {
                                                    "$eq": [
                                                        {
                                                            "$ifNull": [
                                                                "$half_day",
                                                                False,
                                                            ]
                                                        },
                                                        True,
                                                    ]
                                                },
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "work_hours_cents_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        presence_statuses,
                                                    ]
                                                },
                                                {
                                                    "$ne": [
                                                        {
                                                            "$ifNull": [
                                                                "$work_hours",
                                                                None,
                                                            ]
                                                        },
                                                        None,
                                                    ]
                                                },
                                            ]
                                        },
                                        {
                                            "$round": [
                                                {
                                                    "$multiply": [
                                                        "$work_hours",
                                                        100,
                                                    ]
                                                },
                                                0,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        presence_statuses,
                                                    ]
                                                },
                                                {
                                                    "$ne": [
                                                        {
                                                            "$ifNull": [
                                                                "$work_hours",
                                                                None,
                                                            ]
                                                        },
                                                        None,
                                                    ]
                                                },
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                {
                                                    "$ifNull": [
                                                        "$late_minutes",
                                                        0,
                                                    ]
                                                },
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "total_late_minutes": {
                                "$sum": {
                                    "$ifNull": ["$late_minutes", 0]
                                }
                            },
                            "leave_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "LEAVE"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "ON_DUTY"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "monthly_metrics",
            }
        },
        {
            "$set": {
                "metrics": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$monthly_metrics", 0]},
                        zero_metrics,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$metrics.present_days"},
                "work_hours_cents_sum": {
                    "$sum": "$metrics.work_hours_cents_sum"
                },
                "work_hours_count": {
                    "$sum": "$metrics.work_hours_count"
                },
                "late_count": {"$sum": "$metrics.late_count"},
                "total_late_minutes": {
                    "$sum": "$metrics.total_late_minutes"
                },
                "leave_count": {"$sum": "$metrics.leave_count"},
                "on_duty_count": {"$sum": "$metrics.on_duty_count"},
            }
        },
        {
            "$sort": {"_id": 1}
        },
    ]

    return pipeline



def build_late_leaderboard_pipeline(
    start_str: str,
    end_str: str,
    limit: int = 10,
    department: Optional[str] = None,
) -> list:
    """Shared aggregation pipeline for leaderboard and explain."""

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": start_str,
                    "$lt": end_str,
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    # Restrict department before ranking.
    if department is not None:
        pipeline.append({
            "$match": {
                "employee.department": department,
            }
        })

    pipeline.extend([
        {
            "$group": {
                "_id": "$emp_code",
                "name": {"$first": "$employee.name"},
                "department": {"$first": "$employee.department"},
                "total_late_minutes": {
                    "$sum": {
                        "$ifNull": ["$late_minutes", 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    {
                                        "$ifNull": [
                                            "$late_minutes",
                                            0,
                                        ]
                                    },
                                    0,
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$match": {
                "total_late_minutes": {"$gt": 0},
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {
                    "total_late_minutes": -1,
                },
                "output": {
                    "rank": {
                        "$rank": {},
                    }
                },
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit},
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1,
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    return pipeline



def build_department_trend_pipeline(
    department: str,
    from_date: date,
    to_date: date,
) -> list:
    """Build the shared trend aggregation for API and explain."""

    if to_date < from_date:
        raise HTTPException(
            status_code=422,
            detail="'to' must not be earlier than 'from'",
        )

    day_count = (to_date - from_date).days + 1

    if day_count > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range must not exceed 92 days",
        )

    start_datetime = datetime.combine(
        from_date,
        time.min,
        tzinfo=UTC,
    )

    presence_statuses = ["PRESENT", "WFH", "ON_DUTY"]

    pipeline = [
        {
            "$match": {
                "department": department,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on",
                    }
                },
            }
        },
        {
            "$set": {
                "_emp_codes": {
                    "$map": {
                        "input": "$employees",
                        "as": "employee",
                        "in": "$$employee.emp_code",
                    }
                },
                "_calendar_dates": {
                    "$map": {
                        "input": {"$range": [0, day_count]},
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {
                                    "$dateAdd": {
                                        "startDate": start_datetime,
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                                "timezone": "UTC",
                            }
                        },
                    }
                },
            }
        },
        {
            "$unwind": "$_calendar_dates"
        },
        {
            "$set": {
                "date": "$_calendar_dates",
                "is_working_day": {
                    "$lte": [
                        {
                            "$isoDayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$_calendar_dates",
                                }
                            }
                        },
                        5,
                    ]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "employee",
                            "cond": {
                                "$lte": [
                                    "$$employee.joined_on",
                                    "$_calendar_dates",
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "_calendar_dates",
                "foreignField": "date",
                "as": "_daily_logs",
            }
        },
        {
            "$set": {
                "_department_logs": {
                    "$filter": {
                        "input": "$_daily_logs",
                        "as": "log",
                        "cond": {
                            "$in": [
                                "$$log.emp_code",
                                "$_emp_codes",
                            ]
                        },
                    }
                }
            }
        },
        {
            "$set": {
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$_department_logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$in": [
                                            "$$log.status",
                                            presence_statuses,
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {
                                                "$eq": [
                                                    {
                                                        "$ifNull": [
                                                            "$$log.half_day",
                                                            False,
                                                        ]
                                                    },
                                                    True,
                                                ]
                                            },
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$_department_logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {
                                        "$ifNull": [
                                            "$$log.late_minutes",
                                            0,
                                        ]
                                    },
                                    0,
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {"$toDecimal": "$present_count"},
                                {"$toDecimal": "$headcount"},
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$ne": ["$attendance_rate", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$attendance_rate",
                                                    Decimal128("10000"),
                                                ]
                                            },
                                            Decimal128("0.5"),
                                        ]
                                    }
                                },
                                Decimal128("10000"),
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {
                    "_calendar_dates": 1,
                },
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$moving_avg_7d",
                                                    Decimal128("10000"),
                                                ]
                                            },
                                            Decimal128("0.5"),
                                        ]
                                    }
                                },
                                Decimal128("10000"),
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_calendar_dates",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {
            "$sort": {
                "date": 1,
            }
        },
    ]

    return pipeline




# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)

    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")

        if parsed.isoformat() != value:
            raise ValueError("joined_on must be YYYY-MM-DD")

        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError(
                "shift_start must differ from shift_end"
            )
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    
    
class RegularizeIn(BaseModel):
    status: Literal[
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    ] | None = None

    punch_in: StrictInt | None = None
    punch_out: StrictInt | None = None

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable",
        )

    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )

    doc.pop("_id", None)
    doc["created_at"] = datetime_to_epoch_ms(doc["created_at"])

    return doc



@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}

    if department is not None:
        query["department"] = department

    skip = (page - 1) * page_size

    total = db.employees.count_documents(query)

    cursor = (
        db.employees
        .find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    items = []

    for doc in cursor:
        if doc.get("created_at") is not None:
            doc["created_at"] = datetime_to_epoch_ms(
                doc["created_at"]
            )

        items.append(doc)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one(
        {"emp_code": body.emp_code}
    )

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(
            status_code=422,
            detail="Invalid punch-in status",
        )

    ts = epoch_ms_to_datetime(body.punched_at)

    attendance_date = compute_attendance_date(
        ts,
        emp["shift_start"],
        emp["shift_end"],
    )

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts,
            emp["shift_start"],
            attendance_date,
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        )

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc["punch_in"]),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": doc["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    allowed_statuses = {
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    }

    if status is not None and status not in allowed_statuses:
        raise HTTPException(
            status_code=422,
            detail="Invalid attendance status",
        )

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must not be after date_to",
        )

    query = {}

    if emp_code is not None:
        query["emp_code"] = emp_code

    if date_from is not None or date_to is not None:
        query["date"] = {}

        if date_from is not None:
            query["date"]["$gte"] = date_from.isoformat()

        if date_to is not None:
            query["date"]["$lte"] = date_to.isoformat()

    if status is not None:
        query["status"] = status

    skip = (page - 1) * page_size

    total = db.attendance_logs.count_documents(query)

    cursor = (
        db.attendance_logs
        .find(query, {"_id": 0})
        .sort([
            ("date", -1),
            ("emp_code", 1),
        ])
        .skip(skip)
        .limit(page_size)
    )

    items = [
        attendance_to_response(doc)
        for doc in cursor
    ]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    
    
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    # 1. Check that the employee exists.
    emp = db.employees.find_one(
        {"emp_code": body.emp_code}
    )

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    # 2. Convert the requested punch-out time to UTC.
    ts = epoch_ms_to_datetime(body.punched_at)

    # 3. Find the most recent punch-in not later than this time.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
        },
        sort=[("punch_in", -1)],
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No punch-in record found",
        )

    # 4. Reject an already-closed record.
    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Attendance already punched out",
        )

    punch_in_time = record["punch_in"]

    # 5. Punch-out must be after punch-in and within 24 hours.
    elapsed_seconds = int(
        (ts - punch_in_time).total_seconds()
    )

    if elapsed_seconds <= 0 or elapsed_seconds > 24 * 3600:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in and within 24 hours",
        )

    # 6. Compute the derived values.
    work_hours = compute_work_hours(
        punch_in_time,
        ts,
    )

    overtime_minutes = compute_overtime(
        ts,
        emp["shift_start"],
        emp["shift_end"],
        record["date"],
    )

    half_day = work_hours < 4.50

    # 7. Atomically close the record.
    # Only one concurrent request can change punch_out from null.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance already punched out",
        )

    # 8. Return the updated record in API format.
    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]}
    )

    return attendance_to_response(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    # 1. Validate the calendar date.
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
        if parsed_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date must be YYYY-MM-DD",
        )

    fields = body.model_fields_set

    # Explicit nulls are not valid for these optional request fields.
    if "status" in fields and body.status is None:
        raise HTTPException(422, "status cannot be null")

    for field in ("punch_in", "punch_out"):
        if field in fields and getattr(body, field) is None:
            raise HTTPException(
                status_code=422,
                detail=f"{field} cannot be null",
            )

    # 2. Verify employee and attendance record exist.
    emp = db.employees.find_one({"emp_code": emp_code})

    if emp is None:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date,
    })

    if record is None:
        raise HTTPException(404, "Attendance record not found")

    # 3. Work out the proposed final values.
    old_status = record["status"]
    new_status = body.status if "status" in fields else old_status

    old_in = record.get("punch_in")
    old_out = record.get("punch_out")

    new_in = (
        epoch_ms_to_datetime(body.punch_in)
        if "punch_in" in fields
        else old_in
    )
    new_out = (
        epoch_ms_to_datetime(body.punch_out)
        if "punch_out" in fields
        else old_out
    )

    # ABSENT and LEAVE must have no punch times.
    if new_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in fields or "punch_out" in fields:
            raise HTTPException(
                422,
                "Do not supply punch times with ABSENT or LEAVE",
            )

        new_in = None
        new_out = None

    else:
        # Presence statuses require a punch-in.
        if new_in is None:
            raise HTTPException(
                422,
                "A presence status requires punch_in",
            )

        # Punch-in must remain on this attendance day.
        expected_date = compute_attendance_date(
            new_in,
            emp["shift_start"],
            emp["shift_end"],
        )

        if expected_date != date:
            raise HTTPException(
                422,
                "punch_in must stay on the attendance date",
            )

        # If there is a punch-out, validate its time.
        if new_out is not None:
            elapsed = int((new_out - new_in).total_seconds())

            if elapsed <= 0 or elapsed > 24 * 3600:
                raise HTTPException(
                    422,
                    "punch_out must be after punch_in and within 24 hours",
                )

    # 4. Recompute derived fields from the final values.
    if new_status in {"ABSENT", "LEAVE"}:
        new_work_hours = None
        new_late = 0
        new_overtime = 0
        new_half_day = False
    else:
        new_late = compute_late_minutes(
            new_in,
            emp["shift_start"],
            date,
        )

        if new_out is not None:
            new_work_hours = compute_work_hours(new_in, new_out)

            new_overtime = compute_overtime(
                new_out,
                emp["shift_start"],
                emp["shift_end"],
                date,
            )

            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime = 0
            new_half_day = False

    # 5. Build an audit trail containing only actual changes.
    old_values = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }

    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half_day,
    }

    changes = {
        field: {
            "from": old_values[field],
            "to": new_values[field],
        }
        for field in old_values
        if old_values[field] != new_values[field]
    }

    if not changes:
        raise HTTPException(
            422,
            "The correction does not change anything",
        )

    history_entry = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # 6. Compare-and-swap: prevent concurrent edits from losing history.
    condition = {"_id": record["_id"]}

    guarded_fields = (
        "status", "punch_in", "punch_out", "work_hours",
        "late_minutes", "overtime_minutes", "half_day", "history",
    )

    for field in guarded_fields:
        if field in record:
            condition[field] = record[field]
        else:
            condition[field] = {"$exists": False}

    result = db.attendance_logs.update_one(
        condition,
        {
            "$set": new_values,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            409,
            "Attendance changed concurrently; reload and retry",
        )

    updated = db.attendance_logs.find_one({
        "_id": record["_id"],
    })

    return attendance_to_response(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
):
    # 1. Verify that employee exists.
    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "emp_code": 1, "joined_on": 1},
    )

    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    # 2. Get the month's start and exclusive end.
    month_start = date.fromisoformat(month + "-01")

    if month_start.month == 12:
        next_month = date(month_start.year + 1, 1, 1)
    else:
        next_month = date(
            month_start.year,
            month_start.month + 1,
            1,
        )

    start_str = month_start.isoformat()
    end_str = next_month.isoformat()

    # 3. Calculate working days from the employee's join date.
    joined_on = date.fromisoformat(employee["joined_on"])
    working_start = max(month_start, joined_on)
    month_end = next_month - timedelta(days=1)

    working_days = 0
    current_day = working_start

    while current_day <= month_end:
        if current_day.weekday() < 5:
            working_days += 1

        current_day += timedelta(days=1)

    # 4. Aggregate attendance metrics in MongoDB.
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": start_str,
                    "$lt": end_str,
                },
            }
        },
        {
            "$set": {
                "_parsed_date": {
                    "$dateFromString": {
                        "dateString": "$date",
                    }
                }
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
                                    {
                                        "$in": [
                                            "$status",
                                            [
                                                "PRESENT",
                                                "WFH",
                                                "ON_DUTY",
                                            ],
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            {
                                                "$isoDayOfWeek":
                                                    "$_parsed_date"
                                            },
                                            5,
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {
                                        "$eq": [
                                            {
                                                "$ifNull": [
                                                    "$half_day",
                                                    False,
                                                ]
                                            },
                                            True,
                                        ]
                                    },
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },

                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    {
                                        "$ifNull": [
                                            "$late_minutes",
                                            0,
                                        ]
                                    },
                                    0,
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },

                "total_late_minutes": {
                    "$sum": {
                        "$ifNull": ["$late_minutes", 0]
                    }
                },

                "total_overtime_minutes": {
                    "$sum": {
                        "$ifNull": ["$overtime_minutes", 0]
                    }
                },
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    # 5. Employees with no attendance logs still get a summary.
    if results:
        metrics = results[0]
    else:
        metrics = {
            "present_days": 0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
        }

    present_days = metrics["present_days"]

    # 6. Calculate attendance percentage using half-up rounding.
    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = float(
            (
                Decimal(str(present_days))
                / Decimal(working_days)
                * Decimal(100)
            ).quantize(
                Decimal("0.0001"),
                rounding=ROUND_HALF_UP,
            )
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": metrics["leave_days"],
        "late_count": metrics["late_count"],
        "total_late_minutes": metrics["total_late_minutes"],
        "total_overtime_minutes": metrics["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }
    



@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    department: Optional[str] = None,
):
    month_start = date.fromisoformat(month + "-01")

    if month_start.month == 12:
        next_month = date(month_start.year + 1, 1, 1)
    else:
        next_month = date(
            month_start.year,
            month_start.month + 1,
            1,
        )

    start_str = month_start.isoformat()
    end_str = (next_month - timedelta(days=1)).isoformat()
    next_month_str = next_month.isoformat()

    # Reuse the shared pipeline builder.
    pipeline = build_department_summary_pipeline(
        start_str=start_str,
        end_str=end_str,
        next_month_str=next_month_str,
        department=department,
    )

    rows = list(db.employees.aggregate(pipeline))

    items = []

    for row in rows:
        count = row["work_hours_count"]

        if count == 0:
            avg_work_hours = None
        else:
            average = (
                Decimal(str(row["work_hours_cents_sum"]))
                / Decimal("100")
                / Decimal(count)
            )

            avg_work_hours = float(
                average.quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )
            )

        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": row["present_days"],
            "avg_work_hours": avg_work_hours,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })

    return {
        "month": month,
        "items": items,
    }




@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_start = date.fromisoformat(month + "-01")

    if month_start.month == 12:
        next_month = date(month_start.year + 1, 1, 1)
    else:
        next_month = date(
            month_start.year,
            month_start.month + 1,
            1,
        )

    pipeline = build_late_leaderboard_pipeline(
        start_str=month_start.isoformat(),
        end_str=next_month.isoformat(),
        limit=limit,
        department=department,
    )

    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items,
    }

    
 
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    pipeline = build_department_trend_pipeline(
        department=department,
        from_date=from_date,
        to_date=to_date,
    )

    rows = list(db.employees.aggregate(pipeline))

    if not rows:
        raise HTTPException(
            status_code=404,
            detail="Department not found",
        )

    items = []

    for row in rows:
        item = dict(row)

        for field in ("attendance_rate", "moving_avg_7d"):
            value = item[field]
            item[field] = (
                float(Decimal(str(value)))
                if value is not None
                else None
            )

        items.append(item)

    return {
        "department": department,
        "items": items,
    }
   


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(
        None,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
):
    # 1. Attendance list explain
    if endpoint == "attendance_list":
        query = build_attendance_list_query(
            emp_code=emp_code,
            date_from=date_from,
            date_to=date_to,
            status=status,
        )

        find_command = {
            "find": "attendance_logs",
            "filter": query,
            "projection": {"_id": 0},
            "sort": {
                "date": -1,
                "emp_code": 1,
            },
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }

        raw_explain = db.command(
            "explain",
            find_command,
            verbosity="executionStats",
        )

        collection = "attendance_logs"

    # 2. Employee monthly explain
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required",
            )

        employee = db.employees.find_one(
            {"emp_code": emp_code},
            {"_id": 0, "emp_code": 1},
        )

        if employee is None:
            raise HTTPException(
                status_code=404,
                detail="Employee not found",
            )

        month_start = date.fromisoformat(month + "-01")

        if month_start.month == 12:
            next_month = date(month_start.year + 1, 1, 1)
        else:
            next_month = date(
                month_start.year,
                month_start.month + 1,
                1,
            )

        pipeline = build_employee_monthly_pipeline(
            emp_code,
            month_start.isoformat(),
            next_month.isoformat(),
        )

        raw_explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        collection = "attendance_logs"
        
    
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(
                status_code=422,
                detail="month is required",
            )

        month_start = date.fromisoformat(month + "-01")

        if month_start.month == 12:
            next_month = date(month_start.year + 1, 1, 1)
        else:
            next_month = date(
                month_start.year,
                month_start.month + 1,
                1,
            )

        start_str = month_start.isoformat()
        end_str = (next_month - timedelta(days=1)).isoformat()
        next_month_str = next_month.isoformat()

        pipeline = build_department_summary_pipeline(
            start_str=start_str,
            end_str=end_str,
            next_month_str=next_month_str,
            department=department,
        )

        raw_explain = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        collection = "employees"
        
        
    
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(
                status_code=422,
                detail="month is required",
            )

        month_start = date.fromisoformat(month + "-01")

        if month_start.month == 12:
            next_month = date(month_start.year + 1, 1, 1)
        else:
            next_month = date(
                month_start.year,
                month_start.month + 1,
                1,
            )

        pipeline = build_late_leaderboard_pipeline(
            start_str=month_start.isoformat(),
            end_str=next_month.isoformat(),
            limit=limit,
            department=department,
        )

        raw_explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        collection = "attendance_logs"

    
    elif endpoint == "department_trend":
        if department is None or from_date is None or to_date is None:
            raise HTTPException(
                status_code=422,
                detail="department, from and to are required",
            )

        # Match the real endpoint's 404 behaviour.
        existing_department = db.employees.find_one(
            {"department": department},
            {"_id": 0, "emp_code": 1},
        )

        if existing_department is None:
            raise HTTPException(
                status_code=404,
                detail="Department not found",
            )

        pipeline = build_department_trend_pipeline(
            department=department,
            from_date=from_date,
            to_date=to_date,
        )

        raw_explain = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        collection = "employees"

    

    else:
        raise HTTPException(
            status_code=501,
            detail=f"Explain builder for '{endpoint}' is not wired yet",
        )

    # MongoDB BSON values -> JSON-safe Extended JSON
    explain_json = json.loads(
        json_util.dumps(
            raw_explain,
            json_options=json_util.CANONICAL_JSON_OPTIONS,
        )
    )

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain_json,
    }







    

# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
