# 공식 데이터 출처 / 2026-10-23 현재 확인

| 기관 | 데이터 | 링크 | 주의사항 |
|---|---|---|---|
| KRX Data Marketplace | 주식 투자자별 거래실적(개별종목), 순매수 상위, 외국인 보유량 추이, 공매도 거래/잔고, 파생상품 | https://data.krx.co.kr/ | 다운로드 가능한 웹 통계와 Open API 커버리지가 다름; 이용/재배포 조건 확인 |
| KRX Open API | 주식·ETF·파생상품 일별매매정보 등 | https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd | 공개된 서비스 목록을 기준으로 구현; 투자자별 개별종목 API가 기본 목록에 있다고 가정하지 말 것 |
| OpenDART (한국 금융감독원) | 대량보유 상황보고 majorstock | https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS004&apiId=2019021 | API 키/종목의 corp_code 필요; 공시 원문 검증 |
| SEC | Form 13F 대용량 분기 데이터 ZIP | https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets | 분기 공개. 매입 시점·숏 포지션 알 수 없음 |
| SEC | EDGAR filings 검색/다운로드 | https://www.sec.gov/edgar/search/ | 공시 제출일, 정정신고 확인; Fair Access 제한(초당 10 요청) |
| SEC | API/개발자 리소스 | https://www.sec.gov/about/developer-resources | 표준 HTTP, 적절한 User-Agent, 요청속도 정책 준수 |
| SEC | Schedule 13D/13G 대량보유 | https://www.sec.gov/resources-small-businesses/going-public/officers-directors-10-shareholders | 일반적으로 5% 초과 보유 신고 체계 |
| FINRA | 미국 공매도 잔고 및 설명 | https://otce.finra.org/otce/equityShortInterest | short-interest와 daily-short-sale-volume은 다르며 공개·갱신주기 확인 |
| CFTC | COT | https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm | 화요일 기준, 일반적으로 금요일 발표; 개별 투자자/개별 주식 포지션 아님 |
| OpenAI | ChatGPT 프로젝트 만들기 | https://help.openai.com/en/articles/10169521-projects-in-chatgpt | 프로젝트 지침·참고파일 업로드 |

## 경제성
공식 웹 자료를 수동으로 다운로드해 분석하는 초기 검증은 대체로 별도 유료 시세 서비스 없이 시작할 수 있다. 주가와 외국인 일별 수급을 재배포하는 서비스 또는 자동 대량호출은 데이터 라이선스와 상업적 이용조건을 별도로 확인해야 한다. 미국 실시간 옵션 체결·상세 호가·실시간 주문흐름 분석에는 일반적으로 별도 유료 마켓데이터 공급사가 필요하다.
