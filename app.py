"""Factory energy dashboard backed by Schneider iEM3155 meters via USR-DR134."""

from __future__ import annotations

import socket
import struct
import threading
import math
from datetime import datetime

from flask import Flask, jsonify, render_template

app = Flask(__name__)

# DR134 Modbus TCP endpoint. Override these if the network configuration changes.
MODBUS_HOST = "192.168.1.181"
MODBUS_PORT = 502
MODBUS_TIMEOUT_SECONDS = 2.0

# Only ID 1 is enabled because that is the meter currently tested in Modbus Poll.
# Add entries after assigning unique Slave IDs to the other physical meters.
METER_CONFIG = [
    {"id": "M01", "name": "TEST METER · ID 1", "slave_id": 1},
]

# iEM3155 addresses are zero-based Modbus PDU addresses. Each Float32 is two
# 16-bit registers, with the high word first.
REGISTER_MAP = {
    "voltage": 3025,       # Average voltage L-L, V
    "kw": 3059,            # Total active power, kW
    "kva": 3075,           # Total apparent power, kVA
    "pf": 3083,            # Total power factor
    "kwh": 45099,          # Total active energy import, kWh
}

_poll_lock = threading.Lock()
_history = []
_transaction_id = 0


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


def _read_float32(sock: socket.socket, slave_id: int, address: int) -> float:
    global _transaction_id
    _transaction_id = (_transaction_id + 1) & 0xFFFF
    transaction_id = _transaction_id

    # MBAP header + FC03 request for two registers.
    request = struct.pack(">HHHBBHH", transaction_id, 0, 6, slave_id, 3, address, 2)
    sock.sendall(request)

    header = _recv_exact(sock, 7)
    response_transaction, protocol_id, length = struct.unpack(">HHH", header[:6])
    response_unit = header[6]
    if response_transaction != transaction_id or protocol_id != 0:
        raise ModbusError("Header respons Modbus TCP tidak sesuai permintaan.")
    if length < 2:
        raise ModbusError("Respons Modbus TCP terlalu pendek.")

    pdu = _recv_exact(sock, length - 1)
    function = pdu[0]
    if function == 0x83:
        code = pdu[1] if len(pdu) > 1 else "tidak diketahui"
        raise ModbusError(f"Meter ID {response_unit} menolak register {address} (exception {code}).")
    if response_unit != slave_id or function != 3 or len(pdu) != 6 or pdu[1] != 4:
        raise ModbusError(f"Format respons tidak valid untuk Slave ID {slave_id}, register {address}.")

    return struct.unpack(">f", pdu[2:6])[0]


def _read_meter(sock: socket.socket, meter: dict) -> dict:
    readings = {
        key: _read_float32(sock, meter["slave_id"], address)
        for key, address in REGISTER_MAP.items()
    }
    # Python's JSON encoder emits NaN as the bare token NaN, which is not valid
    # JSON and causes browser response.json() to reject the entire API payload.
    readings = {
        key: value if math.isfinite(value) else None
        for key, value in readings.items()
    }
    return {**meter, **readings, "online": True, "error": None}


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


def _poll_all_meters() -> list[dict]:
    # One TCP connection and one Modbus master poll the meters sequentially.
    # This avoids each dashboard browser tab opening its own meter connection.
    with _poll_lock:
        try:
            with socket.create_connection(
                (MODBUS_HOST, MODBUS_PORT), timeout=MODBUS_TIMEOUT_SECONDS
            ) as sock:
                sock.settimeout(MODBUS_TIMEOUT_SECONDS)
                results = []
                for meter in METER_CONFIG:
                    try:
                        results.append(_read_meter(sock, meter))
                    except (OSError, ModbusError, struct.error) as exc:
                        results.append(_empty_meter(meter, str(exc)))
                return results
        except OSError as exc:
            message = f"Tidak dapat terhubung ke DR134 {MODBUS_HOST}:{MODBUS_PORT}: {exc}"
            return [_empty_meter(meter, message) for meter in METER_CONFIG]


def _aggregate(meters: list[dict]) -> dict:
    online = [meter for meter in meters if meter["online"]]
    if not online:
        return {"kw": None, "kva": None, "kwh": None, "voltage": None, "pf": None}

    total_kw = sum(meter["kw"] for meter in online)
    total_kva = sum(meter["kva"] for meter in online)
    return {
        "kw": total_kw,
        "kva": total_kva,
        "kwh": sum(meter["kwh"] for meter in online),
        "voltage": sum(meter["voltage"] for meter in online) / len(online),
        "pf": total_kw / total_kva if total_kva else None,
    }


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/data")
def data():
    meters = _poll_all_meters()
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

    return jsonify({
        "updated": now.strftime("%d %b %Y %H:%M:%S"),
        "status": "online" if online_count == len(meters) else "offline",
        "onlineCount": online_count,
        "meterCount": len(meters),
        "totals": totals,
        "machines": meters,
        "history": _history,
        "alarms": [],
    })


if __name__ == "__main__":
    # Disable the reloader so it does not create duplicate polling processes.
    app.run(debug=False, use_reloader=False, host="127.0.0.1", port=5000)
