# AI Robo Stock Lab PoC

국내 KRX 주식을 대상으로 `3M`, `6M`, `1Y`, `2Y` 기간별 상대수익률 순위와 Top-K outperform 확률을 실험하는 Python PoC입니다.

이 저장소의 첫 목표는 정확한 목표가 예측이 아니라 아래 루프를 빠르게 반복하는 것입니다.

- KOSPI/KOSDAQ 일별 가격 데이터 수집
- 미래 초과수익률 label 생성
- 모멘텀, 거래대금, 변동성 중심 feature 생성
- LightGBM 기반 분류/회귀 모델과 단순 factor baseline 비교
- Top-K 추천 포트폴리오 백테스트
- 최신 추천 CSV/Markdown/HTML 리포트 생성
- CSV/저장 HTML 기반 애널리스트 리포트, 목표가 변경, 컨센서스, 신뢰도 feature 실험
- RTX 3090 optional GPU 환경에서 PatchTST shadow model과 Backtest Gate 실험
- 예측 결과 backtest, 모델 정확도 gate, FastAPI/Streamlit 로컬 대시보드

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

GPU 학습은 optional입니다. 기본 테스트와 CLI는 PyTorch 없이도 동작합니다.

```bash
python -m pip install torch --index-url https://download.pytorch.org/whl/cu121
python -m pip install -e ".[gpu]"
```

## CLI

```bash
python scripts/collect_prices.py --config configs/poc.yaml
python scripts/collect_market_metrics.py --config configs/poc.yaml
python scripts/collect_investor_flows.py --config configs/poc.yaml --start 2024-01-31 --end 2024-01-31
python scripts/import_analyst_reports.py --config configs/analyst_sources.yaml
python scripts/update_analyst_outcomes.py --config configs/poc.yaml
python scripts/update_analyst_scores.py --config configs/poc.yaml
python scripts/build_feature_matrix.py --config configs/poc.yaml
python scripts/build_dataset.py --config configs/poc.yaml
python scripts/train_models.py --config configs/poc.yaml --horizon 3M
python scripts/run_backtest.py --config configs/poc.yaml --horizon 3M --top-k 20
python scripts/generate_recommendations.py --config configs/poc.yaml --date latest
```

`build_dataset.py`는 기존 호환용이고, v2 feature를 명시적으로 갱신할 때는 `build_feature_matrix.py`를 사용합니다.
애널리스트 데이터는 import-first 방식입니다. `configs/analyst_sources.yaml`의 CSV 또는 저장 HTML fixture를 읽으며, 직접 웹 수집 adapter는 기본 pipeline에서 실행하지 않습니다.

## v6 PatchTST Shadow Model

DNN 모델은 바로 production 추천 점수에 반영하지 않습니다. 먼저 `model_predictions`에 shadow prediction을 저장하고, Backtest Gate 통과 후에만 `model_registry.production_weight=0.05`로 등록합니다.

```bash
python scripts/train_patchtst.py --config configs/train_patchtst.yaml --horizon 3M
python scripts/predict_patchtst.py --config configs/train_patchtst.yaml --horizon 3M
python scripts/run_backtest_gate.py --config configs/backtest_gate.yaml --model patchtst_v1_lookback252 --baseline lightgbm
```

DuckDB는 단일 writer 제약이 있습니다. `train_patchtst.py`, `predict_patchtst.py`, `run_backtest_gate.py`처럼 DB에 쓰는 명령을 실행하기 전에는 `streamlit run app_streamlit.py` 등 DuckDB를 열어 둔 프로세스를 종료하세요.

## v7 Backtest Dashboard

예측 결과와 label이 쌓인 뒤 아래 명령으로 모델별 검증 결과와 대시보드 snapshot을 만듭니다.

```bash
python scripts/run_prediction_backtest.py --config configs/poc.yaml --horizon 60
python scripts/run_model_gatekeeper.py --config configs/poc.yaml
python scripts/build_dashboard_snapshot.py --config configs/poc.yaml --horizon 3M
```

전체 horizon을 한 번에 갱신하려면:

```bash
scripts/local_backtest_all.sh
```

로컬 웹 데모:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
streamlit run app_streamlit.py --server.address 0.0.0.0 --server.port 8501
```

- FastAPI dashboard: `http://localhost:8000/dashboard`
- FastAPI backtest: `http://localhost:8000/backtest`
- API docs: `http://localhost:8000/docs`
- Streamlit dashboard: `http://localhost:8501`

