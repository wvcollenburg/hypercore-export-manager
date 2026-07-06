"""The actual export job: trigger export, poll the task, prune old copies."""
from __future__ import annotations

import logging
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from urllib.parse import quote, urlsplit, urlunsplit

import models
from hypercore import HyperCoreClient, HyperCoreError

log = logging.getLogger("hcem.exporter")

POLL_INTERVAL_S = 15
# Exports of large VMs can take hours; give up after this long.
EXPORT_TIMEOUT_S = 8 * 3600

TIMESTAMP_FMT = "%Y%m%d-%H%M%S"


def safe_name(name: str) -> str:
    """VM names can contain characters that are awkward in folder names."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "vm"


def build_path_uri(base: str, user: str | None, password: str | None) -> str:
    """Inject share credentials into a credential-free base URI.

    The user types a clean destination (smb://nas.local/share/path) plus a
    plain username/password in separate fields; HyperCore's pathURI wants them
    embedded as smb://user:pass@host/... with special characters percent-
    encoded. Doing the encoding here means users never hand-escape passwords.

    With no username (e.g. NFS, or an anonymous share) the base is returned
    unchanged, so a URI that already carries its own credentials still works.
    """
    if not user:
        return base
    parts = urlsplit(base)
    userinfo = quote(user, safe="")
    if password:
        userinfo += ":" + quote(password, safe="")
    # Rebuild netloc from host[:port] only, dropping any userinfo already present.
    host = parts.hostname or parts.netloc
    netloc = f"{userinfo}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit(parts._replace(netloc=netloc))


def client_for(cluster_row) -> HyperCoreClient:
    return HyperCoreClient(
        host=cluster_row["host"],
        username=cluster_row["username"],
        password=models.decrypt_password(cluster_row["password_enc"]),
        verify_tls=bool(cluster_row["verify_tls"]),
    )


def run_export(schedule_id: int):
    """Entry point used by both the scheduler and the 'Run now' button."""
    sched = models.get_schedule(schedule_id)
    if sched is None:
        log.warning("Schedule %s no longer exists, skipping", schedule_id)
        return
    if not sched["enabled"]:
        log.info("Schedule %s (%s) is disabled, skipping", schedule_id, sched["vm_name"])
        return
    if models.is_run_active(schedule_id):
        log.warning("Schedule %s (%s) still has a run in progress, skipping",
                    schedule_id, sched["vm_name"])
        return

    run_id = models.start_run(schedule_id)
    folder = f"{safe_name(sched['vm_name'])}_{datetime.now().strftime(TIMESTAMP_FMT)}"
    smb_pass = models.decrypt_password(sched["smb_pass_enc"]) if sched["smb_pass_enc"] else None
    path_uri = build_path_uri(f"{sched['path_uri_base']}/{folder}",
                              sched["smb_user"], smb_pass)
    models.update_run(run_id, export_path=folder)
    log.info("[%s] Exporting VM '%s' to %s", run_id, sched["vm_name"], folder)

    cluster = models.get_cluster(sched["cluster_id"])
    try:
        hc = client_for(cluster)
        task_tag = hc.export_vm(sched["vm_uuid"], path_uri,
                                compress=bool(sched["compress"]))
        models.update_run(run_id, task_tag=task_tag)
        _wait_for_task(hc, task_tag, run_id)
    except HyperCoreError as e:
        models.update_run(run_id, status="ERROR", message=str(e),
                          finished_at=_now())
        log.error("[%s] Export failed: %s", run_id, e)
        return
    except Exception as e:  # never let a job crash the scheduler thread
        models.update_run(run_id, status="ERROR", message=f"Unexpected error: {e}",
                          finished_at=_now())
        log.exception("[%s] Unexpected error", run_id)
        return

    # Export succeeded -- prune old copies if we have filesystem access.
    prune_msg = prune_old_exports(sched)
    models.update_run(run_id, status="COMPLETE", finished_at=_now(),
                      message=f"Export complete. {prune_msg}")
    log.info("[%s] Done. %s", run_id, prune_msg)


def _wait_for_task(hc: HyperCoreClient, task_tag: str, run_id: int):
    """Poll TaskTag until COMPLETE, raise on ERROR or timeout."""
    deadline = time.monotonic() + EXPORT_TIMEOUT_S
    last_pct = -1
    while time.monotonic() < deadline:
        status = hc.task_status(task_tag)
        state = status.get("state", "UNINITIALIZED")
        pct = status.get("progressPercent", 0)
        if pct != last_pct:
            models.update_run(run_id, message=f"{state} {pct}%")
            last_pct = pct
        if state == "COMPLETE":
            return
        if state == "ERROR":
            detail = status.get("formattedMessage") or status.get(
                "formattedDescription") or "task reported ERROR"
            raise HyperCoreError(f"Export task failed: {detail}")
        time.sleep(POLL_INTERVAL_S)
    raise HyperCoreError(f"Export task {task_tag} did not finish within "
                         f"{EXPORT_TIMEOUT_S // 3600}h")


def prune_old_exports(sched) -> str:
    """Delete oldest export folders beyond the retention count.

    Only possible when prune_path is set: a path where the NAS share is
    mounted inside this container. HyperCore itself cannot delete from the
    NAS, so without a mount we can only warn.
    """
    if not sched["prune_path"]:
        return "Pruning skipped (no NAS mount path configured)."

    base = Path(sched["prune_path"])
    if not base.is_dir():
        return f"Pruning skipped: {base} is not accessible from this container."

    prefix = safe_name(sched["vm_name"]) + "_"
    pattern = re.compile(re.escape(prefix) + r"\d{8}-\d{6}$")
    copies = sorted(
        (d for d in base.iterdir() if d.is_dir() and pattern.match(d.name)),
        key=lambda d: d.name,  # timestamp format sorts lexicographically
    )
    excess = copies[:-sched["retention"]] if sched["retention"] > 0 else []
    for old in excess:
        try:
            shutil.rmtree(old)
            log.info("Pruned old export %s", old)
        except OSError as e:
            return f"Pruning error on {old.name}: {e}"
    kept = min(len(copies), sched["retention"])
    return f"Retention: kept {kept}, removed {len(excess)}."


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
