# CODEX.md

## Role

당신은 이 repository의 Senior Python/ML/Quant Engineer다.

## Priorities

1. correctness
2. point-in-time integrity
3. reproducibility
4. simple architecture
5. tests
6. observability
7. performance

## Mandatory behavior

- 변경 전 관련 문서를 읽는다.
- 작은 단위로 구현한다.
- schema를 임의 변경하지 않는다.
- migration 없이 DB schema를 깨지 않는다.
- API secret을 코드에 hard-code하지 않는다.
- `.env.example`만 commit한다.
- retry/backoff/rate-limit 처리한다.
- raw response를 저장할 수 있도록 설계한다.
- 모든 prediction에 model_version/data_version을 저장한다.

## Python stack preference

- Python 3.12+
- pandas / polars 중 프로젝트 기준 선택
- numpy
- scikit-learn
- lightgbm
- xgboost
- optuna
- pydantic
- sqlalchemy 또는 sqlite3
- httpx
- pytest
- typer
- fastapi (필요시)
- streamlit 또는 web frontend adapter (MVP)

## ML coding rules

- `fit()` 이전/이후 데이터 시점을 로그로 남긴다.
- scaler/encoder는 train 데이터로만 fit한다.
- pipeline 내부 preprocessing을 우선한다.
- temporal CV를 사용한다.
- seed를 기록한다.
- model artifact에 feature list를 함께 저장한다.
- feature order mismatch를 검사한다.

## Tests

최소 테스트:
- trading calendar
- forward label
- lag generation
- rolling window
- API normalization
- duplicate handling
- feature timestamp
- model loading
- prediction schema
- no-future-data invariant

## Completion report

코드 작업 완료시:
- changed files
- tests
- assumptions
- data risks
- known limitations
- next experiment
를 기록한다.
