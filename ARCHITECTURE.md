# ARCHITECTURE.md

```text
Official APIs / Broker / News / Macro
             |
             v
        Ingestion Layer
             |
             v
        RAW immutable
             |
             v
      Normalization Layer
             |
             v
     Point-in-Time Store
             |
             v
       Feature Engine
       /     |      \
      /      |       \
  Price    Flow     News/Macro
      \      |       /
       \     |      /
        Model Ensemble
             |
             v
    Calibration + Regime
             |
             v
       Signal Policy
             |
             v
  Dashboard / Paper Trading
             |
             v
       Outcome Evaluator
             |
             v
       Drift / QA Report
```

## Module proposal

```text
src/
  config/
  connectors/
    krx/
    dart/
    broker/
    bok/
    news/
    global_market/
  ingestion/
  db/
  calendar/
  features/
    price/
    flow/
    breadth/
    derivatives/
    macro/
    news/
  labels/
  models/
  validation/
  backtest/
  signals/
  service/
  ui/
tests/
experiments/
models/
data/
```
