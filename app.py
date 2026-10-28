"""Factory energy dashboard backed by Schneider iEM3155 meters via USR-DR134."""

from __future__ import annotations

import concurrent.futures
import ipaddress
import math
import os
import socket
import struct
import threading
import time
from datetime import date, datetime

from flask import Flask, jsonify, render_template, request, send_file

import load_profile
import reporting
import storage

app = Flask(__name__)

# Reload dashboard.html on every request so UI tweaks take effect with a normal
# browser refresh instead of a process restart.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# --- DR134 Modbus TCP endpoint ---------------------------------------------
# The USR-DR134 takes its address from DHCP, so it can move to a new IP without
# warning. By default the dashboard discovers it automatically by scanning the
# local network for a Modbus TCP gateway on port 502.
#
# Override any of these with environment variables:
#   DR134_HOST=192.168.1.192      pin a fixed address (skips discovery)
#   DR134_PORT=502                TCP port
#   DR134_SUBNET=192.168.1.0/24   extra scan ranges, comma separated
#   DR134_REQUEST_DELAY=0.05      seconds between register reads (RS-485 gap)
MODBUS_PORT = int(os.environ.get("DR134_PORT", "502"))
MANUAL_HOST = os.environ.get("DR134_HOST", "").strip() or None
MANUAL_SUBNETS = [part.strip() for part in os.environ.get("DR134_SUBNET", "").split(",") if part.strip()]

# The DR134 bridges to the meter over RS-485. The line is half-duplex and the
# gateway needs a short gap between transactions, otherwise responses get lost.
# Modbus Poll works because it scans each window once per second.
MODBUS_TIMEOUT_SECONDS = float(os.environ.get("DR134_TIMEOUT", "1.5"))
MODBUS_REQUEST_DELAY_SECONDS = float(os.environ.get("DR134_REQUEST_DELAY", "0.05"))
# The DR134 broadcasts other masters' frames to every client, so a reply may be
# preceded by many unrelated frames. Cap how many we skip before giving up.
MAX_FRAMES_PER_READ = 64
# Start our transaction ids high so they stay clear of masters that count up
# from 0 (e.g. Modbus Poll), which would otherwise eventually collide.
MODBUS_TRANSACTION_BASE = 0xF000
# One background thread polls the meter; the web API only serves the latest
# snapshot, so a request can never block on the (slow, shared) RS-485 bus.
POLL_INTERVAL_SECONDS = float(os.environ.get("DR134_POLL_INTERVAL", "3"))

# Discovery tuning. The first response from an RS-485 gateway can take up to a
# scan cycle, so the Modbus probe waits longer than a plain port check. The
# timeout applies to each slave id the probe tries.
DISCOVERY_CONNECT_TIMEOUT_SECONDS = 0.5
DISCOVERY_PROBE_TIMEOUT_SECONDS = 2.0
DISCOVERY_MAX_WORKERS = 64
DISCOVERY_COOLDOWN_SECONDS = 15.0
DISCOVERY_MAX_HOSTS = 1024

# One entry per physical meter. Every meter on the shared RS-485 bus needs a
# unique Modbus Slave ID.
METER_CONFIG = [
    {"id": "M01", "name": "Mesin Extruder", "slave_id": 1},
    {"id": "M02", "name": "Mesin Coating", "slave_id": 2},
    {"id": "M03", "name": "Mesin Trimming HDL 1", "slave_id": 3},
    {"id": "M04", "name": "Mesin Trimming HDL 2", "slave_id": 4},
    {"id": "M05", "name": "Mesin Trimming HDL 3", "slave_id": 5},
]

