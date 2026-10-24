# 미국·한국 다음 세션 연구예측

최신 지수 bar 날짜는 2026-10-23이다. 실제 수집·발행 시각은 각 데이터와 실행별로 다르다. 한국 예측의 publication decision은 2026-10-24 00:46:32.234481 UTC, 미국 예측은 00:47:02.778382 UTC에 기록됐다. 두 발행 receipt는 `replay=false`이며 payload checksum 검증을 통과했다. 한국 결과는 주말 `preliminary` 단계다. 월요일 `premarket_final` 예측으로 보지 않는다.

| 지수 | 선택 모델 / 입력 | 상승확률 | 예상수익률 | 예상 종가 | 명목 80% 종가 범위 | 신뢰성 게이트 |
| --- | --- | ---: | ---: | ---: | --- | --- |
| KOSPI | ensemble / price_us | 58.243% | +0.326651% | 5568.43 | 5414.76–5722.10 | rejected |
| KOSDAQ | linear / price | 57.870% | +0.175818% | 753.01 | 733.90–772.12 | rejected |
| S&P 500 | linear / price | 53.131% | +0.082679% | 7629.98 | 7551.13–7708.83 | rejected |
| Nasdaq Composite | linear / price | 55.570% | −0.006845% | 25953.57 | 25549.96–26357.17 | rejected |

대상은 모두 다음 현지 거래 세션인 2026-10-26이다. 한국 종가는 10월 26일 15:30 KST, 미국 종가는 10월 26일 16:00 EDT(10월 27일 05:00 KST)로 계산했다. 상승확률과 기대수익률은 별도 추정치이므로 Nasdaq의 소폭 음수 기대수익률을 양수로 해석하지 않는다. 명목 80% 범위는 보장 구간이 아니다.

## 비교 및 신뢰성 게이트

한국 후보는 `price`와 사전 고정한 6개 미국 입력을 추가한 `price_us`를 같은 학습 및 OOS 날짜에서 비교했다. KOSPI 주모델은 `price_us`, KOSDAQ은 `price`를 선택했다. 같은 모델 가족과 같은 252개 terminal 날짜의 Brier 차이(`price_us − price`)에 대한 20일 블록, 2,000회 paired bootstrap 95% CI는 KOSPI ensemble −0.024757 [−0.044680, −0.013119], KOSDAQ linear −0.006610 [−0.012553, +0.001304]였다. KOSPI의 개선은 retrospective 비교에서 관찰됐고, KOSDAQ 구간은 0을 포함한다.

별도 사전 지정 KOSDAQ `linear / price_us` challenger는 상승확률 51.910%, 기대수익률 +0.401604%, 예상 종가 754.71, 명목 범위 735.85–773.57이었다. 자체 calibration 252개를 썼지만 게이트를 통과하지 못해 주모델로 승격하지 않았다.

| 지수 | Terminal n | Brier / 상승률 기준 | 명목 80% 실제 커버리지 | 판단 |
| --- | ---: | --- | ---: | --- |
| KOSPI | 252 | 0.216225 / 0.240517 | 62.70% | development 통과; terminal 커버리지 기준(75–85%) 미달 |
| KOSDAQ | 252 | 0.252973 / 0.247897 | 67.06% | development 확률·로그손실 게이트 및 terminal 상승률 기준·로그손실·MAE·커버리지·하방위험 기준 미달 |
| S&P 500 | 252 | 0.252282 / 0.249727 | 81.746% | 커버리지는 범위 내; development 확률·로그손실·MAE 및 terminal Brier 유의성·로그손실 기준 미달 |
| Nasdaq Composite | 252 | 0.252582 / 0.249765 | 78.175% | 커버리지는 범위 내; development 및 terminal 확률·로그손실·MAE 게이트 미달 |

네 모델 모두 `rejected`다. 이미 확인한 terminal 이력을 재사용한 미국 입력 실험은 retrospective이며 새 독립 검증이나 production 승인으로 해석하지 않는다.

## 뉴스 가중치와 현재 위험

현재 수집분은 71개(Fed 20, UN 30, ECB 15, 이란 관련 맥락 6개)이며, 모두 지정학 기사나 독립 확인된 사건이라는 뜻은 아니다. 완전한 point-in-time coverage window는 0개이고 성숙한 뉴스 OOS 표본도 없다. 따라서 뉴스 모델은 준비되지 않았으며 네 시장 모두 `α=0`이다. resolved forecast에서 뉴스 혼합 전후 전체 forecast 값이 정확히 같음을 확인했다. 출처 신뢰도·기사 노후도 attention은 학습된 가격 방향 효과가 아니다.

[AP의 10월 22일 해운 보도](https://apnews.com/article/iran-united-states-war-hormuz-trump-israel-2abebce2fca2c085e0e0ee717e42cf28)는 이란 측 사건 주장을 독립적으로 확인하지 못했다고 밝혔다. [AP의 10월 23일 보도](https://apnews.com/article/donald-trump-cuba-venezuela-florida-3b33aab6719558e289632e974c7a82ad)와 [Reuters 신디케이션](https://www.internazionale.it/ultime-notizie-reuters/2026/10/23/trump-weighs-quick-end-to-iran-war-against-letting-conflict-wither)은 발언과 정세 맥락을 보도한 출처이지, 새로운 작전이나 통항량을 독립 확인한 자료가 아니다. 제한된 최신 확인 범위에서 10월 23일 새 직접 공격을 검증하지 못했다. 이는 검색 범위의 결과이며 공격이 없었다는 주장은 아니다. 확전·해운 차질·완화는 조건부 시나리오로만 다루고 확률을 임의 조정하지 않았다.

## 검증과 한계

Clean-tree pytest 369 passed, 1 skipped(기존 warning 207개), scoped strict 37 passed, Smart Money Python 3.12 unittest 10 passed, collector flow 9 passed, 변경 파일 Ruff 및 사후 scoped audit 통과. 소프트웨어 검증은 금융 모델 승인이 아니다. Smart Money 하위 프로젝트의 실제 가격·수급·보유 관측은 0건이므로 포지션, 시장 영향, uplift는 미확인이다. Production 가중치는 0이며 실제 final 예측 60일 요구도 충족하지 않았다.

[출처와 재현성](provenance.md) · [미국 시장·이벤트 프로토콜](../../docs/US_MARKET_EVENT_PROTOCOL.md) · [연구 화면](../../docs/reliable-index-forecast.html)
