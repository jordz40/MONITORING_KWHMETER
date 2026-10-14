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
from datetime import datetime

from flask import Flask, jsonify, render_template

app = Flask(__name__)

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
MODBUS_TIMEOUT_SECONDS = 3.0
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
# scan cycle, so the Modbus probe waits longer than a plain port check.
DISCOVERY_CONNECT_TIMEOUT_SECONDS = 0.5
DISCOVERY_PROBE_TIMEOUT_SECONDS = 3.0
DISCOVERY_MAX_WORKERS = 64
DISCOVERY_COOLDOWN_SECONDS = 15.0
DISCOVERY_MAX_HOSTS = 1024

# One entry per physical meter. Every meter on the shared RS-485 bus needs a
# unique Modbus Slave ID.
METER_CONFIG = [
    {"id": "M01", "name": "Mesin Trimming HDL 3", "slave_id": 1},
    {"id": "M02", "name": "Mesin Extruder", "slave_id": 2},
    {"id": "M03", "name": "Mesin Coating", "slave_id": 3},
    {"id": "M04", "name": "Mesin Trimming HDL 1", "slave_id": 4},
    {"id": "M05", "name": "Mesin Trimming HDL 2", "slave_id": 5},
]

# iEM3155 Modbus PDU (zero-based) addresses, matching the register list. Each
# Float32 is two 16-bit registers, high word first. Modbus Poll shows these as
# one-based (3026, 3060, ...).
REGISTER_MAP = {
    "voltage": 3025,       # Average voltage L-L, V
    "kw": 3059,            # Total active power, kW
    "kva": 3075,           # Total apparent power, kVA
    "pf": 3083,            # Total power factor
    "kwh": 45099,          # Total active energy import, kWh
}

# The four power/voltage registers sit close together, so read them in one
# block. One block instead of four requests halves the traffic on the shared
# RS-485 line and keeps every value from the same frame.
VALUE_BLOCK_START = 3025
VALUE_BLOCK_COUNT = 60                        # 3025..3084 (pf is a 2-register float)
VALUE_OFFSETS = {
    key: (address - VALUE_BLOCK_START) * 2
    for key, address in REGISTER_MAP.items() if key != "kwh"
}
ENERGY_BLOCK_START = 45099
ENERGY_BLOCK_COUNT = 2

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

    time.sleep(MODBUS_REQUEST_DELAY_SECONDS)
    try:
        block = _read_block(sock, meter["slave_id"], ENERGY_BLOCK_START, ENERGY_BLOCK_COUNT)
        value = struct.unpack(">f", block[0:4])[0]
        readings["kwh"] = value if math.isfinite(value) else None
    except (OSError, ModbusError, struct.error) as exc:
        errors.append(f"energi: {exc}")

    online = any(value is not None for value in readings.values())
    return {**meter, **readings, "online": online, "error": "; ".join(errors) or None}


def _empty_meter(meter: dict, error: str) -> dict:
    return {
        **meter,
        "kw": None,
        "kva": None,
        "pf": None,
        "voltage": None,
        "kwh": None,
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


def _modbus_probe(host: str) -> bool:
    """True when the host answers our Modbus TCP request.

    Any well-formed frame carrying our transaction id counts, even an exception
    reply, because that still proves a Modbus server is listening.
    """
    probe_transaction = 0xFFFF
    deadline = time.monotonic() + DISCOVERY_PROBE_TIMEOUT_SECONDS
    try:
        with socket.create_connection((host, MODBUS_PORT), timeout=DISCOVERY_PROBE_TIMEOUT_SECONDS) as sock:
            sock.settimeout(DISCOVERY_PROBE_TIMEOUT_SECONDS)
            while time.monotonic() < deadline:
                request = struct.pack(">HHHBBHH", probe_transaction, 0, 6, METER_CONFIG[0]["slave_id"], 3, REGISTER_MAP["voltage"], 2)
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

    total_kw = total("kw")
    total_kva = total("kva")
    voltages = [meter["voltage"] for meter in online if meter["voltage"] is not None]

    return {
        "kw": total_kw,
        "kva": total_kva,
        "kwh": total("kwh"),
        "voltage": sum(voltages) / len(voltages) if voltages else None,
        "pf": total_kw / total_kva if total_kw is not None and total_kva else None,
    }


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
        "status": "online" if online_count == len(meters) else "offline",
        "onlineCount": online_count,
        "meterCount": len(meters),
        "totals": totals,
        "machines": meters,
        "history": list(_history),
        "alarms": [],
        "dr134": {"host": _active_host(), "port": MODBUS_PORT, "error": _last_error},
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
        "totals": {"kw": None, "kva": None, "kwh": None, "voltage": None, "pf": None},
        "machines": [_empty_meter(meter, "Menunggu pembacaan pertama ...") for meter in METER_CONFIG],
        "history": [],
        "alarms": [],
        "dr134": {"host": MANUAL_HOST, "port": MODBUS_PORT, "error": None},
    }


def _poll_loop() -> None:
    global _latest
    while True:
        try:
            snapshot = _build_snapshot(_poll_all_meters())
        except Exception:  # keep the poller alive whatever the meter does
            app.logger.exception("Polling meter gagal.")
        else:
            with _state_lock:
                _latest = snapshot
        time.sleep(POLL_INTERVAL_SECONDS)


def start_poller() -> None:
    global _poller_started
    with _poller_lock:
        if _poller_started:
            return
        _poller_started = True
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


if __name__ == "__main__":
    # Poll in the background so web requests never block on the meter.
    start_poller()
    # Disable the reloader so it does not create duplicate polling processes.
    app.run(debug=False, use_reloader=False, host="127.0.0.1", port=5000)
