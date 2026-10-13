FACTORY ENERGY MONITORING
========================

The dashboard reads live Modbus TCP data from a Schneider iEM3155 through a
USR-DR134. Modbus communication uses Python's standard library; Flask is the
only package dependency.

Current meter configuration
----------------------------
- DR134 IP: 192.168.137.202
- TCP port: 502
- Meter: Slave ID 1
- Serial settings on DR134 and meter must match (currently 19200, 8 data bits,
  Even parity, 1 stop bit).

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
