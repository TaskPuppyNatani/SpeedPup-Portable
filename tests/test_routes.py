# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic tests for passive operating-system default-route lookup."""

from __future__ import annotations

import base64
import json
import subprocess
from types import SimpleNamespace

import pytest

from speedpup.core import routes


IPV4_HEADER = "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT"


def ipv4_route(name: str, metric: int, *, flags: str = "0003", destination: str = "00000000", mask: str = "00000000") -> str:
    return f"{name} {destination} 0100000A {flags} 0 0 {metric} {mask} 0 0 0"


def linux_route_files(monkeypatch, ipv4: str = IPV4_HEADER, ipv6: str = "") -> None:
    contents = {
        "/proc/net/route": ipv4,
        "/proc/net/ipv6_route": ipv6,
    }
    monkeypatch.setattr(routes.platform, "system", lambda: "Linux")
    monkeypatch.setattr(routes, "_read_route_file", lambda path: contents[path])


def windows_route(family: str, index: int, route_metric: int, interface_metric: int, alias: str):
    return {
        "family": family,
        "interface_index": index,
        "route_metric": route_metric,
        "interface_metric": interface_metric,
        "alias_utf16le_b64": base64.b64encode(alias.encode("utf-16-le")).decode("ascii"),
    }


def test_linux_selects_lowest_metric_default_among_wifi_and_virtual_bridge(monkeypatch):
    table = "\n".join(
        (
            IPV4_HEADER,
            ipv4_route("br-8ed0425b5a54", 700),
            ipv4_route("wlo1", 100),
            ipv4_route("down0", 1, flags="0002"),
            ipv4_route("reject0", 0, flags="0203"),
            ipv4_route("lan0", 0, destination="0001A8C0", mask="00FFFFFF"),
        )
    )
    linux_route_files(monkeypatch, ipv4=table)

    assert routes.default_route_interface() == "wlo1"


def test_linux_does_not_exclude_a_virtual_interface_that_owns_the_best_route(monkeypatch):
    table = "\n".join(
        (
            IPV4_HEADER,
            ipv4_route("wlo1", 100),
            ipv4_route("br-default", 10),
        )
    )
    linux_route_files(monkeypatch, ipv4=table)

    assert routes.default_route_interface() == "br-default"


def test_linux_uses_ipv6_default_only_when_ipv4_has_none(monkeypatch):
    ipv6 = "\n".join(
        (
            # This is the local ::/0 entry and does not have RTF_UP set.
            f"{'0' * 32} 00 {'0' * 32} 00 {'0' * 32} 00000001 00000001 00000000 00200200 lo",
            # Reject routes are not usable even when the kernel marks them up.
            f"{'0' * 32} 00 {'0' * 32} 00 {'0' * 32} 00000000 00000001 00000000 00000201 reject6",
            # An active global default uses RTF_UP (bit 0).
            f"{'0' * 32} 00 {'0' * 32} 00 {'0' * 32} 00000064 00000001 00000000 00000001 wlp2s0",
            # Ignore source-specific and non-default routes.
            f"{'0' * 32} 00 {'1' * 31}1 80 {'0' * 32} 00000001 00000001 00000000 00000001 vpn0",
        )
    )
    linux_route_files(monkeypatch, ipv4=IPV4_HEADER, ipv6=ipv6)

    assert routes.default_route_interface() == "wlp2s0"


def test_linux_prefers_ipv4_when_both_families_have_defaults(monkeypatch):
    ipv4 = "\n".join((IPV4_HEADER, ipv4_route("eth4", 999)))
    ipv6 = f"{'0' * 32} 00 {'0' * 32} 00 {'0' * 32} 00000000 00000001 00000000 00000001 eth6"
    linux_route_files(monkeypatch, ipv4=ipv4, ipv6=ipv6)

    assert routes.default_route_interface() == "eth4"


def test_linux_equal_cost_routes_have_a_stable_name_tie_break(monkeypatch):
    table = "\n".join(
        (
            IPV4_HEADER,
            ipv4_route("z-vpn", 20),
            ipv4_route("a-wifi", 20),
        )
    )
    linux_route_files(monkeypatch, ipv4=table)

    assert routes.default_route_interface() == "a-wifi"