# iEM3155 Modbus PDU (zero-based) addresses, matching the register list. Each
# Float32 is two 16-bit registers, high word first. Modbus Poll shows these as
# one-based (3026, 3060, ...).
REGISTER_MAP = {
    "current_r": 2999,     # Current L1 (R), A
    "current_s": 3001,     # Current L2 (S), A
    "current_t": 3003,     # Current L3 (T), A
    "voltage": 3025,       # Average voltage L-L, V
    "kw": 3059,            # Total active power, kW
    "kva": 3075,           # Total apparent power, kVA
    "pf": 3083,            # Total power factor
    "kwh": 45099,          # Total active energy import, kWh
    "kwh_t1": 45119,       # Active energy import rate A (tariff 1), kWh
    "kwh_t2": 45121,       # Active energy import rate B (tariff 2), kWh
}

# Currents, voltages, power and power factor all live in one contiguous range,
# so a single request covers them (FC03 allows up to 125 registers). This keeps
# the bus traffic at two requests per meter per poll.
VALUE_BLOCK_START = 2999
VALUE_BLOCK_COUNT = 86                        # 2999..3084
VALUE_KEYS = ("current_r", "current_s", "current_t", "voltage", "kw", "kva", "pf")
VALUE_OFFSETS = {
    key: (REGISTER_MAP[key] - VALUE_BLOCK_START) * 2 for key in VALUE_KEYS
}

# The energy registers are contiguous too (45099..45126): total import followed
# by the per-tariff rate A/B counters, so one read covers all of them. Tariff
# counters only exist on the iEM3155 and read 0 until multi-tariff is enabled
# on the meter.
ENERGY_BLOCK_START = 45099
ENERGY_BLOCK_COUNT = 28
ENERGY_KEYS = ("kwh", "kwh_t1", "kwh_t2")
ENERGY_OFFSETS = {
    key: (REGISTER_MAP[key] - ENERGY_BLOCK_START) * 2 for key in ENERGY_KEYS
}

# 64-bit integer energy counters, in Wh. Some firmware returns NaN for the
# Float32 copies above but still serves these, so they act as a fallback.
ENERGY_INT64_ADDRESS = 3203      # total active energy import, 4 registers
TARIFF_INT64_ADDRESS = 4195      # rate A, 4 registers; rate B follows at 4199

_poll_lock = threading.Lock()
_host_lock = threading.Lock()
_current_host = MANUAL_HOST
_last_discovery = 0.0
_last_error: str | None = None
_history = []
_transaction_id = MODBUS_TRANSACTION_BASE - 1
_state_lock = threading.Lock()
_poller_lock = threading.Lock()
_latest: dict | None = None
_poller_started = False
_last_sample_slot: datetime | None = None
_last_finalize_day: date | None = None
_storage_state: dict = {"ok": False, "error": "belum tersambung"}


class ModbusError(RuntimeError):
    """Raised when a Modbus TCP request is invalid or receives an exception."""


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ModbusError("DR134 menutup koneksi sebelum respons lengkap.")
        chunks.extend(chunk)
    return bytes(chunks)


def _next_transaction_id() -> int:
    global _transaction_id
    _transaction_id += 1
    if _transaction_id > 0xFFFE:
        _transaction_id = MODBUS_TRANSACTION_BASE
    return _transaction_id


