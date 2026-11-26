# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the operating system's active default route without network probes.

The selected route is a useful automatic-interface hint for passive traffic
monitoring.  This module only reads local route state: Linux procfs, the macOS
``route`` query, or Windows NetTCPIP route objects.  It never opens a socket,
resolves a host name, or sends a packet.  Unsupported or unreadable route data
returns ``None`` so callers can use their documented deterministic fallback.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
import platform
import subprocess
from typing import Any


_COMMAND_TIMEOUT_SECONDS = 3.0


def _valid_interface_name(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return None
    return value


def _read_route_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _parse_linux_ipv4_routes(contents: str) -> list[tuple[int, str]]:
    """Return active IPv4 default routes as ``(metric, interface)`` pairs."""
    routes: list[tuple[int, str]] = []
    for line in contents.splitlines():
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            destination = int(fields[1], 16)
            flags = int(fields[3], 16)
            metric = int(fields[6], 10)
            mask = int(fields[7], 16)
        except ValueError:
            continue
        # RTF_UP is 0x1.  A zero destination and mask identify 0.0.0.0/0.
        if destination != 0 or mask != 0 or not flags & 0x1 or flags & 0x200:
            continue
        interface = _valid_interface_name(fields[0])
        if interface is not None and metric >= 0:
            routes.append((metric, interface))
    return routes


def _parse_linux_ipv6_routes(contents: str) -> list[tuple[int, str]]:
    """Return active global IPv6 default routes as ``(metric, interface)``."""
    routes: list[tuple[int, str]] = []
    zero_address = "0" * 32
    for line in contents.splitlines():
        fields = line.split()
        if len(fields) < 10:
            continue
        destination, destination_prefix = fields[0], fields[1]
        source, source_prefix = fields[2], fields[3]
        try:
            metric = int(fields[5], 16)
            flags = int(fields[8], 16)
        except ValueError:
            continue
        # Source-specific defaults are not a generally applicable default
        # route.  The kernel's local ::/0 route is also ignored unless RTF_UP.
        if (
            destination != zero_address
            or destination_prefix != "00"
            or source != zero_address
            or source_prefix != "00"
            or not flags & 0x1
            or flags & 0x200
        ):
            continue
        interface = _valid_interface_name(fields[9])
        if interface is not None and metric >= 0:
            routes.append((metric, interface))
    return routes


def _choose_metric_route(routes: list[tuple[int, str]]) -> str | None:
    if not routes:
        return None
    # Equal-cost routes do not identify one flow-independent device.  Pick a
    # stable name so automatic monitoring remains deterministic.
    return min(routes, key=lambda route: (route[0], route[1].casefold(), route[1]))[1]


def _linux_default_route_interface() -> str | None:
    ipv4 = _parse_linux_ipv4_routes(_read_route_file("/proc/net/route"))
    if ipv4:
        # Metrics are only comparable within an address family.  Prefer IPv4
        # when both families have defaults, then use IPv6 as a fallback.
        return _choose_metric_route(ipv4)
    ipv6 = _parse_linux_ipv6_routes(_read_route_file("/proc/net/ipv6_route"))
    return _choose_metric_route(ipv6)


def _parse_macos_route_output(output: str) -> str | None:
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().casefold() == "interface":
            return _valid_interface_name(value.strip())
    return None


def _run_local_command(arguments: list[str]) -> subprocess.CompletedProcess[str] | None:
    environment = os.environ.copy()
    # The macOS route utility's field labels are parsed below.  Its C locale
    # keeps those labels stable; the interface value itself remains Unicode.
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"
    options: dict[str, object] = {
        "capture_output": True,
        "check": False,
        "encoding": "utf-8",
        "errors": "replace",
        "env": environment,
        "timeout": _COMMAND_TIMEOUT_SECONDS,
    }
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.run(
            arguments,
            **options,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _macos_default_route_interface() -> str | None:
    # route(8) performs a kernel routing-table lookup; -n avoids name service.
    for family in ("-inet", "-inet6"):
        result = _run_local_command(["route", "-n", "get", family, "default"])
        if result is None or result.returncode != 0:
            continue
        interface = _parse_macos_route_output(result.stdout)
        if interface is not None:
            return interface
    return None


_WINDOWS_ROUTE_QUERY = r"""
$ErrorActionPreference = 'Stop'
try {
    $routes = @()
    foreach ($familySpec in @(
        @{ Family = 'IPv4'; Prefix = '0.0.0.0/0' },
        @{ Family = 'IPv6'; Prefix = '::/0' }
    )) {
        $metrics = @{}
        Get-NetIPInterface -AddressFamily $familySpec.Family -PolicyStore ActiveStore -ErrorAction SilentlyContinue | ForEach-Object {
            $metrics[[string]$_.InterfaceIndex] = [uint64]$_.InterfaceMetric
        }
        Get-NetRoute -AddressFamily $familySpec.Family -DestinationPrefix $familySpec.Prefix -PolicyStore ActiveStore -ErrorAction SilentlyContinue | ForEach-Object {
            $key = [string]$_.InterfaceIndex
            $alias = [string]$_.InterfaceAlias
            if ($metrics.ContainsKey($key) -and -not [string]::IsNullOrWhiteSpace($alias)) {
                $routes += [PSCustomObject]@{
                    family = $familySpec.Family
                    interface_index = [uint32]$_.InterfaceIndex
                    route_metric = [uint64]$_.RouteMetric
                    interface_metric = [uint64]$metrics[$key]
                    alias_utf16le_b64 = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($alias))
                }
            }
        }
    }
    ConvertTo-Json -InputObject $routes -Compress -Depth 3
    exit 0
} catch {
    exit 1
}
"""


def _parse_windows_route_candidates(output: str) -> list[tuple[int, int, int, str]]:
    """Parse JSON candidates as ``(family, metric, index, alias)`` tuples."""
    try:
        decoded: Any = json.loads(output.lstrip("\ufeff").strip())
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(decoded, list):
        return []

    candidates: list[tuple[int, int, int, str]] = []
    for item in decoded:
        if not isinstance(item, dict):
            continue
        family_name = item.get("family")
        family = 0 if family_name == "IPv4" else 1 if family_name == "IPv6" else None
        encoded_alias = item.get("alias_utf16le_b64")
        if not isinstance(encoded_alias, str):
            continue
        try:
            interface = _valid_interface_name(
                base64.b64decode(encoded_alias, validate=True).decode("utf-16-le")
            )
        except (binascii.Error, UnicodeError, ValueError):
            continue
        metric = item.get("route_metric")
        interface_metric = item.get("interface_metric")
        index = item.get("interface_index")
        if (
            family is None
            or interface is None
            or isinstance(metric, bool)
            or not isinstance(metric, int)
            or metric < 0
            or isinstance(interface_metric, bool)
            or not isinstance(interface_metric, int)
            or interface_metric < 0
            or isinstance(index, bool)
            or not isinstance(index, int)
            or index <= 0
        ):
            continue
        candidates.append((family, metric + interface_metric, index, interface))
    return candidates


def _choose_windows_route(candidates: list[tuple[int, int, int, str]]) -> str | None:
    if not candidates:
        return None
    # Windows applies route and interface metrics within each address family.
    # Prefer IPv4 if both families have defaults because their metric scales
    # are not comparable; break equal costs deterministically.
    family = min(candidate[0] for candidate in candidates)
    matching_family = (candidate for candidate in candidates if candidate[0] == family)
    return min(
        matching_family,
        key=lambda candidate: (
            candidate[1], candidate[2], candidate[3].casefold(), candidate[3]
        ),
    )[3]


def _windows_default_route_interface() -> str | None:
    result = _run_local_command(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            _WINDOWS_ROUTE_QUERY,
        ]
    )
    if result is None or result.returncode != 0:
        return None
    return _choose_windows_route(_parse_windows_route_candidates(result.stdout))


def default_route_interface() -> str | None:
    """Return the active default-route interface name, or ``None``.

    Linux route metrics select within IPv4 first and IPv6 is used if no IPv4
    default exists.  macOS and Windows use their local routing facilities and
    apply the same deterministic IPv4-first family preference.  If the route
    table cannot be read, callers should use a stable eligible-interface
    fallback rather than infer an interface from adapter names.
    """
    system = platform.system()
    if system == "Linux":
        return _linux_default_route_interface()
    if system == "Darwin":
        return _macos_default_route_interface()
    if system == "Windows":
        return _windows_default_route_interface()
    return None
