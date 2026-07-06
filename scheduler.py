"""Background scheduler: one daily cron job per enabled export schedule."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

import exporter
import models

log = logging.getLogger("hcem.scheduler")

_scheduler = BackgroundScheduler(
    job_defaults={
        "max_instances": 1,       # never run the same schedule twice at once
        "coalesce": True,         # collapse missed runs into one
        "misfire_grace_time": 3600,
    }
)


def _job_id(schedule_id: int) -> str:
    return f"export-{schedule_id}"


def sync_jobs():
    """Reconcile APScheduler jobs with the schedules table. Called on start
    and after any schedule create/toggle/delete."""
    wanted = {}
    for s in models.get_schedules():
        if s["enabled"]:
            wanted[_job_id(s["id"])] = s

    existing = {j.id: j for j in _scheduler.get_jobs()}

    for job_id in existing:
        if job_id not in wanted:
            _scheduler.remove_job(job_id)
            log.info("Removed job %s", job_id)

    for job_id, s in wanted.items():
        hour, minute = s["run_time"].split(":")
        _scheduler.add_job(
            exporter.run_export,
            trigger="cron",
            hour=int(hour),
            minute=int(minute),
            id=job_id,
            args=[s["id"]],
            replace_existing=True,
        )
    log.info("Scheduler synced: %d active job(s)", len(wanted))


def run_now(schedule_id: int):
    """Fire a one-off export immediately (in the scheduler's thread pool)."""
    _scheduler.add_job(exporter.run_export, args=[schedule_id],
                       id=f"manual-{schedule_id}", replace_existing=True)


def run_import(import_id: int):
    """Fire a one-off VM import immediately (in the scheduler's thread pool)."""
    import importer
    _scheduler.add_job(importer.run_import, args=[import_id],
                       id=f"import-{import_id}", replace_existing=True)


def start():
    _scheduler.start()
    sync_jobs()


def next_run_time(schedule_id: int):
    job = _scheduler.get_job(_job_id(schedule_id))
    return job.next_run_time if job else None
