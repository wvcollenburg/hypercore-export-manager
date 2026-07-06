"""The import job: pull a previously exported VM from a share into a cluster.

The companion to exporter.py. Reuses exporter's HyperCore client factory,
credential-URI assembly, and task-polling so exports and imports behave
identically (same progress display, same "jobs never raise" guarantee).
"""
from __future__ import annotations

import logging

import exporter
import models
from hypercore import HyperCoreError

log = logging.getLogger("hcem.importer")


def run_import(import_id: int):
    """Entry point used by the scheduler thread. Never raises: any failure is
    written to the import row so a crashing job can't take down the thread."""
    imp = models.get_import(import_id)
    if imp is None:
        log.warning("Import %s no longer exists, skipping", import_id)
        return

    cluster = models.get_cluster(imp["cluster_id"])
    if cluster is None:
        models.update_import(import_id, status="ERROR", finished_at=exporter._now(),
                             message="Target cluster no longer exists.")
        return

    log.info("[import %s] Importing from %s into cluster '%s'",
             import_id, imp["source_uri"], imp["cluster_name"])
    try:
        hc = exporter.client_for(cluster)
        password = (models.decrypt_password(imp["smb_pass_enc"])
                    if imp["smb_pass_enc"] else None)
        source_uri = exporter.build_path_uri(imp["source_uri"], imp["smb_user"], password)
        task_tag, created = hc.import_vm(source_uri, name=imp["target_name"] or None)
        models.update_import(import_id, task_tag=task_tag, created_uuid=created or None)
        exporter.wait_for_task(
            hc, task_tag,
            lambda state, pct: models.update_import(import_id, message=f"{state} {pct}%"))
    except HyperCoreError as e:
        models.update_import(import_id, status="ERROR", message=str(e),
                             finished_at=exporter._now())
        log.error("[import %s] Failed: %s", import_id, e)
        return
    except Exception as e:  # never let a job crash the scheduler thread
        models.update_import(import_id, status="ERROR",
                             message=f"Unexpected error: {e}", finished_at=exporter._now())
        log.exception("[import %s] Unexpected error", import_id)
        return

    models.update_import(import_id, status="COMPLETE", finished_at=exporter._now(),
                         message="Import complete.")
    log.info("[import %s] Done. New VM UUID: %s", import_id, imp["target_name"] or "(kept)")