## Public PoC (GitHub Pages)

로컬 DuckDB 스냅샷을 `docs/` 정적 **Top50 Daily Dashboard**로 export한 뒤 GitHub Pages에 배포합니다.
(로컬 미리보기: `python -m http.server 8787 --directory reports/github_pages_site` 또는 export 후 `docs/`)

```bash
# 데이터 갱신 + export + commit + push (한 번에)
.venv/bin/python scripts/publish_github_pages.py

# DB가 이미 최신일 때 export + push만
.venv/bin/python scripts/publish_github_pages.py --skip-refresh

# export only (8787 미리보기와 동일 구조 → docs/)
.venv/bin/python scripts/export_github_pages_site.py --output docs
```

- legacy Today/up-down only export: `scripts/export_github_pages_snapshot.py`
- 공개 URL (Pages 활성화 후): `https://junhyuk.github.io/stock_pred/`
- GitHub 저장소 설정: Settings → Pages → Source = **GitHub Actions**
- `.env`, DuckDB, models는 커밋되지 않습니다. `docs/` JSON/HTML만 push됩니다.

주요 API:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/api/dashboard/snapshot
curl 'http://localhost:8000/api/backtest/summary?horizon=60'
curl http://localhost:8000/api/models/accuracy
curl 'http://localhost:8000/api/recommendations/top20-price-forecast?horizons=3M,6M,9M,1Y&limit=20'
```

Top20 예상가격 API는 최신 추천 Top20에 대해 `3M/6M/9M/1Y` 예상가와 상단/하단 범위를 반환합니다. 이 값은 모델 기반 정보제공용 전망이며 목표가나 수익 보장이 아닙니다.

### Recent Prediction Price Gap

최근 예측일 기준 30일 창의 예측수익률과 실제 가격 흐름의 괴리를 확인합니다.
목표 30일이 아직 지나지 않은 예측은 `pending`으로 분리하고, 현재까지의 실제수익률과
괴리도 함께 표시합니다.

```bash
.venv/bin/python scripts/run_prediction_price_gap_backtest.py \
  --config configs/poc.yaml \
  --lookback-days 30 \
  --target-days 30 \
  --horizon 3M
```

출력:

- `reports/prediction_price_gap_30d_3m.csv`
- `reports/prediction_price_gap_30d_3m.md`

웹/API:

- Backtest page: `http://localhost:8000/backtest`
- API: `http://localhost:8000/api/backtest/price-gap?lookback_days=30&target_days=30&horizon=3M`

## Samsung KOSPI 100 Demo

KOSPI 시가총액 상위 100개 실데이터를 수집해 삼성전자 중심 `3M/6M` 예측, Top20,
산업별 추천, 5개 KMeans 클러스터, backtest, dashboard snapshot을 한 번에 생성합니다.

```bash
python scripts/run_samsung_demo.py \
  --config configs/samsung_demo.yaml \
  --reset-demo-data
```

수집된 가격을 재사용해 모델과 화면만 갱신하려면:

```bash
python scripts/run_samsung_demo.py \
  --config configs/samsung_demo.yaml \
  --skip-collect
```

주요 화면과 API:

- Samsung focus: `http://localhost:8000/api/focus/005930?horizon=3M`
- Cluster list: `http://localhost:8000/api/clusters?horizon=3M`
- Samsung peers: `http://localhost:8000/api/stocks/005930/cluster?horizon=3M`
- Stock detail: `http://localhost:8000/stock/005930`

`pykrx`가 로그인되지 않았거나 빈 결과를 반환하면 종목 목록과 가격은
FinanceDataReader로 자동 전환됩니다. 수급·밸류 데이터가 없으면 관련 factor는
중립값으로 계산되고 dashboard의 데이터 품질 영역에 표시됩니다.

## Samsung + SL Two Stock Demo

Top50 production universe는 유지한 채 삼성전자 `005930`과 에스엘 `005850`을
강조하는 로컬 데모를 생성합니다. 내부 학습 표본은 KOSPI 시가총액 상위 100개에
에스엘을 필수 포함한 universe를 사용합니다.

```bash
.venv/bin/python scripts/run_two_stock_demo.py \
  --config configs/two_stock_demo.yaml
```

