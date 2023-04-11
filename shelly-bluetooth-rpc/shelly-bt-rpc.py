#!/usr/bin/env python3
#
# Copyright (c) 2023 Allterco Robotics
# All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# This utility depends on Bleak - an asynchronous GATT client software,
# capable of discovering and connecting to BLE devices acting as GATT servers.
# To install run 'pip3 install bleak'.


from __future__ import annotations

import argparse
import asyncio
import logging
import json
import random
import struct
import sys
from typing import Any, Final

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData


RPC_SVC_UUID: Final[str] = "5f6d4f53-5f52-5043-5f53-56435f49445f"
RPC_CHAR_DATA_UUID: Final[str] = "5f6d4f53-5f52-5043-5f64-6174615f5f5f"
RPC_CHAR_TX_CTL_UUID: Final[str] = "5f6d4f53-5f52-5043-5f74-785f63746c5f"
RPC_CHAR_RX_CTL_UUID: Final[str] = "5f6d4f53-5f52-5043-5f72-785f63746c5f"


async def call(
    client: BleakClient,
    method: str,
    params: dict[str, Any] | None = None,
    resp: bool = True,
) -> None:
    services = client.services
    svc = services.get_service(RPC_SVC_UUID)
    data_char = svc.get_characteristic(RPC_CHAR_DATA_UUID)
    tx_ctl_char = svc.get_characteristic(RPC_CHAR_TX_CTL_UUID)
    rx_ctl_char = svc.get_characteristic(RPC_CHAR_RX_CTL_UUID)

    req = {
        "method": method,
        "params": params or {},
    }
    if resp:
        req["id"] = random.randint(1, 1000000000)
    req_json = json.dumps(req).encode("utf-8")
    req_len = len(req_json)
    logging.debug(f"Request: {req_json}")
    logging.debug(f"Writing length ({req_len})...")
    await client.write_gatt_char(tx_ctl_char, struct.pack(">I", req_len), response=True)
    logging.debug(f"Sending request...")
    await client.write_gatt_char(data_char, req_json, response=True)
    while True:
        raw_rx_frame = await client.read_gatt_char(rx_ctl_char)
        frame_len = struct.unpack(">I", raw_rx_frame)[0]
        logging.debug(f"RX frame len: {frame_len}")
        if frame_len == 0:
            await asyncio.sleep(0.1)
            continue
        frame_data, n_chunks = b"", 0
        while len(frame_data) < frame_len:
            chunk = await client.read_gatt_char(data_char)
            frame_data += chunk
            n_chunks += 1
        logging.debug(f"RX Frame data (rec'd in {n_chunks} chunks): {frame_data!r}")
        frame = json.loads(frame_data)
        if frame.get("id", 0) != req["id"]:
            continue
        if "result" in frame:
            print(json.dumps(frame["result"], ensure_ascii=False, indent=2))
            sys.exit(0)
        elif "error" in frame:
            print(json.dumps(frame["error"], indent=2))
            sys.exit(2)
        else:
            logging.error(f"Invalid frame: {frame}")
            sys.exit(1)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-v", "--v", type=int, default=logging.INFO, help="Verbosity level"
    )
    sp = parser.add_subparsers(title="Actions", dest="action", required=True)
    # scan
    scan_parser = sp.add_parser("scan", help="Scan for devices")
    scan_parser.add_argument(
        "-t", "--time", type=float, default=10.0, help="Scan for this long"
    )
    # call
    call_parser = sp.add_parser("call", help="Invoke an RPC method")
    call_parser.add_argument(
        "target", action="store", help="Name or MAC address of the device"
    )
    call_parser.add_argument("method", action="store", help="Method to invoke")
    call_parser.add_argument(
        "params", action="store", nargs="?", help="Call parameters, JSON object"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.v,
        format="[%(asctime)s %(levelno)d] %(message)s",
        datefmt="%Y/%m/%d %H:%M:%S",
        stream=sys.stderr,
    )

    if args.action == "scan":

        def cb(device: BLEDevice, adv: AdvertisementData) -> None:
            print(f"{device.address} {device.rssi} {device.name or '?'}")

        async with BleakScanner(cb):
            await asyncio.sleep(args.time)
        return

    elif args.action == "call":
        params = {}
        if args.params is not None:
            params = json.loads(args.params)

        device = None
        if len(args.target.split(":")) == 6:
            addr = args.target
        elif len(args.target.split("-")) == 6:
            addr = ":".join(args.target.split("-"))
        else:
            logging.info(f"Resolving {args.target}...")
            device = await BleakScanner.find_device_by_filter(
                lambda d, adv: d.name.startswith(args.target)
            )
            if not device:
                logging.error(f"Could not resolve {args.target}")
                sys.exit(1)
            addr = device.address

        logging.info(f"Connecting to {addr}...")
        async with BleakClient(device if device else addr) as client:
            await call(client, args.method, params)
            await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
