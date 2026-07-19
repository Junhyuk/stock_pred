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

### Official News And URL Crawl

공식 데이터 확장 경로는 `configs/official_data_sources.yaml`을 사용합니다. Naver Search
News는 Top50 종목별 pagination 기본값 `display=100`, `max_pages=3`으로 수집하고,
BIGKinds/GDELT/OpenDART/KRX/Data.go.kr는 API key 또는 endpoint가 있을 때만 실행합니다.
키가 없으면 fake 데이터 없이 `collection_failures`와 Pages validation warning에 남깁니다.

```bash
.venv/bin/python scripts/collect_naver_news.py \
  --config configs/top50_normal.yaml \
  --universe-rule prediction_top_market_cap \
  --display 100 --max-pages 3 --allow-missing-key

.venv/bin/python scripts/collect_bigkinds_issues.py --config configs/official_data_sources.yaml --allow-missing-key
.venv/bin/python scripts/collect_gdelt_news.py --config configs/official_data_sources.yaml
.venv/bin/python scripts/collect_dart_disclosures.py --config configs/official_data_sources.yaml --allow-missing-key
.venv/bin/python scripts/collect_official_market_prices.py --config configs/official_data_sources.yaml --allow-missing-key
```

기존 뉴스 URL 크롤러는 DB/config에 이미 저장된 URL만 대상으로 하며 robots.txt를 확인하고
title/meta/canonical/짧은 발췌만 저장합니다. 원문 전문 저장, paywall·로그인·캡차 우회,
포털/언론사 대량 크롤링은 하지 않습니다.

```bash
.venv/bin/python scripts/crawl_existing_news_urls.py --config configs/top50_normal.yaml --limit 100
```

GitHub Pages export는 `official_news_status.json`, `news_url_crawl.json`과 validation count를
함께 생성합니다.

```bash
.venv/bin/python scripts/build_news_signal_features.py \
  --config configs/top50_normal.yaml \
  --date latest
```

### MarketNewsFeed Public Headline Shadow

`https://marketnewsfeed.com/`은 공개 HTML에 `loading…` 상태와 `t.me/marketfeed`
headline 링크를 노출합니다. 로컬 Chrome이 있으면 공개 렌더 DOM의 최신 카드에서 제목,
UTC 시각, 공개 원문 링크, 카테고리, 관련 자산, 중요도만 `marketnewsfeed_headlines`에
shadow 데이터로 저장합니다. 기사 전문, 차트 반응값, 숨은 JavaScript endpoint와 private
API는 수집하지 않습니다. Chrome 또는 렌더 수집이 실패하면 기존 사이트 메타데이터 1행만
`market_news_feed`에 `source=marketnewsfeed_site`로 유지합니다.

원본 headline은 기존 `news_signal_daily`와 production 모델 입력에 직접 연결하지 않습니다.
별도 로컬 LLM shadow를 실행하면 구조화된 최종 JSON만
`marketnewsfeed_llm_*` 테이블과 별도 feature column으로 저장합니다. 기본
`configs/top50_normal.yaml`의 production feature 목록에는 이 column을 넣지 않으며,
충분한 이력과 walk-forward A/B gate 통과 전에는 production 모델에 반영하지 않습니다.

```bash
.venv/bin/python scripts/collect_marketnewsfeed_site.py \
  --config configs/marketnewsfeed.yaml

.venv/bin/python scripts/collect_telegram_signals.py \
  --config configs/telegram_signals.yaml

.venv/bin/python scripts/collect_x_market_news.py \
  --config configs/x_market_news.yaml \
  --allow-missing-key
```

`TELEGRAM_API_ID`/`TELEGRAM_API_HASH` 또는 `X_BEARER_TOKEN`이 없으면 해당 live 수집만
fake 없이 건너뜁니다. GitHub Pages validation은 `marketnewsfeed_site`,
`marketnewsfeed_headlines`, `telegram_marketfeed`, `x_market_news_feed` count를 표시합니다.

### MarketNewsFeed Local LLM Shadow (Legacy Compatibility)