수집된 가격을 재사용해 모델과 화면만 갱신하려면:

```bash
.venv/bin/python scripts/run_two_stock_demo.py \
  --config configs/two_stock_demo.yaml \
  --skip-collect
```

주요 화면:

- Two-stock demo: `http://localhost:8000/demo/two-stocks`
- Samsung detail: `http://localhost:8000/stock/005930`
- SL detail: `http://localhost:8000/stock/005850`
- API: `http://localhost:8000/api/demo/two-stocks?horizon=3M`

## Four Stock Prediction Demo

삼성전자 `005930`, SK하이닉스 `000660`, LG전자 `066570`, 에스엘 `005850`의
기존 `3M/6M` 예측, 최신 종가, 추천 상태, 글로벌 보정, 30일 가격 괴리와
클러스터 유사 종목을 한 화면에서 비교합니다. 이 화면은 표시용 데모라 재학습이나
production 추천값 변경을 수행하지 않습니다.

Normal 실행은 축약 실행 대신 가격 수집, 지원되는 factor 데이터 보강, feature 재생성,
`3M/6M` 학습, 추천, backtest, gatekeeper, snapshot 생성 후 웹을 시작합니다.
실행 메타는 `model=5.5`, `quality=high`, `speed=default`로 고정합니다.

```bash
.venv/bin/python scripts/run_four_stock_normal.py
```

명령만 확인하려면:

```bash
.venv/bin/python scripts/run_four_stock_normal.py --dry-run
```

로컬 웹을 실행한 뒤 아래 주소를 엽니다.

- Four-stock demo: `http://localhost:8000/demo/four-stocks`
- API: `http://localhost:8000/api/demo/four-stocks?horizon=3M`
- LG전자 detail: `http://localhost:8000/stock/066570`
- 3M 상승확률·상승여력 Top20: `http://localhost:8000/recommendations/top20-upside`
- API: `http://localhost:8000/api/recommendations/top20-upside?horizon=3M`

LG전자가 추천 Top20에 없더라도 최신 prediction이 있으면 `Top20 밖 / 예측값 있음`으로
표시하고, 추천 점수 대신 예측 상승확률을 표시 점수로 사용합니다.

## Latest Market Impact Retrain

Top50 최신 가격을 먼저 확인한 뒤, 가격 최신일이 최신 완료 거래일보다 오래되면
재학습을 막고 `partial_ready` 상태로 급등락 설명만 갱신합니다. 한국시간 장 마감/일봉
확정 전에는 당일이 아니라 직전 거래일을 최신 완료 거래일로 봅니다. 최신 가격이 확인되면
`2M/3M/6M` 재학습, 추천, 상승·하락/롱·숏, backtest/gatekeeper, dashboard snapshot,
급등락 원인+예측 context 생성을 순서대로 실행합니다.

```bash
.venv/bin/python scripts/run_latest_market_impact_retrain.py \
  --config configs/top50_normal.yaml \
  --universe-config configs/universe_top50.yaml \
  --provider fdr_poc \
  --restart-web
```

명령 순서만 확인하려면:

```bash
.venv/bin/python scripts/run_latest_market_impact_retrain.py --dry-run
```

Naver API 키가 없으면 종목별 뉴스는 fake 없이 건너뛰고, 공식 RSS/curated market context만
급등락 설명에 사용합니다. 설명 API는 예측값을 추천 점수에 반영하지 않고
`prediction_context`로만 노출합니다.

뉴스 headline 신호는 저장된 Naver 공식 Search API 결과와 공식 RSS/curated market feed만
사용해 `news_signal_daily`에 집계합니다. 이 신호는 `2M/3M` 학습 feature에는 포함하고,
`6M/9M/1Y` 장기 horizon에서는 기본 제외합니다.
홍보성·마케팅성 기사로 긍정 편향이 커지는 문제를 줄이기 위해 부정 headline은
`negative_weight=2.0`, 긍정 headline은 `positive_weight=0.75`로 편향 보정 감성 점수와
부정 뉴스 attention feature를 별도로 생성합니다. 해외 headline은 기존 공식 RSS/curated
경로 안에서 `Samsung Electronics`, `SK hynix`, `LG Electronics`, `SL Corp` alias를
국내 종목 코드로 매핑합니다.

```bash
.venv/bin/python scripts/build_news_signal_features.py \
  --config configs/top50_normal.yaml \
  --date latest
```

