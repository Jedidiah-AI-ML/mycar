"""Importing this package registers every job handler."""

from app.jobs.handlers import account, system  # noqa: F401
