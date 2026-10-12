"""Operator-facing cable run and diagnostics contracts."""

import json

from sfp_loopback import cable_diagnostics, cable_host


STATUS_CSR_NAMES = cable_diagnostics.CABLE_STATUS_CSR_NAMES


def good_phy_status(mdio):
    return {"link_up": True, "delay_configuration_passed": True}


class Register:
    def __init__(self, value=0, on_write=None):
        self.value = value
        self.on_write = on_write

    def read(self):
        return self.value

    def write(self, value):
        self.value = value
        if self.on_write:
            self.on_write(value)


class CableBus:
    def __init__(self):
        self.regs = type("Regs", (), {})()
        for name in STATUS_CSR_NAMES:
            setattr(self.regs, name, Register())
        self.regs.host_phy_mdio_w = Register()
        self.regs.host_phy_mdio_r = Register()
        self.regs.cable_control_direction = Register(
            on_write=lambda value: setattr(self.regs.cable_status_active_direction, "value", value)
        )
        self.regs.cable_status_active_direction.value = 0
        for name in ("sfp0_link_up", "sfp1_link_up", "sfp0_qpll_locked", "sfp1_qpll_locked",
                     "sfp0_tx_ready", "sfp1_tx_ready", "sfp0_rx_ready", "sfp1_rx_ready"):
            getattr(self.regs, name).value = 1
        for name in ("sfp0_control_tx_disable", "sfp1_control_tx_disable",
                     "sfp0_loss_of_signal", "sfp1_loss_of_signal"):
            getattr(self.regs, name).value = 0
        self.regs.rtl8211f_reset_done.value = 1
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


def usb_interface(root):
    interface = root / "enxtest"
    interface.mkdir()
    for name, value in dict(type="1", flags="0x1003", carrier="1", speed="1000", mtu="1500",
                            address="10:20:30:40:50:60").items():
        (interface / name).write_text(value)
    device = root / "devices" / "usb1" / "1-1" / "net"
    device.mkdir(parents=True)
    (interface / "device").symlink_to(device)


class CableTransport:
    def __init__(self, bus, *, route_frames=True, reply=True):
        self.bus = bus
        self.route_frames = route_frames
        self.reply = reply
        self.packets = []

    def _increment(self, name):
        register = getattr(self.bus.regs, name)
        register.value = (register.value + 1) & 0xFFFFFFFF

    def send(self, frame):
        if self.route_frames:
            direction = self.bus.regs.cable_control_direction.value
            peer = 1 - direction
            self._increment("host_ingress_rx_frames")
            self._increment("host_ingress_tx_frames")
            self._increment(f"sfp{direction}_tx_frames")
            self._increment(f"sfp{peer}_return_rx_frames")
            self._increment(f"sfp{peer}_return_tx_frames")
            self._increment("host_tx_frames")
        # Linux's outgoing packet copy must be ignored by the host verifier.
        self.packets.append((frame, 4))
        if self.reply:
            reply = frame[6:12] + frame[:6] + frame[12:]
            self.packets.append((reply, 0))

    def receive(self, timeout):
        return self.packets.pop(0) if self.packets else None

    def close(self):
        pass


def run(tmp_path, bus, *, direction="sfp1-to-sfp2", route_frames=True, reply=True):
    usb_interface(tmp_path)
    report_path = tmp_path / "cable.json"
    transport = CableTransport(bus, route_frames=route_frames, reply=reply)
    status = cable_host.main(
        ["--interface", "enxtest", "--direction", direction,
         "--run-id", "42", "--report", str(report_path)],
        interface_root=tmp_path,
        bus_factory=lambda *args, **kwargs: bus,
        transport_factory=lambda name: transport,
        phy_status_reader=good_phy_status,
    )
    return status, json.loads(report_path.read_text()), bus


def test_cable_status_reader_reports_direction_ports_and_counter_snapshot():
    bus = CableBus()
    bus.regs.host_ingress_rx_frames.value = 12

    status = cable_diagnostics.read_cable_status(bus)

    assert status["host_ingress_rx_frames"] == 12
    assert status["sfp0_link_up"] == 1
    assert status["cable_status_busy"] == 0


