# 공식 데이터 출처 Reference

2026-10-23 문서 기준. 구체 엔드포인트/API 신청 절차, 이용조건은 제공기관의 최신 안내를 우선하며, 자동화 실행 전에 반드시 확인하세요.

- KRX 공식 OPEN API: https://openapi.krx.co.kr/ (일별 거래 API `stk_bydd_trd` / `ksq_bydd_trd`, `AUTH_KEY` 헤더)
- KRX Data Marketplace 통계/다운로드: https://data.krx.co.kr/ (투자자별 거래, 외국인 보유현황 등; 홈페이지 화면 구성 변동 가능)
- DART OpenAPI 대량보유 상황: https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS004&apiId=2019021 (최신 개발가이드에서 정확한 항목 선택)
- SEC 13F 분기별 공개 ZIP: https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets
- SEC 13F 지침/금액 단위: https://www.sec.gov/filergroup/announcements-old/edgar-release-2241 (2023-01-03부터 정수 달러)
- FINRA Reg SHO 거래량: https://developer.finra.org/docs/api-explorer/query_api-equity-reg_sho_daily_short_sale_volume
- FINRA short volume ≠ short interest: https://syndication.finra.org/content/short-interest-what-it-what-it-not
- CFTC TFF 역대 ZIP: https://www.cftc.gov/MarketReports/CommitmentsofTraders/HistoricalCompressed/index.htm
- CFTC TFF CSV 컬럼명: https://www.cftc.gov/MarketReports/CommitmentsofTraders/HistoricalViewable/cotvariablestfm.html

웹 거래 표를 파싱할 때는 **URL을 제공했다는 이유만으로 자동수집 권한을 가진 것이 아닙니다.** 별도 서면허가/약관/robots 확인 및 데이터 단위 검증을 통과한 공개 정적 HTML 표만 읽도록 했습니다. 사이트별 파서가 아닌 일반적인 헤더 기반 CSV 변환기이며 특수 표는 수동 검토가 필요합니다.
