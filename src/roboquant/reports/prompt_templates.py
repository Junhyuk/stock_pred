DISCLAIMER = (
    "본 리포트는 투자 참고용 정보이며 특정 금융투자상품의 매수 또는 매도 권유가 아닙니다. "
    "목표가와 애널리스트 의견은 보정된 참고 정보이며 수익을 보장하지 않습니다. "
    "과거 성과는 미래 수익을 보장하지 않으며 투자 판단과 책임은 이용자 본인에게 있습니다."
)

BANNED_PHRASES = [
    "무조건 상승",
    "확정 수익",
    "원금 보장",
    "손실 없음",
    "반드시 매수",
    "지금 사야",
    "목표가 보장",
    "급등 확실",
]


def validate_report_text(text: str) -> str:
    for phrase in BANNED_PHRASES:
        if phrase in text:
            raise ValueError(f"banned phrase detected: {phrase}")
    return text