### Daily GitHub Pages Publish

로컬 또는 개인 서버에서 장마감 후 최신 Top50 재학습을 실행하고, 공개용 정적 대시보드만
GitHub Pages용 `gh-pages` branch에 배포합니다. DuckDB, 모델 파일, `.env` 값은 배포하지
않고 `reports/github_pages_site/`의 `index.html`, `assets/`, `data/*.json`만 복사합니다.

```bash
.venv/bin/python scripts/run_daily_pages_publish.py --publish
```

배포 없이 산출물과 대상 파일 목록만 확인하려면:

```bash
.venv/bin/python scripts/run_daily_pages_publish.py --dry-run
```

빈 데이터 검증을 무시하고 로컬 preview만 만들 때는 `--allow-empty`를 붙입니다. 실제
`--publish`에서는 `validation.json`이 publish 가능 상태가 아니면 push를 막습니다.

정적 site만 별도로 생성하려면:

```bash
.venv/bin/python scripts/export_github_pages_site.py \
  --config configs/top50_normal.yaml \
  --today-config configs/today_update.yaml \
  --output reports/github_pages_site
```

로컬 cron 예시:

```cron
30 18 * * 1-5 cd /home/joon/work/coding/AI/stock_pred && .venv/bin/python scripts/run_daily_pages_publish.py --publish >> logs/daily_pages_publish.log 2>&1
```

GitHub Pages publish source는 `gh-pages` branch root로 설정합니다. 기본 remote는
`origin`, 기본 worktree는 저장소 밖의 `../stock_pred_gh_pages`입니다.

KORU는 미국 상장 한국시장 일간 3배 레버리지 ETF로, 장기 알파가 아니라 단기 한국시장
레버리지 심리/충격 feature로만 사용합니다. latest runner는 미국장이 한국시간 심야·새벽에
거래되는 점을 감안해 `global_market_intraday_snapshot`의 KORU/EWY/SPY/QQQ 현재가
snapshot을 먼저 쓰고, 없으면 D-1 미국장 종가로 fallback합니다. 시장충격 trigger는
개별 종목이 아니라 `benchmark_daily`의 KOSPI 또는 KOSDAQ 1D 수익률이 `<= -2%`일 때만
발생합니다.

```bash
.venv/bin/python scripts/build_koru_korea_linkage.py --config configs/top50_normal.yaml --date latest
.venv/bin/python scripts/run_koru_weight_gate.py --config configs/top50_normal.yaml
```

KORU overlay weight는 ablation/backtest gate 결과가 없으면 `0`으로 저장되어 production
추천 점수에는 반영되지 않습니다. API: `http://localhost:8000/api/koru/linkage?date=latest`

### KOSPI/KOSDAQ Today/Week Market Outlook

latest runner는 `build_feature_matrix` 이후 KOSPI/KOSDAQ 단기 전망도 생성합니다.
전망은 지수 직접 모델과 Top50 breadth 집계 모델을 `0.65 / 0.35`로 결합하며,
오늘 종가와 이번주 마지막 KRX 거래일 종가 기준 예상 등락률, 예상 범위, 상승확률,
하락확률, `-2%` 충격확률을 `market_outlook_forecasts`에 저장합니다.
이 전망은 정보제공용이며 기존 `2M/3M/6M` production 추천 점수에는 반영하지 않습니다.

```bash
.venv/bin/python scripts/build_market_outlook_features.py --config configs/top50_normal.yaml --date latest
.venv/bin/python scripts/train_market_outlook.py --config configs/top50_normal.yaml --date latest
.venv/bin/python scripts/generate_market_outlook.py --config configs/top50_normal.yaml --date latest
```

API와 화면:

- `http://localhost:8000/api/market-outlook?date=latest&horizon=all`
- `http://localhost:8000/demo/today`
- `http://localhost:8000/demo/tomorrow`

## v9 Global Market Regime 준비

미국장 급락, 반도체 지수, VIX, 금리, 환율 충격을 한국 장전 추천에 반영하기 위한
로컬 PoC 수집과 레짐 보정 pipeline입니다. 국내 예측값은 덮어쓰지 않고
`regime_adjusted_score`, 글로벌 위험 원인, 권장 현금비중, 종목 weight cap을
데모/API에 별도로 표시합니다.

```bash
python -m pip install -e ".[global]"
```

