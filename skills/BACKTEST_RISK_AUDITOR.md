# Backtest & Risk Auditor Skill

개발 agent와 독립적으로 결과를 검증한다.

## Audit checklist
- point-in-time
- survivorship
- look-ahead
- overlap
- transaction cost
- slippage
- parameter search
- test reuse
- class balance
- missing data
- regime concentration
- benchmark choice

## Required benchmark
- buy-and-hold
- cash
- naive previous-direction
- simple trend
- logistic baseline

## Reject conditions
- test data leakage
- unrealistic execution
- only in-sample gain
- unexplained huge Sharpe
- results dominated by few days
- no reproducible run
