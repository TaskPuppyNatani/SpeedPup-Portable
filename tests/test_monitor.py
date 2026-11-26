# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic tests for passive network-interface monitoring."""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from speedpup.core.monitor import (
    CounterSnapshot,
    InterfaceInfo,
    NetworkMonitor,
    PsutilProvider,
)


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeProvider:
    def __init__(self, interfaces=(), counters=None) -> None:
        self.interfaces = tuple(interfaces)
        self.counters = dict(counters or {})
        self.discovery_exception: Exception | None = None
        self.counter_exception: Exception | None = None

    def discover_interfaces(self):
        if self.discovery_exception:
            raise self.discovery_exception
        return self.interfaces

    def read_counters(self):
        if self.counter_exception:
            raise self.counter_exception
        return dict(self.counters)


def interface(
    name="eth0",
    *,
    is_up=True,
    addresses=("192.0.2.10",),
    mac="02:00:00:00:00:01",
    is_loopback=False,
):
    return InterfaceInfo(
        name=name,
        is_up=is_up,
        addresses=tuple(addresses),
        mac_address=mac,
        speed_mbps=1000.0,
        is_loopback=is_loopback,
    )


def counters(sent=0, recv=0, packets_sent=0, packets_recv=0):
    return CounterSnapshot(
        bytes_sent=sent,
        bytes_recv=recv,
        packets_sent=packets_sent,
        packets_recv=packets_recv,
    )


def running_monitor(provider=None, *, clock=None, wall_clock=None, **kwargs):
    clock = clock or FakeClock()
    if wall_clock is None:
        wall_clock = lambda: 1000.0 + clock.now
    provider = provider or FakeProvider(
        [interface()], {"eth0": counters()}
    )
    monitor = NetworkMonitor(provider=provider, clock=clock, wall_clock=wall_clock, **kwargs)
    monitor.start()
    return monitor, provider, clock


def test_psutil_provider_uses_raw_counters_and_formats_interface_details():
    link_family = 77
    calls = []
    fake_psutil = SimpleNamespace(
        AF_LINK=link_family,
        net_if_addrs=lambda: {
            "wifi0": [
                SimpleNamespace(family=socket.AF_INET6, address="fe80::1%wifi0"),
                SimpleNamespace(family=socket.AF_INET, address="192.0.2.4"),
                SimpleNamespace(family=link_family, address="02:00:00:00:00:02"),
            ],
            "lo": [SimpleNamespace(family=socket.AF_INET, address="127.0.0.1")],
        },
        net_if_stats=lambda: {
            "wifi0": SimpleNamespace(isup=True, speed=866),
            "lo": SimpleNamespace(isup=True, speed=0),
        },
        net_io_counters=lambda **kwargs: (
            calls.append(kwargs)
            or {"wifi0": SimpleNamespace(bytes_sent=5, bytes_recv=7, packets_sent=2, packets_recv=3)}
        ),
    )
    provider = PsutilProvider(fake_psutil)

    found = {item.name: item for item in provider.discover_interfaces()}
    assert found["wifi0"] == InterfaceInfo(
        name="wifi0",
        is_up=True,
        addresses=("192.0.2.4", "fe80::1%wifi0"),
        mac_address="02:00:00:00:00:02",
        speed_mbps=866.0,
        connection_type=None,
        is_loopback=False,
    )
    assert found["lo"].is_loopback
    assert found["lo"].speed_mbps is None
    assert provider.read_counters()["wifi0"] == counters(5, 7, 2, 3)
    assert calls == [{"pernic": True, "nowrap": False}]


def test_automatic_selection_is_stable_and_chooses_an_active_non_loopback():
    provider = FakeProvider(
        [
            interface("z-wifi", addresses=("2001:db8::1",)),
            interface("lo", addresses=("127.0.0.1",), is_loopback=True),
            interface("down", is_up=False),
            interface("eth0"),
        ]
    )
    clock = FakeClock()
    monitor = NetworkMonitor(
        provider=provider, clock=clock, wall_clock=lambda: 1000.0 + clock.now
    )

    monitor.discover_interfaces()
    assert monitor.selected_interface == "eth0"

    provider.interfaces = (interface("aaa"), *provider.interfaces)
    monitor.discover_interfaces()
    assert monitor.selected_interface == "eth0"

    provider.interfaces = tuple(
        interface(item.name, is_up=False) if item.name == "eth0" else item
        for item in provider.interfaces
    )
    monitor.discover_interfaces()
    assert monitor.selected_interface == "aaa"


