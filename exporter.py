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


def wait_for_task(hc: HyperCoreClient, task_tag: str, on_progress) -> None:
    """Poll TaskTag until COMPLETE, raise on ERROR or timeout.

    `on_progress(state, pct)` is called whenever the percentage changes, so
    both exports and imports can surface progress into their own run rows.
    """
    deadline = time.monotonic() + EXPORT_TIMEOUT_S
    last_pct = -1
    while time.monotonic() < deadline:
        status = hc.task_status(task_tag)
        state = status.get("state", "UNINITIALIZED")
        pct = status.get("progressPercent", 0)
        if pct != last_pct:
            on_progress(state, pct)
            last_pct = pct
        if state == "COMPLETE":
            return
        if state == "ERROR":
            detail = status.get("formattedMessage") or status.get(
                "formattedDescription") or "task reported ERROR"
            raise HyperCoreError(f"Task failed: {detail}")
        time.sleep(POLL_INTERVAL_S)
    raise HyperCoreError(f"Task {task_tag} did not finish within "
                         f"{EXPORT_TIMEOUT_S // 3600}h")


def _wait_for_task(hc: HyperCoreClient, task_tag: str, run_id: int):
    wait_for_task(hc, task_tag,
                  lambda state, pct: models.update_run(run_id, message=f"{state} {pct}%"))


def resume_export(run_id: int):
    """Re-attach to an export whose monitoring thread died on a restart.

    The HyperCore task keeps running on the cluster independently of this app,
    so we poll its taskTag to completion and finish the tail (prune + mark
    complete). Never raises."""
    run = models.get_run(run_id)
    if run is None or run["status"] != "RUNNING":
        return
    sched = models.get_schedule(run["schedule_id"])
    if sched is None:
        models.update_run(run_id, status="ERROR", finished_at=_now(),
                          message="Interrupted by a restart; schedule no longer exists.")
        return
    cluster = models.get_cluster(sched["cluster_id"])
    try:
        hc = client_for(cluster)
        state = hc.task_status(run["task_tag"]).get("state", "UNINITIALIZED")
        if state == "ERROR":
            models.update_run(run_id, status="ERROR", finished_at=_now(),
                              message="Export task reported ERROR (detected after restart).")
            return
        if state == "UNINITIALIZED":
            # The task is gone: it finished and aged out before we re-attached.
            # An export can't be verified from the taskTag alone, so finalize as
            # complete-with-caveat and still attempt the retention prune.
            prune_msg = prune_old_exports(sched)
            models.update_run(run_id, status="COMPLETE", finished_at=_now(),
                              message="Resumed after restart; the cluster no longer tracks "
                                      "the task -- assumed complete, verify the export on the "
                                      f"NAS. {prune_msg}")
            return
        if state in ("QUEUED", "RUNNING"):
            wait_for_task(hc, run["task_tag"],
                          lambda s, p: models.update_run(run_id, message=f"{s} {p}%"))
        # COMPLETE (or wait finished) -> fall through to prune + mark complete
    except HyperCoreError as e:
        models.update_run(run_id, status="ERROR", message=str(e), finished_at=_now())
        return
    except Exception as e:  # never let recovery crash the scheduler thread
        models.update_run(run_id, status="ERROR", message=f"Unexpected error: {e}",
                          finished_at=_now())
        log.exception("[%s] Resume error", run_id)
        return

    prune_msg = prune_old_exports(sched)
    models.update_run(run_id, status="COMPLETE", finished_at=_now(),
                      message=f"Export complete (resumed after restart). {prune_msg}")


def _smb_username(user: str | None) -> str:
    """Normalize a share username for the smbprotocol client.

    smbprotocol only understands DOMAIN\\user (backslash) or user@domain, but
    HyperCore accepts DOMAIN/user (forward slash) and users often save it that
    way -- so exports work while our direct SMB browse/prune fails auth with
    STATUS_LOGON_FAILURE. Convert a lone forward slash to a backslash.
    """
    user = (user or "").strip()
    if "\\" not in user and "@" not in user and "/" in user:
        user = user.replace("/", "\\", 1)
    return user


