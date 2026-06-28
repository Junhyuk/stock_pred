# AI Robo Stock Codex Instructions

이 저장소의 모든 검토, 계획, 구현 작업은 아래 문서를 먼저 확인한 뒤 시작한다.

1. `HARNESS_AI_ROBO_STOCK_TOKEN_EFFICIENT.md`
2. `.codex/STATE.md`
3. 사용자가 지정한 현재 개발 지시서 한 개
4. `README.md`
5. 현재 작업과 직접 관련된 파일

## Required Workflow

- 하네스의 phase 순서를 따른다.
- 한 요청에서는 원칙적으로 phase 하나만 구현한다.
- 관련 없는 코드와 과거 기능을 리팩터링하지 않는다.
- 기존 DuckDB, `src/roboquant`, `scripts`, FastAPI, Streamlit 구조를 유지한다.
- 대용량 데이터, 로그, `.venv`, 캐시 디렉터리를 불필요하게 탐색하지 않는다.
- 가장 좁은 단위 테스트부터 실행한다.
- production 경로에 임시 mock 데이터를 남기지 않는다.
- 승인되지 않은 포털 또는 언론사 크롤러를 운영 데이터 소스로 추가하지 않는다.
- API key와 비밀번호는 `.env`에서만 읽고 출력하거나 저장소에 기록하지 않는다.
- 신규 ML 모델은 backtest gate 통과 전 production에 반영하지 않는다.

## State Tracking

- 각 phase 완료 후 `.codex/STATE.md`를 갱신한다.
- 상태 파일은 120줄 이내로 유지한다.
- 완료 항목, 수정 파일, 검증 명령, 알려진 문제와 다음 권장 phase를 기록한다.
- 다음 작업은 상태 파일의 `Next recommended phase`를 기준으로 시작한다.

## Current v8 Direction

- 기준 문서: `ai_robo_stock_development_proposal_v8_universe_top50_codex.md`
- 기존 KOSPI 100 결과는 `legacy_kospi100`으로 보존한다.
- 신규 활성 Universe는 `prediction_top_market_cap` 규칙의 KOSPI 30 + KOSDAQ 20이다.
- 2026-06-05 이전 Top50 Universe snapshot을 추정하여 생성하지 않는다.
- 기존 `run_samsung_demo.py`는 legacy KOSPI 100 데모로 유지한다.