환경변수:

```bash
GLOBAL_MARKET_PROVIDER=yfinance_poc
FRED_API_KEY=
```

설정 파일:

- `configs/global_market.yaml`
- `configs/focus_stocks_demo.yaml`

글로벌 보정 데모 화면:

- Focus stock demo: `http://localhost:8000/demo/focus-stocks`
- API: `http://localhost:8000/api/demo/focus-stocks?horizon=3M`
- Regime API: `http://localhost:8000/api/market-regime/current`
- Global latest API: `http://localhost:8000/api/global-markets/latest`

현재 화면은 삼성전자 `005930`, SK하이닉스 `000660`, 에스엘 `005850`의 국내 예측값과
글로벌 보정 점수를 함께 보여줍니다. 글로벌 레짐 데이터가 없으면 `데이터 수집 대기`로
표시하고, 아래 pipeline이 `market_regime_daily`를 채우면 자동으로 보정 상태가 `ready`가 됩니다.

```bash
python scripts/collect_global_market_daily.py --config configs/global_market.yaml --from-date 2022-01-01 --to-date latest
python scripts/collect_premarket_snapshot.py --config configs/global_market.yaml --cutoff now
python scripts/build_market_regime.py --config configs/global_market.yaml --prediction-date latest --cutoff latest
python scripts/run_premarket_global_pipeline.py --config configs/global_market.yaml
```

운영 스케줄 기본값은 `07:30 KST` 글로벌 수집, `08:00 KST` 장전 regime 생성,
`18:00 KST` 국내 데이터 갱신입니다. `FRED_API_KEY`가 없으면 금리·원자재 신호는
결측으로 처리하고 임의 값을 생성하지 않습니다.

로컬 cron 예시:

```cron
30 7 * * 1-5 cd /home/joon/work/coding/AI/stock_pred && .venv/bin/python scripts/collect_global_market_daily.py --config configs/global_market.yaml --from-date 2022-01-01 --to-date latest
0 8 * * 1-5 cd /home/joon/work/coding/AI/stock_pred && .venv/bin/python scripts/run_premarket_global_pipeline.py --config configs/global_market.yaml
```

## v8 Top50 Universe Seed

2026-06-05 기준 문서 seed를 DuckDB에 저장합니다. Raw snapshot은 KOSPI 32개와
KOSDAQ 20개이며, 예측 Universe는 우선주와 ETF를 제외한 KOSPI 30개와 KOSDAQ
20개입니다.

```bash
python scripts/seed_prediction_universe.py --config configs/universe_top50.yaml
```

같은 날짜의 seed를 의도적으로 교체할 때만 `--force`를 사용합니다.

```bash
python scripts/seed_prediction_universe.py \
  --config configs/universe_top50.yaml \
  --force
```

이 단계는 Universe 목록만 초기화합니다. KOSDAQ 가격 수집과 Top50 예측은 후속
pipeline phase에서 수행합니다.

### Top50 Market Data Provider

로컬 검증용 기본 provider는 FinanceDataReader를 감싼 `fdr_poc`입니다.

```bash
MARKET_DATA_PROVIDER=fdr_poc
```

`krx_openapi`와 `broker` adapter는 승인된 API 연결 전까지 환경변수만 검증하며
endpoint를 임의로 호출하지 않습니다.

```bash
MARKET_DATA_PROVIDER=krx_openapi
KRX_OPENAPI_KEY=
KRX_OPENAPI_SERVICE_ID=

MARKET_DATA_PROVIDER=broker
KIS_APP_KEY=
KIS_APP_SECRET=
```

## Today Market Update Demo

오늘 국내 포커스 종목, 해외시장 동향, 글로벌 레짐, Naver 뉴스 검색 결과를 한 번에
갱신해 `/demo/today`에서 확인합니다. 뉴스는 Naver 공식 Search API만 사용하며,
`NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`가 없으면 뉴스 단계만 건너뛰고 가짜 뉴스는
생성하지 않습니다.

```bash
ALLOW_UNOFFICIAL_YAHOO=true \
.venv/bin/python scripts/run_today_market_update.py \
  --config configs/today_update.yaml \
  --restart-web
```

뉴스만 수집할 때:

```bash
.venv/bin/python scripts/collect_naver_news.py \
  --config configs/today_update.yaml \
  --allow-missing-key
```