def test_fixed_cable_run_requires_host_and_expected_sfp_route_evidence(tmp_path):
    status, report, bus = run(tmp_path, CableBus())

    assert status == 0
    assert report["host_result"]["passed"] is True
    assert report["hardware_evidence"]["passed"] is True
    assert report["hardware_evidence"]["rtl8211f_status_passed"] is True
    assert report["passed"] is True
    assert report["hardware_evidence"]["direction"] == "sfp1-to-sfp2"
    assert report["hardware_evidence"]["host_link_before"] == {
        "carrier": 1, "speed_mbps": 1000,
    }
    assert report["hardware_evidence"]["host_link_passed"] is True
    assert report["hardware_evidence"]["route_deltas"] == {
        "host_ingress_rx_frames": 1000,
        "host_ingress_tx_frames": 1000,
        "sfp0_tx_frames": 1000,
        "sfp1_return_rx_frames": 1000,
        "sfp1_return_tx_frames": 1000,
        "host_tx_frames": 1000,
        "sfp1_tx_frames": 0,
        "sfp0_return_rx_frames": 0,
    }
    assert bus.regs.cable_control_enable.value == 0


def test_reverse_direction_checks_the_opposite_sfp_route(tmp_path):
    status, report, _ = run(tmp_path, CableBus(), direction="sfp2-to-sfp1")

    assert status == 0
    assert report["hardware_evidence"]["route_deltas"] == {
        "host_ingress_rx_frames": 1000,
        "host_ingress_tx_frames": 1000,
        "sfp1_tx_frames": 1000,
        "sfp0_return_rx_frames": 1000,
        "sfp0_return_tx_frames": 1000,
        "host_tx_frames": 1000,
        "sfp0_tx_frames": 0,
        "sfp1_return_rx_frames": 0,
    }


def test_sustained_cable_mode_runs_the_full_60_seconds_in_one_direction(tmp_path):
    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()
    bus = CableBus()
    usb_interface(tmp_path)
    report_path = tmp_path / "sustained.json"

    class TimedTransport(CableTransport):
        def receive(self, timeout):
            packet = super().receive(timeout)
            if packet is not None and packet[1] != 4:
                clock.now += 0.25
            return packet

    transport = TimedTransport(bus)
    status = cable_host.main(
        ["--interface", "enxtest", "--direction", "sfp2-to-sfp1", "--duration", "60",
         "--run-id", "42", "--report", str(report_path)],
        interface_root=tmp_path, bus_factory=lambda *args, **kwargs: bus,
        transport_factory=lambda name: transport, phy_status_reader=good_phy_status,
        clock=clock,
    )
    report = json.loads(report_path.read_text())

    assert status == 0
    assert report["mode"] == "sustained_correctness"
    assert report["requested_duration_seconds"] == 60.0
    assert report["host_result"]["active_seconds"] == 60.0
    assert report["host_result"]["sent"] == report["host_result"]["received"] == 240
    assert report["hardware_evidence"]["passed"] is True


def test_cable_tool_rejects_a_shorter_than_accepted_sustained_mode(tmp_path):
    usb_interface(tmp_path)
    report_path = tmp_path / "short.json"
    try:
        cable_host.main(
            ["--interface", "enxtest", "--direction", "sfp1-to-sfp2", "--duration", "30",
             "--report", str(report_path)],
            interface_root=tmp_path,
        )
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("A 30-second run must not be accepted as the sustained test")
    assert not report_path.exists()


def test_counter_wrap_is_reported_as_a_small_positive_delta(tmp_path):
    bus = CableBus()
    bus.regs.host_ingress_rx_frames.value = 0xFFFFFFFD
    status, report, _ = run(tmp_path, bus)

    assert status == 0
    assert report["hardware_evidence"]["route_deltas"]["host_ingress_rx_frames"] == 1000


