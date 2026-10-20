"""Client for the re-ddns node API (``/api/collections``).

Apps only need it for two things: listing items that live on other hosts
and asking the node to fetch one of them into the local tree.  When no node
is configured (a laptop without re-ddns) everything degrades to local-only.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from reflex_ddns_auth.collections.errors import ItemNotFound, NodeUnavailable

Progress = Callable[[int, int], None]


class NodeClient:
    def __init__(self, base_url: str, http: Any = None):
        self.base_url = (base_url or "").rstrip("/")
        self._http = http

    @property
    def available(self) -> bool:
        if not self.base_url:
            return False
        try:
            import httpx  # noqa: F401
        except ImportError:
            return False
        return True

    def _client(self):
        import httpx

        if self._http is not None:
            return self._http
        self._http = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(10.0, read=120.0))
        return self._http

    def _url(self, path: str) -> str:
        return path if self._http is not None and not self.base_url else f"{self.base_url}{path}"

    def remote_items(self, collection: str, kind: str | None = None) -> list[dict]:
        if not self.available:
            return []
        params = {"scope": "remote"}
        if kind:
            params["kind"] = kind
        try:
            r = self._client().get(self._url(f"/api/collections/{collection}/items"), params=params)
            if r.status_code != 200:
                return []
            items = r.json().get("items", [])
        except Exception:  # noqa: BLE001 - a node outage must not break the app
            return []
        return [i for i in items if isinstance(i, dict)]

    def quota(self, collection: str, owner: str) -> dict | None:
        """``{"limit", "used"}`` in bytes for ``owner`` here (limit 0 = none); ``None`` when unknown.

        An unreachable node, or one that predates quotas, means no quota.
        """
        if not self.available:
            return None
        try:
            r = self._client().get(self._url(f"/api/collections/{collection}/quota/{owner}"), timeout=5.0)
            data = r.json() if r.status_code == 200 else None
        except Exception:  # noqa: BLE001 - a node outage must not block publishing
            return None
        return data if isinstance(data, dict) else None

    def status(self, ref: str) -> dict:
        collection = ref.split(":", 1)[0]
        try:
            r = self._client().get(self._url(f"/api/collections/{collection}/fetch-status"), params={"ref": ref})
            return r.json() if r.status_code == 200 else {}
        except Exception:  # noqa: BLE001
            return {}

    def fetch(self, ref: str, timeout: float = 900.0, progress: Progress | None = None) -> dict:
        """Ask the node to make ``ref`` local.  Blocks until the copy is complete."""
        if not self.available:
            raise NodeUnavailable("no re-ddns node configured (DDNS_COLLECTIONS_NODE_URL)")
        collection = ref.split(":", 1)[0]

        def _post() -> dict:
            import httpx

            try:
                r = self._client().post(
                    self._url(f"/api/collections/{collection}/fetch"), json={"ref": ref}, timeout=timeout
                )
            except httpx.HTTPError as exc:
                raise NodeUnavailable(f"node request failed: {exc}") from exc
            if r.status_code == 404:
                raise ItemNotFound(f"{ref}: {_detail(r)}")
            if r.status_code != 200:
                raise NodeUnavailable(f"node answered HTTP {r.status_code}: {_detail(r)}")
            return r.json()

        if progress is None:
            return _post()

        result: dict[str, Any] = {}
        error: list[BaseException] = []

        def _run() -> None:
            try:
                result.update(_post())
            except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
                error.append(exc)

        worker = threading.Thread(target=_run, name="collections-fetch", daemon=True)
        worker.start()
        while worker.is_alive():
            st = self.status(ref)
            if st:
                progress(int(st.get("bytes_done") or 0), int(st.get("bytes_total") or 0))
            worker.join(0.5)
        if error:
            raise error[0]
        total = int(result.get("bytes") or 0)
        progress(total, total)
        return result


def _detail(r: Any) -> str:
    try:
        return str(r.json().get("detail", ""))[:200]
    except Exception:  # noqa: BLE001
        return str(getattr(r, "text", ""))[:200]