주요 화면과 API:

- Today demo: `http://localhost:8000/demo/today`
- Next trading day demo: `http://localhost:8000/demo/tomorrow`
- Snapshot API: `http://localhost:8000/api/today/update-snapshot`
- Next trading day API: `http://localhost:8000/api/tomorrow/update-snapshot`
- News API: `http://localhost:8000/api/news/latest?symbol=005930`
- 2% move explanations API: `http://localhost:8000/api/market-move/explanations?scope=top50`
- KORU linkage API: `http://localhost:8000/api/koru/linkage?date=latest`
- US sector linkage API: `http://localhost:8000/api/sector-linkage?date=latest&sector=auto`

다음 거래일 화면은 KOSPI/KOSDAQ 시장전망과 공식 신용거래융자 잔고 변동을 결합해
정보제공용 LONG/SHORT 노출 범위를 표시합니다. 신용잔고는 공식 금융투자협회/공공데이터
endpoint와 `DATA_GO_KR_SERVICE_KEY`가 있을 때만 수집하며, 키가 없으면 결측으로 남기고
fake 값을 만들지 않습니다.

```bash
.venv/bin/python scripts/collect_market_credit_balance.py \
  --config configs/top50_normal.yaml \
  --date latest \
  --allow-missing-key
```

2% 이상 급등락 원인 분석만 단독 생성하려면:

```bash
.venv/bin/python scripts/build_market_move_explanations.py \
  --config configs/top50_normal.yaml \
  --date latest
```

저장 테이블은 `news_articles`, `news_signal_daily`, `market_move_explanations`, `koru_korea_linkage`,
`koru_weight_decisions`, `us_sector_linkage_daily`,
`today_market_update_runs`, `today_market_snapshot`입니다.
`ALLOW_UNOFFICIAL_YAHOO=true`가 없으면 Yahoo/yfinance 수집은 생략됩니다. 글로벌 레짐은
기존 `configs/global_market.yaml`과 `run_premarket_global_pipeline.py`를 재사용하며,
FRED key가 없으면 금리·원자재 신호만 결측 처리합니다.

### US Similar-Sector Linkage

국내 섹터별로 미국 유사섹터 proxy의 등락을 연결합니다. 기본 proxy는 반도체
`SOXX/^SOX/SMH/TSM/NVDA`, 자동차·부품 `DRIV/XLY/TSLA/GM/F`, 산업재 `XLI`,
금융 `XLF`, 헬스케어·바이오 `XLV/IBB/XBI`, 에너지·소재 `XLE/XLB/LIT`입니다.
한국 예측일 기준으로 미래 미국장 데이터를 섞지 않도록 `target_date - 1` 이전
미국 일봉만 사용합니다.

```bash
.venv/bin/python scripts/collect_global_market_daily.py \
  --config configs/global_market.yaml \
  --from-date 2025-01-01 \
  --to-date latest

.venv/bin/python scripts/build_us_sector_linkage.py \
  --config configs/global_market.yaml \
  --date latest
```

생성 feature는 `us_sector_return_1d`, `us_sector_return_5d`,
`us_sector_zscore_20d`, `us_sector_beta_60d`, `us_sector_corr_60d`,
`us_sector_impact_score`, `us_sector_direction_agreement`입니다. `2M/3M` 학습
feature에는 포함하지만 `6M` 기본 feature에서는 제외합니다. Today 화면과
`/stock/005850`에는 즉시 설명용으로 표시하며, production 추천 점수 overlay는
backtest/gatekeeper 통과 전에는 적용하지 않습니다.

## Telegram News Signal MVP

미국 주식/매크로와 한국 시장 전략 Telegram 공개 채널을 “매수·매도 신호”가 아니라 관심도와
이벤트 감지용 텍스트 센서로 수집합니다. 1차 채널은 `kwusa`, `FastStockNewsUSA`,
`mkglobalinvest`, `sypark_strategy`, `marketfeed`이며 링크된 언론사 본문은 크롤링하지 않습니다.

Telethon은 선택 의존성입니다.

```bash
python -m pip install -e ".[telegram]"
```

`.env`에는 Telegram 개발자 페이지에서 발급받은 값을 넣습니다. 세션 파일은 기본적으로
`data/interim/telegram_stock_robo.session`에 생성되며 저장소 추적 대상이 아닙니다.

