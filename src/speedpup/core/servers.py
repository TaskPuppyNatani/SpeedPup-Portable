# SPDX-License-Identifier: GPL-3.0-or-later
"""LibreSpeed server-list loading and validation."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from threading import Event

from .models import Server

SERVER_LIST_URL = "https://librespeed.org/backend-servers/servers.php"
USER_AGENT = "SpeedPup-Portable/0.1"
MAX_SERVER_LIST_BYTES = 2 * 1024 * 1024


class ServerListError(RuntimeError):
    """The LibreSpeed server list could not be loaded or parsed."""


def _http_url(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"server entry has no {field}")

    candidate = value.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"server entry has an invalid {field} URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"server entry has credentials in its {field} URL")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"server entry has an invalid {field} port") from exc
    return urllib.parse.urlunsplit(parsed)


def server_from_entry(entry: object) -> Server:
    """Validate one public LibreSpeed-compatible server-list entry."""

    if not isinstance(entry, dict):
        raise ValueError("server entry is not an object")

    base = _http_url(entry.get("server"), field="server")
    # LibreSpeed commonly lists a backend prefix such as /backend and supplies
    # paths relative to it, so preserve that prefix when resolving endpoints.
    base_parts = urllib.parse.urlsplit(base)
    base = urllib.parse.urlunsplit(
        base_parts._replace(path=base_parts.path.rstrip("/") + "/")
    )

    def endpoint(key: str) -> str:
        raw = entry.get(key)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"server entry has no {key}")
        raw = raw.strip()
        if raw.startswith("//"):
            raw = "https:" + raw
        result = urllib.parse.urljoin(base, raw)
        result = _http_url(result, field=key)
        parts = urllib.parse.urlsplit(result)
        def origin(url):
            p = urllib.parse.urlsplit(url)
            return (p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80))
        if origin(result) != origin(base):
            raise ValueError(f"server entry has cross-origin {key}")
        if not parts.path:
            raise ValueError(f"server entry has an empty {key} path")
        return result

    raw_id = entry.get("id")
    server_id = str(raw_id).strip() if raw_id is not None else ""
    if not server_id:
        raise ValueError("server entry has no id")
    if len(server_id) > 128:
        raise ValueError("server entry id is too long")

    raw_name = entry.get("name")
    name = raw_name.strip() if isinstance(raw_name, str) else ""
    if not name:
        name = base_parts.hostname or "LibreSpeed server"
    sponsor_value = entry.get("sponsorName", "")
    sponsor = sponsor_value.strip() if isinstance(sponsor_value, str) else ""

    return Server(
        id=server_id,
        name=name,
        sponsor=sponsor,
        base_url=base,
        ping_url=endpoint("pingURL"),
        download_url=endpoint("dlURL"),
        upload_url=endpoint("ulURL"),
    )


def server_to_entry(server: Server) -> dict[str, str]:
    """Serialize a validated server into the entry shape accepted by server_from_entry."""

    return {
        "id": server.id,
        "name": server.name,
        "sponsorName": server.sponsor,
        "server": server.base_url,
        "pingURL": server.ping_url,
        "dlURL": server.download_url,
        "ulURL": server.upload_url,
    }


def parse_server_list(payload: object) -> list[Server]:
    """Return valid entries and reject a wholly malformed server response."""

    if not isinstance(payload, list):
        raise ServerListError("LibreSpeed server list is not a JSON array")
    servers: list[Server] = []
    for entry in payload:
        try:
            servers.append(server_from_entry(entry))
        except (TypeError, ValueError):
            # One bad volunteer record should not make all other servers
            # undiscoverable. The caller receives an error if none survive.
            continue
    if not servers:
        raise ServerListError("LibreSpeed server list contains no usable servers")
    return servers


def fetch_servers(
    *,
    url: str = SERVER_LIST_URL,
    timeout: float = 5.0,
    cancel_event: Event | None = None,
) -> list[Server]:
    """Fetch the public server list without retaining it or sending telemetry."""

    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("server discovery cancelled")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.build_opener(_ListRedirectHandler()).open(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise ServerListError(
                    f"LibreSpeed server list returned HTTP {response.status}"
                )
            payload_bytes = response.read(MAX_SERVER_LIST_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ServerListError(
            f"LibreSpeed server list returned HTTP {exc.code}"
        ) from exc
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise ServerListError(f"could not load LibreSpeed servers: {exc}") from exc
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("server discovery cancelled")
    if len(payload_bytes) > MAX_SERVER_LIST_BYTES:
        raise ServerListError("LibreSpeed server list is too large")
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServerListError("LibreSpeed server list is invalid JSON") from exc
    return parse_server_list(payload)


class _ListRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never let the trusted discovery URL redirect to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if (new.scheme, new.hostname, new.port or 443) != ("https", old.hostname, old.port or 443):
            raise ServerListError("LibreSpeed server list redirected to an unexpected origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)
