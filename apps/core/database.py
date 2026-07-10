"""Database access for the RPA platform."""

from apps.core.job_db import JOBS_DB_PATH, JobDatabase, get_job_database

__all__ = ["JOBS_DB_PATH", "JobDatabase", "get_job_database"]