def test_non_gigabit_host_link_is_recorded_and_fails_hardware_evidence(tmp_path):
    bus = CableBus()
    usb_interface(tmp_path)
    (tmp_path / "enxtest/speed").write_text("100")
    report_path = tmp_path / "cable.json"
    status = cable_host.main(
        ["--interface", "enxtest", "--direction", "sfp1-to-sfp2",
         "--run-id", "42", "--report", str(report_path)],
        interface_root=tmp_path, bus_factory=lambda *args, **kwargs: bus,
        transport_factory=lambda name: CableTransport(bus),
        phy_status_reader=good_phy_status,
    )
    report = json.loads(report_path.read_text())

    assert status == 1
    assert report["host_result"] is None
    assert report["hardware_evidence"]["host_link_before"]["speed_mbps"] == 100
    assert report["hardware_evidence"]["host_link_passed"] is False
    assert report["passed"] is False


def test_wrong_phy_delay_state_refuses_to_send_and_saves_status(tmp_path):
    bus = CableBus()
    usb_interface(tmp_path)
    report_path = tmp_path / "cable.json"
    status = cable_host.main(
        ["--interface", "enxtest", "--direction", "sfp1-to-sfp2",
         "--run-id", "42", "--report", str(report_path)],
        interface_root=tmp_path, bus_factory=lambda *args, **kwargs: bus,
        transport_factory=lambda name: CableTransport(bus),
        phy_status_reader=lambda mdio: {"link_up": True, "delay_configuration_passed": False},
    )
    report = json.loads(report_path.read_text())

    assert status == 1
    assert report["host_result"] is None
    assert report["hardware_evidence"]["rtl8211f_before"]["delay_configuration_passed"] is False
    assert bus.regs.cable_control_enable.value == 0
    assert "do not match" in report["errors"][0]


def test_direction_changes_only_after_disabled_control_readback(tmp_path):
    bus = CableBus()

    class StuckEnabled(Register):
        def write(self, value):
            pass

    bus.regs.cable_control_enable = StuckEnabled(1)
    usb_interface(tmp_path)
    report_path = tmp_path / "cable.json"
    transport_calls = []

    status = cable_host.main(
        ["--interface", "enxtest", "--direction", "sfp2-to-sfp1",
         "--report", str(report_path)],
        interface_root=tmp_path,
        bus_factory=lambda *args, **kwargs: bus,
        transport_factory=lambda name: transport_calls.append(name),
        phy_status_reader=good_phy_status,
    )
    report = json.loads(report_path.read_text())

    assert status == 1
    assert bus.regs.cable_control_direction.value == 0
    assert transport_calls == []
    assert "remain disabled" in report["errors"][0]


def test_host_echo_cannot_pass_without_the_expected_cable_counter_deltas(tmp_path):
    status, report, _ = run(tmp_path, CableBus(), route_frames=False)

    assert status == 1
    assert report["host_result"]["passed"] is True
    assert report["hardware_evidence"]["passed"] is False
    assert report["passed"] is False
    assert report["hardware_evidence"]["route_assertions"]["sfp0_tx_frames"]["passed"] is False


def test_counter_error_or_drop_delta_fails_an_otherwise_clean_host_run(tmp_path):
    bus = CableBus()
    transport = CableTransport(bus)
    original_send = transport.send

    def send_with_rx_error(frame):
        original_send(frame)
        bus.regs.host_mac_rx_datapath_crc_errors.value += 1

    transport.send = send_with_rx_error
    usb_interface(tmp_path)
    report_path = tmp_path / "cable.json"
    status = cable_host.main(
        ["--interface", "enxtest", "--direction", "sfp1-to-sfp2",
         "--run-id", "42", "--report", str(report_path)],
        interface_root=tmp_path, bus_factory=lambda *args, **kwargs: bus,
        transport_factory=lambda name: transport,
        phy_status_reader=good_phy_status,
    )
    report = json.loads(report_path.read_text())

    assert status == 1
    assert report["host_result"]["passed"] is True
    assert report["hardware_evidence"]["passed"] is False
    assert report["hardware_evidence"]["error_deltas"]["host_mac_rx_datapath_crc_errors"] == 1000


