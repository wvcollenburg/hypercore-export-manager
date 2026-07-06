"""HyperCore Export Manager -- register clusters, browse VMs, schedule daily
exports to a NAS with timestamped folders and retention pruning."""
from __future__ import annotations

import logging
import os
import re

from flask import (Flask, flash, jsonify, redirect, render_template, request,
                   url_for)

import models
import scheduler
from exporter import client_for, safe_name, smb_list_dirs
from hypercore import HyperCoreError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("hcem")

app = Flask(__name__)
app.secret_key = os.environ.get("HCEM_SECRET", "dev-only")

models.init_db()
scheduler.start()


# ------------------------------------------------------------------ dashboard
@app.route("/")
def index():
    return redirect(url_for("schedules"))


# ------------------------------------------------------------------- clusters
@app.route("/clusters")
def clusters():
    return render_template("clusters.html", clusters=models.get_clusters())


@app.route("/clusters/add", methods=["POST"])
def clusters_add():
    name = request.form["name"].strip()
    host = request.form["host"].strip().removeprefix("https://").rstrip("/")
    username = request.form["username"].strip()
    password = request.form["password"]
    verify_tls = "verify_tls" in request.form
    if not all([name, host, username, password]):
        flash("All fields are required.", "error")
        return redirect(url_for("clusters"))

    cluster_id = models.add_cluster(name, host, username, password, verify_tls)

    # Connection check -- non-fatal, the cluster may simply be offline now.
    try:
        client_for(models.get_cluster(cluster_id)).ping()
        flash(f"Cluster '{name}' registered and reachable.", "ok")
    except HyperCoreError as e:
        flash(f"Cluster '{name}' registered, but the connection check failed: {e}", "warn")
    return redirect(url_for("clusters"))


@app.route("/clusters/<int:cluster_id>/delete", methods=["POST"])
def clusters_delete(cluster_id):
    models.delete_cluster(cluster_id)
    scheduler.sync_jobs()
    flash("Cluster removed, including its schedules.", "ok")
    return redirect(url_for("clusters"))


# ------------------------------------------------------------------------ vms
@app.route("/clusters/<int:cluster_id>/vms")
def vms(cluster_id):
    cluster = models.get_cluster(cluster_id)
    if cluster is None:
        flash("Cluster not found.", "error")
        return redirect(url_for("clusters"))

    scheduled = {s["vm_uuid"]: s for s in models.get_schedules(cluster_id)}
    try:
        vm_list = client_for(cluster).list_vms()
        error = None
    except HyperCoreError as e:
        vm_list, error = [], str(e)
    return render_template("vms.html", cluster=cluster, vms=vm_list,
                           scheduled=scheduled, error=error)


# ------------------------------------------------------------------ schedules
@app.route("/schedules")
def schedules():
    rows = []
    for s in models.get_schedules():
        last = models.last_run_for(s["id"])
        nxt = scheduler.next_run_time(s["id"])
        rows.append({
            "s": s,
            "last": last,
            "next": nxt.strftime("%Y-%m-%d %H:%M") if nxt else None,
        })
    return render_template("schedules.html", rows=rows)


@app.route("/schedules/add", methods=["POST"])
def schedules_add():
    cluster_id = int(request.form["cluster_id"])
    run_time = request.form.get("run_time", "03:00")
    try:
        hh, mm = run_time.split(":")
        assert 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
    except (ValueError, AssertionError):
        flash("Run time must be HH:MM (24h).", "error")
        return redirect(url_for("vms", cluster_id=cluster_id))

    try:
        retention = max(1, int(request.form.get("retention", 7)))
    except ValueError:
        retention = 7

    try:
        models.add_schedule(
            cluster_id=cluster_id,
            vm_uuid=request.form["vm_uuid"],
            vm_name=request.form["vm_name"],
            path_uri_base=request.form["path_uri_base"].strip(),
            smb_user=request.form.get("smb_user", "").strip(),
            smb_password=request.form.get("smb_password", ""),
            prune_path=request.form.get("prune_path", "").strip(),
            retention=retention,
            run_time=f"{int(hh):02d}:{int(mm):02d}",
            compress="compress" in request.form,
        )
        scheduler.sync_jobs()
        flash(f"Daily export scheduled for '{request.form['vm_name']}'.", "ok")
    except Exception as e:
        flash(f"Could not create schedule: {e}", "error")
    return redirect(url_for("vms", cluster_id=cluster_id))


@app.route("/schedules/<int:schedule_id>/toggle", methods=["POST"])
def schedules_toggle(schedule_id):
    models.toggle_schedule(schedule_id)
    scheduler.sync_jobs()
    return redirect(url_for("schedules"))