def _read_block(sock: socket.socket, slave_id: int, address: int, count: int) -> bytes:
    # The DR134 forwards every frame it sees on the RS-485 line to all TCP
    # clients, so replies meant for other masters interleave with ours. Keep
    # reading and discard frames until our own transaction id comes back, and
    # re-send the request while there is time left if the bus is very busy.
    deadline = time.monotonic() + MODBUS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        transaction_id = _next_transaction_id()
        # MBAP header + FC03 request.
        request = struct.pack(">HHHBBHH", transaction_id, 0, 6, slave_id, 3, address, count)
        sock.sendall(request)

        for _ in range(MAX_FRAMES_PER_READ):
            if time.monotonic() >= deadline:
                break
            header = _recv_exact(sock, 7)
            response_transaction, protocol_id, length = struct.unpack(">HHH", header[:6])
            response_unit = header[6]
            if length < 2:
                continue
            pdu = _recv_exact(sock, length - 1)
            if protocol_id != 0 or response_transaction != transaction_id or response_unit != slave_id:
                continue

            function = pdu[0]
            if function == 0x83:
                code = pdu[1] if len(pdu) > 1 else "tidak diketahui"
                raise ModbusError(f"Meter ID {response_unit} menolak register {address} (exception {code}).")
            if function != 3 or len(pdu) < 2:
                raise ModbusError(f"Format respons tidak valid untuk Slave ID {slave_id}, register {address}.")

            data = pdu[2:2 + pdu[1]]
            if len(data) < count * 2:
                raise ModbusError(f"Data terlalu pendek untuk register {address}.")
            return data

    raise ModbusError(f"Tidak ada respons untuk register {address} dalam {MODBUS_TIMEOUT_SECONDS:.0f}s.")


def _read_float32(sock: socket.socket, slave_id: int, address: int) -> float:
    return struct.unpack(">f", _read_block(sock, slave_id, address, 2)[0:4])[0]


def _read_meter(sock: socket.socket, meter: dict) -> dict:
    readings: dict[str, float | None] = {key: None for key in REGISTER_MAP}
    errors: list[str] = []

    try:
        block = _read_block(sock, meter["slave_id"], VALUE_BLOCK_START, VALUE_BLOCK_COUNT)
        for key, offset in VALUE_OFFSETS.items():
            value = struct.unpack(">f", block[offset:offset + 4])[0]
            # Python's JSON encoder emits NaN as the bare token NaN, which is not
            # valid JSON and makes the browser reject the whole API payload.
            readings[key] = value if math.isfinite(value) else None
    except (OSError, ModbusError, struct.error) as exc:
        errors.append(f"daya/tegangan: {exc}")

    # An unwired slave answers nothing at all, so skip its energy block instead
    # of burning another timeout on it. This keeps polling quick while meters
    # are still being installed.
    meter_answered = any(readings[key] is not None for key in VALUE_KEYS)
    if meter_answered:
        time.sleep(MODBUS_REQUEST_DELAY_SECONDS)
        try:
            block = _read_block(sock, meter["slave_id"], ENERGY_BLOCK_START, ENERGY_BLOCK_COUNT)
            for key, offset in ENERGY_OFFSETS.items():
                value = struct.unpack(">f", block[offset:offset + 4])[0]
                readings[key] = value if math.isfinite(value) else None
        except (OSError, ModbusError, struct.error) as exc:
            errors.append(f"energi: {exc}")

    # Some firmware answers NaN for the Float32 energy copies above (the
    # Extruder meter does). The 64-bit Wh counters hold the same totals, so
    # fill in anything that is still missing from them.
    if meter_answered and readings["kwh"] is None:
        try:
            raw = _read_block(sock, meter["slave_id"], ENERGY_INT64_ADDRESS, 4)
            readings["kwh"] = struct.unpack(">q", raw[:8])[0] / 1000.0
        except (OSError, ModbusError, struct.error) as exc:
            errors.append(f"energi 64-bit: {exc}")

    if meter_answered and (readings["kwh_t1"] is None or readings["kwh_t2"] is None):
        try:
            raw = _read_block(sock, meter["slave_id"], TARIFF_INT64_ADDRESS, 8)
            if readings["kwh_t1"] is None:
                readings["kwh_t1"] = struct.unpack(">q", raw[0:8])[0] / 1000.0
            if readings["kwh_t2"] is None:
                readings["kwh_t2"] = struct.unpack(">q", raw[8:16])[0] / 1000.0
        except (OSError, ModbusError, struct.error) as exc:
            errors.append(f"tarif 64-bit: {exc}")

    online = any(value is not None for value in readings.values())
    return {**meter, **readings, "online": online, "error": "; ".join(errors) or None}