공개 렌더 DOM에서 이미 저장한 headline, 공개 시각, 자산 태그만 로컬 Ollama에 전달합니다.
원문 URL을 따라가지 않고, 숨은 API와 private Telegram/X 데이터도 사용하지 않습니다.
`llm_models/`는 Git에서 제외됩니다. 기본 설정은 RTX 3090의
`llm_models/gguf/Qwen3.5-27B-Q4_K_M.gguf`와 loopback-only
`http://127.0.0.1:11434`를 사용합니다.

Ollama가 아직 없다면 공식 Linux 설치 절차를 사용합니다.

```bash
curl -fsSL https://ollama.com/install.sh | sh

.venv/bin/python scripts/setup_local_ollama.py \
  --config configs/marketnewsfeed_llm.yaml \
  --install-user-service --start-service --import-model
```

`setup_local_ollama.py`는 임시 Modelfile의 `FROM`에 GGUF 절대 경로만 넣어
`roboquant-news:qwen3.5-27b-q4`를 import합니다. 모델을 임의로 다운로드하지 않으며,
import 또는 capability probe가 실패하면 `model_import_failed`/`unavailable` 상태로만
남깁니다. 일반 headline은 thinking을 쓰지 않고, 중요도 4 이상 또는 위험 keyword만
저비용 thinking probe를 시도합니다. reasoning trace는 DB, 로그, 웹에 저장하지 않습니다.

```bash
.venv/bin/python scripts/collect_marketnewsfeed_site.py \
  --config configs/marketnewsfeed.yaml
.venv/bin/python scripts/summarize_marketnewsfeed_llm.py \
  --config configs/marketnewsfeed_llm.yaml
.venv/bin/python scripts/build_marketnewsfeed_llm_features.py \
  --config configs/marketnewsfeed_llm.yaml
.venv/bin/python scripts/run_marketnewsfeed_llm_shadow.py \
  --config configs/marketnewsfeed_llm.yaml
```

headline은 최대 280자, 배치는 최대 6건, 한 실행은 신규 30건, context는 4096,
배치 출력은 360 token으로 제한합니다. `headline_id + model_digest + prompt_version`
cache가 같은 headline의 재호출을 막습니다. 2M/3M stock A/B와 KOSPI/KOSDAQ range
shadow는 최소 60개의 signal/mature date 전까지 `not_ready`이고, 통과해도
`accepted_shadow`, `production_weight=0.0`으로 유지됩니다.

로컬 상세 화면은 `http://localhost:8000/demo/marketnewsfeed-llm`, API는
`http://localhost:8000/api/news/marketnewsfeed/llm`입니다. GitHub Pages에는 headline과
요약을 내보내지 않고 count, token aggregate, gate 상태만 `marketnewsfeed_llm_status.json`
으로 export합니다.

### Integrated Local Ollama News + Tomorrow Market Shadow

통합 경로는 이미 DuckDB에 저장된 MarketNewsFeed, 시장 뉴스, Naver 메타데이터,
GDELT, BIGKinds, OpenDART 제목만 작은 문서 큐로 정규화합니다. URL 본문, private
Telegram/X, 숨은 API와 reasoning trace는 처리하거나 저장하지 않습니다. Qwen은 구조화
요약과 위험 분류에만 사용하고, 다음 거래일의 production KOSPI/KOSDAQ 예측은 기존 Ridge
모델이 계속 생성합니다.

```bash
.venv/bin/python scripts/setup_local_ollama.py \
  --config configs/local_llm_news.yaml \
  --import-model

# 장마감 신규 문서 최대 30건, 장전 신규 문서 최대 12건
.venv/bin/python scripts/run_local_llm_news_pipeline.py \
  --config configs/local_llm_news.yaml --stage close
.venv/bin/python scripts/run_local_llm_news_pipeline.py \
  --config configs/local_llm_news.yaml --stage premarket

# Ridge production 예측을 갱신하고 LGBM+LLM 후보와 짧은 시장 브리프를 별도 생성
.venv/bin/python scripts/run_tomorrow_market_ml.py \
  --config configs/local_llm_news.yaml --date latest
```