@app.route("/schedules/<int:schedule_id>/delete", methods=["POST"])
def schedules_delete(schedule_id):
    models.delete_schedule(schedule_id)
    scheduler.sync_jobs()
    flash("Schedule removed. Existing exports on the NAS are untouched.", "ok")
    return redirect(url_for("schedules"))


@app.route("/schedules/<int:schedule_id>/run", methods=["POST"])
def schedules_run(schedule_id):
    scheduler.run_now(schedule_id)
    flash("Export started. Watch progress under runs.", "ok")
    return redirect(url_for("schedules"))


# ----------------------------------------------------------------------- runs
@app.route("/runs")
def runs():
    schedule_id = request.args.get("schedule_id", type=int)
    return render_template("runs.html",
                           runs=models.get_runs(schedule_id),
                           schedule=models.get_schedule(schedule_id) if schedule_id else None)


@app.route("/runs/status")
def runs_status():
    """Lightweight JSON snapshot the Runs page polls for live progress.

    Returns instantly and holds no connection open -- deliberately a poll,
    not a WebSocket/SSE stream, so it never ties up one of the worker's
    limited threads."""
    schedule_id = request.args.get("schedule_id", type=int)
    rows = models.get_runs(schedule_id)
    runs = [{"id": r["id"], "status": r["status"],
             "message": r["message"] or "", "task_tag": r["task_tag"] or ""}
            for r in rows]
    return jsonify(runs=runs, active=any(r["status"] == "RUNNING" for r in runs))


# --------------------------------------------------------------------- imports
def _derive_name(folder: str) -> str:
    """Strip a `_YYYYMMDD-HHMMSS` export suffix to guess the original VM name."""
    m = re.match(r"^(.*)_\d{8}-\d{6}$", folder or "")
    return m.group(1) if m else (folder or "")


@app.route("/import")
def imports():
    return render_template("import.html",
                           clusters=models.get_clusters(),
                           imports=models.get_imports())


@app.route("/import/browse", methods=["POST"])
def import_browse():
    """AJAX: list the export folders on an SMB share so the user can pick one."""
    base = request.form.get("source_uri", "").strip()
    if not base.lower().startswith("smb://"):
        return jsonify(error="Browsing is only available for smb:// shares. "
                             "For NFS, type the full source path manually."), 400
    try:
        folders = smb_list_dirs(base,
                                request.form.get("smb_user", "").strip(),
                                request.form.get("smb_password", ""))
    except HyperCoreError as e:
        return jsonify(error=str(e)), 502
    return jsonify(folders=folders)


@app.route("/import/start", methods=["POST"])
def import_start():
    cluster_id = int(request.form["cluster_id"])
    cluster = models.get_cluster(cluster_id)
    if cluster is None:
        flash("Target cluster not found.", "error")
        return redirect(url_for("imports"))

    base = request.form.get("source_uri", "").strip().rstrip("/")
    folder = request.form.get("source_folder", "").strip().strip("/")
    if not base:
        flash("A source path is required.", "error")
        return redirect(url_for("imports"))
    source_uri = f"{base}/{folder}" if folder else base

    smb_user = request.form.get("smb_user", "").strip()
    smb_password = request.form.get("smb_password", "")
    target_name = request.form.get("target_name", "").strip()

    # Duplicate-name guard: block if the effective name already exists on target.
    effective = target_name or _derive_name(folder)
    if effective:
        try:
            existing = {(v["name"] or "").lower() for v in client_for(cluster).list_vms()}
            if effective.lower() in existing:
                flash(f"Cluster '{cluster['name']}' already has a VM named "
                      f"'{effective}'. Enter a different target name.", "error")
                return redirect(url_for("imports"))
        except HyperCoreError as e:
            flash(f"Could not verify names on '{cluster['name']}' ({e}). "
                  "Import not started -- retry when the cluster is reachable.", "error")
            return redirect(url_for("imports"))

    import_id = models.add_import(cluster_id, source_uri, smb_user, smb_password, target_name)
    scheduler.run_import(import_id)
    flash("Import started. Watch progress below.", "ok")
    return redirect(url_for("imports"))


# ------------------------------------------------------------ template helper
@app.template_filter("gib")
def gib(mem_bytes):
    try:
        return f"{int(mem_bytes) / (1024 ** 3):.0f} GiB"
    except (TypeError, ValueError):
        return "-"


@app.context_processor
def inject_helpers():
    return {"safe_name": safe_name}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("HCEM_PORT", 8080)))
