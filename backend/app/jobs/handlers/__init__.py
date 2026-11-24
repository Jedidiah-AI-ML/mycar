"""Importing this package registers every job handler."""

from app.jobs.handlers import (  # noqa: F401
    account,
    alerts,
    dtc_interpret,
    system,
    trip_summary,
    trips,
)
