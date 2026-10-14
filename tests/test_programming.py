"""Programming reports retain external hardware failures."""

import json

import pytest

from sfp_loopback import program


FLASH_ID_CSR_NAMES = (
    "spiflash_spi_cs",
    "spiflash_spi_mosi",
    "spiflash_spi_control",
    "spiflash_spi_status",
    "spiflash_spi_miso",
)


def write_flash_id_csr_csv(path):
    path.write_text("".join(f"csr_register,{name},0x0,1,rw\n" for name in FLASH_ID_CSR_NAMES))
    return path


def test_flash_identification_failure_saves_failed_report_without_programming(tmp_path, monkeypatch):
    image = tmp_path / "gateware/sfp2_loopback.bin"
    image.parent.mkdir()
    image.write_bytes(b"image")
    write_flash_id_csr_csv(tmp_path / "csr.csv")
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


def test_bridge_flash_uses_helper_for_identity_and_target_map_for_status(tmp_path, monkeypatch):
    output_dir = tmp_path / "bridge500"
    image = output_dir / "gateware" / "dual_sfp_bridge.bin"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"qualified bridge image")
    target_csr_csv = output_dir / "csr.csv"
    target_csr_csv.write_text("csr_base,bridge_status,0x0,,\n")
    helper_csr_csv = write_flash_id_csr_csv(tmp_path / "cable-helper.csv")
    report = tmp_path / "programming.json"
    calls = {"uart": [], "vivado": []}

    class UART:
        def __init__(self, *args, **kwargs):
            calls["uart"].append(kwargs)

        def __enter__(self):
            return object()

        def __exit__(self, *args):
            pass

    class VivadoResult:
        returncode = 0

    monkeypatch.setattr(
        "sys.argv",
        ["program", "--flash", "--output-dir", str(output_dir),
         "--image-basename", "dual_sfp_bridge", "--status-profile", "bridge",
         "--flash-id-csr-csv", str(helper_csr_csv), "--report", str(report)],
    )
    monkeypatch.setattr(program.shutil, "which", lambda name: "/usr/bin/vivado")
    monkeypatch.setattr(program, "BoundedUART", UART)
    monkeypatch.setattr(program, "read_flash_id", lambda bus: "20ba18")
    monkeypatch.setattr(program, "read_bridge_status", lambda bus: {"profile": "bridge"})
    monkeypatch.setattr(
        program.subprocess,
        "run",
        lambda command, **kwargs: calls["vivado"].append(command) or VivadoResult(),
    )

    assert program.main() == 0

    evidence = json.loads(report.read_text())
    assert evidence["passed"] is True
    assert evidence["flash_jedec_id"] == "20ba18"
    assert evidence["status_after_boot"] == {"profile": "bridge"}
    assert calls["uart"] == [
        {"csr_csv": str(helper_csr_csv)},
        {"csr_csv": str(target_csr_csv)},
    ]
    assert calls["vivado"][0][-4:] == ["flash", str(image.resolve()),
                                       "localhost:3121/xilinx_tcf/Digilent/210357A7D00EA",
                                       program.FLASH_PARTS["20ba18"]]


@pytest.mark.parametrize("image_basename", ["dual_sfp_bridge", "custom_bridge500"])
def test_bridge_boot_uses_target_status_without_spi_identity_csrs(tmp_path, monkeypatch, image_basename):
    output_dir = tmp_path / "bridge500"
    target_csr_csv = output_dir / "csr.csv"
    target_csr_csv.parent.mkdir()
    target_csr_csv.write_text("csr_base,bridge_status,0x0,,\n")
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

    monkeypatch.setattr("sys.argv", ["program", "--boot", "--output-dir", str(output_dir),
                                    "--image-basename", image_basename,
                                    "--status-profile", "bridge", "--report", str(report)])
    monkeypatch.setattr(program.shutil, "which", lambda name: "/usr/bin/vivado")
    monkeypatch.setattr(program, "BoundedUART", UART)
    monkeypatch.setattr(program, "read_flash_id", lambda bus: (_ for _ in ()).throw(
        AssertionError("Boot must not read the SPI identity helper map")
    ))
    monkeypatch.setattr(program, "read_bridge_status", lambda bus: {"profile": "bridge"})
    monkeypatch.setattr(program.subprocess, "run", lambda command, **kwargs: (
        calls.update(command=command) or VivadoResult()
    ))

    assert program.main() == 0

    evidence = json.loads(report.read_text())
    assert evidence["passed"] is True
    assert evidence["status_after_boot"] == {"profile": "bridge"}
    assert calls["uart"] == {"csr_csv": str(target_csr_csv)}
    assert calls["command"][-4] == "boot"


