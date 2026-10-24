# 출처와 재현성

## 고정된 실험

한국은 7개 가격 입력 `price`와 6개 미국 입력을 추가한 `price_us`를 같은 학습 및 OOS 날짜에서 비교했다. 추가 입력은 SPX·Nasdaq·SOX 1일 수익률, VIX 수준·1일 수익률, SOX−Nasdaq 수익률 차이다. FX와 금리는 포함하지 않았다. 별도 linear `price_us` challenger는 사전 지정했으며 terminal 결과로 주모델을 교체하지 않았다. 미국 SPX와 Nasdaq은 각각 자체 7개 가격 입력, 라벨, OOF 보정을 사용했다.

뉴스 α 후보는 0/.05/.10/.20으로 고정하고 development에서만 선택한다. Terminal은 α를 0으로 veto할 수 있지만 더 나은 α를 다시 선택하지 않는다. 양의 혼합에는 혼합 OOF 자체의 확률 및 잔차 보정과 동일 날짜 baseline 검증이 필요하다. 현재 완전한 PIT coverage가 없어 α는 0이며 네 시장의 base/blended forecast 값은 정확히 같다.

## 관측 및 시간

최신 지수 bar 날짜는 2026-10-23이다. 한국 입력은 그 날짜까지 KOSPI와 KOSDAQ 각각 1,916개 세션이다. 마지막 종가 5550.30과 751.69를 FDR/Yahoo 자료끼리 교차 확인했다. 원본 공개 시각은 알 수 없으므로 SQLite 행의 `published_at`은 null이고 실제 수집 시각만 기록했다.

미국 자료는 Yahoo Finance, `yfinance 1.7.0`을 통해 2026-10-23 23:53:02–23:53:05 UTC에 수집했다. 대상 `^GSPC`, `^IXIC`, `^SOX`, `^VIX`는 2019-01-02부터 2026-10-23까지 기대한 1,964개 정규 세션을 포함한다. 마지막 값은 SPX 7623.6802, Nasdaq 25955.3418, SOX 10779.4639, VIX 19.45다. VIX 자료의 비정규 세션 추가값은 정규장 계산에서 제외했다. 원래 공개 시각과 전체 과거 수정 이력은 검증하지 못했다. 역사적 미국 입력 가용 시각은 거래소 세션 기준으로 재구성했으며 원본 vintage의 PIT 증거가 아니다. 실제 수집(first-seen)은 과거로 소급하지 않았다.

한국 예측 run `739038b49424b73c81e2b93f9be8af34121ce7e47a25787f541f7be19ea642ea`의 publication decision은 2026-10-24T00:46:32.234481Z다. 미국 run `5ce64d3ffc022520d6490d1aad19d6c06ff8d65072e17a103971196dc8399dad`는 00:47:02.778382Z다. 두 실행 모두 `replay=false`; `reliable_publication_v1` resolver가 receipt와 payload checksum을 검증했다. 시간 기준은 큰 payload 준비 후 publication decision 검사이며, 실제 파일 가시성의 나노초 보장을 뜻하지 않는다.