기본 token 예산은 문서 320자, 설정상 최대 6건 배치, `num_ctx=4096`, 일반 배치 출력 180 token입니다.
180-token 한도에서는 구조화 JSON 완결성을 위해 실제 배치를 최대 2건으로 자동 축소합니다.
중요도 높은 위험 신호만 하루 최대 4회의 thinking probe를 쓰며, 동일 문서는
`source_document_id + model_digest + prompt_version` cache로 다시 호출하지 않습니다.
`market-outlook-lgbm-news-shadow-v1`은 q10/q50/q90 및 conformal 80% 범위를 비교하지만,
최소 60개 신호일과 성숙 label의 gate 전까지 `production_weight=0.0`입니다.

로컬 확인 화면과 API:

- `http://localhost:8000/demo/local-llm-news`
- `http://localhost:8000/demo/marketnewsfeed-llm` (호환 별칭)
- `http://localhost:8000/api/news/local-llm/status`
- `http://localhost:8000/api/market-outlook/shadow`

장마감 18:20, 장전 07:30, 예측 08:00 KST timer는 먼저 dry-run으로 확인합니다.

```bash
.venv/bin/python scripts/install_local_llm_timers.py --install --enable --dry-run

# 확인 후 실제 설치 및 활성화
.venv/bin/python scripts/install_local_llm_timers.py --install --enable
systemctl --user list-timers 'roboquant-*'
```

timer는 한국 평일 기준이며, 휴장일에는 데이터가 없으면 fake 신호나 예측을 만들지 않습니다.
GitHub Pages에는 문서 수, token 합계, 출처 수와 gate 상태만 export하고 headline, 요약,
URL, 로컬 경로, reasoning은 내보내지 않습니다.

### Daily GitHub Pages Publish

권장 운영 경로는 로컬/Cafe24에서 장마감 후 전체 Top50 재학습을 실행하고, 공개용
정적 산출물인 `docs/`만 `master`에 push하는 방식입니다. 기존 GitHub Actions workflow는
`docs/**` push를 감지해 Pages deploy만 수행합니다. DuckDB, 모델 파일, `.env`, 원본
reports는 커밋하지 않습니다.

전체 로컬 재학습 + `docs/` export + commit/push:

```bash
.venv/bin/python scripts/publish_github_pages.py --refresh-mode full
```

로컬 검증만 하고 commit/push를 하지 않으려면:

```bash
.venv/bin/python scripts/publish_github_pages.py --refresh-mode export-only --skip-push
```

명령 순서만 확인하려면:

```bash
.venv/bin/python scripts/publish_github_pages.py --refresh-mode full --dry-run
```

옵션:

- `--refresh-mode full`: Universe refresh, 가격/지수/수급/글로벌/뉴스, feature, LightGBM `2M/3M/6M/9M/1Y`, 추천, long/short, backtest, dashboard snapshot 후 `docs/` export
- `--refresh-mode light`: 공식 RSS/오늘 snapshot/market up-down 중심 경량 갱신 후 export
- `--refresh-mode export-only`: 현재 DuckDB snapshot으로 `docs/`만 재생성
- `--skip-push`: export와 `validation.json` 확인까지만 수행하고 git stage/commit/push는 생략
- `--restart-web`: 완료 후 FastAPI/Streamlit을 다시 시작

`docs/data/validation.json`의 `can_publish`가 `true`가 아니면 push를 막습니다.
빈 데이터 검증을 무시한 로컬 preview가 꼭 필요할 때만 `--allow-empty`를 사용하세요.

로컬 cron 예시:

```cron
35 18 * * 1-5 cd /home/joon/work/coding/AI/stock_pred && .venv/bin/python scripts/publish_github_pages.py --refresh-mode full >> logs/github_pages_full_update.log 2>&1
30 20 * * 1-5 cd /home/joon/work/coding/AI/stock_pred && .venv/bin/python scripts/publish_github_pages.py --refresh-mode full >> logs/github_pages_full_update_retry.log 2>&1
```

`scripts/run_daily_pages_publish.py`의 `gh-pages` worktree 방식은 legacy 경로로 남겨 두되,
기본 운영은 `docs/` push + `.github/workflows/github-pages.yml` deploy를 사용합니다.

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

### Cutoff-aware Premarket Shadow

장마감 `18:00 KST` preliminary와 다음 거래일 `08:00 KST` final premarket을 별도
shadow 행으로 저장합니다. Point return은 direct Ridge, overnight gap과 open-to-close는
별도 Ridge 진단 head, 방향확률은 LightGBM classifier가 생성합니다.

