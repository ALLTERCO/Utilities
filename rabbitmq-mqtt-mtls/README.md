# mTLS with RabbitMQ + Shelly over MQTT

This repository includes a **one‑shot setup script** that turns a fresh Ubuntu/Debian host into a **RabbitMQ** broker that speaks **MQTT over TLS (8883)** and enforces **mutual TLS (mTLS)** for clients - with a focus on **Shelly** devices.

> This is a **development example**: a teaching aid showing what’s possible and how to stand up a working mTLS MQTT stack quickly. It’s opinionated by design (RabbitMQ series, TLS policy, ACLs). For production, review and adapt to your environment, security policies, and compliance needs.

---

## Why mTLS for IoT?

- **Device identity**: Every device proves who it is with a client certificate signed by _your_ CA.
- **No shared passwords**: Credentials aren’t copy‑pasted secrets; they’re cryptographic keys.
- **Least privilege**: Topic‑level authorization restricts devices to the topics they actually need.
- **Safer migration path**: You can keep plaintext ports open temporarily while you cut over.

---

## What you’ll build

- A RabbitMQ broker with:
  - MQTT on **8883** (TLS only) and optional **1883** during migration.
  - Management UI on **HTTPS 15671** (optional **15672** HTTP).
  - **mTLS required**: client certs signed by your private CA.
  - **Topic ACLs** that fence devices into your `MQTT_PREFIX`.
  - (RabbitMQ > 4.x) **Client ID bound to cert SAN (URI)** for extra safety.
- A **client bundle** (CA, client cert, client key) ready to upload in the Shelly UI.
- A friendly summary that prints exactly what to paste into your device settings.

> ⚠️ **TLS policy in this example** is pinned to **TLS 1.2** for broad device compatibility.

---

## Architecture at a glance

```log
[Shelly device]
    |
    |  MQTT over TLS (mTLS) - port 8883
    v
[RabbitMQ MQTT listener]  --->  [amq.topic exchange]  --->  [queues/subscriptions]
           |                               |
           |                               ‘-- topic ACLs scoped to your MQTT prefix
           |
           ‘-- Management UI (HTTPS, 15671)
```

Key ideas:

- Devices don’t send a vhost; `mqtt.vhost` routes to one (`/shelly` by default).
- The **CN** becomes the RabbitMQ user for cert login; the **SAN URI** can lock the MQTT **client_id** on RabbitMQ 4.x.
- ACLs limit publish/subscribe to your **prefix** (e.g., `home/shelly/...`).

---

## Prerequisites

- Ubuntu 24.04 host with `sudo`, `systemd`, and `apt-get`.
- Network egress to RabbitMQ repos and OpenPGP key server.
- Ingress on **8883/TCP** and **15671/TCP** from your devices (and you).

> The script intentionally pins RabbitMQ/Erlang repositories to **Ubuntu noble** as a consistent baseline.

---

### Common options

- `--mqtt-prefix <prefix>`
  - The topic prefix you configure in Shelly (e.g., `shelly`).
- `--connect-dns <name>` or `--connect-ip <ip>`
  - What devices will dial; used for the **server cert CN/SAN** and shown as `<host>:8883`.
- `--keep-plaintext`
  - Keep **1883** (MQTT) and **15672** (HTTP mgmt) open for migration.
- `--debug`
  - Stream command output; disables spinner.
- `--force-regen`
  - Regenerate CA/server/client certs even if they exist (rotate credentials).
- `--rmq-series 3.13|4.0|4.1`
  - Choose RabbitMQ series (default **4.1**). Maps Erlang appropriately.

### Admin & identity

- `--admin-user <name>` (default `admin`)
- `-p, --admin-pass <pass>` (otherwise auto‑generated)
- `--client-cn <CN>` (default `Shelly-Group`)
- `--client-id <id>` (defaults to `CLIENT_CN`; also used as **SAN URI**)
- `--vhost <vhost>` (default `/shelly`)

### Paths

- `--tls-dir <dir>` (default `/etc/rabbitmq-tls`)
- `--export-dir <dir>` (default `/etc/mqtt-cert`)
- `--no-monitor-cert` to skip the secondary client bundle
- `--monitor-export-dir <dir>` (default `/etc/mqtt-cert-monitor`)

### Environment variables (overrideable by flags)

