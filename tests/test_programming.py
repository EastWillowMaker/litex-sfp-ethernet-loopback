"""Programming reports retain external hardware failures."""

import json

from sfp_loopback import program


def test_flash_identification_failure_saves_failed_report_without_programming(tmp_path, monkeypatch):
    image = tmp_path / "gateware/sfp2_loopback.bin"
    image.parent.mkdir()
    image.write_bytes(b"image")
    report = tmp_path / "programming.json"
    monkeypatch.setattr("sys.argv", ["program", "--flash", "--output-dir", str(tmp_path),
                                    "--report", str(report)])
    monkeypatch.setattr(program.shutil, "which", lambda name: "/usr/bin/vivado")

    def unplugged_uart(*args, **kwargs):
        raise OSError("CH340 disconnected")

    def unexpected_programming(*args, **kwargs):
        raise AssertionError("Cannot program before identifying flash")

    monkeypatch.setattr(program, "BoundedUART", unplugged_uart)
    monkeypatch.setattr(program.subprocess, "run", unexpected_programming)
    assert program.main() == 1
    evidence = json.loads(report.read_text())
    assert evidence["passed"] is False
    assert evidence["error"] == "OSError: CH340 disconnected"
    assert "vivado_returncode" not in evidence


def test_load_selects_custom_image_and_cable_status_reader(tmp_path, monkeypatch):
    image = tmp_path / "gateware/cable_loopback.bit"
    image.parent.mkdir()
    image.write_bytes(b"image")
    report = tmp_path / "programming.json"
    calls = {}

    class UART:
        def __init__(self, *args, **kwargs):
            calls["uart"] = kwargs

        def __enter__(self):
            return object()

        def __exit__(self, *args):
            pass

    class VivadoResult:
        returncode = 0

    monkeypatch.setattr("sys.argv", ["program", "--load", "--output-dir", str(tmp_path),
                                    "--image-basename", "cable_loopback",
                                    "--status-profile", "cable", "--report", str(report)])
    monkeypatch.setattr(program.shutil, "which", lambda name: "/usr/bin/vivado")
    monkeypatch.setattr(program, "BoundedUART", UART)
    monkeypatch.setattr(program, "read_cable_status", lambda bus: {"profile": "cable"})
    monkeypatch.setattr(program.subprocess, "run", lambda command, **kwargs: (
        calls.update(command=command) or VivadoResult()
    ))

    assert program.main() == 0
    evidence = json.loads(report.read_text())
    assert evidence["image"] == str(image.resolve())
    assert evidence["status_after_boot"] == {"profile": "cable"}
    assert calls["uart"]["csr_csv"] == str(tmp_path / "csr.csv")
    assert str(image.resolve()) in calls["command"]


def test_existing_sfp2_defaults_still_select_sfp2_image_and_status(tmp_path, monkeypatch):
    report = tmp_path / "programming.json"
    calls = {}

    class UART:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return object()

        def __exit__(self, *args):
            pass

    class VivadoResult:
        returncode = 0

    monkeypatch.setattr("sys.argv", ["program", "--boot", "--report", str(report)])
    monkeypatch.setattr(program.shutil, "which", lambda name: "/usr/bin/vivado")
    monkeypatch.setattr(program, "BoundedUART", UART)
    monkeypatch.setattr(program, "read_status", lambda bus: {"profile": "sfp2"})
    monkeypatch.setattr(program, "read_cable_status", lambda bus: (_ for _ in ()).throw(
        AssertionError("Cable status reader must not be selected by default")
    ))
    monkeypatch.setattr(program.subprocess, "run", lambda command, **kwargs: (
        calls.update(command=command) or VivadoResult()
    ))

    assert program.main() == 0
    evidence = json.loads(report.read_text())
    assert evidence["image"].endswith("build/sfp2/gateware/sfp2_loopback.bit")
    assert evidence["status_after_boot"] == {"profile": "sfp2"}


def test_missing_cable_status_csr_keeps_programming_failure_artifact(tmp_path, monkeypatch):
    report = tmp_path / "programming.json"

    class UART:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return object()

        def __exit__(self, *args):
            pass

    class VivadoResult:
        returncode = 0

    monkeypatch.setattr("sys.argv", ["program", "--boot", "--status-profile", "cable",
                                    "--report", str(report)])
    monkeypatch.setattr(program.shutil, "which", lambda name: "/usr/bin/vivado")
    monkeypatch.setattr(program, "BoundedUART", UART)
    monkeypatch.setattr(program, "read_cable_status", lambda bus: (_ for _ in ()).throw(
        AttributeError("Missing cable_status_busy CSR")
    ))
    monkeypatch.setattr(program.subprocess, "run", lambda command, **kwargs: VivadoResult())

    assert program.main() == 1
    evidence = json.loads(report.read_text())
    assert evidence["passed"] is False
    assert evidence["error"] == "AttributeError: Missing cable_status_busy CSR"
