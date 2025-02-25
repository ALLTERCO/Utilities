#!/usr/bin/env python3
"""
mDNS Shelly Device Discovery Script

This script listens for mDNS services of type "_shelly._tcp.local." for a specified duration
(default: 10 seconds), extracts available information—including key TXT record properties like
Device Name (from the "app" property), Version ("ver"), Generation ("gen"), etc.—and displays the
results in a colorful table using the Rich library.

It also supports sorting the results (by generation, version, device name, service name, or server)
and prints the total count of discovered devices at the bottom.

Usage:
    python find-shelly-mdns-devices.py [--scan-time SECONDS] [--sort SORT_BY]

Requirements:
    - Python 3.6+
    - zeroconf: pip install zeroconf
    - rich: pip install rich
"""

import time
import socket
import argparse
import logging
from zeroconf import Zeroconf, ServiceBrowser, ServiceListener
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, BarColumn, TimeRemainingColumn, TextColumn

# Configure logging (set to WARNING to avoid clutter during scan)
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

class ShellyListener(ServiceListener):
    """
    Custom ServiceListener to collect mDNS information for Shelly devices.

    It gathers:
      - Service name and server.
      - IP addresses and port.
      - TXT record properties (e.g., "app" for Device Name, "ver" for Version, "gen" for Generation,
        plus any additional properties).
    """
    def __init__(self):
        self.devices = []  # List to store details for each discovered device

    def add_service(self, zeroconf, service_type, name):
        """
        Called when a new service is discovered.
        Attempts to retrieve the full service info and store it.
        """
        try:
            info = zeroconf.get_service_info(service_type, name)
            if info is None:
                logging.debug("No service info for %s", name)
                return

            # Create a dictionary with device details
            device = {
                "name": name,
                "server": info.server,
                "addresses": [],
                "port": info.port,
                "properties": {},
                "type": service_type,
            }

            # Process IP addresses (convert from bytes to string)
            for address in info.addresses:
                try:
                    addr_str = socket.inet_ntoa(address)
                except Exception as e:
                    logging.error("Error converting address %s: %s", address, e)
                    addr_str = str(address)
                device["addresses"].append(addr_str)

            # Process TXT record properties (convert bytes to string)
            for key, value in info.properties.items():
                key_str = key.decode("utf-8") if isinstance(key, bytes) else key
                value_str = value.decode("utf-8") if isinstance(value, bytes) else value
                device["properties"][key_str] = value_str

            self.devices.append(device)
            # Do not print output during scan; final results will be shown in the table.
        except Exception as ex:
            logging.error("Error processing service %s: %s", name, ex)

    def update_service(self, zeroconf, service_type, name):
        """
        Called when an already discovered service is updated.
        Devices may reannounce or change TXT records, triggering this callback.
        """
        logging.debug("Service updated: %s", name)

    def remove_service(self, zeroconf, service_type, name):
        """
        Called when a service is removed. The device is then removed from the internal list.
        """
        self.devices = [d for d in self.devices if d["name"] != name]
        logging.debug("Service removed: %s", name)

def main(scan_time: int, sort_by: str):
    """
    Main function to perform mDNS discovery for Shelly devices, sort the results, and display them.

    Parameters:
        scan_time (int): Duration in seconds for scanning.
        sort_by (str): Field to sort by. Options: 'gen', 'version', 'device', 'service', 'server'.
    """
    console = Console()
    zeroconf = Zeroconf()

    # Instantiate our custom ShellyListener and start browsing for Shelly devices.
    listener = ShellyListener()
    service_type = "_shelly._tcp.local."
    ServiceBrowser(zeroconf, service_type, listener)

    console.print(f"[bold green]Scanning for Shelly devices for {scan_time} seconds...[/bold green]\n")

    # Use a Rich progress bar for the scan countdown.
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Scanning...", total=scan_time)
        while not progress.finished:
            time.sleep(1)
            progress.update(task, advance=1)

    console.print("\n[bold green]Scan complete. Discovered Shelly devices:[/bold green]\n")

    # Optionally sort the devices list based on the chosen sort field.
    if sort_by:
        def sort_key(device):
            props = device.get("properties", {})
            if sort_by == "gen":
                return props.get("gen", "")
            elif sort_by == "version":
                return props.get("ver", "")
            elif sort_by == "device":
                # Device Name is taken from the "app" property.
                return props.get("app", "")
            elif sort_by == "service":
                return device.get("name", "")
            elif sort_by == "server":
                return device.get("server", "")
            else:
                return device.get("name", "")
        listener.devices = sorted(listener.devices, key=sort_key)

    # Build a Rich table with the desired columns.
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Service Name", style="cyan", no_wrap=True)
    table.add_column("Server", style="yellow")
    table.add_column("IP Address(es)", style="green")
    table.add_column("Port", style="red")
    table.add_column("Device Name", style="blue")
    table.add_column("Version", style="blue")
    table.add_column("Gen", style="blue")
    table.add_column("Other Properties", style="white")

    # Process each discovered device.
    for device in listener.devices:
        addresses = ", ".join(device["addresses"]) if device["addresses"] else "N/A"
        props = device["properties"]
        # Use the 'app' property for Device Name.
        device_name = props.get("app", "N/A")
        version = props.get("ver", "N/A")
        gen = props.get("gen", "N/A")
        # Exclude 'app', 'ver', and 'gen' from other properties.
        other_props = {k: v for k, v in props.items() if k not in ("app", "ver", "gen")}
        other_str = "; ".join(f"{k}: {v}" for k, v in other_props.items()) if other_props else "N/A"

        table.add_row(
            device["name"],
            device["server"],
            addresses,
            str(device["port"]),
            device_name,
            version,
            gen,
            other_str
        )

    console.print(table)
    console.print(f"\n[bold green]Total Devices Discovered: {len(listener.devices)}[/bold green]\n")
    zeroconf.close()

if __name__ == '__main__':
    # Parse command-line arguments for scan duration and sort field.
    parser = argparse.ArgumentParser(
        description="Discover Shelly devices via mDNS and display their details."
    )
    parser.add_argument(
        "--scan-time", type=int, default=10,
        help="Duration (in seconds) to scan for mDNS services (default: 10 seconds)"
    )
    parser.add_argument(
        "--sort", type=str, default="",
        choices=["gen", "version", "device", "service", "server"],
        help="Sort results by: 'gen', 'version', 'device', 'service', or 'server'"
    )
    args = parser.parse_args()

    try:
        main(args.scan_time, args.sort)
    except KeyboardInterrupt:
        logging.info("Interrupted by user. Exiting...")