```bash
.venv/bin/python scripts/run_premarket_shadow.py \
  --config configs/top50_normal.yaml \
  --stage preliminary_after_close \
  --date latest

.venv/bin/python scripts/run_premarket_global_pipeline.py \
  --config configs/global_market.yaml \
  --top50-config configs/top50_normal.yaml \
  --cutoff latest \
  --prediction-date latest
```

`final_premarket`은 target 직전 완결 미국 종가가 없으면 forecast를 저장하지 않습니다.
S&P500/Nasdaq/SOX/VIX/EWY/KORU/USD-KRW의 signed return만 모델에 사용하며,
`global_risk_score`는 cash/risk gate 필드로만 저장합니다. 두 stage 모두
`production_weight=0.0`이며 기존 `market_outlook_forecasts`를 변경하지 않습니다.

API와 화면:

- `http://localhost:8000/api/market-outlook?date=latest&horizon=all`
- `http://localhost:8000/demo/today`
- `http://localhost:8000/demo/tomorrow`

### Nasdaq/SOX Korea Linkage Shadow

전일 확정 Nasdaq와 Philadelphia Semiconductor Index(`^SOX`)를 다음 KRX 세션에
cutoff-safe하게 연결하는 별도 shadow입니다. KOSPI와 KOSDAQ은 완전히 분리해
Huber/Ridge 글로벌 overlay를 학습하고, `SOX - Nasdaq` 상대수익률로 두 지수의 높은
공선성을 줄입니다. 뉴스와 local LLM feature는 이 후보의 입력에서 제외합니다.

장마감 뒤 실제 지수 종가가 확정되면 baseline/candidate 오차, 방향 적중, 하방 이탈과
80% interval score를 close-loop로 기록합니다. 최근 60개의 성숙한 관측에서 글로벌
후보가 baseline보다 좋아야만 dynamic overlay weight가 `0.00`부터 최대 `0.20`까지
증가합니다. 첫 예측값은 immutable하게 보존하며, 실제값만 나중에 기록합니다.

```bash
# 역사적 cutoff-safe feature 및 시장별 walk-forward shadow
.venv/bin/python scripts/build_us_korea_linkage_features.py \
  --config configs/top50_normal.yaml --date latest
.venv/bin/python scripts/run_us_korea_linkage_shadow.py \
  --config configs/top50_normal.yaml --date latest

# 장마감 close-loop feedback / 장전 shadow daily stage
.venv/bin/python scripts/update_us_korea_linkage_feedback.py \
  --config configs/top50_normal.yaml
.venv/bin/python scripts/run_us_korea_linkage_daily.py \
  --config configs/top50_normal.yaml --stage premarket --date latest
```

`run_premarket_global_pipeline.py`는 글로벌 수집과 final premarket shadow 뒤에 이
linkage shadow도 실행합니다. 최소 252개 OOS와 시장별 60개 live 관찰 전에는 gate를
통과하더라도 `accepted_shadow`, `production_weight=0.0`으로 유지합니다. MAE, 방향,
80% 포함률, 범위 폭, 하방 이탈 gate 중 하나라도 실패하면 `rejected`이며 기존
`market-outlook-v2` production Ridge는 변경하지 않습니다.

- Local demo: `http://localhost:8000/demo/us-korea-linkage`
- APIs: `http://localhost:8000/api/market-us-linkage`,
  `/api/market-us-linkage/history`, `/api/market-us-linkage/feedback`
- Static Pages payload: `docs/data/market_us_linkage_shadow.json`

### KOSPI/KOSDAQ Anchor Basket Range Shadow

KOSPI 다음 거래일 80% 범위의 별도 shadow는 삼성전자(`005930`), SK하이닉스(`000660`),
현대차(`005380`)를 고정 앵커로 사용합니다. KOSDAQ은 각 과거 기준일의 KRX 시가총액
원본에서 ETF·ETN·SPAC·우선주를 제외한 상위 일반주 3개를 다시 선택합니다. 오늘의
구성종목을 과거에 적용하거나 시가총액 결측을 동일가중치로 대체하지 않습니다.

