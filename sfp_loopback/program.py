"""Load, flash, or boot a generated image using the connected Digilent cable."""

import argparse
import csv
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from sfp_loopback.cable_diagnostics import read_cable_status
from sfp_loopback.bridge_diagnostics import read_bridge_status
from sfp_loopback.diagnostics import BoundedUART, read_flash_id, read_status


# Verified against Vivado 2025.2 data/xicom/spi.cfg and table.csv.
FLASH_PARTS = {
    "20ba18": "mt25ql128-spi-x1_x2_x4",
    "20bb18": "mt25ql128-spi-x1_x2_x4",
    "ef4018": "w25q128jvq-spi-x1_x2_x4",
}
FLASH_ID_CSR_NAMES = (
    "spiflash_spi_cs",
    "spiflash_spi_mosi",
    "spiflash_spi_control",
    "spiflash_spi_status",
    "spiflash_spi_miso",
)


def validate_flash_id_csr_csv(path: Path) -> None:
    """Require SPI registers for the read-only JEDEC command before UART access."""
    if not path.is_file():
        raise ValueError(
            f"SPI flash identity CSR map not found: {path}; "
            "pass --flash-id-csr-csv with a map from an image that exposes SPI CSRs."
        )
    with path.open(newline="", encoding="utf-8") as stream:
        names = {
            row[1]
            for row in csv.reader(stream)
            if len(row) > 1 and row[0] == "csr_register"
        }
    missing = [name for name in FLASH_ID_CSR_NAMES if name not in names]
    if missing:
        raise ValueError(
            f"SPI flash identity CSR map {path} is missing required registers: "
            f"{', '.join(missing)}; the bridge image has no SPI CSRs, so pass "
            "--flash-id-csr-csv from a separately loaded helper image."
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--load", action="store_true", help="Load volatile SRAM to read identity/test the image.")
    mode.add_argument("--flash", action="store_true", help="Identify flash, program, verify, and boot its image.")
    mode.add_argument("--boot", action="store_true", help="Boot the existing SPI image without reprogramming.")
    parser.add_argument("--output-dir", type=Path, default=Path("build/sfp2"))
    parser.add_argument("--image-basename", default="sfp2_loopback",
                        help="Gateware image basename (default: sfp2_loopback)")
    parser.add_argument("--status-profile", choices=("sfp2", "cable", "bridge"), default="sfp2",
                        help="UART status reader matching the loaded target")
    parser.add_argument("--flash-id-csr-csv", type=Path,
                        help="CSR map for the currently loaded image's SPI flash ID reader (flash only)")
    parser.add_argument("--port", default="/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0")
    parser.add_argument("--target", default="localhost:3121/xilinx_tcf/Digilent/210357A7D00EA")
    parser.add_argument("--report", type=Path, default=Path("artifacts/programming.json"))
    args = parser.parse_args()
    if args.image_basename == "dual_sfp_bridge" and args.status_profile != "bridge":
        parser.error("The dual_sfp_bridge image requires --status-profile bridge.")
    if args.flash_id_csr_csv is not None and not args.flash:
        parser.error("--flash-id-csr-csv is only valid with --flash.")
    vivado = shutil.which("vivado")
    if vivado is None:
        parser.error("vivado must be on PATH")
    operation = "load" if args.load else "flash" if args.flash else "boot"
    if not args.image_basename or Path(args.image_basename).name != args.image_basename or args.image_basename in (".", ".."):
        parser.error("--image-basename must be a filename without a directory")
    image = args.output_dir / "gateware" / (args.image_basename + (".bin" if args.flash else ".bit"))
    if not args.boot and not image.is_file():
        parser.error(f"Build the image first: {image}")
    report = {"operation": operation, "image": str(image.resolve()), "target": args.target}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    log = args.report.with_suffix(".log")
    report["passed"] = False
    try:
        flash_part = ""
        if args.flash:
            flash_id_csr_csv = args.flash_id_csr_csv or (args.output_dir / "csr.csv")
            validate_flash_id_csr_csv(flash_id_csr_csv)
            report["flash_id_csr_csv"] = str(flash_id_csr_csv.resolve())
            with BoundedUART(args.port, csr_csv=str(flash_id_csr_csv)) as bus:
                flash_id = read_flash_id(bus)
            report["flash_jedec_id"] = flash_id
            if flash_id not in FLASH_PARTS:
                raise RuntimeError(f"Unsupported flash ID {flash_id}; no flash operation performed.")
            flash_part = FLASH_PARTS[flash_id]
            report["flash_part"] = flash_part
        if not args.boot:
            report["image_sha256"] = hashlib.sha256(image.read_bytes()).hexdigest()
        script = Path(__file__).resolve().parent.parent / "scripts/program.tcl"
        with log.open("w") as stream:
            result = subprocess.run([vivado, "-mode", "batch", "-nolog", "-nojournal",
                "-source", str(script), "-tclargs", operation, str(image.resolve()),
                args.target, flash_part], stdout=stream, stderr=subprocess.STDOUT)
        report["vivado_returncode"] = result.returncode
        if result.returncode == 0:
            with BoundedUART(args.port, csr_csv=str(args.output_dir / "csr.csv")) as bus:
                status_reader = {
                    "sfp2": read_status,
                    "cable": read_cable_status,
                    "bridge": read_bridge_status,
                }[args.status_profile]
                report["status_after_boot"] = status_reader(bus)
            report["passed"] = True
    except (OSError, RuntimeError, ValueError, AttributeError) as error:
        report["error"] = f"{type(error).__name__}: {error}"
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print(f"Vivado log: {log}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