```bash
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_SESSION_PATH=data/interim/telegram_stock_robo
```

인증 없이 설정만 확인:

```bash
.venv/bin/python scripts/collect_telegram_signals.py \
  --config configs/telegram_signals.yaml \
  --dry-run
```

수집, 티커 언급 집계, 일간 Top10 관심 리포트 생성:

```bash
.venv/bin/python scripts/collect_telegram_signals.py \
  --config configs/telegram_signals.yaml
```

출력:

- DuckDB tables: `telegram_posts`, `telegram_ticker_mentions`, `telegram_signal_daily`, `telegram_market_signal_daily`
- Report: `reports/telegram_signals/daily_report.md`

10분 주기 cron 예시:

```cron
*/10 * * * * cd /home/joon/work/coding/AI/stock_pred && .venv/bin/python scripts/collect_telegram_signals.py --config configs/telegram_signals.yaml >> logs/telegram_signals.log 2>&1
```

수집기는 configured public channel에 대해 join/subscribe를 시도하고, 이미 가입된 채널은
no-op 처리합니다. `.env`에 `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`가 없으면 fake 없이 optional
failure로 기록하고 기존 데이터만으로 학습을 계속합니다.

MVP는 룰 기반 티커/테마/감성/긴급도/리스크 키워드를 사용합니다. market-wide feature
(`telegram_attention_score`, `telegram_sentiment_score`, `telegram_urgency_score`,
`telegram_risk_score`, `telegram_semiconductor_score`, `telegram_macro_score`)는 `2M/3M`
학습 feature에 포함하고 `6M` 기본 feature에서는 제외합니다. Telegram evidence는
`/api/market-move/explanations`와 `/demo/today`의 급등락 설명에 표시되며, production 추천
점수 overlay는 backtest/gatekeeper 통과 전에는 적용하지 않습니다.

## Unofficial Yahoo/yfinance Provider

Yahoo/yfinance 기반 데이터는 공식 승인 provider가 아니므로 로컬 PoC와 개인 연구용으로만
사용합니다. 이 경로는 production 예측, 추천, 글로벌 레짐 테이블을 자동으로 덮어쓰지 않고
별도 `yahoo_prices_daily`, `yahoo_fundamentals_snapshot` 테이블에만 저장합니다.

명시적으로 opt-in해야 실행됩니다.

```bash
ALLOW_UNOFFICIAL_YAHOO=true \
.venv/bin/python scripts/collect_yahoo_unofficial.py \
  --config configs/yahoo_unofficial.yaml \
  --from-date 2024-01-01 \
  --to-date latest
```

부분 수집 예시:

```bash
ALLOW_UNOFFICIAL_YAHOO=true \
.venv/bin/python scripts/collect_yahoo_unofficial.py \
  --config configs/yahoo_unofficial.yaml \
  --symbols 005930.KS,000660.KS,005850.KS,SPY,^IXIC \
  --from-date 2026-05-01 \
  --to-date latest
```

`ALLOW_UNOFFICIAL_YAHOO=true`가 없으면 실행이 차단됩니다. 기본 설정은 최대 100개 심볼,
심볼당 1초 sleep입니다. Yahoo HTML 직접 scraping, captcha 우회, 인증 우회, 대량 병렬 요청은
구현하지 않습니다.

`fdr_poc`는 로컬 PoC 전용입니다. 운영 환경에서는 데이터 저장·가공 범위를 확인한
승인 provider를 연결해야 합니다.

### Top50 Universe Refresh

현재 provider의 시가총액 후보를 조회해 ETF/ETN/SPAC/우선주/거래정지/신규상장 및
최근 가격 부족 종목을 제외한 뒤 KOSPI 30개, KOSDAQ 20개를 새 snapshot으로
저장합니다. 수량이 부족하거나 provider가 실패하면 새 snapshot은 커밋하지 않고
직전 `ready` Universe를 유지합니다.

```bash
MARKET_DATA_PROVIDER=fdr_poc \
python scripts/refresh_prediction_universe.py \
  --config configs/universe_top50.yaml \
  --date latest
```

평일 18:00 KST cron 예시:

```cron
TZ=Asia/Seoul
0 18 * * 1-5 cd /home/joon/work/coding/AI/stock_pred && .venv/bin/python scripts/refresh_prediction_universe.py --config configs/universe_top50.yaml --date latest >> logs/universe_refresh.log 2>&1
```