@pytest.mark.parametrize("operation", ["--load", "--flash", "--boot"])
@pytest.mark.parametrize("profile", ["sfp2", "cable"])
def test_canonical_bridge_image_rejects_wrong_status_profile_before_hardware(
    tmp_path, monkeypatch, operation, profile, capsys
):
    output_dir = tmp_path / "bridge500"
    image = output_dir / "gateware" / f"dual_sfp_bridge.{'bin' if operation == '--flash' else 'bit'}"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"qualified bridge image")
    report = tmp_path / "programming.json"

    monkeypatch.setattr(
        "sys.argv",
        ["program", operation, "--output-dir", str(output_dir),
         "--image-basename", "dual_sfp_bridge", "--status-profile", profile,
         "--report", str(report)],
    )
    monkeypatch.setattr(program.shutil, "which", lambda name: "/usr/bin/vivado")

    def hardware_access(*args, **kwargs):
        raise AssertionError("Mismatched image and status profile must fail before hardware access")

    monkeypatch.setattr(program, "BoundedUART", hardware_access)
    monkeypatch.setattr(program.subprocess, "run", hardware_access)

    with pytest.raises(SystemExit) as error:
        program.main()

    assert error.value.code == 2
    assert "requires --status-profile bridge" in capsys.readouterr().err


def test_bridge_flash_without_spi_helper_fails_before_uart_or_vivado(tmp_path, monkeypatch):
    output_dir = tmp_path / "bridge500"
    image = output_dir / "gateware" / "dual_sfp_bridge.bin"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"qualified bridge image")
    (output_dir / "csr.csv").write_text("csr_base,bridge_status,0x0,,\n")
    report = tmp_path / "programming.json"

    monkeypatch.setattr("sys.argv", ["program", "--flash", "--output-dir", str(output_dir),
                                    "--image-basename", "dual_sfp_bridge",
                                    "--status-profile", "bridge", "--report", str(report)])
    monkeypatch.setattr(program.shutil, "which", lambda name: "/usr/bin/vivado")

    def hardware_access(*args, **kwargs):
        raise AssertionError("SPI CSR validation must precede UART and Vivado access")

    monkeypatch.setattr(program, "BoundedUART", hardware_access)
    monkeypatch.setattr(program.subprocess, "run", hardware_access)

    assert program.main() == 1

    evidence = json.loads(report.read_text())
    assert evidence["passed"] is False
    assert "--flash-id-csr-csv" in evidence["error"]
    assert "spiflash_spi_control" in evidence["error"]


@pytest.mark.parametrize(
    ("flash_id", "read_error", "expected_error"),
    [
        ("123456", None, "Unsupported flash ID 123456"),
        (None, "Invalid flash response 000000; do not program flash.", "Invalid flash response 000000"),
    ],
)
def test_bridge_flash_unknown_or_invalid_jedec_never_calls_vivado(
    tmp_path, monkeypatch, flash_id, read_error, expected_error
):
    output_dir = tmp_path / "bridge500"
    image = output_dir / "gateware" / "dual_sfp_bridge.bin"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"qualified bridge image")
    (output_dir / "csr.csv").write_text("csr_base,bridge_status,0x0,,\n")
    helper_csr_csv = write_flash_id_csr_csv(tmp_path / "cable-helper.csv")
    report = tmp_path / "programming.json"

    class UART:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return object()

        def __exit__(self, *args):
            pass

    def read_identity(bus):
        if read_error is not None:
            raise RuntimeError(read_error)
        return flash_id

    monkeypatch.setattr("sys.argv", ["program", "--flash", "--output-dir", str(output_dir),
                                    "--image-basename", "dual_sfp_bridge",
                                    "--status-profile", "bridge", "--flash-id-csr-csv",
                                    str(helper_csr_csv), "--report", str(report)])
    monkeypatch.setattr(program.shutil, "which", lambda name: "/usr/bin/vivado")
    monkeypatch.setattr(program, "BoundedUART", UART)
    monkeypatch.setattr(program, "read_flash_id", read_identity)
    monkeypatch.setattr(program.subprocess, "run", lambda *args, **kwargs: pytest.fail(
        "Vivado must not run when flash identity is unknown or invalid"
    ))

    assert program.main() == 1

    evidence = json.loads(report.read_text())
    assert evidence["passed"] is False
    assert expected_error in evidence["error"]
