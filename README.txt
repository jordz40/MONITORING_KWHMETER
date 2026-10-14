FACTORY ENERGY MONITORING
========================

The dashboard reads live Modbus TCP data from a Schneider iEM3155 through a
USR-DR134. Modbus communication uses Python's standard library; Flask is the
only package dependency.

Current meter configuration
----------------------------
- DR134 IP: AUTO-DETECTED (see below)
- TCP port: 502
- Meter: Slave ID 1
- Serial settings on DR134 and meter must match (currently 19200, 8 data bits,
  Even parity, 1 stop bit).

Automatic DR134 IP discovery
----------------------------
The USR-DR134 gets its address from DHCP, so its IP can change between days.
The dashboard no longer relies on a fixed IP. On start (and whenever the
remembered address stops answering) it scans the local network:

  1. Builds a host list from this machine's own /24 subnets.
  2. Checks every host for TCP port 502 (fast, parallel).
  3. Probes each open host with a Modbus request to confirm it is the gateway.
  4. Remembers the working IP and re-scans automatically if it drops again.

The detected address and port are shown on the Settings page and in the
"LIVE DATA" badge on the Overview page.

To pin a fixed address instead of scanning, set an environment variable:

    Windows CMD:        set DR134_HOST=192.168.1.181
    Windows PowerShell: $env:DR134_HOST="192.168.1.181"
    Linux/macOS:        export DR134_HOST=192.168.1.181

Optional variables:
    DR134_PORT=502                 TCP port
    DR134_SUBNET=192.168.1.0/24   scan range(s), comma separated
                                   (default: this host's /24 networks)
    DR134_REQUEST_DELAY=0.05      gap in seconds between register reads.
                                   Increase (e.g. 0.2) if readings are flaky.
    DR134_POLL_INTERVAL=3         seconds between background meter polls.

Reading the meter
-----------------
The DR134 bridges to the meter over a half-duplex RS-485 line, so requests are
kept few and spaced out. Voltage, kW, kVA and PF all live close together, so
they are read as one block (registers 3025-3084) and the energy register
(45099) is read separately: two requests per poll instead of five. Because all
four values come from the same response frame, they always agree with each
other. Each Float32 value uses two registers, high word first.

The DR134 forwards every frame on the RS-485 line to all connected TCP clients,
so replies meant for another master (e.g. Modbus Poll running at the same time)
interleave with ours. The dashboard tags its requests with its own transaction
id and skips frames that do not belong to it, so it keeps working even while
Modbus Poll is open.

Polling runs in a single background thread. The web API (/api/data) only returns
the most recent snapshot, so a browser refresh is instant and can never block
waiting on the meter. The page shows "CONNECTING" only until the first poll
completes (a few seconds at startup).

Register map (zero-based PDU address; Modbus Poll shows these one-based):

    Average voltage L-L     3025   (Modbus Poll 3026)
    Total active power      3059   (3060)
    Total apparent power    3075   (3076)
    Total power factor      3083   (3084)
    Total active energy     45099  (45100)

Diagnostics
-----------
Open http://127.0.0.1:5000/api/discover to force a network scan. It reports the
hosts with port 502 open and which one answered Modbus. The dashboard Settings
page also has a "Re-scan network" button, the detected gateway IP and the last
error message.

Troubleshooting: if discovery still finds nothing, make sure this computer is
on the same subnet as the DR134. Assigning the DR134 a static IP or a DHCP
reservation is the most reliable fix.

Run
---
1. Install dependencies: pip install -r requirements.txt
2. Start the dashboard: python app.py
3. Open http://127.0.0.1:5000

The web server polls five iEM3155 values every time the browser refreshes the
data endpoint: average L-L voltage, total active power, total apparent power,
total power factor, and total active energy import. Measurements are read
sequentially and combined into one dashboard update.

Adding meters
-------------
Configure a unique Modbus Slave ID on each meter. Add one entry per connected
meter to METER_CONFIG in app.py, for example:

    {"id": "M02", "name": "TRIMMING 2", "slave_id": 2},

All meters on the RS-485 bus must use the same serial settings. Keep the meter
IDs unique. Only Slave ID 1 is enabled in the current configuration because it
is the meter tested so far.
