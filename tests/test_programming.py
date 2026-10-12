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