systemd timer를 쓰는 경우 `OnCalendar=Mon..Fri 18:00:00 Asia/Seoul`로 동일하게
실행할 수 있습니다. DuckDB writer 작업 전에는 FastAPI/Streamlit처럼 DB를 열어 둔
프로세스를 종료하세요.

### Top50 Normal Retraining

v8 `prediction_top_market_cap` universe의 KOSPI 30 + KOSDAQ 20을 기준으로 최신 가격
수집, feature/label 재생성, `3M/6M` 재학습, 추천, backtest, gatekeeper, dashboard
snapshot을 한 번에 갱신합니다. 이 경로는 `fdr_poc` 로컬 검증용이며 승인된 운영
데이터 소스가 아닙니다.

```bash
.venv/bin/python scripts/run_top50_normal.py \
  --config configs/top50_normal.yaml \
  --universe-config configs/universe_top50.yaml \
  --provider fdr_poc
```

명령 순서만 확인하려면:

```bash
.venv/bin/python scripts/run_top50_normal.py --dry-run
```

가격 수집만 단독으로 확인하려면:

```bash
.venv/bin/python scripts/collect_prediction_universe_prices.py \
  --config configs/top50_normal.yaml \
  --snapshot-date latest
```

runner는 최초 실행 시 active KOSPI100 demo 산출 테이블을
`legacy_kospi100_*` 테이블로 보존한 뒤 Top50 산출물로 active dashboard/API 테이블을
갱신합니다.

### Top50 Long-Short Simulation

Top50 예측 위에 시장중립 랭크 스프레드 시뮬레이션을 생성합니다. `SHORT`는 실제
공매도/대차 가능 종목이 아니라 예측 하위권 후보를 이용한 모의 숏 레그입니다.
기본 horizon은 단기 `2M=42` 거래일, 장기 `1Y=252` 거래일입니다.

```bash
.venv/bin/python scripts/generate_long_short_predictions.py \
  --config configs/top50_normal.yaml \
  --horizon 2M

.venv/bin/python scripts/run_long_short_backtest.py \
  --config configs/top50_normal.yaml \
  --horizon 1Y
```

명령만 확인하려면:

```bash
.venv/bin/python scripts/generate_long_short_predictions.py --config configs/top50_normal.yaml --horizon 2M --dry-run
.venv/bin/python scripts/run_long_short_backtest.py --config configs/top50_normal.yaml --horizon 1Y --dry-run
```

출력:

- `reports/top50_normal/long_short_2M.md`
- `reports/top50_normal/long_short_1Y.md`
- `long_short_recommendations`, `long_short_backtest_results` DuckDB tables
- API: `http://localhost:8000/api/long-short/latest?horizon=2M`
- API: `http://localhost:8000/api/long-short/backtest?horizon=1Y`

## Daily Pipeline

```bash
python scripts/run_daily_pipeline.py --config configs/pipeline.yaml --dry-run
python scripts/run_daily_pipeline.py --config configs/pipeline.yaml
```

## Internal Dashboard

```bash
streamlit run app_streamlit.py
```

## Outputs

- `data/processed/roboquant.duckdb`
- `models/{horizon}/model.pkl`
- `models/{horizon}/metrics.json`
- `reports/backtest_{horizon}.html`
- `reports/backtest_comparison_{horizon}.html`
- `reports/data_quality_{date}.md`
- `reports/report_context/{date}/{horizon}/{symbol}.json`
- `reports/recommendations_latest.md`
- `reports/recommendations_latest.html`
- `reports/top50_normal/long_short_{horizon}.md`
- `models/dnn/{model_name}/model.pt`
- `models/dnn/{model_name}/metadata.json`
- `backtest_results`, `model_performance_daily`, `dashboard_snapshot` DuckDB tables
- `long_short_recommendations`, `long_short_backtest_results` DuckDB tables
- `http://localhost:8000/dashboard`, `http://localhost:8000/backtest`

## Important Disclaimer

본 PoC의 출력은 투자 참고용 정보이며 특정 금융투자상품의 매수 또는 매도 권유가 아닙니다. 목표가와 애널리스트 의견은 보정된 참고 정보이며 수익을 보장하지 않습니다. 과거 성과는 미래 수익을 보장하지 않으며 투자 판단과 책임은 이용자 본인에게 있습니다.
