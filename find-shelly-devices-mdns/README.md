# Shelly Device Discovery Script

## Overview

This script scans the network for Shelly devices using **mDNS (Multicast DNS)** and displays their details in a user-friendly table format. It listens for `_shelly._tcp.local.` services and extracts relevant information, such as device name, version, generation, and IP addresses. The results are presented in a structured way using the `rich` library for better readability.

## Features

- **Automatic Discovery**: Finds Shelly devices using mDNS without requiring manual IP configuration.
- **Rich Table Display**: Results are displayed in a color-coded table.
- **Sorting**: Sort results by device name, version, generation, service name, or server.
- **Progress Indicator**: Displays a progress bar while scanning.
- **Command-line Customization**: Users can specify scan duration and sorting preferences.

## Prerequisites

Ensure you have Python 3.6+ installed. The script requires the following dependencies:

```bash
pip install -r requirements.txt
```

## Usage

Run the script with default settings (10-second scan):

```bash
python find-shelly-mdns-devices.py
```

### Optional Arguments

- `--scan-time SECONDS` → Set the duration of the scan (default: 10 seconds).
- `--sort SORT_BY` → Sort results by **gen**, **version**, **device**, **service**, or **server**.

#### Example

Scan for 15 seconds and sort results by device name:

```bash
python find-shelly-mdns-devices.py --scan-time 15 --sort device
```

## Output

The script outputs a structured table with the following columns:

- **Service Name**: The discovered service name.
- **Server**: The server hosting the Shelly service.
- **IP Address(es)**: The device’s IP address.
- **Port**: The port number for the service.
- **Device Name**: Extracted from the `app` property.
- **Version**: Firmware version of the device.
- **Generation**: Shelly device generation (if available).
- **Other Properties**: Additional TXT record properties.

Example output:

```bash
Scanning for Shelly devices for 10 seconds...

Scan complete. Discovered Shelly devices:

+---------------+------------------+---------------+------+-------------+---------+-----+----------------+
| Service Name  | Server           | IP Address(es)| Port | Device Name | Version | Gen | Other Properties |
+---------------+------------------+---------------+------+-------------+---------+-----+----------------+
| shelly1-12345| shelly1.local.    | 192.168.1.10  | 80   | Shelly 1    | 1.9.3   | 2   | mode: relay    |
+---------------+------------------+---------------+------+-------------+---------+-----+----------------+

Total Devices Discovered: 1
```

## Troubleshooting & Tips

### Check Multicast Traffic

- Some routers or firewalls block UDP port 5353 (used by mDNS). Ensure your firewall and network settings allow multicast traffic.

### Same Network Segment

- Make sure the resolver (your computer or script) and the Shelly devices are on the same Layer‑2 subnet. Multicast packets typically won’t traverse VPNs or network segments unless specifically configured.

### Test Other Tools

- If the Python script can’t find devices or you want to test with other tools, try dns-sd (macOS) or avahi-browse (Linux) to confirm that Shelly devices are actually announcing themselves via mDNS.

### Give It Time

- Shelly devices may take a few seconds to respond. If you suspect something’s missing, scan for a longer duration (e.g., 15–30 seconds).