def test_manual_selection_survives_disappearance_and_start_does_not_switch():
    provider = FakeProvider(
        [interface("eth0"), interface("wlan0")],
        {"eth0": counters(), "wlan0": counters()},
    )
    clock = FakeClock()
    monitor = NetworkMonitor(
        provider=provider, clock=clock, wall_clock=lambda: 1000.0 + clock.now
    )
    monitor.discover_interfaces()
    monitor.select_interface("eth0")
    provider.interfaces = (interface("wlan0"),)

    monitor.start()
    clock.advance(1)
    point = monitor.sample()

    assert monitor.automatic_selection is False
    assert monitor.selected_interface == "eth0"
    assert point.gap_reason == "interface unavailable"


def test_automatic_selection_prefers_default_route_without_excluding_virtual_interfaces():
    provider = FakeProvider(
        [interface('br-virtual'), interface('wlo1'), interface('vpn0')],
        {'br-virtual': counters(), 'wlo1': counters(), 'vpn0': counters()},
    )
    provider.default_route_interface = lambda: 'wlo1'
    monitor, _, clock = running_monitor(provider)
    assert monitor.automatic_selection
    assert monitor.selected_interface == 'wlo1'
    provider.counters['br-virtual'] = counters(recv=1_000_000)
    provider.counters['wlo1'] = counters(recv=1000)
    clock.advance(1)
    point = monitor.sample()
    assert point.interface_name == 'wlo1'
    assert point.download_mbps == pytest.approx(0.008)

    provider.default_route_interface = lambda: None
    monitor.discover_interfaces()
    assert monitor.selected_interface == 'wlo1'

    provider.default_route_interface = lambda: 'vpn0'
    monitor.discover_interfaces()
    assert monitor.selected_interface == 'vpn0'
    monitor.select_interface('br-virtual')
    monitor.discover_interfaces()
    assert not monitor.automatic_selection
    assert monitor.selected_interface == 'br-virtual'
    monitor.use_automatic_selection()
    assert monitor.selected_interface == 'vpn0'


def test_automatic_selection_uses_stable_fallback_when_route_is_unavailable():
    provider = FakeProvider([interface('br-virtual'), interface('wlo1')])
    provider.default_route_interface = lambda: 'unknown-interface'
    monitor, _, _ = running_monitor(provider)
    assert monitor.selected_interface == 'br-virtual'
    provider.default_route_interface = lambda: None
    monitor.discover_interfaces()
    assert monitor.selected_interface == 'br-virtual'


def test_rates_and_session_byte_packet_totals_use_received_sent_order():
    monitor, provider, clock = running_monitor()
    provider.counters["eth0"] = counters(sent=700, recv=1600, packets_sent=13, packets_recv=24)
    clock.advance(1)

    point = monitor.sample()

    assert point.download_mbps == pytest.approx(0.0128)
    assert point.upload_mbps == pytest.approx(0.0056)
    assert monitor.totals_bytes == (1600, 700)
    assert monitor.totals_packets == (24, 13)
    assert point.total_download_bytes == 1600
    assert point.total_upload_bytes == 700


def test_counter_decrease_breaks_the_graph_and_rebaselines_without_negative_totals():
    provider = FakeProvider(
        [interface()], {"eth0": counters(sent=100, recv=100, packets_sent=5, packets_recv=5)}
    )
    monitor, provider, clock = running_monitor(provider)
    provider.counters["eth0"] = counters(sent=10, recv=5, packets_sent=1, packets_recv=1)
    clock.advance(1)
    reset_point = monitor.sample()
    assert reset_point.gap_reason == "counter reset"
    assert monitor.totals_bytes == (0, 0)

    provider.counters["eth0"] = counters(sent=110, recv=105, packets_sent=3, packets_recv=4)
    clock.advance(1)
    point = monitor.sample()
    assert point.download_mbps == pytest.approx(0.0008)
    assert point.upload_mbps == pytest.approx(0.0008)
    assert monitor.totals_bytes == (100, 100)