| Variable | Default | Purpose |
|---|---|---|
| `ADMIN_USER` | `admin` | Management admin user |
| `ADMIN_PASS` | _(random)_ | Admin password if not using `-p` |
| `CLIENT_CN` | `Shelly-Group` | CN used for cert login (becomes a RabbitMQ user) |
| `CLIENT_ID` | `CLIENT_CN` | Also embedded as **SAN URI** in client cert (binds `client_id` on ≥4.0) |
| `VHOST` | `/shelly` | Vhost used by the MQTT plugin |
| `KEEP_PLAINTEXT` | `false` | Keep ports **1883** and **15672** open |
| `TLS_DIR` | `/etc/rabbitmq-tls` | Stores CA/server/client materials |
| `EXPORT_DIR` | `/etc/mqtt-cert` | Safe folder with only `ca.crt`, `client.crt`, `client.key` |
| `MAKE_MONITOR_CERT` | `true` | Create a second bundle for monitoring |
| `MONITOR_CLIENT_ID` | `${CLIENT_ID}-mon` | Distinct client_id for monitoring |
| `MONITOR_EXPORT_DIR` | `/etc/mqtt-cert-monitor` | Safe export for monitor bundle |
| `MQTT_PREFIX` | `something` | Topic prefix enforced by ACLs (slash & dot forms) |
| `SERVER_IP` | _(auto)_/manual | Primary IP detection (default route) |
| `CONNECT_DNS` / `CONNECT_IP` | _(auto)_/manual | Determines server cert CN/SAN and device dial target |
| `LOG_LEVEL` | `info` | Broker log level |
| `FORCE_REGEN` | `false` | Rotate certificates even if present |
| `RMQ_SERIES` | `4.1` | RabbitMQ series (`3.13`, `4.0`, `4.1`) |

---

## Quick start

1) Make the script executable and run it with your own prefix and host that devices will dial:

    ```bash
    chmod +x setup-rabbitmq-mqtt.sh

    ./setup-rabbitmq-mqtt.sh --admin-user admin   --admin-pass 'Sh3lly-i0T!'   -C 'Shelly-Group'   --client-id 'test-device'   --vhost /shelly   --mqtt-prefix dimmer
    ```

2) When it finishes, copy the **Shelly Settings** the script prints and upload the three files it prepared:
   - `ca.crt`, `client.crt`, `client.key` (paths are printed at the end).
3) Open the Management UI:
   - `https://<SERVER_IP>:15671` using the **admin** user and password displayed.

---

## A guided tour of the script

### 1) Installing RabbitMQ

- Adds Team RabbitMQ keys + repos (pinned to **noble**).
- Installs Erlang and RabbitMQ for a chosen series: `3.13`, `4.0`, or `4.1` (default **4.1**).
  - 3.13 -> Erlang **26**; 4.x -> Erlang **27**.

### 2) Enabling the right plugins/features

- `rabbitmq_management` -> UI/API
- `rabbitmq_mqtt` -> MQTT protocol
- `rabbitmq_auth_mechanism_ssl` -> CN/SAN login helpers
- Feature flag `detailed_queues_endpoint` -> nicer UI details

### 3) Creating cryptographic identity (CA, server, client)