한국 결과는 주말 `preliminary`이며 대상 2026-10-26 종가 시각은 06:30 UTC(15:30 KST)다. 미국 목표는 현지 2026-10-26이며 XNYS 개장 13:30 UTC, 종가 20:00 UTC(10월 27일 05:00 KST)다. 한국 결과에는 아직 발생하지 않은 미국 월요일 종가가 입력되지 않았다. 시장시간 판단은 [KRX](https://global.krx.co.kr/contents/GLB/06/0602/0602010201/GLB0602010201T1.jsp), [NYSE](https://www.nyse.com/markets/hours-calendars?ecid=psgonsgcgaen1n), [Nasdaq](https://www.nasdaq.com/market-activity/stock-market-holiday-schedule), [Cboe VIX FAQ](https://www.cboe.com/tradable_products/vix/faqs)를 참고했다. VIX의 특별 조기폐장 시각은 독립 확인하지 못했다. 그 경우 16:15 ET는 실제 조기폐장 완료 시각으로 검증된 값이 아니라 보수적인 상한 가정이다. 15분 가용성 buffer는 시스템 가정이지 제공자의 공개 시각 증명이 아니다.

## 뉴스 및 실측 범위

Feed 설정은 [international_news_feeds.yaml](../../configs/international_news_feeds.yaml), PIT 및 평가 계약은 [뉴스 모델 카드](../../docs/INTERNATIONAL_NEWS_MODEL_CARD.md)에 기록했다. 수집 metadata는 Fed 20개, UN 30개, ECB 15개, 검토한 이란 관련 맥락 6개로 합계 71개다. 6개 필수 source coverage window 중 완전한 PIT coverage는 0개다. 현재 기사가 수집됐다는 사실만으로 연속 coverage나 성숙 OOF 표본이 생기는 것은 아니다. 한 GDELT 요청은 timeout이었고 나머지 두 쿼리는 시도하지 않았다.

RSS item의 publication, update, 실제 최초 수집 시각과 주장 상태는 서로 구분했다. [AP 10월 22일 보도](https://apnews.com/article/iran-united-states-war-hormuz-trump-israel-2abebce2fca2c085e0e0ee717e42cf28)의 사건 주장은 AP가 독립 확인하지 못한 정보다. [AP 10월 23일 보도](https://apnews.com/article/donald-trump-cuba-venezuela-florida-3b33aab6719558e289632e974c7a82ad)와 [Reuters 신디케이션](https://www.internazionale.it/ultime-notizie-reuters/2026/10/23/trump-weighs-quick-end-to-iran-war-against-letting-conflict-wither)은 발언과 정세 맥락을 전하지만, 그 자체로 작전이나 통항량을 독립 검증하지 않는다. 날짜만 있거나 시각이 없는 기사는 임의 시각을 부여하지 않았고 현재 기사 내용을 과거 feature row에 넣지 않았다.

Smart Money 하위 프로젝트의 가격·수급·보유 관측은 0건이다. 따라서 포지션과 시장 영향은 insufficient/inconclusive이며 uplift는 측정되지 않았다. 공개 집계 자료로 특정 계좌 보유, 매입가, 헤지 내역을 복원하지 않는다.

## 검증과 소스 범위

검증 결과: clean-tree pytest 369 passed, 1 skipped, 기존 warning 207개; scoped strict 37 passed; Smart Money Python 3.12 unittest 10 passed; collector flow 9 passed; 변경 파일 Ruff 통과; 사후 scoped audit 통과. 소프트웨어 통과와 네 금융 모델의 `rejected` 판단은 서로 다른 결과다. Production 가중치는 0이며 실제 final 예측 60일 요건은 충족하지 않았다.

검토된 소스 hash는 `us_market_pipeline.py` `b684b0fa40c13397a322f2932f17a3feabd8dbe6661239fa097625898618622b`, `event_context.py` `d1d075665c54a58dfe56b48e87eab450f4cc32216906f9e2a950fc73183d898c`, `international_news.py` `bd721218f6b8f7fa55d01e771e5a6ffe1729c0d2d8c2c1cb3cbcd65e0a176488`다. 기준은 `origin/master` `40486c6aa35a0a46cab8d373673130d0e8249d7f`, 대상 브랜치는 `feat/us-market-index-refresh-20261024`다. 변경에는 필요한 코드, tests, 설정, 문서, 선별한 파생 보고서만 포함한다. credentials, 환경, SQLite/DB 및 WAL/SHM, backups, raw 시세·뉴스 자료, private maps, local scratch는 제외한다. 원본 dirty 작업에는 prior-port baseline 이후의 새 미국/뉴스 변경만 반영하며 관련 없는 변경은 보존한다. 최종 commit/remote SHA는 외부 handoff에 기록한다. Force push와 hosting 배포는 하지 않는다.

[요약 결과](summary.md) · [미국 시장·이벤트 프로토콜](../../docs/US_MARKET_EVENT_PROTOCOL.md) · [뉴스 모델 카드](../../docs/INTERNATIONAL_NEWS_MODEL_CARD.md)
