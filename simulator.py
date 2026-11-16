"""EPMS 시뮬레이터: 건물 전력 모델 기반 계층 일관성 + Modbus TCP 서버 + HTTP 제어 API."""
import json
import os
import random
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusSlaveContext, ModbusServerContext
from pymodbus.server import StartTcpServer

from register_map import INPUT_REGISTERS, REGISTER_COUNT, encode_float32

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(os.getenv("MODBUS_PORT", "5020"))
CONTROL_PORT = int(os.getenv("CONTROL_PORT", "8080"))
UPDATE_PERIOD_S = 1.0
FC_DISCRETE_INPUT = 2
FC_INPUT_REGISTER = 4

NOMINAL_V = 230.0
NOMINAL_FREQ = 60.0

# 건물 전체 부하 (계층 합산의 기준값)
BLDG_BASE_KW = 30.0     # 야간 기본 부하
BLDG_MAX_KW = 520.0     # 피크 부하 (변압기 정격 500kW 기준 80% 초과 시연용)

OCCUPANCY_CURVE = {16: 0.80, 17: 0.70, 18: 0.30, 19: 0.10, 20: 0.05, 21: 0.03, 22: 0.01}

# 계층: 분전반(feeder, 5~15) → 저압 주배전반(3) → 변압기 1호(2) → 수전반(1)
# 역률보상반(4)은 전력을 흘리지 않는 알람 장비
DEVICE_PROFILES = {
    1:  {"name": "B01_HV",   "role": "bus",    "di": ["ST"],         "kwh0": 152340.0},
    2:  {"name": "B01_TR01", "role": "bus",    "di": ["ALM"],        "kwh0": 88210.0},
    3:  {"name": "B01_LV",   "role": "bus",    "di": ["ST", "TRIP"], "kwh0": 91005.0},
    4:  {"name": "B01_APFC", "role": "zero",   "di": ["ALM"],        "kwh0": 0.0},
    5:  {"name": "B01_DB",   "role": "feeder", "weight": 0.07, "di": [],     "kwh0": 12000.0},
    6:  {"name": "FL01_DB",  "role": "feeder", "weight": 0.06, "di": [],     "kwh0": 8000.0},
    7:  {"name": "FL19_DB",  "role": "feeder", "weight": 0.08, "di": ["ST"], "kwh0": 9000.0},
    8:  {"name": "FL20_DB",  "role": "feeder", "weight": 0.08, "di": [],     "kwh0": 9000.0},
    9:  {"name": "FL21_DB",  "role": "feeder", "weight": 0.08, "di": [],     "kwh0": 9000.0},
    10: {"name": "FL22_DB",  "role": "feeder", "weight": 0.08, "di": [],     "kwh0": 9000.0},
    11: {"name": "FL23_DB",  "role": "feeder", "weight": 0.08, "di": [],     "kwh0": 9000.0},
    12: {"name": "FL24_DB",  "role": "feeder", "weight": 0.08, "di": [],     "kwh0": 9000.0},
    13: {"name": "FL25_DB",  "role": "feeder", "weight": 0.08, "di": [],     "kwh0": 9000.0},
    14: {"name": "B02_DB",   "role": "feeder", "weight": 0.10, "di": [],     "kwh0": 5000.0},
    15: {"name": "B03_DB",   "role": "feeder", "weight": 0.10, "di": [],     "kwh0": 5000.0},
}

FEEDER_UNITS = [u for u, p in DEVICE_PROFILES.items() if p["role"] == "feeder"]

DI_DEFAULT = {"ST": 1, "TRIP": 0, "ALM": 0}
ANALOG_FAULTS = ("overcurrent", "voltage_sag", "low_pf")
STATUS_FAULTS = ("trip", "alarm", "open")

SCENARIO_MAP = {
    "OVERCURRENT": "overcurrent",
    "VOLTAGE_SAG": "voltage_sag",
    "LOW_PF": "low_pf",
    "TRIP": "trip",
    "ALARM": "alarm",
    "OPEN": "open",
    "CLEAR": "clear",
}

SIMULATORS: dict[int, "EpmsSimulator"] = {}


def occupancy_at(now: datetime) -> float:
    h, m = now.hour, now.minute
    if h in OCCUPANCY_CURVE and (h + 1) in OCCUPANCY_CURVE:
        a, b = OCCUPANCY_CURVE[h], OCCUPANCY_CURVE[h + 1]
        return a + (b - a) * (m / 60)
    return OCCUPANCY_CURVE.get(h, 0.05)


