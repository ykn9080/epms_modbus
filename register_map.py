"""Eastron SDM630 기준 입력 레지스터 맵 (FC04, float32, 상위 워드 먼저 = ABCD)."""
import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class Register:
    key: str        # 시뮬레이터 내부 이름
    address: int    # 0-base 주소 (참조번호 30001 -> 0)
    unit: str
    description: str


INPUT_REGISTERS: list[Register] = [
    Register("v_l1",   0x0000, "V",  "상전압 L1-N"),
    Register("v_l2",   0x0002, "V",  "상전압 L2-N"),
    Register("v_l3",   0x0004, "V",  "상전압 L3-N"),
    Register("i_l1",   0x0006, "A",  "전류 L1"),
    Register("i_l2",   0x0008, "A",  "전류 L2"),
    Register("i_l3",   0x000A, "A",  "전류 L3"),
    Register("p_l1",   0x000C, "W",  "유효전력 L1"),
    Register("p_l2",   0x000E, "W",  "유효전력 L2"),
    Register("p_l3",   0x0010, "W",  "유효전력 L3"),
    Register("pf_l1",  0x001E, "-",  "역률 L1"),
    Register("p_total", 0x0034, "W",  "총 유효전력"),
    Register("s_total", 0x0038, "VA", "총 피상전력"),
    Register("freq",   0x0046, "Hz", "주파수"),
    # 주소는 SDM630 매뉴얼로 반드시 확인 후 수정하세요 (아래 주의 참고)
    Register("kwh_import", 0x0156, "kWh", "총 수입 유효전력량"),
]

REGISTER_COUNT = 0x0158  # 서버 측 레지스터 공간 크기


def encode_float32(value: float) -> list[int]:
    """float -> 16비트 레지스터 2개 (상위 워드 먼저)."""
    high, low = struct.unpack(">HH", struct.pack(">f", value))
    return [high, low]


def decode_float32(high: int, low: int) -> float:
    """16비트 레지스터 2개 -> float."""
    return struct.unpack(">f", struct.pack(">HH", high, low))[0]