def smb_list_dirs(base_uri: str, user: str, password: str | None) -> list[str]:
    """List subdirectory names of an SMB path, newest-name first.

    Used by the import browser to show which exported VM folders exist on a
    share. Raises HyperCoreError with a readable message on any SMB failure.
    """
    import smbclient

    parts = urlsplit(base_uri)
    server = parts.hostname
    port = parts.port or 445
    segments = [s for s in parts.path.split("/") if s]
    if not server or not segments:
        raise HyperCoreError(f"'{base_uri}' is not a valid SMB share path.")
    unc = "\\\\" + server + "\\" + "\\".join(segments)
    conn = {"username": _smb_username(user), "password": password, "port": port}
    try:
        names = [e.name for e in smbclient.scandir(unc, **conn) if e.is_dir()]
    except Exception as e:  # noqa: BLE001 -- turn any SMB error into a UI message
        raise HyperCoreError(f"Could not list {unc}: {e}") from e
    return sorted(names, reverse=True)


def _select_excess(dir_names, vm_name: str, retention: int):
    """Given the folder names in the export directory, pick which to delete.

    Only names matching this VM's `{safe_name}_YYYYMMDD-HHMMSS` shape are
    candidates -- sibling VMs' folders are never touched. The timestamp
    format sorts lexicographically == chronologically, so keeping the last
    `retention` entries keeps the newest.
    """
    prefix = safe_name(vm_name) + "_"
    pattern = re.compile(re.escape(prefix) + r"\d{8}-\d{6}$")
    copies = sorted(n for n in dir_names if pattern.match(n))
    excess = copies[:-retention] if retention > 0 else []
    kept = min(len(copies), retention)
    return excess, kept


def prune_old_exports(sched) -> str:
    """Delete oldest export folders beyond the retention count.

    HyperCore writes exports to the NAS but has no API to delete from it, so
    the app must reach the files itself. Two ways to do that:

      * SMB destinations -- connect straight to the share with the stored
        credentials and prune over the protocol. No mount required.
      * Anything else (NFS), or when a mount is explicitly configured --
        prune through `prune_path`, a local bind-mount of the same share.

    An explicit `prune_path` always wins, so existing mount-based setups are
    unchanged.
    """
    if sched["prune_path"]:
        return _prune_via_mount(sched)

    scheme = urlsplit(sched["path_uri_base"]).scheme.lower()
    if scheme == "smb":
        if not sched["smb_user"]:
            return ("Pruning skipped: SMB share has no stored username to "
                    "authenticate with; add one, or set a NAS mount path.")
        return _prune_via_smb(sched)

    return "Pruning skipped (no NAS mount path configured)."


def _prune_via_mount(sched) -> str:
    base = Path(sched["prune_path"])
    if not base.is_dir():
        return f"Pruning skipped: {base} is not accessible from this container."

    names = [d.name for d in base.iterdir() if d.is_dir()]
    excess, kept = _select_excess(names, sched["vm_name"], sched["retention"])
    for name in excess:
        try:
            shutil.rmtree(base / name)
            log.info("Pruned old export %s", base / name)
        except OSError as e:
            return f"Pruning error on {name}: {e}"
    return f"Retention: kept {kept}, removed {len(excess)}."


def _prune_via_smb(sched) -> str:
    import smbclient
    from smbclient import shutil as smb_shutil

    parts = urlsplit(sched["path_uri_base"])
    server = parts.hostname
    port = parts.port or 445
    segments = [s for s in parts.path.split("/") if s]
    if not server or not segments:
        return f"Pruning skipped: SMB URI '{sched['path_uri_base']}' has no share component."
    # \\server\share\sub\dir  (share is the first path segment)
    unc_base = "\\\\" + server + "\\" + "\\".join(segments)

    password = (models.decrypt_password(sched["smb_pass_enc"])
                if sched["smb_pass_enc"] else None)
    conn = {"username": _smb_username(sched["smb_user"]), "password": password, "port": port}

    try:
        names = [e.name for e in smbclient.scandir(unc_base, **conn) if e.is_dir()]
    except Exception as e:  # noqa: BLE001 -- surface any SMB/connection failure in the run log
        return f"Pruning skipped: could not list {unc_base} over SMB: {e}"

    excess, kept = _select_excess(names, sched["vm_name"], sched["retention"])
    for name in excess:
        try:
            smb_shutil.rmtree(unc_base + "\\" + name, **conn)
            log.info("Pruned old export (SMB) %s\\%s", unc_base, name)
        except Exception as e:  # noqa: BLE001
            return f"Pruning error over SMB on {name}: {e}"
    return f"Retention (SMB): kept {kept}, removed {len(excess)}."


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
