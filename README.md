# EPMS Modbus 시뮬레이터 & 수집기

스마트빌딩 데모를 위한 전력 관리 시스템(EPMS) 시뮬레이터입니다. 건물 전력 계층(수전반 → 변압기 → 저압 주배전반 → 분전반)을 모사하고, Eastron SDM630 기준 레지스터 맵으로 Modbus TCP 서버를 제공합니다. 수집기는 DB에 정의된 포인트 설정을 읽어 장비를 폴링한 뒤 InfluxDB에 적재합니다.

## 주요 기능

- **건물 전력 모델:** 시간대별 점유율 곡선을 기준으로 건물 전체 부하를 계산하고, 계층 순서대로 합산합니다.
- **Modbus TCP 서버:** 장비 15대(unit ID 1~15)를 각각 독립된 슬레이브로 제공합니다.
- **이상 상황 주입:** HTTP API 또는 콘솔 명령으로 과전류, 전압 저하, 역률 저하, 트립, 알람, 개방 상태를 주입하고 해제할 수 있습니다.
- **설정 기반 수집:** PostgreSQL의 포인트 정보를 우선 사용하고, 실패 시 `point_map.yaml`로 대체합니다.
- **시계열 적재:** 수집한 값을 InfluxDB `epms` measurement에 저장하여 Grafana에서 시각화합니다.

## 아키텍처

```
[epms-modbus-sim]  ── Modbus TCP (502) ──>  [epms-modbus-collector]
  전력 모델 계산                              포인트 맵 로드 (PostgreSQL / YAML)
  Input Register / Discrete Input           5초 주기 폴링
  HTTP 제어 API (8080)                        │
                                              ▼
                                       [InfluxDB: node_red_bucket / epms]
                                              │
                                              ▼
                                           [Grafana]
```

## 장비 계층

| Unit | 장비명 | 역할 | 비고 |
|---|---|---|---|
| 1 | B01_HV | 수전반 | 최상위, ST 상태 |
| 2 | B01_TR01 | 변압기 1호 | 알람 |
| 3 | B01_LV | 저압 주배전반 | ST, TRIP |
| 4 | B01_APFC | 역률보상반 | 전력 없음, 알람만 |
| 5 | B01_DB | 분전반 | 가중치 0.07 |
| 6 | FL01_DB | 분전반 | |
| 7 | FL19_DB | 분전반 | ST |
| 8~13 | FL20~FL25_DB | 분전반 | |
| 14 | B02_DB | 분전반 | |
| 15 | B03_DB | 분전반 | |

분전반(5~15번)이 건물 부하를 가중치로 나누어 가지며, 그 합이 상위 계층으로 올라갑니다.

## 디렉터리 구조

```
.
├── simulator.py        # 전력 모델, Modbus 서버, HTTP 제어 API
├── collector.py        # Modbus 폴링, InfluxDB 적재
├── register_map.py     # 입력 레지스터 맵, float32 인코딩/디코딩
├── point_map.yaml      # 포인트 설정 (DB 실패 시 대체용)
├── requirements.txt
├── Dockerfile
└── docker-compose.yml  # (상위 디렉터리) 전체 서비스 정의
└── .env_example        # influxdb, postgres,grafana 등 credential
```

## 요구 사항

- Docker, Docker Compose
- InfluxDB 2.x, PostgreSQL 15 (compose에 포함)
- Python 3.12 (컨테이너 내 실행 시 불필요)

## 환경 변수

`.env` 파일에 아래 값을 설정합니다.

| 변수 | 설명 |
|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | 포인트 설정을 읽을 PostgreSQL 접속 정보 |
| `INFLUXDB_ORGANIZATION` | InfluxDB 조직 이름 |
| `INFLUX_COLLECTOR_TOKEN` | 수집기가 InfluxDB에 쓰기 위한 토큰 |