def build_values(p_w: float, occ: float, faults: list[str], phase_noise: bool) -> dict[str, float]:
    """전력(W)을 받아 3상 값을 만든다. phase_noise=False면 상별 합이 입력 전력과 정확히 일치한다."""
    pf = 0.85 + 0.10 * occ + random.uniform(-0.01, 0.01)
    volt = NOMINAL_V + random.uniform(-0.5, 0.5)
    freq = NOMINAL_FREQ + random.uniform(-0.02, 0.02)

    if "voltage_sag" in faults:
        volt *= 0.83
    if "low_pf" in faults:
        pf = 0.70

    values = {}
    for ph in (1, 2, 3):
        p_ph = p_w / 3
        if phase_noise:
            p_ph *= random.uniform(0.95, 1.05)
        i_ph = p_ph / (volt * pf)
        if "overcurrent" in faults:
            i_ph *= 1.25
        v_ph = volt * random.uniform(0.995, 1.005) if phase_noise else volt
        values[f"v_l{ph}"] = v_ph
        values[f"i_l{ph}"] = i_ph
        values[f"p_l{ph}"] = p_ph

    values["pf_l1"] = pf
    values["p_total"] = values["p_l1"] + values["p_l2"] + values["p_l3"]
    values["s_total"] = values["p_total"] / pf
    values["freq"] = freq
    return values


class EpmsSimulator:
    def __init__(self, unit: int, profile: dict):
        self.unit = unit
        self.name = profile["name"]
        self.role = profile["role"]
        self.di_names = list(profile["di"])
        self.kwh = profile["kwh0"]
        self._faults: set[str] = set()
        self._di = {n: DI_DEFAULT[n] for n in self.di_names}
        self._lock = threading.Lock()

    def inject(self, fault: str) -> None:
        if fault != "clear" and fault not in ANALOG_FAULTS + STATUS_FAULTS:
            raise ValueError(f"unknown fault: {fault}")
        with self._lock:
            if fault == "clear":
                self._faults.clear()
                self._di = {n: DI_DEFAULT[n] for n in self.di_names}
                return
            self._faults.add(fault)
            if fault == "trip" and "TRIP" in self._di:
                self._di["TRIP"] = 1
                if "ST" in self._di:
                    self._di["ST"] = 0
            elif fault == "alarm" and "ALM" in self._di:
                self._di["ALM"] = 1
            elif fault == "open" and "ST" in self._di:
                self._di["ST"] = 0

    def active_faults(self) -> list[str]:
        with self._lock:
            return sorted(self._faults)

    def di_snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._di)

    def accumulate(self, p_total_w: float) -> None:
        self.kwh += p_total_w / 1000.0 * UPDATE_PERIOD_S / 3600.0

    def write_to(self, slave: ModbusSlaveContext, values: dict[str, float]) -> None:
        values = dict(values)
        values["kwh_import"] = self.kwh
        for reg in INPUT_REGISTERS:
            slave.setValues(FC_INPUT_REGISTER, reg.address, encode_float32(values[reg.key]))
        di = self.di_snapshot()
        for idx, name in enumerate(self.di_names):
            slave.setValues(FC_DISCRETE_INPUT, idx, [di[name]])


class Building:
    """건물 전체 부하를 계산하고 계층 순서대로 합산한다."""

    def __init__(self, sims: dict[int, EpmsSimulator]):
        self.sims = sims

    def faults(self, unit: int) -> list[str]:
        return self.sims[unit].active_faults()

    def tick(self, now: datetime) -> dict[int, dict[str, float]]:
        occ = occupancy_at(now)
        bldg_kw = (BLDG_BASE_KW + (BLDG_MAX_KW - BLDG_BASE_KW) * occ) * random.uniform(0.98, 1.02)
        wsum = sum(DEVICE_PROFILES[u]["weight"] for u in FEEDER_UNITS)

        out = {}
        # 1) 분전반: 건물 부하를 가중치로 나눔
        for u in FEEDER_UNITS:
            share = DEVICE_PROFILES[u]["weight"] / wsum
            p_w = bldg_kw * 1000 * share * random.uniform(0.97, 1.03)
            out[u] = build_values(p_w, occ, self.faults(u), phase_noise=True)

        # 2) 저압 주배전반 = 분전반 합계
        lv_w = sum(out[u]["p_total"] for u in FEEDER_UNITS)
        out[3] = build_values(lv_w, occ, self.faults(3), phase_noise=False)

        # 3) 변압기 1호 = 저압반, 4) 수전반 = 변압기
        out[2] = build_values(out[3]["p_total"], occ, self.faults(2), phase_noise=False)
        out[1] = build_values(out[2]["p_total"], occ, self.faults(1), phase_noise=False)

        # 5) 역률보상반: 전력 없음
        out[4] = build_values(0.0, occ, self.faults(4), phase_noise=False)
        return out


