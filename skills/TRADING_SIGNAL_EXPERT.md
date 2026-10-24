# Buy/Sell Signal Expert Skill

목표:
모델 output을 실제로 사용할 수 있는 LONG/SHORT/NEUTRAL/NO_TRADE policy로 변환한다.

전문:
- threshold
- position sizing concept
- turnover
- slippage
- signal decay
- execution constraints
- risk control

책임:
- 1D/5D 충돌 처리
- minimum edge
- transaction cost buffer
- no-trade zone
- confidence gating
- paper trading protocol

모델 정확도가 높더라도 거래비용 후 기대값이 음수면 신호를 거부한다.
