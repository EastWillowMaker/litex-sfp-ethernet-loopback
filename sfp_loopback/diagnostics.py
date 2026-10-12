"""Read loopback status and SPI flash identity over the CH340 UART bridge."""

import argparse
import json
import time

from litex.tools.remote.comm_uart import CommUART


class BoundedUART(CommUART):
    """Fail clearly if UART gateware is absent instead of waiting indefinitely."""

    def __init__(self, port, **kwargs):
        super().__init__(port, **kwargs)
        self.port.timeout = 2
        self.port.write_timeout = 2

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()

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


def _spi_transfer(bus, value, length, chip_select):
    bus.regs.spiflash_spi_cs.write(chip_select)
    bus.regs.spiflash_spi_mosi.write(value)
    bus.regs.spiflash_spi_control.write((length << 8) | 1)
    deadline = time.monotonic() + 2.0
    while True:
        status = bus.regs.spiflash_spi_status.read()
        if status & 4:
            raise RuntimeError("SPI controller rejected the read-ID transfer.")
        if status & 1:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("SPI read-ID did not finish.")
    return bus.regs.spiflash_spi_miso.read() & 0xFFFFFF


def read_flash_id(bus):
    """Issue only JEDEC read-ID (0x9f); never send write/erase commands."""
    # STARTUPE2 consumes initial clock edges when taking over configuration CCLK.
    # Prime it with CS inactive so the first opcode reaches the flash intact.
    _spi_transfer(bus, 0, 8, chip_select=0)
    # The raw 40-bit SPI shifter starts at bit39; send opcode then24 dummy bits.
    identity = _spi_transfer(bus, 0x9F << 32, 32, chip_select=1)
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
    with BoundedUART(args.port, csr_csv=args.csr_csv) as bus:
        result = read_status(bus)
        if args.flash_id:
            result["flash_jedec_id"] = read_flash_id(bus)
        report = json.dumps(result, indent=2)
        print(report)
        if args.output:
            from pathlib import Path
            Path(args.output).write_text(report + "\n")


if __name__ == "__main__":
    main()
