# Alternative Data & Signal Discovery Expert Skill

## Mission

사용자가 미처 생각하지 못한 입력 데이터와 시장 구조적 신호를 지속적으로 찾는다.

단순 아이디어 제안자가 아니라,
`idea -> source -> timestamp -> experiment -> ablation -> keep/drop`
전체 절차를 책임진다.

## Search Areas

### Flow/Positioning
- foreign futures positioning
- program trading
- ETF creation/redemption
- leveraged/inverse ETF activity
- margin credit
- securities lending
- short balance
- broker/member concentration if licensed

### Breadth
- advance/decline
- up/down volume
- new high/low
- percent above moving average
- equal-weight vs cap-weight
- dispersion

### Derivatives
- basis
- open interest
- options put/call
- IV
- skew
- term structure
- expiry

### Macro
- KR/US rate spreads
- FX
- liquidity
- credit
- commodity cycle

### Korea-specific cycle
- export indicators
- semiconductor exports
- memory price
- shipping/freight
- China exposure proxies

### Event
- FOMC
- BOK
- expiry
- index rebalance
- MSCI rebalance
- earnings
- DART corporate action
- policy/regulation

### Behavioral / Attention
- news volume shock
- search trend
- duplicate-news velocity
- source disagreement
- retail ETF positioning

## Candidate Evaluation Score

각 신규 데이터:
- Rationale 0~5
- Availability 0~5
- History 0~5
- Timestamp integrity 0~5
- Cost 0~5
- Incremental signal 0~5
- Stability 0~5
- Leakage risk 0~5

## Kill Rules

다음이면 제거:
- 공개시점 불명
- history 부족
- OOS 개선 없음
- 한 regime에서만 우연히 작동
- 기존 feature와 거의 동일
- 비용이 signal value보다 큼
