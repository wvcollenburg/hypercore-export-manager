"""Minimal HyperCore REST API client.

Uses HTTP Basic auth (supported globally per the HyperCore OpenAPI spec),
which keeps the client stateless -- no session cookie bookkeeping.
"""
from __future__ import annotations

import requests
import urllib3


class HyperCoreError(Exception):
    pass


class HyperCoreClient:
    def __init__(self, host: str, username: str, password: str,
                 verify_tls: bool = True, timeout: int = 30):
        self.base = f"https://{host}/rest/v1"
        self.auth = (username, password)
        self.verify = verify_tls
        self.timeout = timeout
        if not verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # ------------------------------------------------------------------ core
    def _request(self, method: str, path: str, **kwargs):
        url = f"{self.base}{path}"
        try:
            resp = requests.request(
                method, url,
                auth=self.auth,
                verify=self.verify,
                timeout=self.timeout,
                **kwargs,
            )
        except requests.exceptions.SSLError as e:
            raise HyperCoreError(f"TLS error talking to {url}: {e}") from e
        except requests.exceptions.ConnectionError as e:
            raise HyperCoreError(f"Cannot reach {url}: {e}") from e
        except requests.exceptions.Timeout as e:
            raise HyperCoreError(f"Timeout talking to {url}") from e

        if resp.status_code == 401:
            raise HyperCoreError("Authentication failed (401) -- check cluster credentials")
        if resp.status_code == 403:
            raise HyperCoreError("Permission denied (403) -- user lacks rights for this action")
        if not resp.ok:
            detail = ""
            try:
                detail = resp.json().get("error", "")
            except Exception:
                detail = resp.text[:200]
            raise HyperCoreError(f"HyperCore API {resp.status_code} on {path}: {detail}")
        if resp.text:
            return resp.json()
        return None

    # ------------------------------------------------------------ operations
    def ping(self) -> bool:
        self._request("GET", "/ping")
        return True

    def cluster_info(self) -> dict:
        data = self._request("GET", "/Cluster")
        return data[0] if isinstance(data, list) and data else {}

    def list_vms(self) -> list[dict]:
        vms = self._request("GET", "/VirDomain") or []
        # Keep only the fields the UI needs; the full objects are large.
        slim = []
        for vm in vms:
            slim.append({
                "uuid": vm.get("uuid"),
                "name": vm.get("name"),
                "description": vm.get("description", ""),
                "state": vm.get("state", ""),
                "mem": vm.get("mem", 0),
                "numVCPU": vm.get("numVCPU", 0),
                "tags": vm.get("tags", ""),
            })
        return sorted(slim, key=lambda v: (v["name"] or "").lower())

    def export_vm(self, vm_uuid: str, path_uri: str, compress: bool = False) -> str:
        """Start a VM export. Returns the taskTag to poll.

        HyperCore creates the basename directory of path_uri on the target,
        which is how we get one timestamped folder per export.
        """
        body = {"target": {"pathURI": path_uri}}
        if compress:
            body["target"]["compress"] = True
        result = self._request("POST", f"/VirDomain/{vm_uuid}/export", json=body)
        task_tag = (result or {}).get("taskTag")
        if not task_tag:
            raise HyperCoreError(f"Export accepted but no taskTag returned: {result}")
        return task_tag

    def task_status(self, task_tag: str) -> dict:
        """Returns {'state': QUEUED|RUNNING|COMPLETE|ERROR, 'progressPercent': int, ...}."""
        data = self._request("GET", f"/TaskTag/{task_tag}") or []
        if isinstance(data, list) and data:
            return data[0]
        return {"state": "UNINITIALIZED", "progressPercent": 0}