def _empty_meter(meter: dict, error: str) -> dict:
    return {
        **meter,
        **{key: None for key in REGISTER_MAP},
        "online": False,
        "error": error,
    }


# --- DR134 discovery --------------------------------------------------------

def _local_ipv4_addresses() -> list[str]:
    """Return non-loopback IPv4 addresses assigned to this machine."""
    addresses: set[str] = set()
    # The UDP socket is never used to send traffic; connect() only selects the
    # outbound interface and reveals the primary address.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    return sorted(
        address for address in addresses
        if not address.startswith("127.") and not address.startswith("169.254.")
    )


def _scan_networks() -> list[ipaddress.IPv4Network]:
    """Networks to sweep: DR134_SUBNET if given, otherwise this host's /24s."""
    networks: list[ipaddress.IPv4Network] = []
    for raw in MANUAL_SUBNETS:
        try:
            networks.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            app.logger.warning("DR134_SUBNET '%s' tidak valid, dilewati.", raw)
    if networks:
        return networks
    for address in _local_ipv4_addresses():
        networks.append(ipaddress.ip_network(f"{address}/24", strict=False))
    return networks


def _candidate_hosts() -> list[str]:
    hosts: list[str] = []
    for network in _scan_networks():
        if network.num_addresses > DISCOVERY_MAX_HOSTS:
            app.logger.warning("Melewati %s: terlalu banyak alamat untuk dipindai.", network)
            continue
        hosts.extend(str(host) for host in network.hosts())
    return hosts