def test_delayed_sample_is_a_gap_and_does_not_attribute_unknown_traffic():
    monitor, provider, clock = running_monitor(interval_seconds=1)
    provider.counters["eth0"] = counters(sent=1000, recv=2000, packets_sent=3, packets_recv=4)
    clock.advance(5)

    delayed = monitor.sample()

    assert delayed.gap_reason == "delayed sample"
    assert monitor.totals_bytes == (0, 0)
    assert monitor.totals_packets == (0, 0)

    provider.counters["eth0"] = counters(sent=1010, recv=2020, packets_sent=4, packets_recv=6)
    clock.advance(1)
    point = monitor.sample()
    assert point.gap_reason is None
    assert monitor.totals_bytes == (20, 10)
    assert monitor.totals_packets == (2, 1)


def test_six_second_poll_gap_excludes_totals_at_five_second_interval():
    monotonic = FakeClock(50)
    wall = FakeClock(5000)
    provider = FakeProvider([interface()], {"eth0": counters()})
    monitor = NetworkMonitor(
        provider=provider,
        clock=monotonic,
        wall_clock=wall,
        interval_seconds=5,
    )
    monitor.start()

    provider.counters["eth0"] = counters(sent=100, recv=200, packets_sent=2, packets_recv=3)
    wall.advance(6)
    monotonic.advance(6)
    point = monitor.sample()

    assert point.gap_reason == "delayed sample"
    assert monitor.totals_bytes == (0, 0)
    assert monitor.totals_packets == (0, 0)


def test_slightly_late_sample_uses_actual_elapsed_time_for_rate():
    monotonic = FakeClock(50)
    wall = FakeClock(5000)
    provider = FakeProvider([interface()], {"eth0": counters()})
    monitor = NetworkMonitor(
        provider=provider,
        clock=monotonic,
        wall_clock=wall,
        interval_seconds=1,
    )
    monitor.start()
    provider.counters["eth0"] = counters(sent=110, recv=220)
    wall.advance(1.1)
    monotonic.advance(1.1)

    point = monitor.sample()

    assert point.gap_reason is None
    assert point.download_mbps == pytest.approx((220 * 8) / 1.1 / 1_000_000)
    assert point.upload_mbps == pytest.approx((110 * 8) / 1.1 / 1_000_000)
    assert monitor.totals_bytes == (220, 110)


def test_wall_clock_detects_suspend_when_monotonic_clock_stops():
    monotonic = FakeClock(50)
    wall = FakeClock(5000)
    provider = FakeProvider([interface()], {"eth0": counters()})
    monitor = NetworkMonitor(
        provider=provider,
        clock=monotonic,
        wall_clock=wall,
        interval_seconds=1,
    )
    monitor.start()

    provider.counters["eth0"] = counters(sent=200, recv=300, packets_sent=4, packets_recv=5)
    wall.advance(30)
    monotonic.advance(1)
    resumed = monitor.sample()

    assert resumed.gap_reason == "clock discontinuity"
    assert monitor.totals_bytes == (0, 0)
    assert monitor.totals_packets == (0, 0)

    provider.counters["eth0"] = counters(sent=210, recv=320, packets_sent=5, packets_recv=6)
    wall.advance(1)
    monotonic.advance(1)
    next_point = monitor.sample()
    assert next_point.gap_reason is None
    assert monitor.totals_bytes == (20, 10)
    assert monitor.totals_packets == (1, 1)


def test_one_second_suspend_is_detected_with_five_second_sampling_interval():
    monotonic = FakeClock(50)
    wall = FakeClock(5000)
    provider = FakeProvider([interface()], {"eth0": counters()})
    monitor = NetworkMonitor(
        provider=provider,
        clock=monotonic,
        wall_clock=wall,
        interval_seconds=5,
    )
    monitor.start()

    provider.counters["eth0"] = counters(sent=100, recv=200, packets_sent=2, packets_recv=3)
    wall.advance(5)
    monotonic.advance(4)
    point = monitor.sample()

    assert point.gap_reason == "clock discontinuity"
    assert monitor.totals_bytes == (0, 0)
    assert monitor.totals_packets == (0, 0)


