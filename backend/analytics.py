"""CSV-only analytics for the independent healthcare sample dataset."""
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import pandas as pd
from flask import Blueprint, current_app, jsonify, request


DATASET_PATH = Path(__file__).resolve().parents[1] / "dataset" / "healthcare_dataset.csv"
REQUIRED_COLUMNS = (
    "Age",
    "Gender",
    "Medical Condition",
    "Date of Admission",
    "Admission Type",
    "Billing Amount",
)
analytics_blueprint = Blueprint("healthcare_analytics", __name__)


@lru_cache(maxsize=4)
def load_dataset(dataset_path):
    frame = pd.read_csv(dataset_path, dtype=str, keep_default_na=False)
    if not set(REQUIRED_COLUMNS).issubset(frame.columns):
        raise ValueError("The healthcare sample CSV is missing required columns.")

    frame = frame.loc[:, REQUIRED_COLUMNS].copy()
    for column in REQUIRED_COLUMNS:
        frame[column] = frame[column].astype("string").str.strip().replace("", pd.NA)
    frame["Age"] = pd.to_numeric(frame["Age"], errors="coerce")
    frame["Billing Amount"] = pd.to_numeric(frame["Billing Amount"], errors="coerce")
    frame["Date of Admission"] = pd.to_datetime(
        frame["Date of Admission"], format="%d-%m-%Y", errors="coerce"
    )
    return frame.loc[frame["Date of Admission"].notna()].copy()


def _parse_date(value, parameter):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{parameter} must use YYYY-MM-DD format.") from None


def _date_range(args, latest_date):
    period = args.get("period", "monthly")
    if period not in {"daily", "weekly", "monthly", "custom"}:
        raise ValueError("period must be daily, weekly, monthly, or custom.")

    if period == "custom":
        start = _parse_date(args.get("start_date"), "start_date")
        end = _parse_date(args.get("end_date"), "end_date")
        if start > end:
            raise ValueError("start_date must be on or before end_date.")
    else:
        anchor = _parse_date(args.get("anchor_date"), "anchor_date") if args.get("anchor_date") else latest_date
        if period == "daily":
            start = end = anchor
        elif period == "weekly":
            start = anchor - timedelta(days=anchor.weekday())
            end = start + timedelta(days=6)
        else:
            start = anchor.replace(day=1)
            if start.month == 12:
                end = date(start.year + 1, 1, 1) - timedelta(days=1)
            else:
                end = date(start.year, start.month + 1, 1) - timedelta(days=1)
    return period, start, end


def _distribution(frame, column):
    counts = frame[column].fillna("Not reported").value_counts()
    return [{"label": str(label), "value": int(count)} for label, count in counts.items()]


def _age_groups(frame):
    groups = pd.cut(
        frame["Age"],
        bins=[0, 18, 30, 45, 60, 75, float("inf")],
        labels=["Under 18", "18-29", "30-44", "45-59", "60-74", "75+"],
        right=False,
    )
    return [
        {"label": str(label), "value": int(count)}
        for label, count in groups.value_counts(sort=False).items()
    ]


def _billing_distribution(frame):
    amounts = frame["Billing Amount"].dropna()
    if amounts.empty:
        return []
    bins = pd.cut(amounts, bins=10, include_lowest=True, duplicates="drop")
    return [
        {
            "label": f"{interval.left:,.0f} - {interval.right:,.0f}",
            "value": int(count),
        }
        for interval, count in bins.value_counts(sort=False).items()
    ]


def build_analytics_response(dataset_path, query_args):
    frame = load_dataset(str(Path(dataset_path).resolve()))
    if frame.empty:
        raise ValueError("The healthcare sample CSV contains no valid admission dates.")

    available_start = frame["Date of Admission"].min().date()
    available_end = frame["Date of Admission"].max().date()
    period, start, end = _date_range(query_args, available_end)
    selected = frame.loc[frame["Date of Admission"].dt.date.between(start, end)]
    ages = selected["Age"].dropna()
    billing = selected["Billing Amount"].dropna()
    monthly = (
        selected.groupby(selected["Date of Admission"].dt.to_period("M"), observed=True)
        .size()
    )
    return {
        "title": "Healthcare Sample Dataset Analysis",
        "period": {
            "type": period,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "available_start_date": available_start.isoformat(),
            "available_end_date": available_end.isoformat(),
        },
        "empty": selected.empty,
        "kpis": {
            "total_records": int(len(selected)),
            "average_age": round(float(ages.mean()), 1) if not ages.empty else None,
            "total_admissions": int(len(selected)),
            "average_billing": round(float(billing.mean()), 2) if not billing.empty else None,
        },
        "charts": {
            "medical_conditions": _distribution(selected, "Medical Condition"),
            "gender": _distribution(selected, "Gender"),
            "age_groups": _age_groups(selected),
            "admission_types": _distribution(selected, "Admission Type"),
            "monthly_admissions": [
                {"label": str(month), "value": int(count)}
                for month, count in monthly.items()
            ],
            "billing_amounts": _billing_distribution(selected),
        },
    }


@analytics_blueprint.get("/api/analytics/healthcare")
def healthcare_analytics():
    try:
        result = build_analytics_response(
            current_app.config["ANALYTICS_DATASET_PATH"], request.args
        )
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except (OSError, UnicodeDecodeError, pd.errors.ParserError):
        current_app.logger.exception("Unable to read the healthcare sample dataset")
        return jsonify({"error": "The healthcare sample dataset is unavailable or invalid."}), 503
    return jsonify(result)
