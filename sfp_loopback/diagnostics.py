"""Read loopback status and SPI flash identity over the CH340 UART bridge."""

import argparse
import json
import time

from litex.tools.remote.comm_uart import CommUART


class BoundedUART(CommUART):
    """Fail clearly if UART gateware is absent instead of waiting indefinitely."""

    def _read(self, length):
        result = bytearray()
        while len(result) < length:
            chunk = self.port.read(length - len(result))
            if not chunk:
                raise TimeoutError("No UART bridge response; check the loaded image and serial port.")
            result.extend(chunk)
        return bytes(result)


def read_status(bus):
    names = ("link", "lock", "tx_ready", "rx_ready", "loss_of_signal", "rx_frames",
             "tx_frames", "rejected_frames", "error_frames", "overflow_frames")
    result = {name: getattr(bus.regs, "test_status_" + name).read() for name in names}
    for name in ("mac_rx_datapath_crc_errors", "mac_rx_datapath_preamble_errors", "mac_rx_datapath_drops"):
        if hasattr(bus.regs, name):
            result[name] = getattr(bus.regs, name).read()
    return result


def read_flash_id(bus):
    """Issue only JEDEC read-ID (0x9f); never send write/erase commands."""
    bus.regs.spiflash_spi_cs.write(1)
    # The raw 40-bit SPI shifter starts at bit39; send opcode then24 dummy bits.
    bus.regs.spiflash_spi_mosi.write(0x9F << 32)
    bus.regs.spiflash_spi_control.write((32 << 8) | 1)
    deadline = time.monotonic() + 2.0
    while True:
        status = bus.regs.spiflash_spi_status.read()
        if status & 4:
            raise RuntimeError("SPI controller rejected the read-ID transfer.")
        if status & 1:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("SPI read-ID did not finish.")
    identity = bus.regs.spiflash_spi_miso.read() & 0xFFFFFF
    if identity in (0, 0xFFFFFF):
        raise RuntimeError(f"Invalid flash response {identity:06x}; do not program flash.")
    return f"{identity:06x}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0")
    parser.add_argument("--csr-csv", default="build/sfp2/csr.csv")
    parser.add_argument("--flash-id", action="store_true")
    parser.add_argument("--output", help="Save the status as JSON.")
    args = parser.parse_args()
    bus = BoundedUART(args.port, csr_csv=args.csr_csv)
    bus.port.timeout = 2
    bus.port.write_timeout = 2
    try:
        result = read_status(bus)
        if args.flash_id:
            result["flash_jedec_id"] = read_flash_id(bus)
        report = json.dumps(result, indent=2)
        print(report)
        if args.output:
            from pathlib import Path
            Path(args.output).write_text(report + "\n")
    finally:
        bus.close()


if __name__ == "__main__":
    main()
