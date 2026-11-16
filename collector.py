"""Modbus 수집기: point_map.yaml 기준으로 장비를 폴링해 InfluxDB epms에 적재."""
import psycopg2
import psycopg2.extras
import logging
import os
import time
from datetime import datetime, timezone

import yaml
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from pymodbus.client import ModbusTcpClient

from register_map import INPUT_REGISTERS, decode_float32

MODBUS_HOST = os.getenv("MODBUS_HOST", "127.0.0.1")
MODBUS_PORT = int(os.getenv("MODBUS_PORT", "502"))
POLL_INTERVAL_S = float(os.getenv("POLL_INTERVAL_S", "5"))
PG_DSN = os.getenv("PG_DSN", "")   # 예: postgresql://user:pw@postgres:5432/epms

INFLUX_URL = os.getenv("INFLUX_URL", "http://influxdb:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
INFLUX_ORG = os.getenv("INFLUX_ORG", "")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "node_red_bucket")
POINT_MAP_PATH = os.getenv("POINT_MAP", "point_map.yaml")

# Modbus 1회 읽기 최대 125워드를 넘지 않도록 블록을 나눔
READ_BLOCKS = [(0x0000, 0x0048), (0x0156, 0x0002)]
DI_COUNT = 4

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("collector")


#with open(POINT_MAP_PATH, encoding="utf-8") as f:
#    CFG = yaml.safe_load(f)

def load_point_map_from_db(dsn: str) -> dict:
    """tb_point / tb_asset / tb_space / tb_site 를 조인해 point_map과 같은 구조를 만든다."""
    sql = """
        SELECT p.point_id,p.point_name,
               p.unit,
               p.modbus_type,
               p.modbus_key,
               p.modbus_address,
               p.scale,
               a.asset_name,
               a.modbus_unit_id,
               s.site_name
        FROM tb_point p
        JOIN tb_asset  a  ON a.asset_id  = p.asset_id
        JOIN tb_space  sp ON sp.space_id = a.space_id
        JOIN tb_site   s  ON s.site_id   = sp.site_id
        WHERE p.modbus_type IS NOT NULL
          AND a.modbus_unit_id IS NOT NULL
        ORDER BY a.modbus_unit_id, p.point_name
    """
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        raise RuntimeError("tb_point에 Modbus 매핑된 포인트가 없습니다")

    sites = {r["site_name"] for r in rows}
    if len(sites) != 1:
        raise RuntimeError(f"수집 대상 사이트가 하나여야 합니다: {sites}")

    points = {}
    for r in rows:
        m = {
            "asset": r["asset_name"],
            "unit_id": r["modbus_unit_id"],
            "label": r["unit"] or "",
        "point_name": r["point_name"],
        }
        if r["modbus_type"] == "di":
            m.update(type="di", address=r["modbus_address"])
        else:
            m.update(key=r["modbus_key"], scale=float(r["scale"]))
        points[str(r["point_id"])] = m
    return {"site_id": sites.pop(), "points": points}


def load_point_map() -> dict:
    """DB를 우선 사용하고, 실패하면 YAML로 대체한다."""
    if PG_DSN:
        try:
            cfg = load_point_map_from_db(PG_DSN)
            log.info("point_map: DB에서 %d개 포인트 로드", len(cfg["points"]))
            return cfg
        except Exception:
            log.exception("DB 로드 실패, YAML로 대체합니다")

    with open(POINT_MAP_PATH, encoding="utf-8") as f:
        log.info("point_map: YAML에서 로드")
        return yaml.safe_load(f)


CFG = load_point_map()
REG_BY_KEY = {r.key: r for r in INPUT_REGISTERS}
UNITS = sorted({m["unit_id"] for m in CFG["points"].values()})

def read_unit(client: ModbusTcpClient, unit: int):
    words = {}
    for start, count in READ_BLOCKS:
        rr = client.read_input_registers(start, count=count, slave=unit)
        if rr.isError():
            raise IOError(f"IR {start:#06x}: {rr}")
        for i, w in enumerate(rr.registers):
            words[start + i] = w

    di = client.read_discrete_inputs(0, count=DI_COUNT, slave=unit)
    if di.isError():
        raise IOError(f"DI: {di}")
    return words, di.bits


def build_points(data: dict, ts: datetime) -> list[Point]:
    points = []
    for point_id, m in CFG["points"].items():
        if m["unit_id"] not in data:
            continue
        words, bits = data[m["unit_id"]]

        if m.get("type") == "di":
            value = float(bits[m["address"]])
        else:
            reg = REG_BY_KEY[m["key"]]
            raw = decode_float32(words[reg.address], words[reg.address + 1])
            value = raw * m["scale"]

        points.append(
            Point("epms")
            .tag("site_id", CFG["site_id"])
            .tag("asset_id", m["asset"])
            .tag("point_id", point_id)
            .tag("point_name", m.get("point_name", ""))
            .tag("unit", m.get("label", ""))
            .field("value", float(value))
            .time(ts)
        )
    return points


def main() -> None:
    modbus = ModbusTcpClient(MODBUS_HOST, port=MODBUS_PORT)
    influx = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    write_api = influx.write_api(write_options=SYNCHRONOUS)
    log.info("start: units=%s points=%d", UNITS, len(CFG["points"]))

    while True:
        try:
            if not modbus.connected:
                modbus.connect()

            ts = datetime.now(timezone.utc)
            data = {}
            for unit in UNITS:
                try:
                    data[unit] = read_unit(modbus, unit)
                except Exception as e:
                    log.warning("unit %s 읽기 실패: %s", unit, e)

            points = build_points(data, ts)
            if points:
                write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=points)
                log.info("적재 %d개 포인트", len(points))
        except Exception:
            log.exception("수집 주기 오류")

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
