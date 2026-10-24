# Smart Money Position Analyzer (KR + US)

**목표:** 공식 거래·공시 데이터로 한국의 외국인/기관 수급과 미국의 기관 보유 변화를 연구합니다. 이는 **큰손 개인의 실제 거래 내역 또는 계좌 포지션을 복원하는 프로그램이 아닙니다.** 파생 헤지는 동일 계좌 연계 정보 없이는 단정할 수 없습니다.

## 포함된 파일

- `AGENTS.md`, `agents/*.md`: PM·데이터 수집·DB·시그널·보안/법무·QA 전문 Agent 역할
- `skills/smart-money-position/SKILL.md`: 분석·수집 파이프라인 Skill
- `.codex/prompts/*.md`, `CODEX.md`: Codex 지시서 / 단계별 실행 프롬프트
- `harness/`: 테스트 명령, 품질 게이트, 점검 체크리스트
- `smartmoney/`: Python 3.10+ 표준 라이브러리 기반 SQLite DB·CSV/API/ZIP/HTML 수집기·증거 기반 신호 엔진
- `data/smartmoney.sqlite`: **구조와 출처·종목 마스터만 포함**. 실제 주가·거래내역은 비어 있음
- `configs/*.json`, `examples/*.csv`: 형식 템플릿. 실제 수급 데이터라고 주장하지 않음
- `docs/`: 데이터 출처/허가/신호 제한/배포/백테스트 지침

## 가장 빠른 실행 (Ubuntu/macOS, Python 3.10 이상)

압축 해제 후 프로젝트 경로에서 아래 명령을 실행합니다. **별도 Python 의존 라이브러리가 필요 없습니다.**

```bash
python3 -m smartmoney doctor
python3 -m smartmoney status
python3 -m unittest discover -s tests -v
python3 -m smartmoney analyze --market KR --symbol 005930
```

데이터 파일이 비어 있으므로 처음 분석은 **`불확실`** 이 정상입니다.

내 DB를 별도 위치에 만들려면:

```bash
python3 -m smartmoney --db /home/$USER/my-smartmoney.sqlite init-db
python3 -m smartmoney --db /home/$USER/my-smartmoney.sqlite status
```

## 1) 한국 주가: KRX 공식 OPEN API