이 경로는 `q10/q50/q90` LightGBM 분위수 모델, 80% conformal 보정, residual control을
독립 비교합니다. 기존 `market_outlook_forecasts`, 추천 및 production weight는 변경하지
않으며 gate를 통과해도 최소 60일 live final 관찰 전까지 `production_weight=0.0`입니다.

필요한 KRX 인증 정보는 저장소에 넣지 말고 로컬 `.env`에만 둡니다.

```bash
KRX_ID=
KRX_PW=
```

장마감 확정 데이터가 나온 뒤 point-in-time 구성종목을 수집하고 shadow를 실행합니다.
DuckDB writer 작업 전에는 FastAPI/Streamlit 같은 DB 사용 프로세스를 종료하세요.

```bash
.venv/bin/python scripts/backfill_market_anchor_history.py \
  --config configs/market_anchor_shadow.yaml \
  --from-date 2019-01-02 --to-date latest

.venv/bin/python scripts/build_market_anchor_features.py \
  --config configs/market_anchor_shadow.yaml --date latest

.venv/bin/python scripts/train_market_anchor_shadow.py \
  --config configs/market_anchor_shadow.yaml

.venv/bin/python scripts/run_market_anchor_shadow_backtest.py \
  --config configs/market_anchor_shadow.yaml

.venv/bin/python scripts/predict_market_anchor_shadow.py \
  --config configs/market_anchor_shadow.yaml
```

일일 실행은 최신 구성 수집과 feature build를 수행하고, `--retrain`을 붙일 때만 학습·backtest를
함께 실행합니다.

```bash
.venv/bin/python scripts/run_market_anchor_shadow_daily.py \
  --config configs/market_anchor_shadow.yaml --date latest --retrain
```

KRX/pykrx가 과거 시점 시가총액을 비우거나 인증에 실패하면 KOSDAQ shadow는 `not_ready`로
남습니다. 임의의 현재 구성종목, 중립 수급값 또는 추정 시가총액을 사용하지 않습니다.

출력:

- DuckDB: `market_anchor_constituents_daily`, `market_anchor_features_daily`,
  `market_anchor_shadow_forecasts`, `market_anchor_shadow_backtest_runs`
- API: `http://localhost:8000/api/market-anchor-shadow`
- Static Pages export: `docs/data/market_anchor_shadow.json`

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

## X MarketNews Feed Signal

`https://x.com/MarketNews_Feed`는 HTML 크롤링 없이 공식 X API v2로만 수집합니다.
수집한 post text metadata는 기존 `market_news_feed`에 `source=x_marketnews_feed`로 저장하며,
`news_signal_daily`, 시장전망, 내일 예측, 롱숏 리스크 설명, GitHub Pages 뉴스 목록에 같은
뉴스 feature 경로로 반영됩니다.

`.env`에는 X developer portal에서 발급받은 bearer token만 둡니다.

```bash
X_BEARER_TOKEN=
```

인증 없이 설정만 확인:

```bash
.venv/bin/python scripts/collect_x_market_news.py \
  --config configs/x_market_news.yaml \
  --dry-run
```

수집:

```bash
.venv/bin/python scripts/collect_x_market_news.py \
  --config configs/x_market_news.yaml \
  --allow-missing-key
```

30분 주기 cron 예시:

```cron
*/30 * * * * cd /home/joon/work/coding/AI/stock_pred && .venv/bin/python scripts/collect_x_market_news.py --config configs/x_market_news.yaml --allow-missing-key >> logs/x_market_news.log 2>&1
```

`X_BEARER_TOKEN`이 없으면 fake post를 만들지 않고 `collection_failures`에 기록합니다.
X post 기반 news feature는 `2M/3M` 학습 feature에 포함하고 `6M/9M/1Y`에는 기존 정책대로
제외합니다.

재학습 후 X feature가 Top20과 KOSPI/KOSDAQ 범위 예측에 준 영향은 같은 모델로
`with-X` inference와 X feature 중립화 inference를 비교해 저장합니다. 결과는
`x_news_prediction_impact_daily`, `x_market_outlook_impact_daily`에 남고, GitHub Pages에는
`x_news_top20_impact.json`, `x_market_outlook_impact.json`으로 export됩니다.

```bash
.venv/bin/python scripts/build_x_news_impact_analysis.py \
  --config configs/top50_normal.yaml \
  --date latest
```

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