def test_linux_unreadable_route_tables_return_none(monkeypatch):
    monkeypatch.setattr(routes.platform, "system", lambda: "Linux")
    monkeypatch.setattr(routes, "_read_route_file", lambda _path: "")

    assert routes.default_route_interface() is None


def test_macos_parser_preserves_unicode_interface_names():
    assert routes._parse_macos_route_output("gateway: 192.0.2.1\ninterface: VPN 東京\n") == "VPN 東京"
    assert routes._parse_macos_route_output("gateway: 192.0.2.1\n") is None


def test_macos_tries_ipv6_when_ipv4_has_no_route(monkeypatch):
    calls = []
    outputs = iter(
        (
            SimpleNamespace(returncode=0, stdout="gateway: 192.0.2.1\n"),
            SimpleNamespace(returncode=0, stdout="interface: utun7\n"),
        )
    )
    monkeypatch.setattr(routes.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(routes, "_run_local_command", lambda args: calls.append(args) or next(outputs))

    assert routes.default_route_interface() == "utun7"
    assert calls == [
        ["route", "-n", "get", "-inet", "default"],
        ["route", "-n", "get", "-inet6", "default"],
    ]


def test_windows_parser_ranks_combined_route_and_interface_metrics_and_preserves_unicode():
    output = json.dumps(
        [
            windows_route("IPv4", 14, 5, 80, "Bridge"),
            windows_route("IPv4", 6, 50, 10, " Wi-Fi 東京 "),
            # IPv6 metrics are a separate family and do not compete with IPv4.
            windows_route("IPv6", 2, 0, 0, "vEthernet"),
        ]
    )

    candidates = routes._parse_windows_route_candidates(output)
    assert routes._choose_windows_route(candidates) == " Wi-Fi 東京 "


def test_windows_equal_metrics_break_by_index_then_alias():
    candidates = routes._parse_windows_route_candidates(
        json.dumps(
            [
                windows_route("IPv4", 8, 1, 2, "z"),
                windows_route("IPv4", 3, 2, 1, "b"),
            ]
        )
    )

    assert routes._choose_windows_route(candidates) == "b"


@pytest.mark.parametrize("output", ["", "{}", "not json", '[{"family":"IPv4","alias":""}]'])
def test_windows_parser_fails_closed_on_unusable_route_data(output):
    assert routes._choose_windows_route(routes._parse_windows_route_candidates(output)) is None


def test_windows_command_uses_local_active_route_objects_and_bounded_utf8_json(monkeypatch):
    output = json.dumps(
        [
            windows_route("IPv4", 22, 10, 5, "Ethernet")
        ]
    )
    calls = []

    def fake_run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr="")

    monkeypatch.setattr(routes.platform, "system", lambda: "Windows")
    monkeypatch.setattr(routes.subprocess, "run", fake_run)

    assert routes.default_route_interface() == "Ethernet"
    arguments, kwargs = calls[0]
    script = arguments[-1]
    assert arguments[0] == "powershell.exe"
    assert "Get-NetRoute" in script
    assert "Get-NetIPInterface" in script
    assert "-PolicyStore ActiveStore" in script
    assert "alias_utf16le_b64" in script
    assert kwargs["timeout"] == routes._COMMAND_TIMEOUT_SECONDS
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["capture_output"] is True


def test_windows_command_suppresses_console_window_when_supported(monkeypatch):
    calls = []

    def fake_run(arguments, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(arguments, 0, stdout="[]", stderr="")

    monkeypatch.setattr(routes.os, "name", "nt")
    monkeypatch.setattr(routes.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(routes.subprocess, "run", fake_run)

    routes._run_local_command(["powershell.exe", "-NoProfile"])

    assert calls[0]["creationflags"] == 0x08000000


def test_platforms_without_a_supported_route_reader_return_none(monkeypatch):
    monkeypatch.setattr(routes.platform, "system", lambda: "Plan9")

    assert routes.default_route_interface() is None
