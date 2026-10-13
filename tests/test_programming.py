"""Programming reports retain external hardware failures."""

import json

import pytest

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


@pytest.mark.parametrize("image_basename,profile", [("cable_loopback", "cable"), ("dual_sfp_bridge", "bridge")])
def test_load_selects_custom_image_and_matching_status_reader(tmp_path, monkeypatch, image_basename, profile):
    image = tmp_path / "gateware" / (image_basename + ".bit")
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
                                    "--image-basename", image_basename,
                                    "--status-profile", profile, "--report", str(report)])
    monkeypatch.setattr(program.shutil, "which", lambda name: "/usr/bin/vivado")
    monkeypatch.setattr(program, "BoundedUART", UART)
    monkeypatch.setattr(program, "read_cable_status", lambda bus: {"profile": "cable"})
    monkeypatch.setattr(program, "read_bridge_status", lambda bus: {"profile": "bridge"}, raising=False)
    monkeypatch.setattr(program.subprocess, "run", lambda command, **kwargs: (
        calls.update(command=command) or VivadoResult()
    ))

    assert program.main() == 0
    evidence = json.loads(report.read_text())
    assert evidence["image"] == str(image.resolve())
    assert evidence["status_after_boot"] == {"profile": profile}
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


@pytest.mark.parametrize("operation", ["--flash", "--boot"])
@pytest.mark.parametrize("profile", ["bridge", "sfp2", "cable"])
def test_bridge_profile_rejects_persistent_operations_before_hardware_access(monkeypatch, operation, profile, tmp_path, capsys):
    image = tmp_path / "gateware" / "dual_sfp_bridge.bin"
    image.parent.mkdir()
    image.write_bytes(b"image")
    monkeypatch.setattr("sys.argv", ["program", operation, "--status-profile", profile,
                                    "--output-dir", str(tmp_path), "--image-basename", "dual_sfp_bridge"])
    monkeypatch.setattr(program.shutil, "which", lambda name: "/usr/bin/vivado")

    def hardware_access(*args, **kwargs):
        raise AssertionError("SRAM-only bridge operations must fail before hardware access")

    monkeypatch.setattr(program, "BoundedUART", hardware_access)
    monkeypatch.setattr(program.subprocess, "run", hardware_access)
    with pytest.raises(SystemExit) as error:
        program.main()
    assert error.value.code == 2
    assert "supports SRAM loading" in capsys.readouterr().err