시뮬레이터와 수집기의 주요 환경 변수는 다음과 같습니다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `MODBUS_PORT` | `5020` (Dockerfile에서 `502`) | Modbus TCP 포트 |
| `CONTROL_PORT` | `8080` | 제어 API 포트 |
| `MODBUS_HOST` | `127.0.0.1` | 수집기가 접속할 시뮬레이터 주소 |
| `POLL_INTERVAL_S` | `5` | 수집 주기(초) |
| `PG_DSN` | (없음) | 포인트 설정 DB 접속 문자열 |
| `POINT_MAP` | `point_map.yaml` | YAML 대체 파일 경로 |

## 실행 방법

```bash
# 시뮬레이터와 수집기 빌드 및 실행
docker compose up -d --build epms-modbus-sim epms-modbus-collector

# 로그 확인
docker compose logs -f epms-modbus-collector
```

정상 실행되면 수집기 로그에 `point_map: DB에서 N개 포인트 로드`와 `적재 N개 포인트`가 출력됩니다.

## 이상 상황 주입

### HTTP API

```bash
# 장비 1대에 과전류 주입
curl -X POST "http://localhost:8080/inject?scenario=OVERCURRENT&unit=7"

# 모든 장비의 이상 해제
curl -X POST "http://localhost:8080/inject?scenario=CLEAR&unit=all"

# 현재 상태 조회
curl "http://localhost:8080/status"
```

| 시나리오 | 동작 |
|---|---|
| `OVERCURRENT` | 전류 25% 상승 |
| `VOLTAGE_SAG` | 전압 17% 하락 |
| `LOW_PF` | 역률 0.70 고정 |
| `TRIP` | 차단기 트립 (TRIP=1, ST=0) |
| `ALARM` | 알람 접점 ON |
| `OPEN` | 차단기 개방 (ST=0) |
| `CLEAR` | 모든 이상 및 DI 상태 초기화 |

### 콘솔 명령

시뮬레이터 컨테이너에 접속하여 `<unit|all> <fault>` 형식으로 입력합니다.

```
7 overcurrent
all clear
```

## Modbus 레지스터 맵

Eastron SDM630 기준이며, float32 값을 상위 워드부터 두 개의 16비트 레지스터로 전송합니다(ABCD 순서).

| 키 | 주소 (0-base) | 단위 | 설명 |
|---|---|---|---|
| `v_l1`, `v_l2`, `v_l3` | 0x0000, 0x0002, 0x0004 | V | 상전압 |
| `i_l1`, `i_l2`, `i_l3` | 0x0006, 0x0008, 0x000A | A | 전류 |
| `p_l1`, `p_l2`, `p_l3` | 0x000C, 0x000E, 0x0010 | W | 상별 유효전력 |
| `pf_l1` | 0x001E | - | 역률 |
| `p_total` | 0x0034 | W | 총 유효전력 |
| `s_total` | 0x0038 | VA | 총 피상전력 |
| `freq` | 0x0046 | Hz | 주파수 |
| `kwh_import` | 0x0156 | kWh | 총 수입 전력량 |

<!-- 확인 필요: kwh_import 주소는 SDM630 매뉴얼로 재확인 후 수정하세요. -->

## 데이터 모델

InfluxDB에는 다음 형식으로 저장됩니다.

- **Measurement:** `epms`
- **Tags:** `site_id`, `asset_id`, `point_id`, `point_name`, `unit`
- **Field:** `value` (float)

## 포인트 설정

수집기는 아래 테이블을 조인해 Modbus로 읽을 포인트를 가져옵니다.

- `tb_point`: 포인트 정의 (Modbus 타입, 키, 주소, 스케일)
- `tb_asset`: 장비 정의 (Modbus unit ID)
- `tb_space`, `tb_site`: 위치 및 사이트 정보

DB에 매핑된 포인트가 없거나 사이트가 둘 이상이면 오류가 발생합니다. DB 조회에 실패하면 `point_map.yaml`을 사용합니다.

## 알려진 제한 사항

- 수집기는 장비별로 고정된 블록(0x0000~0x0048, 0x0156~0x0002)만 읽습니다.
- 시뮬레이터의 전력값은 1초 간격으로 갱신되며, 수집기는 5초마다 최신값을 읽습니다.
- 이상 상태는 메모리에만 저장되므로 시뮬레이터 재시작 시 초기화됩니다.
