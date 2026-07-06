# HyperCore Export Manager

A small self-hosted web app that schedules daily VM exports from Scale
Computing HyperCore clusters to a NAS, with timestamped folders and
automatic retention pruning.

Replaces the classic "PowerShell script + Task Scheduler" pattern with
something you can hand to a customer: register a cluster, browse its VMs,
attach an export schedule — done.

## What it does

- **Register clusters** — host + API credentials, stored AES-encrypted
  (Fernet, keyed by `HCEM_SECRET`).
- **Browse VMs** — live list from the HyperCore REST API
  (`GET /rest/v1/VirDomain`).
- **Schedule exports** — per VM: NAS destination URI, daily run time,
  retention count, optional qcow2 compression.
- **Timestamped folders** — every run exports to
  `<destination>/<vm-name>_YYYYMMDD-HHMMSS/`. HyperCore creates the folder
  itself (the export API creates the basename of the `pathURI`).
- **Progress tracking** — the export task is polled via
  `GET /rest/v1/TaskTag/{tag}` until `COMPLETE` or `ERROR`; every run is
  logged with status and detail in the Runs view.
- **Retention pruning** — after a successful export, folders beyond the
  retention count are deleted (oldest first). Sibling folders of other VMs
  are never touched.

## Why TLS 1.3 is a non-issue here

The app runs in a Linux container; Python `requests` uses the system
OpenSSL, which negotiates TLS 1.3 natively. No Schannel, no registry keys,
no Windows Server version constraints.

## Quick start

```bash
# 1. Generate a secret (protects stored cluster passwords)
openssl rand -hex 32

# 2. Put it in docker-compose.yml (HCEM_SECRET), set your TZ

# 3. Build and run
docker compose up -d --build

# 4. Open http://<host>:8080
```

Works identically with `podman-compose`.

## The NAS mount (for retention pruning)

HyperCore writes exports to the NAS itself via the SMB/NFS URI you enter as
destination — the app never proxies export data. But **deleting** old
copies requires filesystem access, so mount the same share into the
container:

```bash
# on the container host, e.g. via fstab:
//nas.local/backups /mnt/nas/hypercore cifs credentials=/root/.smbcred,uid=0 0 0
```

```yaml
# docker-compose.yml
volumes:
  - /mnt/nas/hypercore:/mnt/nas/hypercore
```

Then set that path as "Retention prune path" on the schedule. If you leave
it empty, exports still work — you just prune manually.

## Destination URI format

Same format HyperCore uses everywhere:

```
smb://user:password@nas.local/share/subfolder
nfs://nas.local/export/subfolder
```

If the SMB password contains special characters, percent-encode them
(`@` → `%40`, etc.).

## Operational notes

- **One gunicorn worker by design.** The scheduler lives inside the app
  process; multiple workers would fire duplicate exports.
- **Overlap protection.** A schedule whose previous run is still going is
  skipped, not queued up.
- **Export timeout** is 8 hours (`EXPORT_TIMEOUT_S` in `exporter.py`);
  raise it for very large VMs on slow links.
- **Stagger your schedules.** Concurrent exports compete for cluster and
  NAS bandwidth; 03:00 / 03:30 / 04:00 beats three at 03:00.
- **HyperCore user rights:** a dedicated user with VM read + export rights
  is enough. Don't use admin.
- **No app-level login.** Run this on a management network, or put it
  behind a reverse proxy with authentication (nginx + Authelia works well).
- **Backups of the app itself:** everything lives in the `/data` volume
  (SQLite). Copy `hcem.db` and you have the full config — cluster passwords
  in it are only readable with the same `HCEM_SECRET`.

## Restoring an export

Exports are standard HyperCore exports: a qcow2 image per disk plus an XML
domain definition, importable through the HyperCore UI (Import) or
`POST /rest/v1/VirDomain/import` pointing at the timestamped folder.

## Project layout

```
app.py         Flask routes
hypercore.py   REST client (Basic auth, per the HyperCore OpenAPI spec)
exporter.py    export job: trigger, poll task, prune retention
scheduler.py   APScheduler wiring (daily cron per schedule)
models.py      SQLite persistence + password encryption
templates/     UI
```
