# SIGNAL_POLICY.md

## Signal is a policy layer

모델 확률과 거래 신호를 분리한다.

예:
```text
if data_quality < min_quality:
    NO_TRADE
elif confidence < min_confidence:
    NEUTRAL
elif p_up >= long_threshold and expected_return > cost_buffer:
    LONG
elif p_down >= short_threshold and -expected_return > cost_buffer:
    SHORT
else:
    NEUTRAL
```

threshold는 고정하지 말고 validation에서 선택하되
test set을 보고 재조정하지 않는다.

## 1D vs 5D

동시에:
- 1D LONG
- 5D SHORT
가 가능하다.

이를 오류로 보지 않고 horizon conflict로 표시한다.

## Consensus

예:
- Price: bullish
- Flow: bullish
- Macro: bearish
- News: neutral
- Derivatives: bullish

최종 signal과 component disagreement를 저장한다.

## Confidence

confidence는 단순 최대확률이 아니다.

후보:
- calibrated probability
- ensemble agreement
- distance from threshold
- data completeness
- regime familiarity
를 조합한다.

## NO_TRADE

다음 경우 적극 사용:
- API outage
- stale data
- extreme unseen regime
- model disagreement
- major event
- confidence low
