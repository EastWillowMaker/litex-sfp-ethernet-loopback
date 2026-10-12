"""Load or flash the generated SFP2 image using the connected Digilent cable."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from sfp_loopback.diagnostics import BoundedUART, read_flash_id, read_status


# Verified against Vivado 2025.2 data/xicom/spi.cfg and table.csv.
FLASH_PARTS = {
    "20ba18": "mt25ql128-spi-x1_x2_x4",
    "20bb18": "mt25ql128-spi-x1_x2_x4",
    "ef4018": "w25q128jvq-spi-x1_x2_x4",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--load", action="store_true", help="Load volatile SRAM to read identity/test the image.")
    mode.add_argument("--flash", action="store_true", help="Identify flash, program, verify, and boot its image.")
    mode.add_argument("--boot", action="store_true", help="Boot the existing SPI image without reprogramming.")
    parser.add_argument("--output-dir", type=Path, default=Path("build/sfp2"))
    parser.add_argument("--port", default="/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0")
    parser.add_argument("--target", default="localhost:3121/xilinx_tcf/Digilent/210357A7D00EA")
    parser.add_argument("--report", type=Path, default=Path("artifacts/programming.json"))
    args = parser.parse_args()
    vivado = shutil.which("vivado")
    if vivado is None:
        parser.error("vivado must be on PATH")
    operation = "load" if args.load else "flash" if args.flash else "boot"
    image = args.output_dir / "gateware" / ("sfp2_loopback.bin" if args.flash else "sfp2_loopback.bit")
    if not args.boot and not image.is_file():
        parser.error(f"Build the image first: {image}")
    report = {"operation": operation, "image": str(image.resolve()), "target": args.target}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    log = args.report.with_suffix(".log")
    report["passed"] = False
    try:
        flash_part = ""
        if args.flash:
            with BoundedUART(args.port, csr_csv=str(args.output_dir / "csr.csv")) as bus:
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
                report["status_after_boot"] = read_status(bus)
            report["passed"] = True
    except (OSError, RuntimeError, ValueError) as error:
        report["error"] = f"{type(error).__name__}: {error}"
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print(f"Vivado log: {log}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