@pytest.mark.parametrize(
    ("wall_delta", "reason"),
    [(10.0, "clock discontinuity"), (-1.0, "wall clock moved backwards")],
)
def test_wall_clock_adjustments_are_gaps_and_rebaseline(wall_delta, reason):
    monotonic = FakeClock(10)
    wall = FakeClock(1000)
    provider = FakeProvider([interface()], {"eth0": counters()})
    monitor = NetworkMonitor(provider=provider, clock=monotonic, wall_clock=wall)
    monitor.start()
    provider.counters["eth0"] = counters(sent=100, recv=200, packets_sent=1, packets_recv=2)
    monotonic.advance(1)
    wall.advance(wall_delta)

    point = monitor.sample()

    assert point.gap_reason == reason
    assert monitor.totals_bytes == (0, 0)
    assert monitor.totals_packets == (0, 0)

    provider.counters["eth0"] = counters(sent=105, recv=207, packets_sent=2, packets_recv=3)
    monotonic.advance(1)
    wall.advance(1)
    assert monitor.sample().gap_reason is None
    assert monitor.totals_bytes == (7, 5)
    assert monitor.totals_packets == (1, 1)


def test_unavailable_wall_clock_creates_gap_then_recovers():
    monotonic = FakeClock(10)
    wall_values = iter([100.0, float("nan"), 103.0, 104.0])
    provider = FakeProvider([interface()], {"eth0": counters()})
    monitor = NetworkMonitor(
        provider=provider,
        clock=monotonic,
        wall_clock=lambda: next(wall_values),
    )
    monitor.start()
    provider.counters["eth0"] = counters(sent=10, recv=20)
    monotonic.advance(1)
    assert monitor.sample().gap_reason == "wall clock unavailable"

    provider.counters["eth0"] = counters(sent=15, recv=27)
    monotonic.advance(1)
    assert monitor.sample().gap_reason == "wall clock unavailable"
    provider.counters["eth0"] = counters(sent=20, recv=35)
    monotonic.advance(1)
    point = monitor.sample()
    assert point.gap_reason is None
    assert monitor.totals_bytes == (8, 5)


def test_pause_and_resume_exclude_paused_time_and_counter_deltas():
    monitor, provider, clock = running_monitor()
    provider.counters["eth0"] = counters(sent=100, recv=100, packets_sent=1, packets_recv=1)
    clock.advance(1)
    monitor.sample()
    provider.counters["eth0"] = counters(sent=200, recv=200, packets_sent=2, packets_recv=2)
    clock.advance(1)
    monitor.pause()
    assert monitor.paused
    assert monitor.elapsed_seconds == pytest.approx(2)
    assert monitor.totals_bytes == (200, 200)

    provider.counters["eth0"] = counters(sent=900, recv=1000, packets_sent=9, packets_recv=10)
    clock.advance(20)
    monitor.resume()
    assert monitor.history[-1].gap_reason == "resumed"
    assert monitor.elapsed_seconds == pytest.approx(2)
    provider.counters["eth0"] = counters(sent=910, recv=1020, packets_sent=10, packets_recv=12)
    clock.advance(1)
    monitor.sample()

    assert monitor.elapsed_seconds == pytest.approx(3)
    assert monitor.totals_bytes == (220, 210)
    assert monitor.totals_packets == (4, 3)


def test_reset_clears_session_state_but_keeps_sampling_active():
    monitor, provider, clock = running_monitor()
    provider.counters["eth0"] = counters(sent=30, recv=40, packets_sent=3, packets_recv=4)
    clock.advance(1)
    monitor.sample()
    assert monitor.totals_bytes == (40, 30)

    monitor.reset()
    assert monitor.running and not monitor.paused
    assert monitor.elapsed_seconds == 0
    assert monitor.totals_bytes == (0, 0)
    assert monitor.history == ()

    provider.counters["eth0"] = counters(sent=35, recv=50, packets_sent=4, packets_recv=5)
    clock.advance(1)
    point = monitor.sample()
    assert point.gap_reason is None
    assert monitor.totals_bytes == (10, 5)


def test_down_and_disappeared_interfaces_break_sampling_baseline():
    provider = FakeProvider([interface()], {"eth0": counters()})
    clock = FakeClock()
    monitor = NetworkMonitor(
        provider=provider, clock=clock, wall_clock=lambda: 1000.0 + clock.now
    )
    monitor.discover_interfaces()
    monitor.select_interface("eth0")
    monitor.start()
    provider.interfaces = (interface(is_up=False),)
    monitor.discover_interfaces()
    provider.counters["eth0"] = counters(sent=50, recv=75)
    clock.advance(1)
    down = monitor.sample()
    assert down.gap_reason == "interface down"
    assert monitor.totals_bytes == (0, 0)

    provider.interfaces = ()
    monitor.discover_interfaces()
    clock.advance(1)
    missing = monitor.sample()
    assert missing.gap_reason == "interface unavailable"