- Generates a **private CA** you control.
- **Server cert** CN/SAN match what clients dial (`--connect-dns` or `--connect-ip`).
- **Client cert** CN = `CLIENT_CN` (becomes a RabbitMQ user) and **SAN URI = CLIENT_ID`**.
- Optional second client cert for **monitoring** with a distinct client_id.
- Keys are owned by `root:rabbitmq` with restrictive permissions.

### 4) Enforcing mTLS in RabbitMQ

- Global SSL: `verify_peer` + `fail_if_no_peer_cert=true`.
- TLS **1.2 only** (`tlsv1.2`), explicitly avoiding 1.3 in this example.
- MQTT: 8883 (TLS), optional 1883; Management: 15671 (HTTPS), optional 15672 (HTTP).
- (RabbitMQ 4.x) `mqtt.ssl_cert_client_id_from = subject_alternative_name` binds **client_id** to the cert **SAN URI**.

### 5) Teaching least privilege with topic ACLs

- CN user gets minimal configure/write/read perms needed for MQTT subscription queues + `amq.topic`.
- Topic permissions allow **only** your `MQTT_PREFIX` in both slash (`home/shelly/...`) and dot (`home.shelly...`) forms.
- Read‑only allowlist for legacy `shellies/command` topics to ease mixed fleets.

### 6) Making it easy to try

- Script prints a **copy/paste block** for Shelly settings.
- Prepares **export folders** with just `ca.crt`, `client.crt`, `client.key` (safe to serve).
- Creates a tarball under `/tmp/...-mqtt-mtls.tar.gz` for quick transfer.
- Adds UFW rules **only** if UFW is active (no surprises otherwise).

---

## Configure a Shelly

1. Open the device’s **MQTT** settings.
2. Upload **CA**, **Client certificate**, **Client private key** using the files the script printed.
3. Set:
   - **Enable**: ✅
   - **User TLS**: ✅
   - **Use client certificate**: ✅
   - **Server**: `<CONNECT_HOST>:8883`
   - **Client ID**: `<CLIENT_ID>`
   - **MQTT prefix**: `<MQTT_PREFIX>`
   - **Username / Password**: leave blank
   - **MQTT Control / RPC**: enable if you need them
4. Save and reboot the device if prompted.

> Tip: Upload the certs first, then apply the settings. The device must match **client_id = SAN URI** when using RabbitMQ 4.x binding.

---

## Verify it works (copy the commands the script prints)

Instead of separate test recipes, this example relies on the **exact, ready‑to‑run commands the script prints at the end**. After a successful run you’ll see two sections like these:

  **Optional local test (mutual TLS via mosquitto-clients):**

  ```bash
  sudo apt-get -y install mosquitto-clients
  sudo mosquitto_sub -h ${CONNECT_HOST} -p 8883 \
    --cafile ${EXPORT_DIR}/ca.crt --cert ${EXPORT_DIR}/client.crt --key ${EXPORT_DIR}/client.key \
    --id ${CLIENT_ID} --tls-version tlsv1.2 -t '${MQTT_PREFIX}/#' -v
  ```

  **Optional monitoring test (non‑disruptive, if you created the monitor bundle):**

  ```bash
  sudo mosquitto_sub -h ${CONNECT_HOST} -p 8883 \
    --cafile ${MONITOR_EXPORT_DIR}/ca.crt --cert ${MONITOR_EXPORT_DIR}/client.crt --key ${MONITOR_EXPORT_DIR}/client.key \
    --id ${MONITOR_CLIENT_ID} --tls-version tlsv1.2 -t '${MQTT_PREFIX}/#' -v
  ```

  **What success looks like**

- The subscriber connects without asking for a username/password (mTLS auth).
- Messages under your `MQTT_PREFIX` show up.  
- The Management UI shows a connected MQTT client with your **Client ID**.

  **TLS quick diagnostics (already handled by the script)**

- The script prints a **TLS diagnostics** block with local certificate details.
- If it says _“Could not retrieve remote certificate - port closed, firewall, or handshake blocked”_, check security groups/firewalls and that the broker is listening on **8883**.

---

## Customize your lab

- **RabbitMQ series**: `--rmq-series 3.13|4.0|4.1` (default 4.1)
- **TLS materials**: `--tls-dir`, `--export-dir`, `--monitor-export-dir`
- **Identity**: `--client-cn`, `--client-id` (SAN URI), `--mqtt-prefix`, `--vhost`
- **Connectivity**: `--connect-dns` or `--connect-ip`
- **Plaintext for debugging**: `--keep-plaintext`
- **Logging**: `--debug` for streaming output; otherwise see `/tmp/rabbitmq-mqtt-setup.log`
- **Rotation**: `--force-regen` to rotate CA/server/client certs

---

## Troubleshooting

- **Auth failed**: On RabbitMQ 4.x, ensure the device **Client ID equals the cert SAN URI**.
- **TLS handshake fails**: Script enforces **TLS 1.2**; check device TLS support and that all three files were uploaded.
- **Topic permission denied**: Publish/subscribe must live under your `MQTT_PREFIX`.
- **Broker won’t start**: Tail `journalctl -u rabbitmq-server`. The script made a timestamped backup of `/etc/rabbitmq/rabbitmq.conf` you can restore.
- **Ports closed**: If UFW is active the script opens 8883/15671 (and 1883/15672 when kept). Verify with `sudo ufw status`.
- **Where are the files?**: Paths and bundle locations are printed at the end of the script run.

---