def test_frame_rejections_are_reported_without_becoming_receive_errors(tmp_path):
    bus = CableBus()
    transport = CableTransport(bus)
    original_send = transport.send

    def send_with_unrelated_rejection(frame):
        original_send(frame)
        bus.regs.host_ingress_rejected_frames.value += 1

    transport.send = send_with_unrelated_rejection
    usb_interface(tmp_path)
    report_path = tmp_path / "cable.json"
    status = cable_host.main(
        ["--interface", "enxtest", "--direction", "sfp1-to-sfp2",
         "--run-id", "42", "--report", str(report_path)],
        interface_root=tmp_path, bus_factory=lambda *args, **kwargs: bus,
        transport_factory=lambda name: transport,
        phy_status_reader=good_phy_status,
    )
    report = json.loads(report_path.read_text())

    assert status == 0
    assert report["hardware_evidence"]["rejection_deltas"]["host_ingress_rejected_frames"] == 1000
    assert report["hardware_evidence"]["error_deltas"]["host_ingress_error_frames"] == 0


def test_unexpected_sfp_test_frame_fails_otherwise_valid_host_and_route_evidence(tmp_path):
    bus = CableBus()
    bus.regs.sfp0_unexpected_test_frames = Register()
    transport = CableTransport(bus)
    original_send = transport.send

    def send_with_unexpected_sfp_frame(frame):
        original_send(frame)
        bus.regs.sfp0_unexpected_test_frames.value += 1

    transport.send = send_with_unexpected_sfp_frame
    usb_interface(tmp_path)
    report_path = tmp_path / "cable.json"
    status = cable_host.main(
        ["--interface", "enxtest", "--direction", "sfp1-to-sfp2",
         "--run-id", "42", "--report", str(report_path)],
        interface_root=tmp_path, bus_factory=lambda *args, **kwargs: bus,
        transport_factory=lambda name: transport, phy_status_reader=good_phy_status,
    )
    report = json.loads(report_path.read_text())

    assert status == 1
    assert report["host_result"]["passed"] is True
    assert all(item["passed"] for item in report["hardware_evidence"]["route_assertions"].values())
    assert report["hardware_evidence"]["error_deltas"]["sfp0_unexpected_test_frames"] == 1000
    assert report["hardware_evidence"]["passed"] is False


def test_clause22_mdio_reads_one_finite_reply_and_rejects_invalid_addresses():
    class Writer:
        def __init__(self):
            self.values = []

        def write(self, value):
            self.values.append(value)

        def read(self):
            return self.values[-1]

    class Reader:
        def __init__(self, bits):
            self.bits = iter(bits)

        def read(self):
            return next(self.bits)

    value = 0xBEEF
    bits = [1, 0] + [(value >> shift) & 1 for shift in range(15, -1, -1)]
    writer = Writer()
    mdio = cable_diagnostics.Clause22MDIO(
        type("Regs", (), {"host_phy_mdio_w": writer, "host_phy_mdio_r": Reader(bits)})()
    )

    assert mdio.read(1, 2) == value
    assert len(writer.values) <= 240
    try:
        mdio.read(32, 2)
    except ValueError as error:
        assert "0..31" in str(error)
    else:
        raise AssertionError("Invalid PHY address was accepted")


def test_clause22_mdio_samples_phy_before_rising_edge_advances_data():
    class DeferredGPIO:
        def __init__(self):
            self.value = 0
            self.clock = 0
            self.output_enable = 1
            self.response_index = 0

        def write(self, value):
            self.value = value
            new_clock = value & 1
            self.output_enable = (value >> 1) & 1
            if not self.output_enable and not self.clock and new_clock:
                self.response_index += 1
            self.clock = new_clock

        def read(self):
            return self.value

    class PHYInput:
        def __init__(self, gpio):
            self.gpio = gpio
            self.response_bits = [1, 0] + [
                (0x001C >> shift) & 1 for shift in range(15, -1, -1)
            ]
            self.sample_phases = []

        def read(self):
            self.sample_phases.append((self.gpio.clock, self.gpio.output_enable))
            if self.gpio.output_enable:
                return 1
            if self.gpio.response_index >= len(self.response_bits):
                return 1
            return self.response_bits[self.gpio.response_index]

    gpio = DeferredGPIO()
    phy_input = PHYInput(gpio)
    mdio = cable_diagnostics.Clause22MDIO(
        type("Regs", (), {
            "host_phy_mdio_w": gpio,
            "host_phy_mdio_r": phy_input,
        })()
    )

    assert mdio.read(1, 2) == 0x001C
    assert len(phy_input.sample_phases) == 18
    assert all(phase == (0, 0) for phase in phy_input.sample_phases)