def test_same_name_adapter_replacement_resets_elapsed_and_totals():
    provider = FakeProvider([interface()], {"eth0": counters()})
    monitor, _, clock = running_monitor(provider)
    provider.counters["eth0"] = counters(sent=20, recv=30)
    clock.advance(1)
    monitor.sample()
    assert monitor.totals_bytes == (30, 20)
    assert monitor.elapsed_seconds == pytest.approx(1)

    provider.interfaces = (interface(mac="02:00:00:00:00:99"),)
    provider.counters["eth0"] = counters()
    clock.advance(1)
    monitor.discover_interfaces()

    assert monitor.running and not monitor.paused
    assert monitor.elapsed_seconds == 0
    assert monitor.totals_bytes == (0, 0)
    assert monitor.history == ()

    point = monitor.sample()
    assert point.gap_reason == "baseline established"
    provider.counters["eth0"] = counters(sent=2, recv=3)
    clock.advance(1)
    assert monitor.sample().gap_reason is None
    assert monitor.totals_bytes == (3, 2)


def test_malformed_counters_create_gaps_and_unavailable_packet_totals():
    provider = FakeProvider(
        [interface()], {"eth0": CounterSnapshot(bytes_sent=-1, bytes_recv=0)}
    )
    monitor, _, clock = running_monitor(provider)
    clock.advance(1)
    invalid = monitor.sample()
    assert invalid.gap_reason == "invalid byte counters"

    provider.counters["eth0"] = CounterSnapshot(
        bytes_sent=10, bytes_recv=20, packets_sent="bad", packets_recv=2  # type: ignore[arg-type]
    )
    clock.advance(1)
    assert monitor.sample().gap_reason == "baseline established"
    provider.counters["eth0"] = CounterSnapshot(
        bytes_sent=20, bytes_recv=40, packets_sent="bad", packets_recv=4  # type: ignore[arg-type]
    )
    clock.advance(1)
    point = monitor.sample()
    assert point.download_mbps == pytest.approx(0.00016)
    assert monitor.totals_packets == (2, None)


def test_discovery_error_is_reported_without_discarding_last_known_interfaces():
    provider = FakeProvider([interface()])
    clock = FakeClock()
    monitor = NetworkMonitor(
        provider=provider, clock=clock, wall_clock=lambda: 1000.0 + clock.now
    )
    monitor.discover_interfaces()
    provider.discovery_exception = PermissionError("access denied")

    previous = monitor.discover_interfaces()

    assert previous == (interface(),)
    assert monitor.discovery_succeeded is False
    assert monitor.discovery_error == "access denied"


def test_history_is_bounded_by_age_and_point_count():
    monitor, provider, clock = running_monitor(
        history_seconds=3,
        max_history_points=3,
        interval_seconds=0.1,
    )
    for value in range(1, 6):
        provider.counters["eth0"] = counters(sent=value, recv=value)
        clock.advance(0.5)
        monitor.sample()

    points = monitor.history
    assert len(points) == 3
    assert points[0].timestamp >= clock.now - 3
    assert points[-1].timestamp == clock.now


def test_switching_interface_resets_elapsed_totals_and_history():
    provider = FakeProvider(
        [interface("eth0"), interface("wlan0", mac="02:00:00:00:00:02")],
        {"eth0": counters(), "wlan0": counters()},
    )
    monitor, _, clock = running_monitor(provider)
    provider.counters["eth0"] = counters(sent=10, recv=20)
    clock.advance(1)
    monitor.sample()
    assert monitor.elapsed_seconds == pytest.approx(1)

    monitor.select_interface("wlan0")
    assert monitor.running and not monitor.paused
    assert monitor.elapsed_seconds == 0
    assert monitor.totals_bytes == (0, 0)
    assert monitor.history == ()


def test_counter_provider_errors_become_history_gaps():
    monitor, provider, clock = running_monitor()
    provider.counter_exception = OSError("counter read failed")
    clock.advance(1)

    point = monitor.sample()

    assert point.gap_reason == "counter read failed"
    assert monitor.counter_error == "counter read failed"
