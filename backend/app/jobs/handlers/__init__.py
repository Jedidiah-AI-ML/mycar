"""Importing this package registers every job handler."""

from app.jobs.handlers import account, alerts, system, trips  # noqa: F401