1. [KRX OPEN API](https://openapi.krx.co.kr/) 가입, 해당 API 사용 승인 및 `AUTH_KEY` 확보.
2. 아래 환경변수에 키를 넣되 `.env`/깃에 커밋하지 않습니다.
3. KOSPI/코스닥 **일별 종가, 거래량, 거래대금** 자료를 다운로드합니다. 이 엔드포인트에 **투자자별 순매수는 없습니다.**

```bash
export KRX_AUTH_KEY='issued-api-key'
python3 -m smartmoney fetch-krx --date 2026-10-22 --exchange kospi
python3 -m smartmoney fetch-krx --date 2026-10-22 --exchange kosdaq
```

**주의:** 공시/거래 데이터에 시간이 걸릴 수 있어, 최신 영업일 확정 후 실행합니다. 자격·사용량·재배포 권한은 KRX 최신 약관을 우선합니다. 승인 없이 비공개 화면 API를 추측 호출하는 기능은 제공하지 않습니다.

## 2) 한국 외국인 수급·보유율: KRX CSV

1. [KRX 정보데이터시스템](https://data.krx.co.kr/)에서 **종목별 투자자별 거래실적 / 외국인 보유현황**과 같은 해당 자료를 직접 조회·다운로드합니다. 실제 화면명/다운로드 양식은 변할 수 있습니다.
2. 표의 숫자 단위(주/천주, 원/백만원)와 투자자 분류 및 날짜를 확인합니다.
3. 아래 헤더 예제로 재정렬하거나 `configs/krx_flow_mapping.json`으로 열 이름을 연결합니다. 단위가 축약된 다운로드는 가져오기 **전에** 주/원으로 환산합니다.

```bash
python3 -m smartmoney import-csv kr-flow path/to/foreign-flow.csv --market KR --investor FOREIGN
python3 -m smartmoney import-csv kr-ownership path/to/foreign-ownership.csv --market KR
python3 -m smartmoney analyze --market KR --symbol 005930
```

표준 양식의 열 예:

```text
kr-flow:      symbol,trade_date,investor_type,buy_qty,sell_qty,net_qty,buy_value,sell_value,net_value
kr-ownership: symbol,trade_date,held_shares,ownership_pct
price:        symbol,trade_date,close,volume
```

## 3) 한국 공시: OpenDART

`dart_corp_code`는 **8자리**, 종목코드는 **6자리**이며 서로 다릅니다. DART 고유번호 매핑은 공식 `corpCode.xml` 다운로드 또는 발행사 공시에서 정확히 확인한 후 입력합니다.

```bash
export DART_API_KEY='issued-api-key'
python3 -m smartmoney fetch-dart --corp-code 00126380 --symbol 005930
```

`majorstock.json` 결과의 `repror`, `stkqy`, `stkrt`, `rcept_dt` 등을 기록합니다. 공시일과 실제 매매일은 다릅니다. `available_at`은 다운로드 시각으로 기록되므로 과거 백테스트에 새로 수집한 과거 데이터를 미래정보처럼 쓰지 않습니다.

## 4) 미국 기관 13F: SEC 공식 ZIP

1. [SEC 13F Data Sets](https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets)에서 원하는 **공식 ZIP 직접 링크**를 복사합니다. 매 분기 파일명과 URL이 달라 **자동으로 URL을 추정하지 않습니다.**
2. SEC 요청시 소속 또는 개인 식별 가능 연락처가 포함된 User-Agent를 환경변수로 설정하고 낮은 빈도로 다운로드합니다.
3. CIK 또는 CUSIP 필터를 지정해 원본 ZIP에서 `SUBMISSION.tsv`, `COVERPAGE.tsv`, `INFOTABLE.tsv`를 읽습니다.

```bash
export SEC_USER_AGENT='MyResearch research-contact@example.com'
python3 -m smartmoney download-sec13f 'https://www.sec.gov/files/datastandardsinnovation/data/form-13f-data-sets/01jun2026-31aug2026_form13f.zip' --out downloads/sec13f_2026_jun_aug.zip
python3 -m smartmoney import-sec13f downloads/sec13f_2026_jun_aug.zip --manager-cik 1067983
# 다른 기관들을 수집하거나 특정 CUSIP을 필터로 지정할 수 있음
python3 -m smartmoney import-csv cusip-map configs/my_verified_cusip_map.csv --market US
python3 -m smartmoney analyze --market US --symbol AAPL
```

**매우 중요:** SEC는 **2023-01-03 이후 제출 13F를 미국 달러 단위**로 보고합니다. 이전 제출 분량은 천 달러 단위일 수 있으므로 적재기는 제출일 기준으로 `value_unit` 및 `reported_value_usd`를 별도로 관리합니다. 원문의 `VALUE`와 변환값을 모두 보관합니다. 이 금액은 `매수금액`이 아니라 **보유 평가액**입니다. 정정공시·13F-NT·주식분할 보정은 검증 전 예측 신호에 넣지 않습니다.

13F의 분기말 주식 보유량은 **분기 중 거래 날짜·가격·공매도 포지션을 보여주지 않습니다.**

## 5) 미국 공매도 거래량: FINRA

```bash
python3 -m smartmoney fetch-finra --date 2026-10-22 --symbols AAPL,NVDA
```

FINRA Reg SHO 공개 일일 공매도 거래량(`ShortVolume`)을 수집합니다. **이것은 미결제 공매도 잔고(Short Interest)가 아니며 하락 베팅 확정 근거로 사용하면 안 됩니다.** 미국의 실제 short interest는 별도 날짜·출처를 갖는 정규화 CSV로 적재합니다.

```bash
python3 -m smartmoney import-csv short-interest path/to/verified_us_short_interest.csv --market US
```

## 6) CFTC COT (미국 시장 전체 선물 포지션)

[CFTC 과거 압축 보고서](https://www.cftc.gov/MarketReports/CommitmentsofTraders/HistoricalCompressed/index.htm)에서 **Traders in Financial Futures — Futures Only** 파일을 사용합니다.

```bash
python3 -m smartmoney fetch-cftc --year 2026 --contract-contains 'S&P 500'
# 또는 내려받은 공식 ZIP 직접 반입
python3 -m smartmoney import-cftc downloads/financial_futures_tff.zip --contract-contains 'S&P 500'
```

이 자료는 S&P 500 등 **선물 시장 투자자 그룹 포지션**, 개별 주식 AAPL/NVDA를 누가 샀는지 보여주는 자료가 아닙니다.

## 7) 공개 웹 거래 표 HTML 파싱 (선택)

웹 공개 테이블을 이용하려면 **사이트 약관상 기계적 이용이 허용되며 robots.txt도 허용해야 합니다.** 웹서버에 부하를 주거나 로그인/캡차/방화벽을 우회하지 않습니다. JS에서 렌더되는 표, 비공개 웹 내부 API 등은 제외합니다.

```bash
python3 -m smartmoney web-table \
  'https://your-permitted-site.example/data' \
  --allow-host your-permitted-site.example \
  --confirm-rights --table-index 0 --out downloads/table.csv

python3 -m smartmoney import-web-table \
  'https://your-permitted-site.example/data' \
  --allow-host your-permitted-site.example --confirm-rights \
  --kind kr-flow --investor FOREIGN \
  --mapping configs/krx_flow_mapping.json
```

예제 주소는 placeholder로 실제 요청할 수 없습니다. 직접 이용권한이 있는 페이지 URL로 변경합니다. URL 호스트를 정확히 허용해야 하며 HTTPS만 수용하고, robots 확인 실패시 차단합니다. HTML `rowspan/colspan` 같은 복잡한 표는 **잘못 정렬하는 대신 오류**를 반환하므로 CSV를 수작업 검증하세요.

## 8) 결과 해석 및 스케줄 운영

```bash
python3 -m smartmoney analyze --market KR --symbol 005930 --as-of 2026-10-22
python3 -m smartmoney analyze --market US --symbol AAPL
python3 -m smartmoney status
```

- 신호는 **정량 근거/상충 증거/결측/시점** 포함 JSON을 출력하고 `signal_snapshots`에 보관합니다.
- 일별 데이터: 거래소 데이터 확정 뒤 1회; 미국 13F: 공개 이후 분기 1회; COT: 공식 주간 발표 이후 1회.
- 실거래 자동화, 주문 API, 매수/매도 알림, 성과 확률은 **초기 범위에서 제외**합니다.
- 정교한 PIT 워크포워드 분석에는 `available_at`을 사용하며, 이전에 게시된 원천을 **나중에 다운로드한 기록은 수집 시각 기준**으로 보수적으로 처리합니다.

자세한 데이터 계약 및 연구 제한은 `docs/`를 보세요. **실시간 웹 수집은 이 ZIP 제작 과정에서 실행하지 않았습니다. DB의 거래 데이터는 0건입니다.**