def _port_open(host: str) -> bool:
    try:
        with socket.create_connection((host, MODBUS_PORT), timeout=DISCOVERY_CONNECT_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def _probe_slave_ids() -> list[int]:
    """Slave ids to try when looking for a gateway: every configured meter."""
    slave_ids = sorted({meter["slave_id"] for meter in METER_CONFIG})
    return slave_ids or [1]


def _probe_slave(host: str, slave_id: int) -> bool:
    """True when the host answers our request for one slave id."""
    probe_transaction = 0xFF00 + (slave_id & 0xFF)
    deadline = time.monotonic() + DISCOVERY_PROBE_TIMEOUT_SECONDS
    try:
        with socket.create_connection((host, MODBUS_PORT), timeout=DISCOVERY_PROBE_TIMEOUT_SECONDS) as sock:
            sock.settimeout(DISCOVERY_PROBE_TIMEOUT_SECONDS)
            while time.monotonic() < deadline:
                request = struct.pack(">HHHBBHH", probe_transaction, 0, 6, slave_id, 3, REGISTER_MAP["voltage"], 2)
                sock.sendall(request)
                for _ in range(MAX_FRAMES_PER_READ):
                    if time.monotonic() >= deadline:
                        break
                    header = _recv_exact(sock, 7)
                    response_transaction, protocol_id, length = struct.unpack(">HHH", header[:6])
                    if length < 2:
                        continue
                    _recv_exact(sock, length - 1)
                    if protocol_id == 0 and response_transaction == probe_transaction:
                        return True
            return False
    except (OSError, ModbusError, struct.error):
        return False


def _modbus_probe(host: str) -> bool:
    """True when the host answers for any configured meter.

    The gateway is a transparent bridge, so it only replies while a meter with
    the requested slave id exists on the RS-485 bus. Probing just one id would
    miss a gateway whose meters happen to use other ids, so try them all.
    """
    return any(_probe_slave(host, slave_id) for slave_id in _probe_slave_ids())


def _discover_host() -> str | None:
    """Scan the local network and return the first Modbus TCP gateway found."""
    if MANUAL_HOST:
        return MANUAL_HOST

    candidates = _candidate_hosts()
    if not candidates:
        return None

    app.logger.info("Mencari DR134 di %d alamat ...", len(candidates))
    with concurrent.futures.ThreadPoolExecutor(max_workers=DISCOVERY_MAX_WORKERS) as pool:
        open_hosts = [
            host for host, is_open in zip(candidates, pool.map(_port_open, candidates)) if is_open
        ]

    for host in open_hosts:
        if _modbus_probe(host):
            app.logger.info("DR134 ditemukan di %s:%d", host, MODBUS_PORT)
            return host

    app.logger.warning("DR134 tidak ditemukan di jaringan lokal (port 502 terbuka: %s).", open_hosts)
    return None


def _resolve_host(force: bool = False) -> str | None:
    global _current_host, _last_discovery
    if MANUAL_HOST:
        return MANUAL_HOST

    now = time.monotonic()
    with _host_lock:
        if _current_host and not force:
            return _current_host
        # Avoid hammering the network with a full sweep on every failed poll.
        if not force and now - _last_discovery < DISCOVERY_COOLDOWN_SECONDS:
            return None

    host = _discover_host()
    with _host_lock:
        _current_host = host
        _last_discovery = time.monotonic()
    return host


def _active_host() -> str | None:
    with _host_lock:
        return _current_host


def _poll_host(host: str) -> list[dict] | None:
    """Poll every meter over one connection. Returns None if connect fails."""
    try:
        with socket.create_connection((host, MODBUS_PORT), timeout=MODBUS_TIMEOUT_SECONDS) as sock:
            sock.settimeout(MODBUS_TIMEOUT_SECONDS)
            results = []
            for meter in METER_CONFIG:
                try:
                    results.append(_read_meter(sock, meter))
                except (OSError, ModbusError, struct.error) as exc:
                    results.append(_empty_meter(meter, str(exc)))
            return results
    except OSError:
        return None


def _poll_all_meters() -> list[dict]:
    # One TCP connection and one Modbus master poll the meters sequentially.
    # This avoids each dashboard browser tab opening its own meter connection.
    global _current_host, _last_error
    with _poll_lock:
        host = _resolve_host()
        if host:
            results = _poll_host(host)
            if results is not None:
                _last_error = None
                return results
            # The remembered address stopped answering; DHCP likely moved the
            # DR134. Forget it and scan again immediately.
            app.logger.warning("DR134 di %s tidak merespons, mencari ulang ...", host)
            with _host_lock:
                _current_host = None
            host = _resolve_host(force=True)
            if host:
                results = _poll_host(host)
                if results is not None:
                    _last_error = None
                    return results

        _last_error = "DR134 tidak ditemukan otomatis di jaringan lokal."
        return [_empty_meter(meter, _last_error) for meter in METER_CONFIG]


def _aggregate(meters: list[dict]) -> dict:
    online = [meter for meter in meters if meter["online"]]

    def total(key: str) -> float | None:
        values = [meter[key] for meter in online if meter[key] is not None]
        return sum(values) if values else None

    power_factors = [meter["pf"] for meter in online if meter["pf"] is not None]

    # Factory overview: power and energy are summed across the online machines.
    # Voltage is deliberately not aggregated: machines may run on different
    # levels (220 V vs 380 V), so an average would not mean anything. Read the
    # per-machine voltage on the machine card or in the detail view instead.
    # Power factor is a ratio, so it is averaged rather than added up.
    return {
        "kw": total("kw"),
        "kva": total("kva"),
        "kwh": total("kwh"),
        "kwh_t1": total("kwh_t1"),
        "kwh_t2": total("kwh_t2"),
        "pf": sum(power_factors) / len(power_factors) if power_factors else None,
    }


def _record_history(meters: list[dict]) -> None:
    """Save the counters every SAMPLE_MINUTES and roll finished days up.

    Both steps are best effort: if the database is down the dashboard keeps
    showing live data and the next poll tries again.
    """
    global _last_sample_slot, _last_finalize_day, _storage_state
    now = datetime.now()
    interval = max(1, storage.SAMPLE_MINUTES)
    slot = now.replace(minute=(now.minute // interval) * interval, second=0, microsecond=0)

    try:
        if slot != _last_sample_slot:
            for meter in meters:
                if meter["kwh"] is None and meter["kwh_t1"] is None and meter["kwh_t2"] is None:
                    continue
                storage.write_sample(now, meter)
            _last_sample_slot = slot
        if _last_finalize_day != now.date():
            storage.finalize_days([meter["id"] for meter in METER_CONFIG])
            _last_finalize_day = now.date()
        _storage_state = {"ok": True, "error": None, "lastSample": now.strftime("%Y-%m-%d %H:%M:%S")}
    except storage.StorageError as exc:
        _storage_state = {"ok": False, "error": str(exc)}


def _build_snapshot(meters: list[dict]) -> dict:
    totals = _aggregate(meters)
    now = datetime.now()
    online_count = sum(1 for meter in meters if meter["online"])

    if online_count:
        _history.append({
            "time": now.strftime("%H:%M:%S"),
            "kw": totals["kw"],
            "kva": totals["kva"],
        })
        del _history[:-20]

    return {
        "updated": now.strftime("%d %b %Y %H:%M:%S"),
        "status": "online" if online_count else "offline",
        "onlineCount": online_count,
        "meterCount": len(meters),
        "totals": totals,
        "machines": meters,
        "history": list(_history),
        "alarms": [],
        "dr134": {"host": _active_host(), "port": MODBUS_PORT, "error": _last_error},
        "storage": {
            "host": storage.DB_HOST,
            "port": storage.DB_PORT,
            "database": storage.DB_NAME,
            "sampleMinutes": storage.SAMPLE_MINUTES,
            "dayEndHour": storage.DAY_END_HOUR,
            **_storage_state,
        },
    }


def _current_snapshot() -> dict:
    with _state_lock:
        if _latest is not None:
            return _latest
    return {
        "updated": "--",
        "status": "connecting",
        "onlineCount": 0,
        "meterCount": len(METER_CONFIG),
        "totals": {"kw": None, "kva": None, "kwh": None, "kwh_t1": None, "kwh_t2": None, "pf": None},
        "machines": [_empty_meter(meter, "Menunggu pembacaan pertama ...") for meter in METER_CONFIG],
        "history": [],
        "alarms": [],
        "dr134": {"host": MANUAL_HOST, "port": MODBUS_PORT, "error": None},
        "storage": {
            "host": storage.DB_HOST,
            "port": storage.DB_PORT,
            "database": storage.DB_NAME,
            "sampleMinutes": storage.SAMPLE_MINUTES,
            "dayEndHour": storage.DAY_END_HOUR,
            **_storage_state,
        },
    }


def _poll_loop() -> None:
    global _latest
    while True:
        try:
            meters = _poll_all_meters()
            snapshot = _build_snapshot(meters)
        except Exception:  # keep the poller alive whatever the meter does
            app.logger.exception("Polling meter gagal.")
        else:
            with _state_lock:
                _latest = snapshot
            _record_history(meters)
        time.sleep(POLL_INTERVAL_SECONDS)


def start_poller() -> None:
    global _poller_started, _storage_state
    with _poller_lock:
        if _poller_started:
            return
        _poller_started = True
    try:
        storage.init_schema()
        _storage_state = {"ok": True, "error": None}
    except storage.StorageError as exc:
        app.logger.error("Database tidak siap, riwayat tidak dicatat: %s", exc)
        _storage_state = {"ok": False, "error": str(exc)}
    threading.Thread(target=_poll_loop, name="meter-poller", daemon=True).start()


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/data")
def data():
    # Serve the snapshot from the background poller; never touch the meter here.
    start_poller()
    return jsonify(_current_snapshot())


@app.route("/api/discover")
def discover():
    """Force a network scan and report what was found. Useful for diagnostics."""
    if MANUAL_HOST:
        return jsonify({
            "host": MANUAL_HOST,
            "port": MODBUS_PORT,
            "manual": True,
            "reachable": _modbus_probe(MANUAL_HOST),
        })

    candidates = _candidate_hosts()
    with concurrent.futures.ThreadPoolExecutor(max_workers=DISCOVERY_MAX_WORKERS) as pool:
        open_hosts = [
            host for host, is_open in zip(candidates, pool.map(_port_open, candidates)) if is_open
        ]
    probed = {host: _modbus_probe(host) for host in open_hosts}
    found = next((host for host, ok in probed.items() if ok), None)

    if found:
        global _current_host
        with _host_lock:
            _current_host = found

    return jsonify({
        "host": found,
        "port": MODBUS_PORT,
        "manual": False,
        "scanned": len(candidates),
        "networks": [str(network) for network in _scan_networks()],
        "openHosts": open_hosts,
        "probed": probed,
    })


def _report_args() -> tuple[int, int]:
    today = datetime.now()
    year = request.args.get("year", type=int) or today.year
    month = request.args.get("month", type=int) or today.month
    return year, min(max(month, 1), 12)


@app.route("/api/report")
def report_data():
    year, month = _report_args()
    try:
        return jsonify(reporting.build_report(year, month, METER_CONFIG))
    except storage.StorageError as exc:
        return jsonify({"error": str(exc)}), 503


@app.route("/report.xlsx")
def report_excel():
    year, month = _report_args()
    try:
        data = reporting.build_report(year, month, METER_CONFIG)
        stream = reporting.build_workbook(data)
    except storage.StorageError as exc:
        return jsonify({"error": str(exc)}), 503
    return send_file(
        stream,
        as_attachment=True,
        download_name=f"kwh-{year}-{month:02d}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _selected_meter(raw: str | None) -> str:
    meter_id = (raw or "all").strip()
    known = {meter["id"] for meter in METER_CONFIG}
    return meter_id if (meter_id == "all" or meter_id in known) else "all"


def _profile_date(raw: str | None) -> date:
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.now().date()


@app.route("/api/profile")
def profile_data():
    """Average power per hour for one reporting day."""
    try:
        result = load_profile.hourly_profile(
            _profile_date(request.args.get("date")),
            METER_CONFIG,
            _selected_meter(request.args.get("meter")),
        )
    except storage.StorageError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify(result)


@app.route("/api/profile/month")
def profile_month():
    """Monthly heatmap: one row per day, 24 hourly kilowatt values."""
    today = datetime.now()
    year = request.args.get("year", type=int) or today.year
    month = min(max(request.args.get("month", type=int) or today.month, 1), 12)
    try:
        result = load_profile.monthly_heatmap(
            year, month, METER_CONFIG, _selected_meter(request.args.get("meter"))
        )
    except storage.StorageError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify(result)


@app.route("/profile.xlsx")
def profile_excel():
    """Workbook with the hourly profile and the monthly heatmap."""
    day = _profile_date(request.args.get("date"))
    meter = _selected_meter(request.args.get("meter"))
    today = datetime.now()
    year = request.args.get("year", type=int) or today.year
    month = min(max(request.args.get("month", type=int) or today.month, 1), 12)
    try:
        profile = load_profile.hourly_profile(day, METER_CONFIG, meter)
        heatmap = load_profile.monthly_heatmap(year, month, METER_CONFIG, meter)
        stream = reporting.build_profile_workbook(profile, heatmap)
    except storage.StorageError as exc:
        return jsonify({"error": str(exc)}), 503
    return send_file(
        stream,
        as_attachment=True,
        download_name=f"load-profile-{day.isoformat()}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    # Poll in the background so web requests never block on the meter.
    start_poller()
    # Disable the reloader so it does not create duplicate polling processes.
    app.run(debug=False, use_reloader=False, host=storage.DASHBOARD_HOST, port=storage.DASHBOARD_PORT)
