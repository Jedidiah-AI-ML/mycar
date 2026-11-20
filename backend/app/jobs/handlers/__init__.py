"""Importing this package registers every job handler."""

from app.jobs.handlers import account, alerts, system, trip_summary, trips  # noqa: F401
