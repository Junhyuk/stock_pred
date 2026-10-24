# Market Microstructure & Flow Expert Skill

핵심 임무:
외국인/기관/프로그램/파생의 실제 포지셔닝을 단순 일일 순매수보다 깊게 모델링한다.

## Recommended signals

### Foreign cash
- net value
- ratio to turnover
- rolling accumulation
- acceleration
- streak
- price divergence

### Foreign derivatives
- futures net
- options directional proxy
- OI interaction
- cash/futures agreement
- cash/futures divergence

### Program
- total
- arbitrage
- non-arbitrage

### Positioning
- short
- lending
- credit
- ETF flow

### Market internal
- breadth
- dispersion
- concentration

## Interpretation rule

같은 +1조원 외국인 순매수라도
시장 거래대금이 8조일 때와 25조일 때 의미가 다르다.
따라서 raw amount + normalized ratio + z-score를 같이 사용한다.