def test_rtl8211f_delay_configuration_preserves_other_bits_and_restores_page():
    class PHY:
        def __init__(self):
            self.page = 0x0007
            self.pages = {
                0x0007: {2: 0x001C, 3: 0xC916, 0: 0x1140, 1: 0x796D},
                0x0D08: {0x11: 0x814A, 0x15: 0x80A3},
            }

        def read(self, address, register):
            assert address == 1
            if register == 0x1F:
                return self.page
            return self.pages.setdefault(self.page, {}).get(register, 0)

        def write(self, address, register, value):
            assert address == 1
            if register == 0x1F:
                self.page = value
            else:
                self.pages.setdefault(self.page, {})[register] = value

    phy = PHY()

    status = cable_diagnostics.read_rtl8211f_status(phy)
    assert status["phy_id"] == "001cc916"
    assert status["bmcr"] == "1140"
    assert status["bmsr"] == "796d"
    assert status["phy_tx_delay_enabled"] is True
    assert status["phy_rx_delay_enabled"] is False
    assert status["delay_configuration_passed"] is False
    assert phy.page == 0x0007

    result = cable_diagnostics.configure_rtl8211f_delays(phy)

    assert result["tx_register_before"] == "814a"
    assert result["tx_register_after"] == "804a"
    assert result["rx_register_before"] == "80a3"
    assert result["rx_register_after"] == "80ab"
    assert result["phy_tx_delay_enabled"] is False
    assert result["phy_rx_delay_enabled"] is True
    assert phy.page == 0x0007


def test_rtl8211f_status_and_configuration_verify_page_restoration():
    class PHY:
        def __init__(self, *, fail_select=False, fail_restore=False):
            self.page = 0x0007
            self.fail_select = fail_select
            self.fail_restore = fail_restore
            self.pages = {
                0x0007: {2: 0x001C, 3: 0xC916, 0: 0x1140, 1: 0x796D},
                0x0D08: {0x11: 0x814A, 0x15: 0x80A3},
            }

        def read(self, address, register):
            if register == cable_diagnostics.RTL8211F_PAGE_REGISTER:
                return self.page
            return self.pages.setdefault(self.page, {}).get(register, 0)

        def write(self, address, register, value):
            if register == cable_diagnostics.RTL8211F_PAGE_REGISTER:
                if value == cable_diagnostics.RTL8211F_RGMII_PAGE and self.fail_select:
                    return
                if value == 0x0007 and self.fail_restore:
                    return
                self.page = value
                return
            self.pages.setdefault(self.page, {})[register] = value

    for fail_select, fail_restore, expected_error in (
        (False, True, "PHY page restore failed"),
        (True, False, "PHY page select failed"),
    ):
        for operation in (
            cable_diagnostics.read_rtl8211f_status,
            cable_diagnostics.configure_rtl8211f_delays,
        ):
            phy = PHY(fail_select=fail_select, fail_restore=fail_restore)
            try:
                operation(phy)
            except RuntimeError as error:
                assert expected_error in str(error)
            else:
                raise AssertionError("A PHY page selection/restoration failure was accepted")


def test_phy_diagnostics_do_not_report_pass_when_link_or_delay_health_fails(tmp_path, monkeypatch):
    class CSR:
        def read(self):
            return 0

    class UART:
        def __init__(self, *args, **kwargs):
            self.regs = type("Regs", (), {
                **{name: CSR() for name in STATUS_CSR_NAMES},
                "host_phy_mdio_w": object(),
                "host_phy_mdio_r": object(),
            })()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    report_path = tmp_path / "status.json"
    monkeypatch.setattr("sys.argv", ["cable_diagnostics", "--phy", "--output", str(report_path)])
    result = cable_diagnostics.main(
        uart_factory=UART,
        phy_status_reader=lambda mdio: {
            "link_up": False, "delay_configuration_passed": False,
        },
    )

    report = json.loads(report_path.read_text())
    assert result == 1
    assert report["passed"] is False
