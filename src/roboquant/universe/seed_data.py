from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UniverseSeedItem:
    raw_rank: int
    name: str
    symbol: str
    market: str
    security_type: str = "COMMON"
    exclusion_reason: str | None = None


KOSPI_RAW_SEED = (
    UniverseSeedItem(1, "삼성전자", "005930", "KOSPI"),
    UniverseSeedItem(2, "SK하이닉스", "000660", "KOSPI"),
    UniverseSeedItem(
        3,
        "삼성전자우",
        "005935",
        "KOSPI",
        security_type="PREFERRED",
        exclusion_reason="excluded security_type=PREFERRED",
    ),
    UniverseSeedItem(4, "SK스퀘어", "402340", "KOSPI"),
    UniverseSeedItem(5, "현대차", "005380", "KOSPI"),
    UniverseSeedItem(6, "삼성전기", "009150", "KOSPI"),
    UniverseSeedItem(7, "LG에너지솔루션", "373220", "KOSPI"),
    UniverseSeedItem(8, "삼성생명", "032830", "KOSPI"),
    UniverseSeedItem(9, "삼성물산", "028260", "KOSPI"),
    UniverseSeedItem(10, "HD현대중공업", "329180", "KOSPI"),
    UniverseSeedItem(11, "KB금융", "105560", "KOSPI"),
    UniverseSeedItem(12, "현대모비스", "012330", "KOSPI"),
    UniverseSeedItem(13, "기아", "000270", "KOSPI"),
    UniverseSeedItem(14, "삼성바이오로직스", "207940", "KOSPI"),
    UniverseSeedItem(15, "두산에너빌리티", "034020", "KOSPI"),
    UniverseSeedItem(16, "한화에어로스페이스", "012450", "KOSPI"),
    UniverseSeedItem(17, "신한지주", "055550", "KOSPI"),
    UniverseSeedItem(18, "LG전자", "066570", "KOSPI"),
    UniverseSeedItem(19, "삼성SDI", "006400", "KOSPI"),
    UniverseSeedItem(20, "SK", "034730", "KOSPI"),
    UniverseSeedItem(21, "NAVER", "035420", "KOSPI"),
    UniverseSeedItem(22, "셀트리온", "068270", "KOSPI"),
    UniverseSeedItem(23, "HD현대일렉트릭", "267260", "KOSPI"),
    UniverseSeedItem(24, "한화오션", "042660", "KOSPI"),
    UniverseSeedItem(25, "LS ELECTRIC", "010120", "KOSPI"),
    UniverseSeedItem(26, "하나금융지주", "086790", "KOSPI"),
    UniverseSeedItem(27, "미래에셋증권", "006800", "KOSPI"),
    UniverseSeedItem(28, "효성중공업", "298040", "KOSPI"),
    UniverseSeedItem(29, "삼성화재", "000810", "KOSPI"),
    UniverseSeedItem(30, "POSCO홀딩스", "005490", "KOSPI"),
    UniverseSeedItem(
        31,
        "KODEX 200",
        "069500",
        "KOSPI",
        security_type="ETF",
        exclusion_reason="excluded security_type=ETF",
    ),
    UniverseSeedItem(32, "두산", "000150", "KOSPI"),
)

KOSDAQ_RAW_SEED = (
    UniverseSeedItem(1, "알테오젠", "196170", "KOSDAQ"),
    UniverseSeedItem(2, "에코프로비엠", "247540", "KOSDAQ"),
    UniverseSeedItem(3, "에코프로", "086520", "KOSDAQ"),
    UniverseSeedItem(4, "레인보우로보틱스", "277810", "KOSDAQ"),
    UniverseSeedItem(5, "주성엔지니어링", "036930", "KOSDAQ"),
    UniverseSeedItem(6, "코오롱티슈진", "950160", "KOSDAQ"),
    UniverseSeedItem(7, "리노공업", "058470", "KOSDAQ"),
    UniverseSeedItem(8, "삼천당제약", "000250", "KOSDAQ"),
    UniverseSeedItem(9, "HLB", "028300", "KOSDAQ"),
    UniverseSeedItem(10, "원익IPS", "240810", "KOSDAQ"),
    UniverseSeedItem(11, "펩트론", "087010", "KOSDAQ"),
    UniverseSeedItem(12, "이오테크닉스", "039030", "KOSDAQ"),
    UniverseSeedItem(13, "에이비엘바이오", "298380", "KOSDAQ"),
    UniverseSeedItem(14, "파두", "440110", "KOSDAQ"),
    UniverseSeedItem(15, "리가켐바이오", "141080", "KOSDAQ"),
    UniverseSeedItem(16, "로보티즈", "108490", "KOSDAQ"),
    UniverseSeedItem(17, "서진시스템", "178320", "KOSDAQ"),
    UniverseSeedItem(18, "ISC", "095340", "KOSDAQ"),
    UniverseSeedItem(19, "케어젠", "214370", "KOSDAQ"),
    UniverseSeedItem(20, "HPSP", "403870", "KOSDAQ"),
)

RAW_SEED = KOSPI_RAW_SEED + KOSDAQ_RAW_SEED
PREDICTION_SEED = tuple(item for item in RAW_SEED if item.exclusion_reason is None)

