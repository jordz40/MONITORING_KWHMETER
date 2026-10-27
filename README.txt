FACTORY ENERGY MONITORING
========================

The dashboard reads live Modbus TCP data from a Schneider iEM3155 through a
USR-DR134. Modbus communication uses Python's standard library; Flask is the
only package dependency.

Current meter configuration
----------------------------
- DR134 IP: AUTO-DETECTED (see below)
- TCP port: 502
- Meters: 5 on one RS-485 daisy chain
- Serial settings on DR134 and meter must match (currently 19200, 8 data bits,
  Even parity, 1 stop bit).

    Slave ID 1   M01   Mesin Extruder
    Slave ID 2   M02   Mesin Coating
    Slave ID 3   M03   Mesin Trimming HDL 1
    Slave ID 4   M04   Mesin Trimming HDL 2
    Slave ID 5   M05   Mesin Trimming HDL 3

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
    DR134_TIMEOUT=1.5             seconds to wait for a reply. Raise it if
                                   readings are flaky on a very busy bus.

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
    Energy tariff 1 (rate A) 45119 (45120)
    Energy tariff 2 (rate B) 45121 (45122)

Energy and the two tariff counters are contiguous, so they are read in one
block. The tariff registers only exist on the iEM3155 and stay 0 until
multi-tariff is enabled and configured on the meter itself (the dashboard only
reads the counters; which tariff is active is decided by the meter).

Diagnostics
-----------
Open http://127.0.0.1:5000/api/discover to force a network scan. It reports the
hosts with port 502 open and which one answered Modbus. The dashboard Settings
page also has a "Re-scan network" button, the detected gateway IP and the last
error message.

Troubleshooting: if discovery still finds nothing, make sure this computer is
on the same subnet as the DR134. Assigning the DR134 a static IP or a DHCP
reservation is the most reliable fix.

Database and history
--------------------
Daily kWh history is stored in MySQL/MariaDB (no SQLite). Create the database
and an application user once:

    CREATE DATABASE energy CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
    CREATE USER 'energi'@'localhost' IDENTIFIED BY 'PASSWORD_KAMU';
    GRANT ALL PRIVILEGES ON energy.* TO 'energi'@'localhost';

Then copy config.ini.example to config.ini and fill in the password. config.ini
is gitignored, so credentials never get committed. The tables are created
automatically on start:

    samples  one row per meter every sample_minutes (default 1), kept forever
    daily    one row per day per meter; this is what the reports read
    meta     bookkeeping, e.g. how far the daily rollup has run

Polling writes a reading only when the counters change hands to a new
sample_minutes slot, and the daily rollup runs once a day. By default a
reporting day is the calendar date (00:00-24:00). Set day_end_hour = 22 in
config.ini to follow the tariff cycle instead: the day then runs 22:00 -> 22:00
and is labelled with the date it closes on, so one row holds a full LWBP + WBP
cycle. Changing day_end_hour rebuilds the daily table automatically.

A day is marked "sebagian" (partial) when the readings do not bracket its edges
closely, and "tanpa data" when there was no reading at all. A partial day still
reports the part that was actually measured and says where that window started
or ended, for example "mulai 14:22" or "s/d 05:15". An opening reading older
than 30 minutes is never used as a baseline, otherwise a multi-day outage would
make one day's total swallow every day that was never measured. Meters that are
not installed yet are ignored when judging a day.

Because the meter only stores a running total, a day that was never sampled
cannot be reconstructed after the fact, so the program should run 24/7.

Reports
-------
Open the Reports tab: pick a month and a year to see the daily kWh per machine
(M01..M05) plus the factory total, a daily bar chart, the monthly totals and a
"Rincian tarif" toggle for the Tarif 1 / Tarif 2 breakdown. "Ekspor Excel"
downloads an .xlsx with three sheets: Harian per Mesin, Rincian Tarif and
Ringkasan Bulanan. The same data is available as JSON at
/api/report?year=2026&month=10 and as a file at /report.xlsx.

Load profile
------------
The Load Profile tab answers "at what hours is the load high?". Pick a date and
a machine (or "Semua mesin") to chart the average power of every hour, see the
per-hour table with its data coverage and the three busiest hours. Below it a
monthly heatmap shows one row per day and 24 columns, so the pattern of a whole
month is visible at a glance.

Power is never stored directly: it is derived from the cumulative kWh counters
(kW = dkWh / hours), which also keeps the older 15-minute samples usable next to
the newer one-minute ones. An interval longer than 30 minutes is treated as a
gap and skipped, never spread over time that was not measured.

"Ekspor Excel" downloads an .xlsx with two sheets: Beban per Jam (24 rows plus
the peak, peak hour and daily total) and Heatmap Bulanan (one row per day with a
colour scale). The file follows the date, machine, month and year selected on
the page, and is also available directly at
/profile.xlsx?date=2026-10-17&meter=all&year=2026&month=10.

JSON: /api/profile?date=2026-10-17&meter=all and
/api/profile/month?year=2026&month=10

Run
---
1. Install dependencies: pip install -r requirements.txt
2. Copy config.ini.example to config.ini and fill in the database password
3. Start the dashboard: python app.py
4. Open http://127.0.0.1:5000

For a server deployment, set [dashboard] host = 0.0.0.0 in config.ini so other
computers can reach it, and run it as a Windows service / Task Scheduler task so
it keeps collecting history without anyone opening a terminal.

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
IDs unique. All five meters above are already configured in METER_CONFIG.
