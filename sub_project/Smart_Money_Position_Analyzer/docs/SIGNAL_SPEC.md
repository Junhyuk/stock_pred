# 분석 엔진 초안 — 포지션 추정, 단정 금지

## 한국 시장 관측치 (5/20/60거래일)
- `net_foreign_shares_20d = Σ(buy_qty_foreign - sell_qty_foreign)`
- `foreign_buy_persistence_20d = 외국인 순매수 양수 거래일 수 / 정상 거래일 수`
- `foreign_net_to_volume_20d = net_foreign_shares_20d / Σ총거래량`; 매수 강도 **대용지표**이지 거래주체의 점유율 확정치는 아니다.
- `ownership_delta_pp_20d = 외국인 보유비율(t) - 외국인 보유비율(t-20)`; 단위: 퍼센트포인트(pp).
- `price_return_20d` 및 `relative_return_vs_benchmark_20d`, `turnover_20d`, `short_balance_delta_20d`, `futures_net_traded`.

## 한국 신호 판정 (초기 가설 — 학습된 임계값 아님)
- 매집 정황: (a) 외국인 20일 순매수 > 0, (b) 순매수 일수 ≥ 12/20, (c) 외국인 보유율 상승, (d) 기업행동/지분변동 특이점 없음 중 3개 이상이며 a,c가 필수.
- 분산매도 정황: 위 조건 반대 방향 + 거래량 증가/상대 주가 부진 보조 확인.
- 관망/불확실: 주요 항목 누락, 상충 증거가 많음, 데이터의 기준일이 어긋남.
- 현물 외국인 매수 및 선물 외국인 매도를 동시에 확인하더라도 동일 주체의 헤지로 단정하지 않고 `헤지 가능성`으로만 표기한다.

## 미국 시장 관측치
- 13F `change_reported_shares = current_quarter_shares - corporate_action_adjusted_previous_shares`; 집계기간은 **분기말 보유현황**, 최신 공개일은 해당 신고서의 filing_date.
- 13F 보유 금액 변화(`reported_value_usd` 차이)는 주가 변동의 영향이 크므로 매집 강도 직접지표로 사용하지 않는다.
- `manager_breadth = 조사대상 주요 보고기관 중 동일 종목 보유주수가 증가한 기관 수 / 비교 가능한 기관 수`.
- `short_interest_change = latest_short_interest_shares / previous_short_interest_shares - 1` (이전 값이 0이면 결측 처리).
- COT: `net_asset_manager = long - short`; `net_leveraged_funds = long - short`, 지수선물 단위 및 계약승수를 유지한다.
- 옵션: 미결제약정 증감/스큐는 수요 정보일 뿐, 해당 주체가 신규 매수한 방향을 단정할 수 없다.

## 신호 보고 예시(가상 수치 아님, 작성 형식)
> 종목: [티커] / 기준일: [YYYY-MM-DD] / 마지막 공개일: [YYYY-MM-DD]
>
> 판정: 매집 정황(근거 3개, 반대 근거 1개; **정확한 계좌 포지션은 미공개**)
>
> 관측: 20거래일 외국인 순매수 [값], 보유율 변화 [값]pp, 주가 [값]%, 공매도 잔고 [값]
>
> 반대 증거: [내용], [내용]
>
> 주의: 이 결과는 거래 집계로부터 얻은 간접 신호이며 확정 매매내역이 아님.

## 신뢰 수준
- `High evidence`: 2개 이상 독립 공개 원천이 방향을 지지하고 기준일 정합성이 충분함.
- `Medium evidence`: 핵심 1개와 보조 1개가 부합하나 공개 지연 또는 반대 신호 존재.
- `Low evidence`: 보조 신호 위주이거나 최신자료/교차검증 부족.
- 숫자형 '상승 확률'은 워크포워드 검증과 확률 보정(calibration) 전까지 제공하지 않는다.