def apply_fault(target: str, fault: str) -> dict[str, list[str]]:
    target = target.strip().lower()
    if target == "all":
        units = sorted(SIMULATORS)
    else:
        try:
            unit = int(target)
        except ValueError:
            raise ValueError(f"invalid unit: {target}")
        if unit not in SIMULATORS:
            raise KeyError(unit)
        units = [unit]

    for unit in units:
        SIMULATORS[unit].inject(fault)
    return {str(unit): SIMULATORS[unit].active_faults() for unit in units}


def build_context():
    slaves = {}
    for unit_id in DEVICE_PROFILES:
        slaves[unit_id] = ModbusSlaveContext(
            di=ModbusSequentialDataBlock(0, [0] * 16),
            co=ModbusSequentialDataBlock(0, [0] * 16),
            hr=ModbusSequentialDataBlock(0, [0] * 16),
            ir=ModbusSequentialDataBlock(0, [0] * REGISTER_COUNT),
            zero_mode=True,
        )
    context = ModbusServerContext(slaves=slaves, single=False)
    return context, slaves


def run_loop(building: Building, slaves: dict) -> None:
    while True:
        try:
            out = building.tick(datetime.now())
            for unit, values in out.items():
                sim = SIMULATORS[unit]
                sim.accumulate(values["p_total"])
                sim.write_to(slaves[unit], values)
        except Exception:
            traceback.print_exc()
        time.sleep(UPDATE_PERIOD_S)


def control_loop() -> None:
    print("이상 주입 명령: <unit|all> <fault>  (fault: overcurrent, voltage_sag, low_pf, trip, alarm, open, clear)", flush=True)
    while True:
        try:
            line = input().strip()
        except EOFError:
            return
        if not line:
            continue
        try:
            target, fault = line.split()
            result = apply_fault(target, fault)
            print(f"[inject] target={target} fault={fault} -> {result}", flush=True)
        except (ValueError, KeyError) as e:
            print(f"[error] {e}  (형식: <unit|all> <fault>)", flush=True)


class ControlHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/inject":
            self._send_json(404, {"error": "not found"})
            return

        params = parse_qs(url.query)
        scenario = params.get("scenario", [""])[0].upper()
        target = params.get("unit", [""])[0]

        if scenario not in SCENARIO_MAP:
            self._send_json(400, {"error": f"unknown scenario: {scenario}",
                                  "allowed": list(SCENARIO_MAP)})
            return

        try:
            results = apply_fault(target, SCENARIO_MAP[scenario])
        except ValueError as e:
            self._send_json(400, {"error": str(e),
                                  "units": [str(u) for u in sorted(SIMULATORS)] + ["all"]})
            return
        except KeyError as e:
            self._send_json(404, {"error": f"unit {e.args[0]} not found",
                                  "units": sorted(SIMULATORS)})
            return

        self._send_json(200, {"unit": target.lower(), "scenario": scenario, "results": results})

    def do_GET(self):
        if urlparse(self.path).path != "/status":
            self._send_json(404, {"error": "not found"})
            return
        status = {
            str(unit): {"name": sim.name,
                        "faults": sim.active_faults(),
                        "di": sim.di_snapshot()}
            for unit, sim in SIMULATORS.items()
        }
        self._send_json(200, status)

    def log_message(self, format, *args):
        return


def start_control_server() -> None:
    server = ThreadingHTTPServer((LISTEN_HOST, CONTROL_PORT), ControlHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[control] HTTP API listening on :{CONTROL_PORT}", flush=True)


if __name__ == "__main__":
    context, slaves = build_context()
    for unit, profile in DEVICE_PROFILES.items():
        SIMULATORS[unit] = EpmsSimulator(unit, profile)

    building = Building(SIMULATORS)
    threading.Thread(target=run_loop, args=(building, slaves), daemon=True).start()
    threading.Thread(target=control_loop, daemon=True).start()
    start_control_server()
    StartTcpServer(context=context, address=(LISTEN_HOST, LISTEN_PORT))
