# SPDX-License-Identifier: GPL-3.0-or-later
"""Passive, Qt-independent monitoring of host network-interface counters.

The monitor never opens sockets or sends probes. It samples the cumulative
system counters exposed by psutil and derives rates from monotonic timestamps.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import ipaddress
import math
import socket
import time
from typing import Mapping, Protocol

import psutil

from .routes import default_route_interface


@dataclass(frozen=True, slots=True)
class InterfaceInfo:
    """Portable display information for one network interface.

    ``addresses`` contains IP address strings without a CIDR prefix. IPv6
    zone identifiers are retained when the operating system supplies them.
    psutil does not provide a portable gateway, Wi-Fi SSID, signal level, or
    wired/wireless classification, so those values are intentionally absent.
    """

    name: str
    is_up: bool
    addresses: tuple[str, ...] = ()
    mac_address: str | None = None
    speed_mbps: float | None = None
    connection_type: str | None = None
    is_loopback: bool = False


@dataclass(frozen=True, slots=True)
class CounterSnapshot:
    """Raw cumulative counters for one interface."""

    bytes_sent: int | None
    bytes_recv: int | None
    packets_sent: int | None = None
    packets_recv: int | None = None


@dataclass(frozen=True, slots=True)
class MonitorPoint:
    """One timestamped graph point and its cumulative session totals.

    A ``None`` rate marks a discontinuity. Consumers should leave a break in
    the plotted line at that point instead of drawing through the interval.
    Rates use decimal megabits per second (1 Mbps = 1,000,000 bits/s).
    """

    timestamp: float
    download_mbps: float | None
    upload_mbps: float | None
    total_download_bytes: int
    total_upload_bytes: int
    total_download_packets: int | None
    total_upload_packets: int | None
    interface_name: str | None
    gap_reason: str | None = None


class MonitorProvider(Protocol):
    """Small injectable boundary used by deterministic monitor tests."""

    def discover_interfaces(self) -> tuple[InterfaceInfo, ...]: ...

    def read_counters(self) -> Mapping[str, CounterSnapshot]: ...


def _counter_value(value: object) -> int | None:
    """Return a valid nonnegative integer counter, or ``None``."""

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value.bit_length() > 127
    ):
        return None
    return value


def _get_counter(raw: object, name: str) -> int | None:
    if isinstance(raw, Mapping):
        value = raw.get(name)
    else:
        value = getattr(raw, name, None)
    return _counter_value(value)


def _coerce_snapshot(raw: object) -> CounterSnapshot:
    if isinstance(raw, CounterSnapshot):
        return CounterSnapshot(
            bytes_sent=_counter_value(raw.bytes_sent),
            bytes_recv=_counter_value(raw.bytes_recv),
            packets_sent=_counter_value(raw.packets_sent),
            packets_recv=_counter_value(raw.packets_recv),
        )
    return CounterSnapshot(
        bytes_sent=_get_counter(raw, "bytes_sent"),
        bytes_recv=_get_counter(raw, "bytes_recv"),
        packets_sent=_get_counter(raw, "packets_sent"),
        packets_recv=_get_counter(raw, "packets_recv"),
    )


def _is_ip_family(family: object, expected: int) -> bool:
    return family == expected


def _interface_is_loopback(name: str, addresses: tuple[str, ...]) -> bool:
    normalized_name = name.casefold().replace(" ", "")
    if normalized_name in {"lo", "lo0", "loopback"} or normalized_name.startswith(
        "loopbackpseudo-interface"
    ):
        return True
    ip_values = []
    for address in addresses:
        candidate = address.split("%", 1)[0].split("/", 1)[0]
        try:
            ip_values.append(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return bool(ip_values) and all(address.is_loopback for address in ip_values)


class PsutilProvider:
    """Read interface details and raw counters through psutil only."""

    def __init__(self, psutil_module=psutil) -> None:
        self._psutil = psutil_module

    def default_route_interface(self) -> str | None:
        return default_route_interface()

    def discover_interfaces(self) -> tuple[InterfaceInfo, ...]:
        raw_addresses = self._psutil.net_if_addrs()
        raw_stats = self._psutil.net_if_stats()
        names = set(raw_addresses) | set(raw_stats)
        result: list[InterfaceInfo] = []
        link_families = {getattr(self._psutil, "AF_LINK", object())}
        packet_family = getattr(socket, "AF_PACKET", None)
        if packet_family is not None:
            link_families.add(packet_family)

        for name in sorted(names, key=lambda item: (item.casefold(), item)):
            addresses: list[tuple[int, str]] = []
            mac_address = None
            for address_info in raw_addresses.get(name, ()):
                family = getattr(address_info, "family", None)
                address = getattr(address_info, "address", None)
                if not isinstance(address, str) or not address:
                    continue
                if _is_ip_family(family, socket.AF_INET):
                    addresses.append((4, address))
                elif _is_ip_family(family, socket.AF_INET6):
                    addresses.append((6, address))
                elif family in link_families and mac_address is None:
                    mac_address = address

            address_values = tuple(
                address
                for _, address in sorted(set(addresses), key=lambda item: (item[0], item[1]))
            )
            stats = raw_stats.get(name)
            speed = getattr(stats, "speed", None) if stats is not None else None
            if (
                isinstance(speed, bool)
                or not isinstance(speed, (int, float))
                or not math.isfinite(speed)
                or speed <= 0
            ):
                speed = None
            else:
                speed = float(speed)
            result.append(
                InterfaceInfo(
                    name=name,
                    is_up=bool(getattr(stats, "isup", False)),
                    addresses=address_values,
                    mac_address=mac_address,
                    speed_mbps=speed,
                    connection_type=None,
                    is_loopback=_interface_is_loopback(name, address_values),
                )
            )
        return tuple(result)

    def read_counters(self) -> Mapping[str, CounterSnapshot]:
        # nowrap=False is important: monotonic adjustment would conceal a
        # decrease that should break the graph and establish a new baseline.
        raw_counters = self._psutil.net_io_counters(pernic=True, nowrap=False)
        return {
            name: CounterSnapshot(
                bytes_sent=_get_counter(counters, "bytes_sent"),
                bytes_recv=_get_counter(counters, "bytes_recv"),
                packets_sent=_get_counter(counters, "packets_sent"),
                packets_recv=_get_counter(counters, "packets_recv"),
            )
            for name, counters in raw_counters.items()
        }


class NetworkMonitor:
    """Track passive per-interface activity for one monitoring session."""

    def __init__(
        self,
        provider: MonitorProvider | None = None,
        clock=time.monotonic,
        wall_clock=time.time,
        history_seconds: float = 300.0,
        interval_seconds: float = 1.0,
        max_history_points: int = 1200,
        route_lookup=None,
    ) -> None:
        if not math.isfinite(history_seconds) or history_seconds <= 0:
            raise ValueError("history_seconds must be finite and positive")
        if not math.isfinite(interval_seconds) or interval_seconds < 0.1 or interval_seconds > 60:
            raise ValueError("interval_seconds must be between 0.1 and 60")
        if max_history_points < 2:
            raise ValueError("max_history_points must be at least 2")
        self._provider = provider or PsutilProvider()
        self._route_lookup = route_lookup
        self._clock = clock
        self._wall_clock = wall_clock
        self.history_seconds = float(history_seconds)
        self.interval_seconds = float(interval_seconds)
        self.max_history_points = int(max_history_points)

        self._interfaces: tuple[InterfaceInfo, ...] = ()
        self._selected_interface: str | None = None
        self._selected_identity: tuple[str | None, tuple[str, ...]] | None = None
        self._automatic_selection = True
        self._discovery_succeeded = False
        self._discovery_error: str | None = None
        self._counter_error: str | None = None
        self._history: deque[MonitorPoint] = deque(maxlen=self.max_history_points)
        self._totals_download_bytes = 0
        self._totals_upload_bytes = 0
        self._totals_download_packets: int | None = 0
        self._totals_upload_packets: int | None = 0

        self.running = False
        self.paused = False
        self._started_once = False
        self._session_elapsed_base = 0.0
        self._active_since: float | None = None
        self._baseline: CounterSnapshot | None = None
        self._baseline_interface: str | None = None
        self._baseline_timestamp: float | None = None
        self._baseline_wall_timestamp: float | None = None

    @property
    def interfaces(self) -> tuple[InterfaceInfo, ...]:
        return self._interfaces

    @property
    def selected_interface(self) -> str | None:
        return self._selected_interface

    @property
    def selected_info(self) -> InterfaceInfo | None:
        return next(
            (item for item in self._interfaces if item.name == self._selected_interface),
            None,
        )

    @property
    def automatic_selection(self) -> bool:
        return self._automatic_selection

    @property
    def discovery_succeeded(self) -> bool:
        return self._discovery_succeeded

    @property
    def discovery_error(self) -> str | None:
        return self._discovery_error

    @property
    def counter_error(self) -> str | None:
        return self._counter_error

    @property
    def totals_bytes(self) -> tuple[int, int]:
        """Cumulative session bytes as ``(received, sent)``."""

        return self._totals_download_bytes, self._totals_upload_bytes

    @property
    def totals_packets(self) -> tuple[int | None, int | None]:
        """Cumulative session packets as ``(received, sent)``."""

        return self._totals_download_packets, self._totals_upload_packets

    @property
    def history(self) -> tuple[MonitorPoint, ...]:
        self._trim_history(self._now())
        return tuple(self._history)

    @property
    def elapsed_seconds(self) -> float:
        if self._active_since is None:
            return self._session_elapsed_base
        if self.paused or not self.running:
            return self._session_elapsed_base
        return self._session_elapsed_base + max(0.0, self._now() - self._active_since)

    def discover_interfaces(self) -> tuple[InterfaceInfo, ...]:
        """Refresh interface details and apply only automatic selection rules."""

        previous_selected_name = self._selected_interface
        previous_identity = self._selected_identity
        try:
            discovered = tuple(self._provider.discover_interfaces())
            if not all(isinstance(item, InterfaceInfo) for item in discovered):
                raise ValueError("interface provider returned invalid data")
        except Exception as exc:
            self._discovery_succeeded = False
            self._discovery_error = str(exc) or type(exc).__name__
            return self._interfaces

        self._interfaces = tuple(
            sorted(discovered, key=lambda item: (item.name.casefold(), item.name))
        )
        self._discovery_succeeded = True
        self._discovery_error = None
        if self._automatic_selection:
            current = next(
                (item for item in self._interfaces if item.name == self._selected_interface),
                None,
            )
            route_lookup = self._route_lookup or getattr(self._provider, 'default_route_interface', None)
            try:
                route_name = route_lookup() if callable(route_lookup) else None
            except Exception:
                route_name = None
            routed = next(
                (item for item in self._interfaces
                 if item.name == route_name and item.is_up and not item.is_loopback),
                None,
            )
            if routed is not None:
                self._set_selected_interface(routed.name)
            elif not self._eligible_for_automatic_selection(current):
                candidates = [
                    item
                    for item in self._interfaces
                    if self._eligible_for_automatic_selection(item)
                ]
                selected = candidates[0].name if candidates else None
                self._set_selected_interface(selected)
        current_selected_info = self.selected_info
        if (
            self._selected_interface == previous_selected_name
            and previous_identity is not None
            and current_selected_info is not None
            and self._interface_identity_changed(previous_identity, current_selected_info)
        ):
            self._reset_interface_session()
        if current_selected_info is not None:
            self._selected_identity = self._interface_identity(current_selected_info)
        return self._interfaces

    def start(self, interface_name: str | None = None) -> None:
        """Start sampling, keeping an existing manual choice authoritative."""

        if interface_name is not None:
            self.select_interface(interface_name)
        if self.running:
            if self.paused:
                self.resume()
            return
        self.discover_interfaces()
        now = self._now()
        wall_now = self._wall_now()
        self.running = True
        self.paused = False
        self._started_once = True
        self._session_elapsed_base = 0.0
        self._active_since = now
        self._baseline_timestamp = now
        self._read_baseline(now, wall_now)

    def pause(self) -> None:
        """Account for one final active sample, then exclude paused time."""

        if not self.running or self.paused:
            return
        now = self._now()
        self._sample_at(now, self._wall_now())
        self._session_elapsed_base += max(
            0.0, now - (self._active_since if self._active_since is not None else now)
        )
        self._active_since = None
        self.paused = True
        self._baseline = None
        self._baseline_timestamp = None
        self._baseline_wall_timestamp = None

    def resume(self) -> None:
        """Resume a paused session without counting paused traffic or time."""

        if not self.running or not self.paused:
            return
        now = self._now()
        wall_now = self._wall_now()
        self.paused = False
        self._active_since = now
        self._baseline_timestamp = now
        self._read_baseline(now, wall_now)
        self._append_gap(now, "resumed")

    def reset(self) -> None:
        """Clear elapsed time, traffic totals, and chart history."""

        now = self._now()
        wall_now = self._wall_now()
        self._session_elapsed_base = 0.0
        if self.running and not self.paused:
            self._active_since = now
        elif self.paused or not self.running:
            self._active_since = None
        self._totals_download_bytes = 0
        self._totals_upload_bytes = 0
        self._totals_download_packets = 0
        self._totals_upload_packets = 0
        self._history.clear()
        self._baseline = None
        self._baseline_interface = None
        self._baseline_timestamp = now
        self._baseline_wall_timestamp = None
        if self.running and not self.paused:
            self._read_baseline(now, wall_now)

    def select_interface(self, name: str) -> None:
        """Pin monitoring to ``name`` until automatic selection is restored."""

        if not isinstance(name, str) or not name.strip():
            raise ValueError("interface name must be a nonempty string")
        self._automatic_selection = False
        self._set_selected_interface(name)

    def use_automatic_selection(self) -> None:
        """Restore deterministic automatic selection and immediately refresh."""

        self._automatic_selection = True
        self.discover_interfaces()

    def set_interval(self, seconds: float) -> None:
        seconds = float(seconds)
        if not math.isfinite(seconds) or seconds < 0.1 or seconds > 60:
            raise ValueError("interval must be between 0.1 and 60 seconds")
        self.interval_seconds = seconds

    def sample(self) -> MonitorPoint:
        """Sample now and return the resulting point (or discontinuity)."""

        return self._sample_at(self._now(), self._wall_now())

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            raise ValueError("clock must return a finite monotonic timestamp")
        return value

    def _wall_now(self) -> float | None:
        """Read wall time, treating unavailable or malformed values as unknown."""

        try:
            value = float(self._wall_clock())
        except Exception:
            return None
        return value if math.isfinite(value) else None

    @staticmethod
    def _eligible_for_automatic_selection(info: InterfaceInfo | None) -> bool:
        if info is None or not info.is_up or info.is_loopback:
            return False
        if not info.addresses:
            return False
        for address in info.addresses:
            candidate = address.split("%", 1)[0].split("/", 1)[0]
            try:
                parsed = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if not parsed.is_loopback:
                return True
        return False

    def _set_selected_interface(self, name: str | None) -> None:
        if name == self._selected_interface:
            return
        self._selected_interface = name
        self._reset_interface_session()

    @staticmethod
    def _interface_identity(
        info: InterfaceInfo,
    ) -> tuple[str | None, tuple[str, ...]]:
        mac = info.mac_address.casefold() if info.mac_address else None
        return mac, info.addresses

    @staticmethod
    def _interface_identity_changed(
        previous: tuple[str | None, tuple[str, ...]], current: InterfaceInfo
    ) -> bool:
        previous_mac, previous_addresses = previous
        current_mac, current_addresses = NetworkMonitor._interface_identity(current)
        if previous_mac and current_mac:
            return previous_mac != current_mac
        return previous_addresses != current_addresses

    def _reset_interface_session(self) -> None:
        """Start a clean elapsed/counter session while preserving lifecycle state."""

        now = self._now()
        self._totals_download_bytes = 0
        self._totals_upload_bytes = 0
        self._totals_download_packets = 0
        self._totals_upload_packets = 0
        self._history.clear()
        self._baseline = None
        self._baseline_interface = None
        self._baseline_timestamp = None
        self._baseline_wall_timestamp = None
        self._session_elapsed_base = 0.0
        self._active_since = now if self.running and not self.paused else None
        info = self.selected_info
        self._selected_identity = self._interface_identity(info) if info else None

    def _read_counters(self) -> Mapping[str, CounterSnapshot] | None:
        try:
            raw = self._provider.read_counters()
        except Exception as exc:
            self._counter_error = str(exc) or type(exc).__name__
            return None
        self._counter_error = None
        if not isinstance(raw, Mapping):
            self._counter_error = "counter provider returned invalid data"
            return None
        return raw

    def _read_baseline(self, now: float, wall_now: float | None) -> None:
        self._baseline = None
        self._baseline_interface = self._selected_interface
        self._baseline_timestamp = now
        self._baseline_wall_timestamp = wall_now
        if self._selected_interface is None:
            return
        counters = self._read_counters()
        if counters is None:
            return
        raw = counters.get(self._selected_interface)
        if raw is None:
            return
        current = _coerce_snapshot(raw)
        if current.bytes_sent is None or current.bytes_recv is None:
            return
        self._baseline = current

    def _sample_at(self, now: float, wall_now: float | None) -> MonitorPoint:
        if not self.running:
            return self._make_point(now, None, None, "not running")
        if self.paused:
            return self._make_point(now, None, None, "paused")
        if self._selected_interface is None:
            return self._append_gap(now, "no eligible interface")
        info = self.selected_info
        if info is None:
            self._baseline = None
            self._baseline_wall_timestamp = None
            return self._append_gap(now, "interface unavailable")
        if not info.is_up:
            self._baseline = None
            self._baseline_wall_timestamp = None
            return self._append_gap(now, "interface down")

        counters = self._read_counters()
        if counters is None:
            self._baseline = None
            self._baseline_wall_timestamp = None
            return self._append_gap(now, "counter read failed")
        raw = counters.get(self._selected_interface)
        if raw is None:
            self._baseline = None
            self._baseline_wall_timestamp = None
            return self._append_gap(now, "interface unavailable")
        current = _coerce_snapshot(raw)
        if current.bytes_sent is None or current.bytes_recv is None:
            self._baseline = None
            self._baseline_wall_timestamp = None
            return self._append_gap(now, "invalid byte counters")

        if self._baseline is None or self._baseline_interface != self._selected_interface:
            self._baseline = current
            self._baseline_interface = self._selected_interface
            self._baseline_timestamp = now
            self._baseline_wall_timestamp = wall_now
            return self._append_gap(now, "baseline established")

        previous = self._baseline
        previous_time = self._baseline_timestamp
        previous_wall_time = self._baseline_wall_timestamp
        self._baseline = current
        self._baseline_timestamp = now
        self._baseline_wall_timestamp = wall_now
        if previous_time is None:
            return self._append_gap(now, "baseline timestamp unavailable")
        elapsed = now - previous_time
        if not math.isfinite(elapsed) or elapsed <= 0:
            return self._append_gap(now, "invalid sample interval")

        if previous_wall_time is None or wall_now is None:
            return self._append_gap(now, "wall clock unavailable")
        wall_elapsed = wall_now - previous_wall_time
        if wall_elapsed < 0:
            return self._append_gap(now, "wall clock moved backwards")
        # Keep this tolerance fixed and small: scaling it to a long sampling
        # interval can hide a short suspend. Clock-resolution differences,
        # scheduling jitter, or wall-clock adjustment can conservatively make
        # an otherwise usable interval appear as a gap.
        wall_tolerance = 0.05
        if abs(wall_elapsed - elapsed) > wall_tolerance:
            return self._append_gap(now, "clock discontinuity")

        delta_sent = current.bytes_sent - (previous.bytes_sent or 0)
        delta_recv = current.bytes_recv - (previous.bytes_recv or 0)
        if delta_sent < 0 or delta_recv < 0:
            return self._append_gap(now, "counter reset")

        # A poll more than 250 ms past its requested interval may have missed
        # an offline or suspend interval, so neither rates nor totals can
        # safely attribute its counters. Some platform monotonic clocks (for
        # example Windows QPC) advance during suspend; a suspend contained
        # wholly within one requested interval can therefore remain invisible
        # when both clocks advance together.
        if elapsed > self.interval_seconds + 0.25:
            return self._append_gap(now, "delayed sample")
        self._accumulate_totals(previous, current, delta_recv, delta_sent)

        try:
            download_mbps = (delta_recv * 8.0) / elapsed / 1_000_000.0
            upload_mbps = (delta_sent * 8.0) / elapsed / 1_000_000.0
        except OverflowError:
            return self._append_gap(now, "invalid calculated rate")
        if not math.isfinite(download_mbps) or not math.isfinite(upload_mbps):
            return self._append_gap(now, "invalid calculated rate")
        return self._append_point(now, download_mbps, upload_mbps, None)

    def _accumulate_totals(
        self,
        previous: CounterSnapshot,
        current: CounterSnapshot,
        delta_recv: int,
        delta_sent: int,
    ) -> None:
        self._totals_download_bytes += delta_recv
        self._totals_upload_bytes += delta_sent
        if previous.packets_recv is None or current.packets_recv is None:
            self._totals_download_packets = None
        elif current.packets_recv < previous.packets_recv:
            self._totals_download_packets = None
        elif self._totals_download_packets is not None:
            self._totals_download_packets += current.packets_recv - previous.packets_recv
        if previous.packets_sent is None or current.packets_sent is None:
            self._totals_upload_packets = None
        elif current.packets_sent < previous.packets_sent:
            self._totals_upload_packets = None
        elif self._totals_upload_packets is not None:
            self._totals_upload_packets += current.packets_sent - previous.packets_sent

    def _make_point(
        self,
        now: float,
        download_mbps: float | None,
        upload_mbps: float | None,
        gap_reason: str | None,
    ) -> MonitorPoint:
        return MonitorPoint(
            timestamp=now,
            download_mbps=download_mbps,
            upload_mbps=upload_mbps,
            total_download_bytes=self._totals_download_bytes,
            total_upload_bytes=self._totals_upload_bytes,
            total_download_packets=self._totals_download_packets,
            total_upload_packets=self._totals_upload_packets,
            interface_name=self._selected_interface,
            gap_reason=gap_reason,
        )

    def _append_point(
        self,
        now: float,
        download_mbps: float | None,
        upload_mbps: float | None,
        gap_reason: str | None,
    ) -> MonitorPoint:
        point = self._make_point(now, download_mbps, upload_mbps, gap_reason)
        self._history.append(point)
        self._trim_history(now)
        return point

    def _append_gap(self, now: float, reason: str) -> MonitorPoint:
        return self._append_point(now, None, None, reason)

    def _trim_history(self, now: float) -> None:
        cutoff = now - self.history_seconds
        while self._history and self._history[0].timestamp < cutoff:
            self._history.popleft()
