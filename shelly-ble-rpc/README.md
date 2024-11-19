# Shelly RPC BLE Client

## Overview

The Shelly RPC BLE Client is a Python-based tool designed to interact with Shelly devices via the Bluetooth Low Energy (BLE) protocol. It allows users to discover Shelly devices, execute Remote Procedure Calls (RPC), and configure various device settings.

## Features

- Scans and lists Shelly devices with BLE on.
- Executes RPC commands on Shelly devices.
- Supports dynamic parameter configuration for commands.
- Handles device-specific configurations such as Wi-Fi and Ethernet settings.
- Provides a robust logging system for debugging and error tracking.
- Graceful handling of BLE communication retries and timeouts.

## Prerequisites

Ensure you have Python 3.8 or higher installed on your system.

## Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/ALLTERCO/Utilities.git
   cd shelly-ble-rpc
   ```

2. Create a virtual environment:

   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:

    ```bash
    pip install -r requirements.txt
    ```

4. (Optional) Ensure jq is installed for JSON formatting:

   - On Ubuntu/Debian: `sudo apt-get install jq`
   - On macOS: `brew install jq`

## Usage

### Running the Script

Activate the virtual environment and run the script:

   ```bash
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   python3 shelly-ble-rpc.py
   ```

### Command-line Arguments

- **`--scan-duration`**
  
  Duration to scan for BLE devices (in seconds).  
  **Default:** `5` seconds.

- **`--log-level`**
  
  Sets the logging level.  
  **Choices:** `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`  
  **Default:** `INFO`.

- **`--wifi-ssid`**
  
  SSID for WiFi configuration.

- **`--wifi-password`**
  
  Password for WiFi configuration.

- **`--gateway`**
  
  Gateway to use for static IP configuration.

- **`--netmask`**
  
  Netmask to use for static IP configuration.

- **`--nameserver`**
  
  Nameserver to use for static IP configuration.

- **`--filter-name`**
  
  Filter devices by name (case-insensitive).

- **`--filter-address`**
  
  Filter devices by address (case-insensitive).

Example:

   ```bash
   python3 shelly-ble-rpc.py --scan-duration 10 --log-level DEBUG  --wifi-ssid "wifi SSID" --wifi-password "wifi-password" --gateway 10.10.10.1 --netmask 255.255.254.0 --nameserver 8.8.8.8
   ```

### Interactive Features

1. Device Discovery:

- The script scans for Shelly BLE devices within range and displays a list of discovered devices
- Devices are listed with their name, address, and signal strength (RSSI).

2. Device Selection:

- Select a device by its number.
- Options are provided to rescan or exit the script.

3. Command Selection:

- Choose a predefined command or enter a custom RPC method.
- Some commands prompt for parameters (e.g., Wi-Fi SSID and password).

4. Execution:

- The selected command is executed, and the response is displayed in a formatted JSON structure.

### Example Workflows

**Discover Devices**

1. Run the script.
2. Select a device by number from the list.

**Execute Commands**

1. Choose a predefined RPC method (e.g., Shelly.GetDeviceInfo or WiFi.SetConfig).
2. Provide required parameters when prompted.

**Custom Commands**

1. Enter an RPC method name.
2. Input parameters as a JSON string (e.g., {"key": "value"}).

### Error Handling

- Retries: Automatic retries with exponential backoff for BLE connection issues.
- Invalid Commands: Errors such as invalid arguments or unavailable methods are handled gracefully with user feedback.
- Logs: Errors and detailed debug information are logged to shelly_rpc.log.

## Key Components

### BLE Communication

The script uses the bleak library for BLE communication. Core BLE operations include:

- Discovering devices
- Reading and writing GATT characteristics
- Handling RPC request/response communication

### RPC Commands

Predefined commands include:

- Shelly.GetDeviceInfo
- WiFi.SetConfig
- Switch.Toggle
- And more.

Custom commands can also be executed by specifying the method and parameters.

### Logging

Logs are stored in shelly_rpc.log with a rotating file handler. Logging levels are configurable via command-line arguments.

### Colorized Output

The script uses colorama for color-coded terminal output, enhancing the user experience.

## License

This project is licensed under the MIT License.
