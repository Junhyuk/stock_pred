from __future__ import annotations

from fastapi.responses import HTMLResponse


def dashboard_html() -> HTMLResponse:
    return HTMLResponse(
        _page(
            "AI Robo Quant Dashboard",
            """
            <section class="hero">
              <form class="search" onsubmit="goStock(event)">
                <span>Search</span>
                <input id="ticker" placeholder="종목명 또는 코드" />
                <button>조회</button>
              </form>
              <p class="notice">본 서비스는 투자 참고용 정보이며, 과거 수익률과 백테스트 결과가 미래 수익을 보장하지 않습니다.</p>
            </section>
            <article class="panel focus-panel">
              <h2>삼성전자 포커스</h2>
              <div id="focus" class="empty">삼성전자 데이터를 불러오는 중입니다.</div>
            </article>
            <section class="grid cards" id="position"></section>
            <section class="grid two">
              <article class="panel">
                <h2>인기 섹터</h2>
                <div id="sectors" class="stack empty">아직 생성된 추천 결과가 없습니다.</div>
              </article>
              <article class="panel">
                <h2>AI 추천주 Top20</h2>
                <div id="top20" class="table empty">아직 생성된 예측 결과가 없습니다.</div>
              </article>
            </section>
            <section class="grid two">
              <article class="panel">
                <h2>산업별 추천</h2>
                <div id="sectorRanking" class="table empty">아직 생성된 산업별 추천이 없습니다.</div>
              </article>
              <article class="panel">
                <h2>종목 클러스터</h2>
                <div id="clusters" class="stack empty">아직 생성된 클러스터가 없습니다.</div>
              </article>
            </section>
            <section class="grid two">
              <article class="panel">
                <h2>핵심 포트 종목</h2>
                <div id="core" class="card-grid empty">아직 포트폴리오 snapshot이 없습니다.</div>
              </article>
              <article class="panel">
                <h2>정량 포트폴리오</h2>
                <div id="portfolio" class="stack empty">아직 포트폴리오 snapshot이 없습니다.</div>
              </article>
            </section>
            <section class="grid two">
              <article class="panel">
                <h2>Backtest 통계</h2>
                <div id="backtest" class="metrics"></div>
              </article>
              <article class="panel">
                <h2>모델 정확도</h2>
                <div id="accuracy" class="table empty">아직 backtest 결과가 없습니다.</div>
              </article>
            </section>
            <div id="dashboardPage" hidden></div>
            """,
        )
    )


def backtest_html() -> HTMLResponse:
    return HTMLResponse(
        _page(
            "AI Robo Quant Backtest",
            """
            <section class="hero compact">
              <h1>Backtest 검증</h1>
              <p>과거 특정 시점의 예측 결과를 실제 이후 주가와 비교한 검증입니다.</p>
              <label class="inline">기간
                <select id="horizon" onchange="bootBacktest()">
                  <option value="20">1M</option>
                  <option value="60" selected>3M</option>
                  <option value="120">6M</option>
                  <option value="240">1Y</option>
                  <option value="480">2Y</option>
                </select>
              </label>
            </section>
            <section class="grid two">
              <article class="panel"><h2>요약</h2><div id="backtest" class="metrics"></div></article>
              <article class="panel"><h2>모델 정확도</h2><div id="accuracy" class="table"></div></article>
            </section>
            <article class="panel"><h2>Top20 추천 후 실제 성과</h2><div id="top20Backtest" class="table"></div></article>
            <section class="grid two">
              <article class="panel"><h2>최근 30일 예측 괴리 요약</h2><div id="priceGapSummary" class="metrics"></div></article>
              <article class="panel"><h2>괴리 상태</h2><div id="priceGapStatus" class="stack empty">최근 가격 괴리 데이터를 불러오는 중입니다.</div></article>
            </section>
            <article class="panel"><h2>최근 30일 예측 vs 실제 가격 괴리</h2><div id="priceGapTable" class="table empty">최근 가격 괴리 데이터를 불러오는 중입니다.</div></article>
            <div id="backtestPage" hidden></div>
            """,
        )
    )


def two_stock_demo_html() -> HTMLResponse:
    return HTMLResponse(
        _page(
            "AI Robo Quant Two Stock Demo",
            """
            <section class="hero compact">
              <a class="link" href="/dashboard">← Dashboard</a>
              <h1>삼성전자 · 에스엘 예측 데모</h1>
              <p>두 종목의 최신 가격, 3M/6M 예측, 추천 점수, 클러스터 유사 종목을 비교합니다.</p>
              <div class="segmented" aria-label="예측 기간">
                <button type="button" class="active" data-horizon="3M" onclick="selectDemoHorizon(this)">3M</button>
                <button type="button" data-horizon="6M" onclick="selectDemoHorizon(this)">6M</button>
              </div>
            </section>
            <section id="twoStockCards" class="grid two">
              <article class="panel empty">데모 데이터를 불러오는 중입니다.</article>
            </section>
            <article class="panel">
              <h2>예측 이력 요약</h2>
              <div id="twoStockHistory" class="table empty">예측 이력을 불러오는 중입니다.</div>
            </article>
            <p id="twoStockDisclaimer" class="notice"></p>
            <div id="twoStockDemoPage" hidden></div>
            """,
        )
    )


def focus_stock_demo_html() -> HTMLResponse:
    return HTMLResponse(
        _page(
            "AI Robo Quant Focus Stocks Demo",
            """
            <section class="hero compact">
              <a class="link" href="/dashboard">← Dashboard</a>
              <h1>삼성전자 · SK하이닉스 · 에스엘 글로벌 보정 데모</h1>
              <p>국내 예측값은 유지하고, 미국장·반도체·VIX·금리·환율 레짐이 준비되면 보정 점수와 위험 태그를 함께 표시합니다.</p>
              <div class="segmented" aria-label="예측 기간">
                <button type="button" class="active" data-horizon="3M" onclick="selectFocusDemoHorizon(this)">3M</button>
                <button type="button" data-horizon="6M" onclick="selectFocusDemoHorizon(this)">6M</button>
              </div>
            </section>
            <section class="grid two">
              <article class="panel">
                <h2>글로벌 위험 레짐</h2>
                <div id="focusRegime" class="stack empty">글로벌 레짐을 불러오는 중입니다.</div>
              </article>
              <article class="panel">
                <h2>최신 글로벌 입력</h2>
                <div id="focusGlobalMarkets" class="table empty">글로벌 시장 데이터를 불러오는 중입니다.</div>
              </article>
            </section>
            <section id="focusStockCards" class="grid three">
              <article class="panel empty">데모 데이터를 불러오는 중입니다.</article>
            </section>
            <article class="panel">
              <h2>예측 이력 요약</h2>
              <div id="focusStockHistory" class="table empty">예측 이력을 불러오는 중입니다.</div>
            </article>
            <p id="focusStockDisclaimer" class="notice"></p>
            <div id="focusStocksDemoPage" hidden></div>
            """,
        )
    )


def four_stock_demo_html() -> HTMLResponse:
    return HTMLResponse(
        _page(
            "AI Robo Quant Four Stocks Demo",
            """
            <section class="hero compact">
              <a class="link" href="/dashboard">← Dashboard</a>
              <h1>삼성전자 · SK하이닉스 · LG전자 · 에스엘 예측 데모</h1>
              <p>네 종목의 최신 가격, 3M/6M 예측, 글로벌 보정, 최근 가격 괴리와 클러스터 유사 종목을 비교합니다.</p>
              <div class="segmented" aria-label="예측 기간">
                <button type="button" class="active" data-horizon="3M" onclick="selectFourDemoHorizon(this)">3M</button>
                <button type="button" data-horizon="6M" onclick="selectFourDemoHorizon(this)">6M</button>
              </div>
            </section>
            <section class="grid cards four-stock-grid" id="fourStockCards">
              <article class="panel empty">네 종목 예측 데이터를 불러오는 중입니다.</article>
            </section>
            <section class="grid two">
              <article class="panel">
                <h2>글로벌 위험 레짐</h2>
                <div id="fourStockRegime" class="stack empty">글로벌 레짐을 불러오는 중입니다.</div>
              </article>
              <article class="panel">
                <h2>30일 가격 괴리 요약</h2>
                <div id="fourStockGapSummary" class="metrics"></div>
              </article>
            </section>
            <article class="panel">
              <h2>예측 이력 요약</h2>
              <div id="fourStockHistory" class="table empty">예측 이력을 불러오는 중입니다.</div>
            </article>
            <p id="fourStockDisclaimer" class="notice"></p>
            <div id="fourStocksDemoPage" hidden></div>
            """,
        )
    )


def top50_universe_html() -> HTMLResponse:
    return HTMLResponse(
        _page(
            "AI Robo Quant Top50 Universe",
            """
            <section class="hero compact">
              <a class="link" href="/dashboard">← Dashboard</a>
              <a class="link" href="/recommendations/long-short" style="margin-left:12px">Top50 롱·숏 →</a>
              <a class="link" href="/recommendations/up-down" style="margin-left:12px">상승·하락 Top10 →</a>
              <h1>Top50 예측 유니버스</h1>
              <p>KOSPI 30 + KOSDAQ 20 시가총액 기준 유니버스와 최신 가격·예측값을 확인합니다.</p>
              <div class="segmented" aria-label="예측 기간">
                <button type="button" class="active" data-horizon="3M" onclick="selectTop50Horizon(this)">3M</button>
                <button type="button" data-horizon="6M" onclick="selectTop50Horizon(this)">6M</button>
              </div>
            </section>
            <section id="top50Metrics" class="grid cards">
              <article class="metric"><span class="muted">로딩 중</span><strong>-</strong></article>
            </section>
            <article class="panel">
              <h2>Top50 종목</h2>
              <div id="top50Table" class="table empty">유니버스 데이터를 불러오는 중입니다.</div>
            </article>
            <p id="top50Disclaimer" class="notice"></p>
            <div id="top50Page" hidden></div>
            """,
        )
    )


def long_short_html() -> HTMLResponse:
    return HTMLResponse(
        _page(
            "AI Robo Quant Top50 Long-Short",
            """
            <section class="hero compact">
              <a class="link" href="/universe/top50">← Top50 Universe</a>
              <a class="link" href="/recommendations/up-down" style="margin-left:12px">상승·하락 Top10 →</a>
              <h1>Top50 시장별 롱·숏 추천</h1>
              <p>KOSPI/KOSDAQ 각각 LONG·SHORT 레그를 시장 내 랭킹으로 선정합니다. 숏 레그는 모의 시뮬레이션입니다.</p>
              <div class="segmented" aria-label="예측 기간">
                <button type="button" class="active" data-horizon="2M" onclick="selectLongShortHorizon(this)">2M 단기</button>
                <button type="button" data-horizon="6M" onclick="selectLongShortHorizon(this)">6M 장기</button>
              </div>
            </section>
            <section id="longShortMetrics" class="grid cards">
              <article class="metric"><span class="muted">로딩 중</span><strong>-</strong></article>
            </section>
            <section class="grid two">
              <article class="panel">
                <h2>KOSPI LONG</h2>
                <div id="longShortKospiLong" class="table empty">데이터를 불러오는 중입니다.</div>
              </article>
              <article class="panel">
                <h2>KOSPI SHORT</h2>
                <div id="longShortKospiShort" class="table empty">데이터를 불러오는 중입니다.</div>
              </article>
            </section>
            <section class="grid two">
              <article class="panel">
                <h2>KOSDAQ LONG</h2>
                <div id="longShortKosdaqLong" class="table empty">데이터를 불러오는 중입니다.</div>
              </article>
              <article class="panel">
                <h2>KOSDAQ SHORT</h2>
                <div id="longShortKosdaqShort" class="table empty">데이터를 불러오는 중입니다.</div>
              </article>
            </section>
            <p id="longShortDisclaimer" class="notice"></p>
            <div id="longShortPage" hidden></div>
            """,
        )
    )


def market_up_down_html() -> HTMLResponse:
    return HTMLResponse(
        _page(
            "AI Robo Quant Top50 Up-Down",
            """
            <section class="hero compact">
              <a class="link" href="/universe/top50">← Top50 Universe</a>
              <a class="link" href="/recommendations/long-short" style="margin-left:12px">롱·숏 →</a>
              <h1>Top50 시장별 상승·하락 추천</h1>
              <p>KOSPI/KOSDAQ 각각 상승 TOP6·4, 하락 TOP6·4를 시장 내 랭킹으로 선정합니다. 하락 추천은 모델 기반 하방 신호입니다.</p>
              <div class="segmented" aria-label="예측 기간">
                <button type="button" class="active" data-horizon="2M" onclick="selectMarketUpDownHorizon(this)">2M 단기</button>
                <button type="button" data-horizon="6M" onclick="selectMarketUpDownHorizon(this)">6M 장기</button>
              </div>
            </section>
            <section id="marketUpDownMetrics" class="grid cards">
              <article class="metric"><span class="muted">로딩 중</span><strong>-</strong></article>
            </section>
            <section class="grid two">
              <article class="panel">
                <h2>KOSPI 상승 TOP6</h2>
                <div id="marketUpDownKospiUp" class="table empty">데이터를 불러오는 중입니다.</div>
              </article>
              <article class="panel">
                <h2>KOSPI 하락 TOP6</h2>
                <div id="marketUpDownKospiDown" class="table empty">데이터를 불러오는 중입니다.</div>
              </article>
            </section>
            <section class="grid two">
              <article class="panel">
                <h2>KOSDAQ 상승 TOP4</h2>
                <div id="marketUpDownKosdaqUp" class="table empty">데이터를 불러오는 중입니다.</div>
              </article>
              <article class="panel">
                <h2>KOSDAQ 하락 TOP4</h2>
                <div id="marketUpDownKosdaqDown" class="table empty">데이터를 불러오는 중입니다.</div>
              </article>
            </section>
            <p id="marketUpDownDisclaimer" class="notice"></p>
            <div id="marketUpDownPage" hidden></div>
            """,
        )
    )


def top20_upside_html() -> HTMLResponse:
    return HTMLResponse(
        _page(
            "AI Robo Quant 3M Top20 Upside",
            """
            <section class="hero compact">
              <a class="link" href="/dashboard">← Dashboard</a>
              <h1>3개월 상승확률·상승여력 Top20 및 예상가격</h1>
              <p>3M/6M/9M/1Y 예측 기준으로 상승확률, 예측수익률, 예상 가격 범위를 함께 확인합니다.</p>
              <div class="segmented" aria-label="예측 기간">
                <button type="button" class="active" data-horizon="3M" onclick="selectTop20UpsideHorizon(this)">3M</button>
                <button type="button" data-horizon="6M" onclick="selectTop20UpsideHorizon(this)">6M</button>
                <button type="button" data-horizon="9M" onclick="selectTop20UpsideHorizon(this)">9M</button>
                <button type="button" data-horizon="1Y" onclick="selectTop20UpsideHorizon(this)">1Y</button>
              </div>
            </section>
            <section id="top20UpsideMetrics" class="grid cards">
              <article class="metric"><span class="muted">로딩 중</span><strong>-</strong></article>
            </section>
            <article class="panel">
              <h2>Top20 추천</h2>
              <div id="top20UpsideTable" class="table empty">추천 데이터를 불러오는 중입니다.</div>
            </article>
            <article class="panel">
              <h2>3M/6M/9M/1Y 예상 상승·하락 가격</h2>
              <div id="top20PriceForecastTable" class="table empty">예상 가격 전망을 불러오는 중입니다.</div>
            </article>
            <p id="top20UpsideDisclaimer" class="notice"></p>
            <div id="top20UpsidePage" hidden></div>
            """,
        )
    )


def today_market_html() -> HTMLResponse:
    return HTMLResponse(
        _page(
            "AI Robo Quant Today Market Update",
            """
            <section class="hero compact">
              <a class="link" href="/demo/focus-stocks">← Focus Demo</a>
              <a class="link" href="/demo/tomorrow">다음 거래일 예측 →</a>
              <h1>오늘 시장 업데이트</h1>
              <p>국내 포커스 종목, 해외시장 동향, 글로벌 위험 레짐, 종목별 뉴스를 한 화면에서 확인합니다.</p>
            </section>
            <section class="grid cards" id="todayStatus">
              <article class="metric"><span class="muted">로딩 중</span><strong>-</strong></article>
            </section>
            <article class="panel">
              <h2>오늘·이번주 KOSPI/KOSDAQ 전망</h2>
              <div id="todayMarketOutlook" class="stack empty">시장 전망을 불러오는 중입니다.</div>
            </article>
            <article class="panel">
              <h2>오늘 급등락 원인 분석</h2>
              <div id="todayMoveExplanations" class="stack empty">원인 분석을 불러오는 중입니다.</div>
            </article>
            <article class="panel">
              <h2>KORU 레버리지 심리</h2>
              <div id="todayKoru" class="stack empty">KORU linkage를 불러오는 중입니다.</div>
            </article>
            <article class="panel">
              <h2>미국 유사섹터 영향</h2>
              <div id="todaySectorLinkage" class="stack empty">미국 유사섹터 linkage를 불러오는 중입니다.</div>
            </article>
            <section class="grid three">
              <article class="panel">
                <h2>국내 포커스 종목</h2>
                <div id="todayFocusPrices" class="table empty">국내 가격을 불러오는 중입니다.</div>
              </article>
              <article class="panel">
                <h2>Yahoo/yfinance 최신 가격</h2>
                <div id="todayYahooPrices" class="table empty">Yahoo 데이터를 불러오는 중입니다.</div>
              </article>
              <article class="panel">
                <h2>글로벌 위험 레짐</h2>
                <div id="todayRegime" class="stack empty">글로벌 레짐을 불러오는 중입니다.</div>
              </article>
            </section>
            <article class="panel">
              <h2>해외시장 동향</h2>
              <div id="todayGlobalMarkets" class="table empty">해외시장 데이터를 불러오는 중입니다.</div>
            </article>
            <article class="panel">
              <h2>종목별 최신 뉴스</h2>
              <div id="todayNews" class="stack empty">뉴스를 불러오는 중입니다.</div>
            </article>
            <p id="todayDisclaimer" class="notice"></p>
            <div id="todayMarketPage" hidden></div>
            """,
        )
    )


def tomorrow_market_html() -> HTMLResponse:
    return HTMLResponse(
        _page(
            "AI Robo Quant Next Trading Day Forecast",
            """
            <section class="hero compact">
              <a class="link" href="/demo/today">← 오늘 시장 업데이트</a>
              <h1>다음 거래일 시장 예측</h1>
              <p>장마감 기준 최신 시장·뉴스·글로벌 레짐을 반영해 다음 개장일 KOSPI/KOSDAQ 전망을 확인합니다.</p>
            </section>
            <section class="grid cards" id="todayStatus">
              <article class="metric"><span class="muted">로딩 중</span><strong>-</strong></article>
            </section>
            <article class="panel">
              <h2>다음 거래일 KOSPI/KOSDAQ 예측</h2>
              <div id="todayMarketOutlook" class="stack empty">다음 거래일 전망을 불러오는 중입니다.</div>
            </article>
            <article class="panel">
              <h2>다음 거래일 숏·롱 범위</h2>
              <div id="tomorrowLongShortRange" class="table empty">숏·롱 범위를 불러오는 중입니다.</div>
            </article>
            <article class="panel">
              <h2>급등락 원인 분석</h2>
              <div id="todayMoveExplanations" class="stack empty">원인 분석을 불러오는 중입니다.</div>
            </article>
            <article class="panel">
              <h2>KORU 레버리지 심리</h2>
              <div id="todayKoru" class="stack empty">KORU linkage를 불러오는 중입니다.</div>
            </article>
            <article class="panel">
              <h2>미국 유사섹터 영향</h2>
              <div id="todaySectorLinkage" class="stack empty">미국 유사섹터 linkage를 불러오는 중입니다.</div>
            </article>
            <section class="grid three">
              <article class="panel">
                <h2>국내 포커스 종목</h2>
                <div id="todayFocusPrices" class="table empty">국내 가격을 불러오는 중입니다.</div>
              </article>
              <article class="panel">
                <h2>Yahoo/yfinance 최신 가격</h2>
                <div id="todayYahooPrices" class="table empty">Yahoo 데이터를 불러오는 중입니다.</div>
              </article>
              <article class="panel">
                <h2>글로벌 위험 레짐</h2>
                <div id="todayRegime" class="stack empty">글로벌 레짐을 불러오는 중입니다.</div>
              </article>
            </section>
            <article class="panel">
              <h2>해외시장 동향</h2>
              <div id="todayGlobalMarkets" class="table empty">해외시장 데이터를 불러오는 중입니다.</div>
            </article>
            <article class="panel">
              <h2>뉴스·거시 context</h2>
              <div id="todayNews" class="stack empty">뉴스를 불러오는 중입니다.</div>
            </article>
            <p id="todayDisclaimer" class="notice"></p>
            <div id="tomorrowMarketPage" hidden></div>
            """,
        )
    )


def stock_html(symbol: str) -> HTMLResponse:
    escaped = symbol.replace("<", "").replace(">", "")
    return HTMLResponse(
        _page(
            f"AI Robo Quant Stock {escaped}",
            f"""
            <section class="hero compact">
              <a class="link" href="/dashboard">← Dashboard</a>
              <h1 id="stockTitle">{escaped}</h1>
              <p>종목별 추천 근거, 예측 이력, 리포트, Backtest 결과를 한 화면에서 확인합니다.</p>
              <div class="segmented" aria-label="예측 기간">
                <button type="button" class="active" data-horizon="3M" onclick="selectStockHorizon(this)">3M</button>
                <button type="button" data-horizon="6M" onclick="selectStockHorizon(this)">6M</button>
              </div>
            </section>
            <section class="grid cards" id="stockMetrics">
              <article class="metric"><span class="muted">로딩 중</span><strong>-</strong></article>
            </section>
            <section class="grid two">
              <article class="panel">
                <h2>추천 정보</h2>
                <div id="stockInfo" class="stack empty">추천 정보를 불러오는 중입니다.</div>
              </article>
              <article class="panel">
                <h2>Factor 점수</h2>
                <div id="factorScores" class="stack empty">Factor를 불러오는 중입니다.</div>
              </article>
            </section>
            <article class="panel chart-panel">
              <h2>가격 및 거래량</h2>
              <div id="priceChartState" class="empty">가격 데이터를 불러오는 중입니다.</div>
              <canvas id="priceChart" class="chart-canvas" aria-label="가격 및 거래량 차트"></canvas>
            </article>
            <article class="panel chart-panel">
              <h2>예측 추이</h2>
              <div id="predictionChartState" class="empty">예측 이력을 불러오는 중입니다.</div>
              <canvas id="predictionChart" class="chart-canvas" aria-label="예측 확률과 예상수익률 차트"></canvas>
            </article>
            <section class="grid two">
              <article class="panel"><h2>예측 이력</h2><div id="history" class="table empty">예측 이력을 불러오는 중입니다.</div></article>
              <article class="panel">
                <h2>같은 클러스터 종목</h2>
                <div id="clusterSummary" class="stack empty">클러스터를 불러오는 중입니다.</div>
                <div id="clusterPeers" class="table"></div>
              </article>
            </section>
            <article class="panel"><h2>애널리스트 리포트</h2><div id="reports" class="table empty">리포트를 불러오는 중입니다.</div></article>
            <article class="panel">
              <h2>Backtest 결과</h2>
              <div id="stockBacktestSummary" class="metrics"></div>
              <div id="stockBacktest" class="table empty">Backtest 결과를 불러오는 중입니다.</div>
            </article>
            <p id="stockDisclaimer" class="notice"></p>
            <div id="stockPage" data-symbol="{escaped}" hidden></div>
            """,
        )
    )


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    :root {{
      --bg: #252838;
      --panel: #34384a;
      --panel-2: #41475a;
      --border: #5f6678;
      --text: #f7f8fb;
      --muted: #bac1cf;
      --yellow: #ffd96a;
      --red: #ff7686;
      --blue: #66d9f2;
      --green: #80e0b5;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, Pretendard, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
      overflow-x: hidden;
    }}
    main {{ width: min(1280px, calc(100vw - 32px)); margin: 0 auto; padding: 24px 0 48px; }}
    h1, h2, h3, p {{ margin: 0; }}
    h1 {{ font-size: 34px; }}
    h2 {{ font-size: 18px; margin-bottom: 14px; }}
    a {{ color: var(--blue); }}
    .hero {{
      display: grid;
      gap: 12px;
      margin-bottom: 20px;
    }}
    .hero.compact {{ background: var(--panel); border: 1px solid var(--border); padding: 20px; }}
    .search {{
      min-height: 76px;
      display: flex;
      gap: 14px;
      align-items: center;
      background: #ffffff;
      color: #34384a;
      padding: 16px 20px;
      border: 1px solid #e6e7ec;
    }}
    .search span {{ font-size: 28px; font-weight: 800; }}
    .search input {{
      min-width: 0;
      flex: 1;
      border: 0;
      outline: 0;
      font-size: 24px;
      font-weight: 700;
      color: #2d3140;
    }}
    button, select {{
      border: 1px solid var(--border);
      background: var(--panel-2);
      color: var(--text);
      padding: 10px 14px;
      font-weight: 700;
    }}
    button {{ cursor: pointer; }}
    .notice, .muted {{ color: var(--muted); }}
    .grid {{ display: grid; gap: 18px; margin-bottom: 18px; }}
    .cards {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .three {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .two {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .four-stock-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .panel, .metric, .mini-card {{
      background: var(--panel);
      border: 1px solid var(--border);
      padding: 18px;
      min-width: 0;
    }}
    .focus-panel {{ margin-bottom: 18px; border-left: 4px solid var(--yellow); }}
    .focus-grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; }}
    .focus-grid strong {{ display: block; color: var(--yellow); font-size: 20px; margin-top: 5px; }}
    .metric strong {{ display: block; font-size: 26px; margin-top: 8px; color: var(--yellow); }}
    .stack {{ display: grid; gap: 10px; }}
    .row {{ display: flex; justify-content: space-between; gap: 14px; align-items: center; border-bottom: 1px solid rgba(255,255,255,.08); padding: 9px 0; }}
    .chip {{ color: #171a24; background: var(--green); padding: 4px 8px; font-size: 12px; font-weight: 800; }}
    .chip.warn {{ background: var(--yellow); }}
    .card-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .mini-card h3 {{ font-size: 16px; margin-bottom: 8px; }}
    .demo-card h2 {{ display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }}
    .demo-metrics {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-bottom: 14px; }}
    .demo-actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }}
    .demo-actions a {{ border: 1px solid var(--border); padding: 9px 12px; text-decoration: none; font-weight: 800; }}
    .bar {{ height: 8px; background: rgba(255,255,255,.12); overflow: hidden; margin-top: 8px; }}
    .bar span {{ display: block; height: 100%; background: var(--blue); }}
    .factor-line {{ display: grid; grid-template-columns: 120px 1fr 52px; gap: 10px; align-items: center; }}
    .factor-line .bar {{ margin-top: 0; }}
    .segmented {{ display: inline-flex; width: fit-content; border: 1px solid var(--border); }}
    .segmented button {{ border: 0; min-width: 64px; }}
    .segmented button.active {{ background: var(--yellow); color: #202331; }}
    .chart-panel {{ margin-bottom: 18px; }}
    .chart-canvas {{ display: block; width: 100%; height: 300px; }}
    .reason-list {{ margin: 0; padding-left: 20px; display: grid; gap: 6px; }}
    .risk {{ color: var(--red); }}
    .peer-link {{ font-weight: 800; text-decoration: none; }}
    .table {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 9px 8px; border-bottom: 1px solid rgba(255,255,255,.08); text-align: left; vertical-align: top; }}
    td, .metric strong, .metric p {{ overflow-wrap: anywhere; }}
    th {{ color: var(--muted); font-weight: 700; }}
    .empty {{ color: var(--muted); }}
    .inline {{ display: flex; align-items: center; gap: 10px; color: var(--muted); }}
    .link {{ text-decoration: none; font-weight: 800; }}
    @media (max-width: 900px) {{
      main {{ width: min(100vw - 20px, 720px); padding-top: 12px; }}
      .cards, .three, .two, .card-grid, .focus-grid {{ grid-template-columns: 1fr; }}
      .demo-metrics {{ grid-template-columns: 1fr; }}
      .search {{ align-items: stretch; flex-direction: column; }}
      .search input {{ font-size: 20px; }}
      .factor-line {{ grid-template-columns: 90px 1fr 44px; }}
      .chart-canvas {{ height: 240px; }}
    }}
  </style>
</head>
<body>
<main>{body}</main>
<script>
async function json(url) {{
  const response = await fetch(url, {{ cache: "no-store" }});
  if (!response.ok) throw new Error(url + " " + response.status);
  return response.json();
}}
function pct(value) {{
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return (Number(value) * 100).toFixed(1) + "%";
}}
function num(value) {{
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toFixed(3);
}}
function money(value) {{
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Math.round(Number(value)).toLocaleString();
}}
function count(value) {{
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "0";
  return Math.round(Number(value)).toLocaleString();
}}
function num1(value) {{
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toFixed(1);
}}
function shortDate(value) {{
  return value ? String(value).slice(0, 10) : "-";
}}
function escapeHtml(value) {{
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}}
function jsonList(value) {{
  if (Array.isArray(value)) return value;
  if (!value) return [];
  try {{
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  }} catch (_) {{
    return [];
  }}
}}
function errorState(label) {{
  return `<div class="empty">${{escapeHtml(label)}}을 불러오지 못했습니다. 잠시 후 다시 시도하세요.</div>`;
}}
function table(items, columns) {{
  if (!items || items.length === 0) return '<div class="empty">아직 생성된 데이터가 없습니다.</div>';
  const head = columns.map(c => `<th>${{c.label}}</th>`).join("");
  const rows = items.map(item => `<tr>${{columns.map(c => `<td>${{c.format ? c.format(item[c.key], item) : (item[c.key] ?? "-")}}</td>`).join("")}}</tr>`).join("");
  return `<table><thead><tr>${{head}}</tr></thead><tbody>${{rows}}</tbody></table>`;
}}
function metric(label, value, sub = "") {{
  return `<article class="metric"><span class="muted">${{label}}</span><strong>${{value}}</strong><p class="muted">${{sub}}</p></article>`;
}}
function compactText(parts) {{
  return (parts || []).filter(part => part !== null && part !== undefined && String(part).trim() !== "" && String(part) !== "-").join(" · ");
}}
function truncateText(value, maxLength = 500) {{
  const text = String(value ?? "");
  return text.length > maxLength ? text.slice(0, maxLength - 1) + "…" : text;
}}
function uniqueText(values, limit = 3) {{
  return [...new Set((values || []).filter(Boolean).map(value => String(value)))].slice(0, limit);
}}
function latestShortDate(items, key = "date") {{
  const dates = (items || []).map(item => shortDate(item?.[key])).filter(value => value && value !== "-").sort();
  return dates.length ? dates[dates.length - 1] : "-";
}}
function statusLabel(status) {{
  const labels = {{
    ready: "정상",
    partial_ready: "부분 준비",
    not_collected: "미수집",
    missing: "부족",
    stale: "지연"
  }};
  return labels[String(status || "")] || String(status || "-");
}}
function componentValue(value) {{
  return statusLabel(value || "missing");
}}
function directionLabel(value) {{
  const labels = {{ UP: "상승", DOWN: "하락", FLAT: "중립", BULLISH: "강세", BEARISH: "약세", NEUTRAL: "중립" }};
  return labels[String(value || "").toUpperCase()] || String(value || "-");
}}
function goStock(event) {{
  event.preventDefault();
  const value = document.getElementById("ticker").value.trim();
  if (value) location.href = "/stock/" + encodeURIComponent(value);
}}
async function bootDashboard() {{
  const [snapshot, backtest, accuracy] = await Promise.all([
    json("/api/dashboard/snapshot"),
    json("/api/backtest/summary?horizon=60"),
    json("/api/models/accuracy")
  ]);
  const pos = snapshot.position_summary || {{}};
  const focus = snapshot.focus_stock || {{}};
  const fp = focus.prediction || {{}};
  const fc = focus.cluster?.cluster || {{}};
  const latest = focus.latest_price || {{}};
  document.getElementById("focus").innerHTML = focus.symbol ? `
    <div class="focus-grid">
      <div><span class="muted">종목</span><strong>${{focus.name || focus.symbol}}</strong></div>
      <div><span class="muted">최근 종가</span><strong>${{latest.close ? Number(latest.close).toLocaleString() : "데이터 미수집"}}</strong></div>
      <div><span class="muted">상승확률</span><strong>${{pct(fp.pred_prob_top20)}}</strong></div>
      <div><span class="muted">전체 순위</span><strong>${{fp.rank ? fp.rank + "위" : "-"}}</strong></div>
      <div><span class="muted">클러스터</span><strong>${{fc.cluster_label || "미생성"}}</strong></div>
    </div>
    <p class="muted" style="margin-top:12px">${{focus.is_top20 ? "현재 Top20 추천에 포함" : "현재 Top20 밖이며 포커스 종목으로 별도 표시"}} · ${{focus.data_status}}</p>
  ` : "삼성전자 데이터가 아직 수집되지 않았습니다.";
  document.getElementById("position").innerHTML = [
    metric("퀀트 장기", pos.quant?.long_term || "중립", "모델 검증 기반"),
    metric("퀀트 단기", pos.quant?.short_term || "중립", "추천 점수 기반"),
    metric("AI KOSPI", pos.ai_robo?.kospi || "NEUTRAL", "정보제공용"),
    metric("현금 비중", pct(pos.ai_robo?.cash_ratio), "시장 상태 반영")
  ].join("");
  const sectors = snapshot.theme_data?.sectors || [];
  document.getElementById("sectors").innerHTML = sectors.length ? sectors.slice(0, 8).map(s => `<div class="row"><span>${{s.sector}}</span><b>${{pct(s.score)}}</b></div>`).join("") : "아직 생성된 추천 결과가 없습니다.";
  document.getElementById("top20").innerHTML = table(snapshot.ai_recommendations || [], [
    {{key:"rank", label:"#"}},
    {{key:"name", label:"종목"}},
    {{key:"up_probability", label:"상승확률", format:pct}},
    {{key:"expected_return", label:"예상수익률", format:pct}},
    {{key:"risk", label:"리스크", format:num}}
  ]);
  const core = snapshot.core_portfolio || [];
  document.getElementById("core").innerHTML = core.length ? core.map(item => `<article class="mini-card"><h3>${{item.name || item.ticker}}</h3><p class="muted">${{item.sector || "-"}} · rating ${{num(item.rating)}}</p><div class="bar"><span style="width:${{Math.max(4, Math.min(100, Number(item.upside || 0) * 100))}}%"></span></div></article>`).join("") : "아직 포트폴리오 snapshot이 없습니다.";
  const portfolio = snapshot.quant_portfolio || {{}};
  document.getElementById("portfolio").innerHTML = table(portfolio.items || [], [
    {{key:"name", label:"종목"}},
    {{key:"sector", label:"섹터"}},
    {{key:"weight", label:"비중", format:pct}},
    {{key:"final_score", label:"점수", format:num}}
  ]);
  renderBacktest(backtest, "backtest");
  document.getElementById("accuracy").innerHTML = table(accuracy.items || snapshot.model_accuracy || [], modelColumns());
  document.getElementById("sectorRanking").innerHTML = table(snapshot.sector_ranking || [], [
    {{key:"sector", label:"산업"}},
    {{key:"average_score", label:"평균점수", format:num}},
    {{key:"recommendation_count", label:"추천수"}},
    {{key:"average_risk", label:"평균위험", format:num}}
  ]);
  const clusters = snapshot.cluster_data || [];
  document.getElementById("clusters").innerHTML = clusters.length ? clusters.map(c => `<div class="row"><span><b>${{c.cluster_label}}</b><br><small class="muted">Cluster ${{c.cluster_id}}</small></span><span class="chip">${{c.member_count}}종목</span></div>`).join("") : "아직 생성된 클러스터가 없습니다.";
}}
function renderBacktest(data, target) {{
  document.getElementById(target).innerHTML = [
    metric("Hit Ratio", pct(data.hit_ratio), "양수 수익률 비율"),
    metric("Precision@20", pct(data.precision_top20), "Top20 실제 양수 비율"),
    metric("초과수익", pct(data.avg_excess_return), "시장 대비"),
    metric("Rank IC", num(data.rank_ic), "순위 상관")
  ].join("");
}}
function modelColumns() {{
  return [
    {{key:"model_name", label:"모델"}},
    {{key:"model_version", label:"버전"}},
    {{key:"horizon", label:"기간"}},
    {{key:"hit_ratio", label:"Hit", format:pct}},
    {{key:"precision_top20", label:"P@20", format:pct}},
    {{key:"avg_excess_return", label:"초과", format:pct}},
    {{key:"rank_ic", label:"IC", format:num}},
    {{key:"gate_status", label:"상태"}}
  ];
}}
async function bootBacktest() {{
  const horizon = document.getElementById("horizon")?.value || "60";
  const horizonName = horizonToName(horizon);
  const [backtest, accuracy, top20, priceGap] = await Promise.all([
    json("/api/backtest/summary?horizon=" + horizon),
    json("/api/models/accuracy"),
    json("/api/backtest/top20?horizon=" + horizon),
    json("/api/backtest/price-gap?lookback_days=30&target_days=30&horizon=" + encodeURIComponent(horizonName) + "&limit=300")
  ]);
  renderBacktest(backtest, "backtest");
  document.getElementById("accuracy").innerHTML = table(accuracy.items || [], modelColumns());
  document.getElementById("top20Backtest").innerHTML = table(top20.items || [], [
    {{key:"prediction_date", label:"예측일"}},
    {{key:"name", label:"종목"}},
    {{key:"model_name", label:"모델"}},
    {{key:"rank_no", label:"순위"}},
    {{key:"actual_return", label:"실제수익", format:pct}},
    {{key:"excess_return", label:"초과수익", format:pct}},
    {{key:"is_hit", label:"Hit"}}
  ]);
  renderPriceGap(priceGap);
}}
function horizonToName(value) {{
  const map = {{"20":"1M", "60":"3M", "120":"6M", "240":"1Y", "480":"2Y"}};
  return map[String(value)] || String(value || "3M");
}}
function renderPriceGap(data) {{
  const summary = data.summary || {{}};
  document.getElementById("priceGapSummary").innerHTML = [
    metric("표본", summary.sample_count ?? 0, `완료 ${{summary.completed_count ?? 0}} · 대기 ${{summary.pending_count ?? 0}}`),
    metric("Latest MAE", pct(summary.mae_latest), "현재까지 실제수익률 기준"),
    metric("Latest Bias", pct(summary.bias_latest), "양수면 과소예측"),
    metric("방향 적중", pct(summary.direction_accuracy_latest), "현재까지 방향")
  ].join("");
  document.getElementById("priceGapStatus").innerHTML = `
    <div class="row"><span>상태</span><b>${{escapeHtml(data.status || "-")}}</b></div>
    <div class="row"><span>기준일</span><b>${{shortDate(data.as_of_date)}}</b></div>
    <div class="row"><span>조회기간</span><b>${{shortDate(data.start_date)}} ~ ${{shortDate(data.as_of_date)}}</b></div>
    <div class="row"><span>30D 완료 MAE</span><b>${{pct(summary.mae_30d)}}</b></div>
    <p class="muted">${{escapeHtml(data.disclaimer || "")}}</p>
  `;
  document.getElementById("priceGapTable").innerHTML = table(data.items || [], [
    {{key:"prediction_date", label:"예측일", format:shortDate}},
    {{key:"status", label:"상태"}},
    {{key:"name", label:"종목", format:(value, item) => `<a class="peer-link" href="/stock/${{encodeURIComponent(item.symbol)}}">${{escapeHtml(value || item.symbol)}}</a>`}},
    {{key:"horizon", label:"기간"}},
    {{key:"rank_no", label:"순위"}},
    {{key:"predicted_return", label:"예측수익", format:pct}},
    {{key:"actual_return_latest", label:"현재 실제", format:pct}},
    {{key:"return_gap_latest", label:"현재 괴리", format:pct}},
    {{key:"actual_return_30d", label:"30D 실제", format:(value, item) => item.status === "completed" ? pct(value) : "검증 대기"}},
    {{key:"return_gap_30d", label:"30D 괴리", format:pct}},
    {{key:"direction_hit_latest", label:"방향"}}
  ]);
}}
async function bootTwoStockDemo() {{
  const horizon = document.querySelector(".segmented button.active")?.dataset.horizon || "3M";
  try {{
    const data = await json("/api/demo/two-stocks?horizon=" + encodeURIComponent(horizon));
    renderTwoStockDemo(data);
  }} catch (_) {{
    document.getElementById("twoStockCards").innerHTML = '<article class="panel empty">데모 데이터를 불러오지 못했습니다.</article>';
    document.getElementById("twoStockHistory").innerHTML = errorState("예측 이력");
  }}
}}
function selectDemoHorizon(button) {{
  document.querySelectorAll(".segmented button").forEach(item => item.classList.remove("active"));
  button.classList.add("active");
  bootTwoStockDemo();
}}
function renderTwoStockDemo(data) {{
  const items = data.items || [];
  document.getElementById("twoStockCards").innerHTML = items.length ? items.map(item => {{
    const pred = item.prediction || {{}};
    const rec = item.recommendation || {{}};
    const latest = item.latest_price || {{}};
    const cluster = item.cluster || {{}};
    const peers = item.peers || [];
    const score = item.display_score ?? rec.final_score ?? pred.pred_prob_top20;
    return `<article class="panel demo-card">
      <h2><span>${{escapeHtml(item.name || item.symbol)}}</span><small class="muted">${{escapeHtml(item.symbol)}}</small></h2>
      <section class="demo-metrics">
        ${{metric("최근 종가", money(latest.close), shortDate(latest.date))}}
        ${{metric("상승 확률", pct(pred.pred_prob_top20), pred.model_version || "예측 미생성")}}
        ${{metric("예상 상대수익률", pct(pred.pred_return), data.horizon || "-")}}
        ${{metric("추천/예측 점수", num(score), item.is_top20 ? "Top20 포함" : "Top20 밖")}}
      </section>
      <div class="row"><span>시장</span><b>${{escapeHtml(item.market || "-")}}</b></div>
      <div class="row"><span>섹터</span><b>${{escapeHtml(item.sector || "-")}}</b></div>
      <div class="row"><span>위험 점수</span><b>${{pct(rec.risk_score ?? pred.pred_risk)}}</b></div>
      <div class="row"><span>클러스터</span><b>${{escapeHtml(cluster?.cluster_label || "클러스터 미생성")}}</b></div>
      <div><b>유사 종목</b><p class="muted">${{peers.length ? peers.slice(0, 5).map(peer => `${{peer.name || peer.symbol}}`).join(" · ") : "유사 종목 데이터가 없습니다."}}</p></div>
      <div class="demo-actions">
        <a href="/stock/${{encodeURIComponent(item.symbol)}}">상세 보기</a>
        <a href="/api/stocks/${{encodeURIComponent(item.symbol)}}/prediction-history?horizon=${{encodeURIComponent(data.horizon)}}">예측 API</a>
      </div>
    </article>`;
  }}).join("") : '<article class="panel empty">데모 종목 데이터가 없습니다.</article>';

  const rows = items.flatMap(item => (item.history || []).slice(0, 10).map(row => ({{
    ...row,
    name: item.name || item.symbol
  }})));
  document.getElementById("twoStockHistory").innerHTML = table(rows, [
    {{key:"name", label:"종목"}},
    {{key:"prediction_date", label:"예측일", format:shortDate}},
    {{key:"horizon", label:"기간"}},
    {{key:"model_name", label:"모델"}},
    {{key:"predicted_probability", label:"확률", format:pct}},
    {{key:"predicted_return", label:"예상수익", format:pct}},
    {{key:"rank_no", label:"순위"}}
  ]);
  document.getElementById("twoStockDisclaimer").textContent = data.disclaimer || "";
}}
async function bootFocusStocksDemo() {{
  const horizon = document.querySelector(".segmented button.active")?.dataset.horizon || "3M";
  try {{
    const data = await json("/api/demo/focus-stocks?horizon=" + encodeURIComponent(horizon));
    renderFocusStocksDemo(data);
  }} catch (_) {{
    document.getElementById("focusRegime").innerHTML = errorState("글로벌 레짐");
    document.getElementById("focusGlobalMarkets").innerHTML = errorState("글로벌 시장 데이터");
    document.getElementById("focusStockCards").innerHTML = '<article class="panel empty">포커스 종목 데이터를 불러오지 못했습니다.</article>';
    document.getElementById("focusStockHistory").innerHTML = errorState("예측 이력");
  }}
}}
function selectFocusDemoHorizon(button) {{
  document.querySelectorAll(".segmented button").forEach(item => item.classList.remove("active"));
  button.classList.add("active");
  bootFocusStocksDemo();
}}
function renderFocusStocksDemo(data) {{
  renderFocusRegime(data.regime || {{}});
  renderFocusGlobalMarkets(data.global_markets || {{}});
  const items = data.items || [];
  document.getElementById("focusStockCards").innerHTML = items.length ? items.map(item => {{
    const pred = item.prediction || {{}};
    const rec = item.recommendation || {{}};
    const latest = item.latest_price || {{}};
    const cluster = item.cluster || {{}};
    const adj = item.global_adjustment || {{}};
    const score = item.display_score ?? rec.final_score ?? pred.pred_prob_top20;
    const adjusted = adj.regime_adjusted_score;
    const reasons = adj.global_reasons || [];
    return `<article class="panel demo-card">
      <h2><span>${{escapeHtml(item.name || item.symbol)}}</span><small class="muted">${{escapeHtml(item.symbol)}}</small></h2>
      <section class="demo-metrics">
        ${{metric("최근 종가", money(latest.close), shortDate(latest.date))}}
        ${{metric("상승 확률", pct(pred.pred_prob_top20), pred.model_version || "예측 미생성")}}
        ${{metric("원 예측/추천 점수", num(score), item.score_source || "-")}}
        ${{metric("글로벌 보정 점수", adjusted === null || adjusted === undefined ? "보정 대기" : num(adjusted), adj.message || "-")}}
      </section>
      <div class="row"><span>위험 레짐</span><b>${{escapeHtml(adj.regime || "수집 대기")}}</b></div>
      <div class="row"><span>글로벌 패널티</span><b>${{pct(adj.global_risk_penalty)}}</b></div>
      <div class="row"><span>권장 현금비중</span><b>${{pct(adj.cash_ratio)}}</b></div>
      <div class="row"><span>종목 weight cap</span><b>${{pct(adj.suggested_weight_cap)}}</b></div>
      <div class="row"><span>클러스터</span><b>${{escapeHtml(cluster?.cluster_label || "클러스터 미생성")}}</b></div>
      <div><b>민감 입력</b><p class="muted">${{(item.global_sensitivity || []).join(" · ") || "미지정"}}</p></div>
      <div><b>위험 원인</b><ul class="reason-list">${{reasons.length ? reasons.slice(0, 5).map(reason => `<li>${{escapeHtml(reason)}}</li>`).join("") : "<li>글로벌 데이터 수집 대기</li>"}}</ul></div>
      <div class="demo-actions">
        <a href="/stock/${{encodeURIComponent(item.symbol)}}">상세 보기</a>
        <a href="/api/demo/focus-stocks?horizon=${{encodeURIComponent(data.horizon)}}">데모 API</a>
      </div>
    </article>`;
  }}).join("") : '<article class="panel empty">포커스 종목 데이터가 없습니다.</article>';

  const rows = items.flatMap(item => (item.history || []).slice(0, 8).map(row => ({{
    ...row,
    name: item.name || item.symbol
  }})));
  document.getElementById("focusStockHistory").innerHTML = table(rows, [
    {{key:"name", label:"종목"}},
    {{key:"prediction_date", label:"예측일", format:shortDate}},
    {{key:"horizon", label:"기간"}},
    {{key:"model_name", label:"모델"}},
    {{key:"predicted_probability", label:"확률", format:pct}},
    {{key:"predicted_return", label:"예상수익", format:pct}},
    {{key:"rank_no", label:"순위"}}
  ]);
  document.getElementById("focusStockDisclaimer").textContent = data.disclaimer || "";
}}
function renderRegimeDetail(regime) {{
  if (!regime || regime.status !== "ready") {{
    return `
      <div class="row"><span>상태</span><b>수집 대기</b></div>
      <p class="muted">${{escapeHtml(regime?.message || "글로벌 레짐이 아직 없습니다.")}}</p>
    `;
  }}
  const reasons = regime.reasons || [];
  const signals = regime.signals || {{}};
  const scoreRows = [
    ["US Equity", regime.us_equity_score],
    ["Semiconductor", regime.semiconductor_score],
    ["Futures", regime.futures_score],
    ["Volatility", regime.volatility_score],
    ["Rates", regime.rate_score],
    ["FX", regime.fx_score],
    ["Asia", regime.asia_score],
    ["Commodity", regime.commodity_score],
  ].filter(([, value]) => value !== null && value !== undefined);
  const signalRows = Object.entries(signals).map(([key, value]) => ({{
    key,
    value,
    label: key.includes("return") ? pct(value) : num(value),
  }}));
  return `
    <div class="row"><span>Regime</span><b>${{escapeHtml(regime.regime || "-")}}</b></div>
    <div class="row"><span>글로벌 위험도</span><b>${{num(regime.global_risk_score)}}</b></div>
    <div class="row"><span>권장 현금비중</span><b>${{pct(regime.recommended_cash_ratio)}}</b></div>
    <div class="row"><span>기준일</span><b>${{shortDate(regime.prediction_date)}}</b></div>
    ${{scoreRows.length ? `
      <div class="mini-card">
        <h3>위험 점수 구성</h3>
        ${{scoreRows.map(([label, value]) => `<div class="row"><span>${{escapeHtml(label)}}</span><b>${{num(value)}}</b></div>`).join("")}}
      </div>
    ` : ""}}
    ${{signalRows.length ? `
      <div class="mini-card">
        <h3>핵심 신호</h3>
        ${{signalRows.slice(0, 8).map(item => `<div class="row"><span>${{escapeHtml(item.key)}}</span><b>${{item.label}}</b></div>`).join("")}}
      </div>
    ` : ""}}
    <ul class="reason-list">${{reasons.length ? reasons.slice(0, 6).map(reason => `<li>${{escapeHtml(reason)}}</li>`).join("") : "<li>원인 태그 없음</li>"}}</ul>
  `;
}}
function renderFocusRegime(regime) {{
  if (!regime || regime.status !== "ready") {{
    document.getElementById("focusRegime").innerHTML = `
      <div class="row"><span>상태</span><b>데이터 수집 대기</b></div>
      <p class="muted">${{escapeHtml(regime?.message || "장전 글로벌 레짐이 아직 생성되지 않았습니다.")}}</p>
      <p class="muted">후속 수집 파이프라인 실행 후 미국장·VIX·금리·환율 신호가 반영됩니다.</p>
    `;
    return;
  }}
  document.getElementById("focusRegime").innerHTML = renderRegimeDetail(regime);
}}
function renderFocusGlobalMarkets(globalMarkets) {{
  const items = globalMarkets.items || [];
  document.getElementById("focusGlobalMarkets").innerHTML = table(items.slice(0, 12), [
    {{key:"display_name", label:"지표"}},
    {{key:"symbol", label:"심볼"}},
    {{key:"trade_date", label:"일자", format:shortDate}},
    {{key:"close", label:"종가", format:money}},
    {{key:"return_1d", label:"1D", format:pct}},
    {{key:"source_name", label:"소스"}}
  ]);
}}
async function bootFourStocksDemo() {{
  const horizon = document.querySelector(".segmented button.active")?.dataset.horizon || "3M";
  try {{
    const data = await json("/api/demo/four-stocks?horizon=" + encodeURIComponent(horizon));
    renderFourStocksDemo(data);
  }} catch (_) {{
    document.getElementById("fourStockCards").innerHTML = '<article class="panel empty">네 종목 예측 데이터를 불러오지 못했습니다.</article>';
    document.getElementById("fourStockRegime").innerHTML = errorState("글로벌 레짐");
    document.getElementById("fourStockGapSummary").innerHTML = errorState("가격 괴리");
    document.getElementById("fourStockHistory").innerHTML = errorState("예측 이력");
  }}
}}
function selectFourDemoHorizon(button) {{
  document.querySelectorAll(".segmented button").forEach(item => item.classList.remove("active"));
  button.classList.add("active");
  bootFourStocksDemo();
}}
function renderFourStocksDemo(data) {{
  renderFourStockRegime(data.regime || {{}});
  renderFourStockGapSummary(data.price_gap_summary || {{}});
  const items = data.items || [];
  document.getElementById("fourStockCards").innerHTML = items.length ? items.map(item => {{
    const pred = item.prediction || {{}};
    const rec = item.recommendation || {{}};
    const latest = item.latest_price || {{}};
    const cluster = item.cluster || {{}};
    const adj = item.global_adjustment || {{}};
    const gap = item.price_gap || {{}};
    const score = item.display_score ?? rec.final_score ?? pred.pred_prob_top20;
    const adjusted = adj.regime_adjusted_score;
    const peers = item.peers || [];
    return `<article class="panel demo-card">
      <h2><span>${{escapeHtml(item.name || item.symbol)}}</span><small class="muted">${{escapeHtml(item.symbol)}}</small></h2>
      <section class="demo-metrics">
        ${{metric("최근 종가", money(latest.close), shortDate(latest.date))}}
        ${{metric("상승 확률", pct(pred.pred_prob_top20), pred.model_version || "예측 미생성")}}
        ${{metric("예측수익률", pct(pred.pred_return), data.horizon || "-")}}
        ${{metric("점수", num(score), item.score_source || "-")}}
      </section>
      <div class="row"><span>추천 상태</span><b>${{escapeHtml(item.top20_status || (item.is_top20 ? "Top20 포함" : "Top20 밖 / 예측값 있음"))}}</b></div>
      <div class="row"><span>리스크</span><b>${{pct(rec.risk_score ?? pred.pred_risk)}}</b></div>
      <div class="row"><span>글로벌 보정</span><b>${{adjusted === null || adjusted === undefined ? "보정 대기" : num(adjusted)}}</b></div>
      <div class="row"><span>30일 괴리 상태</span><b>${{escapeHtml(gap.status || "검증 대기")}}</b></div>
      <div class="row"><span>현재 실제수익</span><b>${{pct(gap.actual_return_latest)}}</b></div>
      <div class="row"><span>현재 괴리</span><b>${{pct(gap.return_gap_latest)}}</b></div>
      <div class="row"><span>클러스터</span><b>${{escapeHtml(cluster?.cluster_label || "클러스터 미생성")}}</b></div>
      <div><b>유사 종목</b><p class="muted">${{peers.length ? peers.slice(0, 4).map(peer => `${{peer.name || peer.symbol}}`).join(" · ") : "유사 종목 데이터가 없습니다."}}</p></div>
      <div class="demo-actions">
        <a href="/stock/${{encodeURIComponent(item.symbol)}}">상세 보기</a>
        <a href="/api/demo/four-stocks?horizon=${{encodeURIComponent(data.horizon)}}">데모 API</a>
      </div>
    </article>`;
  }}).join("") : '<article class="panel empty">네 종목 예측 데이터가 없습니다.</article>';

  const rows = items.flatMap(item => (item.history || []).slice(0, 8).map(row => ({{
    ...row,
    name: item.name || item.symbol
  }})));
  document.getElementById("fourStockHistory").innerHTML = table(rows, [
    {{key:"name", label:"종목"}},
    {{key:"prediction_date", label:"예측일", format:shortDate}},
    {{key:"horizon", label:"기간"}},
    {{key:"model_name", label:"모델"}},
    {{key:"predicted_probability", label:"확률", format:pct}},
    {{key:"predicted_return", label:"예측수익", format:pct}},
    {{key:"rank_no", label:"순위"}}
  ]);
  document.getElementById("fourStockDisclaimer").textContent = data.disclaimer || "";
}}
function renderFourStockRegime(regime) {{
  document.getElementById("fourStockRegime").innerHTML = renderRegimeDetail(regime);
}}
function renderFourStockGapSummary(summary) {{
  document.getElementById("fourStockGapSummary").innerHTML = [
    metric("표본", summary.sample_count ?? 0, `완료 ${{summary.completed_count ?? 0}} · 대기 ${{summary.pending_count ?? 0}}`),
    metric("Latest MAE", pct(summary.mae_latest), "현재까지 괴리"),
    metric("Latest Bias", pct(summary.bias_latest), "양수면 과소예측"),
    metric("방향 적중", pct(summary.direction_accuracy_latest), "현재까지 방향")
  ].join("");
}}
async function bootTop50Universe() {{
  const horizon = document.querySelector(".segmented button.active")?.dataset.horizon || "3M";
  try {{
    const data = await json("/api/universe/top50?horizon=" + encodeURIComponent(horizon));
    renderTop50Universe(data);
  }} catch (_) {{
    document.getElementById("top50Metrics").innerHTML = errorState("Top50 유니버스");
    document.getElementById("top50Table").innerHTML = errorState("Top50 유니버스");
  }}
}}
function selectTop50Horizon(button) {{
  document.querySelectorAll(".segmented button").forEach(item => item.classList.remove("active"));
  button.classList.add("active");
  bootTop50Universe();
}}
function renderTop50Universe(data) {{
  const summary = data.summary || {{}};
  document.getElementById("top50Metrics").innerHTML = [
    metric("스냅샷", shortDate(data.snapshot_date), data.status || "-"),
    metric("KOSPI", summary.kospi ?? 0, "목표 30"),
    metric("KOSDAQ", summary.kosdaq ?? 0, "목표 20"),
    metric("예측 생성", summary.with_prediction ?? 0, `${{summary.with_price ?? 0}}개 가격 수집`)
  ].join("");
  document.getElementById("top50Table").innerHTML = table(data.items || [], [
    {{key:"prediction_rank", label:"#"}},
    {{key:"name", label:"종목", format:(value, row) => `<a class="peer-link" href="/stock/${{encodeURIComponent(row.symbol)}}">${{escapeHtml(value || row.symbol)}}</a>`}},
    {{key:"symbol", label:"코드"}},
    {{key:"market", label:"시장"}},
    {{key:"sector", label:"섹터"}},
    {{key:"market_cap", label:"시가총액", format:money}},
    {{key:"close", label:"종가", format:money}},
    {{key:"price_date", label:"가격일", format:shortDate}},
    {{key:"pred_prob_top20", label:"상승확률", format:pct}},
    {{key:"pred_return", label:"예측수익", format:pct}},
    {{key:"recommendation_rank", label:"추천순위"}},
    {{key:"refresh_status", label:"상태"}}
  ]);
  document.getElementById("top50Disclaimer").textContent = data.disclaimer || "";
}}
async function bootLongShort() {{
  const horizon = document.querySelector(".segmented button.active")?.dataset.horizon || "2M";
  try {{
    const data = await json("/api/long-short/latest?horizon=" + encodeURIComponent(horizon));
    renderLongShort(data);
  }} catch (_) {{
    document.getElementById("longShortMetrics").innerHTML = errorState("롱·숏 추천");
    ["longShortKospiLong", "longShortKospiShort", "longShortKosdaqLong", "longShortKosdaqShort"].forEach(id => {{
      document.getElementById(id).innerHTML = errorState("롱·숏");
    }});
  }}
}}
function selectLongShortHorizon(button) {{
  document.querySelectorAll(".segmented button").forEach(item => item.classList.remove("active"));
  button.classList.add("active");
  bootLongShort();
}}
function renderLongShort(data) {{
  const markets = data.markets || {{}};
  const kospi = markets.KOSPI || {{ long_leg: [], short_leg: [] }};
  const kosdaq = markets.KOSDAQ || {{ long_leg: [], short_leg: [] }};
  const kospiLong = kospi.long_leg || [];
  const kospiShort = kospi.short_leg || [];
  const kosdaqLong = kosdaq.long_leg || [];
  const kosdaqShort = kosdaq.short_leg || [];
  document.getElementById("longShortMetrics").innerHTML = [
    metric("기준일", shortDate(data.asof_date), data.horizon || "-"),
    metric("KOSPI LONG", kospiLong.length, "목표 6"),
    metric("KOSPI SHORT", kospiShort.length, "목표 6 · 모의"),
    metric("KOSDAQ LONG", kosdaqLong.length, "목표 4"),
    metric("KOSDAQ SHORT", kosdaqShort.length, "목표 4 · 모의")
  ].join("");
  document.getElementById("longShortKospiLong").innerHTML = _longShortTable(kospiLong, "long_score", "KOSPI LONG");
  document.getElementById("longShortKospiShort").innerHTML = _longShortTable(kospiShort, "short_score", "KOSPI SHORT");
  document.getElementById("longShortKosdaqLong").innerHTML = _longShortTable(kosdaqLong, "long_score", "KOSDAQ LONG");
  document.getElementById("longShortKosdaqShort").innerHTML = _longShortTable(kosdaqShort, "short_score", "KOSDAQ SHORT");
  document.getElementById("longShortDisclaimer").textContent = data.disclaimer || "";
}}
function _longShortTable(items, scoreKey = "long_score", emptyLabel = "추천") {{
  if (!items || items.length === 0) {{
    return `<div class="empty">${{escapeHtml(emptyLabel)}} 추천이 아직 없습니다.</div>`;
  }}
  return table(items, [
    {{key:"rank", label:"#"}},
    {{key:"name", label:"종목", format:(value, row) => `<a class="peer-link" href="/stock/${{encodeURIComponent(row.symbol)}}">${{escapeHtml(value || row.symbol)}}</a>`}},
    {{key:"symbol", label:"코드"}},
    {{key:scoreKey, label:"Score", format:num}},
    {{key:"pred_return", label:"예측수익", format:pct}},
    {{key:"weight", label:"Weight", format:num}},
    {{key:"confidence", label:"신뢰도", format:pct}}
  ]);
}}
async function bootMarketUpDown() {{
  const horizon = document.querySelector(".segmented button.active")?.dataset.horizon || "2M";
  try {{
    const data = await json("/api/recommendations/up-down?horizon=" + encodeURIComponent(horizon));
    renderMarketUpDown(data);
  }} catch (_) {{
    document.getElementById("marketUpDownMetrics").innerHTML = errorState("상승·하락 추천");
    ["marketUpDownKospiUp", "marketUpDownKospiDown", "marketUpDownKosdaqUp", "marketUpDownKosdaqDown"].forEach(id => {{
      document.getElementById(id).innerHTML = errorState("상승·하락");
    }});
  }}
}}
function selectMarketUpDownHorizon(button) {{
  document.querySelectorAll(".segmented button").forEach(item => item.classList.remove("active"));
  button.classList.add("active");
  bootMarketUpDown();
}}
function renderMarketUpDown(data) {{
  const markets = data.markets || {{}};
  const kospi = markets.KOSPI || {{ upside: [], downside: [] }};
  const kosdaq = markets.KOSDAQ || {{ upside: [], downside: [] }};
  document.getElementById("marketUpDownMetrics").innerHTML = [
    metric("기준일", shortDate(data.asof_date), data.horizon || "-"),
    metric("KOSPI 상승", (kospi.upside || []).length, "목표 6"),
    metric("KOSPI 하락", (kospi.downside || []).length, "목표 6"),
    metric("KOSDAQ 상승", (kosdaq.upside || []).length, "목표 4"),
    metric("KOSDAQ 하락", (kosdaq.downside || []).length, "목표 4")
  ].join("");
  document.getElementById("marketUpDownKospiUp").innerHTML = _marketUpDownTable(kospi.upside || [], "long_score", "KOSPI 상승");
  document.getElementById("marketUpDownKospiDown").innerHTML = _marketUpDownTable(kospi.downside || [], "short_score", "KOSPI 하락");
  document.getElementById("marketUpDownKosdaqUp").innerHTML = _marketUpDownTable(kosdaq.upside || [], "long_score", "KOSDAQ 상승");
  document.getElementById("marketUpDownKosdaqDown").innerHTML = _marketUpDownTable(kosdaq.downside || [], "short_score", "KOSDAQ 하락");
  document.getElementById("marketUpDownDisclaimer").textContent = data.disclaimer || "";
}}
function _marketUpDownTable(items, scoreKey = "long_score", emptyLabel = "추천") {{
  if (!items || items.length === 0) {{
    return `<div class="empty">${{escapeHtml(emptyLabel)}} 추천이 아직 없습니다.</div>`;
  }}
  return table(items, [
    {{key:"rank", label:"#"}},
    {{key:"name", label:"종목", format:(value, row) => `<a class="peer-link" href="/stock/${{encodeURIComponent(row.symbol)}}">${{escapeHtml(value || row.symbol)}}</a>`}},
    {{key:"symbol", label:"코드"}},
    {{key:scoreKey, label:"Score", format:num}},
    {{key:"pred_return", label:"예측수익", format:pct}},
    {{key:"pred_prob_bottom20", label:"하락확률", format:pct}},
    {{key:"confidence", label:"신뢰도", format:pct}},
    {{key:"risk_flags", label:"리스크", format:(value) => escapeHtml(Array.isArray(value) ? value.join(", ") : (value || "-"))}}
  ]);
}}
async function bootTop20Upside() {{
  const horizon = document.querySelector(".segmented button.active")?.dataset.horizon || "3M";
  try {{
    const [data, forecast] = await Promise.all([
      json("/api/recommendations/top20-upside?horizon=" + encodeURIComponent(horizon) + "&limit=20"),
      json("/api/recommendations/top20-price-forecast?base_horizon=" + encodeURIComponent(horizon) + "&horizons=3M,6M,9M,1Y&limit=20")
    ]);
    renderTop20Upside(data);
    renderTop20PriceForecast(forecast);
  }} catch (_) {{
    document.getElementById("top20UpsideMetrics").innerHTML = errorState("Top20 추천");
    document.getElementById("top20UpsideTable").innerHTML = errorState("Top20 추천");
    document.getElementById("top20PriceForecastTable").innerHTML = errorState("예상 가격");
  }}
}}
function selectTop20UpsideHorizon(button) {{
  document.querySelectorAll(".segmented button").forEach(item => item.classList.remove("active"));
  button.classList.add("active");
  bootTop20Upside();
}}
function renderTop20Upside(data) {{
  const summary = data.summary || {{}};
  document.getElementById("top20UpsideMetrics").innerHTML = [
    metric("기준일", shortDate(data.asof_date), data.horizon || "3M"),
    metric("Top20 수", summary.count ?? 0, "추천 종목"),
    metric("평균 상승확률", pct(summary.average_up_probability), (data.horizon || "3M") + " 모델 확률"),
    metric("평균 상승여력", pct(summary.average_upside_return), "예측수익률")
  ].join("");
  document.getElementById("top20UpsideTable").innerHTML = table(data.items || [], [
    {{key:"rank", label:"#"}},
    {{key:"name", label:"종목", format:(value, row) => `<a class="peer-link" href="/stock/${{encodeURIComponent(row.symbol)}}">${{escapeHtml(value || row.symbol)}}</a>`}},
    {{key:"market", label:"시장"}},
    {{key:"sector", label:"섹터"}},
    {{key:"up_probability", label:"상승확률", format:pct}},
    {{key:"upside_return", label:"상승여력", format:pct}},
    {{key:"combined_score", label:"종합점수", format:num}},
    {{key:"risk_score", label:"리스크", format:pct}},
    {{key:"model_version", label:"모델"}}
  ]);
  document.getElementById("top20UpsideDisclaimer").textContent = data.disclaimer || "";
}}
function renderTop20PriceForecast(data) {{
  const rows = [];
  (data.items || []).forEach(item => {{
    (item.forecasts || []).forEach(forecast => {{
      rows.push({{
        rank: item.rank,
        symbol: item.symbol,
        name: item.name,
        market: item.market,
        forecast_horizon: forecast.horizon,
        current_price: item.current_price,
        expected_return: forecast.expected_return,
        expected_price: forecast.expected_price,
        upside_price: forecast.upside_price,
        downside_price: forecast.downside_price,
        up_probability: forecast.up_probability,
        confidence: forecast.confidence,
        status: forecast.status
      }});
    }});
  }});
  document.getElementById("top20PriceForecastTable").innerHTML = table(rows, [
    {{key:"rank", label:"#"}},
    {{key:"name", label:"종목", format:(value, row) => `<a class="peer-link" href="/stock/${{encodeURIComponent(row.symbol)}}">${{escapeHtml(value || row.symbol)}}</a>`}},
    {{key:"forecast_horizon", label:"기간"}},
    {{key:"current_price", label:"현재가", format:money}},
    {{key:"expected_return", label:"예상수익", format:pct}},
    {{key:"expected_price", label:"예상가", format:money}},
    {{key:"upside_price", label:"상단가", format:money}},
    {{key:"downside_price", label:"하단가", format:money}},
    {{key:"up_probability", label:"상승확률", format:pct}},
    {{key:"confidence", label:"신뢰도", format:pct}},
    {{key:"status", label:"상태", format:escapeHtml}}
  ]);
  const note = data.disclaimer || "";
  if (note) document.getElementById("top20UpsideDisclaimer").textContent = note;
}}
async function bootTodayMarket() {{
  try {{
    const data = await json("/api/today/update-snapshot");
    renderTodayMarket(data);
  }} catch (_) {{
    document.getElementById("todayStatus").innerHTML = metric("업데이트", "오류", "API 응답을 확인하세요");
    document.getElementById("todayFocusPrices").innerHTML = errorState("국내 가격");
    document.getElementById("todayYahooPrices").innerHTML = errorState("Yahoo 가격");
    document.getElementById("todayMarketOutlook").innerHTML = errorState("시장 전망");
    document.getElementById("todayMoveExplanations").innerHTML = errorState("원인 분석");
    document.getElementById("todayKoru").innerHTML = errorState("KORU linkage");
    document.getElementById("todaySectorLinkage").innerHTML = errorState("미국 유사섹터");
    document.getElementById("todayRegime").innerHTML = errorState("글로벌 레짐");
    document.getElementById("todayGlobalMarkets").innerHTML = errorState("해외시장");
    document.getElementById("todayNews").innerHTML = errorState("뉴스");
  }}
}}
async function bootTomorrowMarket() {{
  try {{
    const data = await json("/api/tomorrow/update-snapshot");
    renderTomorrowMarket(data);
  }} catch (_) {{
    document.getElementById("todayStatus").innerHTML = metric("업데이트", "오류", "API 응답을 확인하세요");
    document.getElementById("todayFocusPrices").innerHTML = errorState("국내 가격");
    document.getElementById("todayYahooPrices").innerHTML = errorState("Yahoo 가격");
    document.getElementById("todayMarketOutlook").innerHTML = errorState("다음 거래일 전망");
    document.getElementById("tomorrowLongShortRange").innerHTML = errorState("숏·롱 범위");
    document.getElementById("todayMoveExplanations").innerHTML = errorState("원인 분석");
    document.getElementById("todayKoru").innerHTML = errorState("KORU linkage");
    document.getElementById("todaySectorLinkage").innerHTML = errorState("미국 유사섹터");
    document.getElementById("todayRegime").innerHTML = errorState("글로벌 레짐");
    document.getElementById("todayGlobalMarkets").innerHTML = errorState("해외시장");
    document.getElementById("todayNews").innerHTML = errorState("뉴스");
  }}
}}
function renderTodayMarket(data) {{
  const quality = data.data_quality || {{}};
  const statusCards = todayStatusCards(data);
  document.getElementById("todayStatus").innerHTML = [
    metric("스냅샷 상태", statusCards.snapshot.value, statusCards.snapshot.sub),
    metric("국내 가격", statusCards.domestic.value, statusCards.domestic.sub),
    metric("해외 레짐", statusCards.regime.value, statusCards.regime.sub),
    metric("뉴스", statusCards.news.value, statusCards.news.sub)
  ].join("");
  renderTodayMarketOutlook(data.market_outlook || {{}});
  renderTodayMoveExplanations(data.move_explanations || {{}});
  renderTodayKoru(data.koru_linkage || {{}});
  renderTodaySectorLinkage(data.sector_linkage || {{}});
  document.getElementById("todayFocusPrices").innerHTML = table(data.focus_prices || [], [
    {{key:"name", label:"종목"}},
    {{key:"symbol", label:"코드", format:(value) => `<a class="peer-link" href="/stock/${{encodeURIComponent(value)}}">${{escapeHtml(value)}}</a>`}},
    {{key:"date", label:"일자", format:shortDate}},
    {{key:"close", label:"종가", format:money}},
    {{key:"volume", label:"거래량", format:money}},
    {{key:"source", label:"소스"}},
    {{key:"status", label:"상태"}}
  ]);
  const yahooItems = (data.yahoo_prices || []).slice(0, 14);
  const yahooStatus = quality.components?.yahoo_prices;
  const yahooNotice = yahooStatus && yahooStatus !== "ready"
    ? `<div class="empty">${{escapeHtml((quality.messages || []).find(message => String(message).includes("Yahoo")) || componentValue(yahooStatus))}}</div>`
    : "";
  document.getElementById("todayYahooPrices").innerHTML = yahooItems.length
    ? yahooNotice + table(yahooItems, [
        {{key:"yahoo_symbol", label:"Yahoo"}},
        {{key:"symbol", label:"내부코드"}},
        {{key:"asset_type", label:"구분"}},
        {{key:"date", label:"일자", format:shortDate}},
        {{key:"close", label:"종가", format:money}},
        {{key:"currency", label:"통화"}},
        {{key:"source", label:"소스"}}
      ])
    : `<div class="empty">${{escapeHtml((quality.messages || []).find(message => String(message).includes("Yahoo")) || "Yahoo/yfinance 데이터가 없습니다. ALLOW_UNOFFICIAL_YAHOO=true 설정 후 업데이트를 실행하세요.")}}</div>`;
  document.getElementById("todayRegime").innerHTML = renderRegimeDetail(data.global_regime || {{}});
  renderTodayGlobalMarkets(data.global_markets || {{}});
  renderTodayNews(data.news || [], data.macro_news || [], data.market_context || [], quality);
  document.getElementById("todayDisclaimer").textContent = data.disclaimer || "";
}}
function renderTomorrowMarket(data) {{
  renderTodayMarket(data);
  renderTomorrowMarketOutlook(data.market_outlook || {{}});
  renderTomorrowLongShortRange(data.long_short_range || {{}});
}}
function todayStatusCards(data) {{
  const quality = data.data_quality || {{}};
  const components = quality.components || {{}};
  const freshness = quality.freshness || {{}};
  const focusPrices = data.focus_prices || [];
  const readyFocus = focusPrices.filter(item => item.status === "ready");
  const latestPriceDate = shortDate(freshness.latest_date) !== "-" ? shortDate(freshness.latest_date) : latestShortDate(readyFocus);
  const expectedDate = shortDate(freshness.expected_latest_date);
  const snapshotStatus = `${{statusLabel(data.status)}}${{freshness.stale ? " · 지연" : ""}}`;
  const snapshotSub = compactText([
    `스냅샷 ${{shortDate(data.snapshot_date)}}`,
    latestPriceDate !== "-" ? `가격 ${{latestPriceDate}} 최신` : null,
    expectedDate !== "-" ? `기대 ${{expectedDate}}` : null
  ]);

  const sourceText = uniqueText(readyFocus.map(item => item.source), 2).join(", ");
  const indexDate = shortDate(data.move_explanations?.asof_date || data.koru_linkage?.asof_date);
  const domesticValue = components.domestic_prices === "ready"
    ? `정상 · ${{count(readyFocus.length)}}/${{count(focusPrices.length)}} 종목`
    : componentValue(components.domestic_prices);
  const domesticSub = compactText([
    latestPriceDate !== "-" ? `가격일 ${{latestPriceDate}}` : null,
    sourceText ? `소스 ${{sourceText}}` : null,
    indexDate !== "-" ? `지수 ${{indexDate}} 기준` : null
  ]);

  const regime = data.global_regime || {{}};
  const regimeReasons = Array.isArray(regime.reasons) ? regime.reasons.slice(0, 2).join(" · ") : "";
  const regimeValue = components.market_regime === "ready"
    ? `${{escapeHtml(regime.regime || "레짐")}} · 위험 ${{num1(regime.global_risk_score)}}`
    : componentValue(components.market_regime);
  const regimeSub = compactText([
    shortDate(regime.prediction_date) !== "-" ? `기준 ${{shortDate(regime.prediction_date)}}` : null,
    regime.recommended_cash_ratio !== null && regime.recommended_cash_ratio !== undefined ? `현금 ${{pct(regime.recommended_cash_ratio)}}` : null,
    regimeReasons
  ]);

  const stockNewsCount = (data.news || []).length;
  const macroNewsCount = (data.macro_news || []).length;
  const telegramComponent = data.move_explanations?.data_quality?.components?.telegram_news;
  const telegramText = telegramComponent === "ready" ? "Telegram 반영" : "Telegram 미수집";
  const newsValue = components.news === "ready"
    ? (stockNewsCount ? `종목뉴스 ${{count(stockNewsCount)}}건` : `거시뉴스 ${{count(macroNewsCount)}}건`)
    : componentValue(components.news);
  const newsSub = compactText([
    macroNewsCount ? `거시 ${{count(macroNewsCount)}}건` : null,
    stockNewsCount ? `Naver ${{count(stockNewsCount)}}건` : "종목뉴스 없음",
    telegramText,
    (quality.messages || []).find(message => String(message).includes("뉴스") || String(message).includes("수급"))
  ]);

  return {{
    snapshot: {{ value: escapeHtml(snapshotStatus), sub: escapeHtml(snapshotSub) }},
    domestic: {{ value: escapeHtml(domesticValue), sub: escapeHtml(domesticSub) }},
    regime: {{ value: regimeValue, sub: escapeHtml(regimeSub) }},
    news: {{ value: escapeHtml(newsValue), sub: escapeHtml(newsSub) }}
  }};
}}
function renderTodayMarketOutlook(payload) {{
  const items = payload.items || [];
  const quality = payload.data_quality || {{}};
  const messages = (quality.messages || []).slice(0, 3);
  const status = payload.status || "not_collected";
  const ordered = ["TODAY:KOSPI", "TODAY:KOSDAQ", "WEEK:KOSPI", "WEEK:KOSDAQ"];
  const byKey = Object.fromEntries(items.map(item => [`${{item.horizon}}:${{item.market}}`, item]));
  const cards = ordered
    .filter(key => byKey[key])
    .map(key => outlookCard(byKey[key]))
    .join("");
  const messageHtml = messages.length
    ? `<p class="muted">${{messages.map(escapeHtml).join(" · ")}}</p>`
    : "";
  document.getElementById("todayMarketOutlook").innerHTML = `
    <div class="inline"><span class="chip">${{escapeHtml(statusLabel(status))}}</span><span>기준 ${{shortDate(payload.asof_date)}}</span><span>정보제공용 전망</span></div>
    ${{cards ? `<div class="card-grid">${{cards}}</div>` : '<div class="empty">아직 생성된 시장 전망이 없습니다.</div>'}}
    ${{messageHtml}}
  `;
}}
function renderTomorrowMarketOutlook(payload) {{
  const items = payload.items || [];
  const quality = payload.data_quality || {{}};
  const messages = (quality.messages || []).slice(0, 3);
  const status = payload.status || "not_collected";
  const multiplier = payload.range_multiplier || 1.25;
  const ordered = ["NEXT_TRADING_DAY:KOSPI", "NEXT_TRADING_DAY:KOSDAQ"];
  const byKey = Object.fromEntries(items.map(item => [`${{item.horizon}}:${{item.market}}`, item]));
  const cards = ordered
    .filter(key => byKey[key])
    .map(key => outlookCard(byKey[key]))
    .join("");
  const target = payload.target_date || (items[0] || {{}}).target_date;
  const messageHtml = messages.length
    ? `<p class="muted">${{messages.map(escapeHtml).join(" · ")}}</p>`
    : "";
  document.getElementById("todayMarketOutlook").innerHTML = `
    <div class="inline"><span class="chip">${{escapeHtml(statusLabel(status))}}</span><span>기준 ${{shortDate(payload.asof_date)}}</span><span>다음 거래일 ${{shortDate(target)}}</span><span>보수 범위 ${{Number(multiplier).toFixed(2)}}배</span></div>
    ${{cards ? `<div class="card-grid">${{cards}}</div>` : '<div class="empty">아직 생성된 다음 거래일 전망이 없습니다.</div>'}}
    ${{messageHtml}}
  `;
}}
function renderTomorrowLongShortRange(payload) {{
  const items = payload.items || [];
  const quality = payload.data_quality || {{}};
  const message = (quality.messages || [])[0];
  const notice = message ? `<div class="empty">${{escapeHtml(message)}}</div>` : "";
  if (!items.length) {{
    document.getElementById("tomorrowLongShortRange").innerHTML = notice || '<div class="empty">아직 생성된 숏·롱 범위가 없습니다.</div>';
    return;
  }}
  const rows = items.map(item => ({{
    market: item.market,
    long_range: `${{pct(item.long_low)}} ~ ${{pct(item.long_high)}}`,
    short_range: `${{pct(item.short_low)}} ~ ${{pct(item.short_high)}}`,
    credit_1d: item.credit_delta_1d_pct,
    credit_5d: item.credit_delta_5d_pct,
    credit_pressure: item.credit_pressure_score,
    shock: item.shock_probability,
    confidence: item.confidence,
    basis: compactText([
      item.credit_balance_date ? `신용잔고 ${{shortDate(item.credit_balance_date)}}` : "신용잔고 결측",
      item.credit_source || null
    ])
  }}));
  document.getElementById("tomorrowLongShortRange").innerHTML = `
    <div class="inline"><span class="chip">${{escapeHtml(statusLabel(payload.status || "partial_ready"))}}</span><span>기준 ${{shortDate(payload.asof_date)}}</span><span>target ${{shortDate(payload.target_date)}}</span></div>
    ${{notice}}
    ${{table(rows, [
      {{key:"market", label:"시장"}},
      {{key:"long_range", label:"LONG 범위"}},
      {{key:"short_range", label:"SHORT 범위"}},
      {{key:"credit_1d", label:"신용 1D", format:pct}},
      {{key:"credit_5d", label:"신용 5D", format:pct}},
      {{key:"credit_pressure", label:"Credit pressure", format:pct}},
      {{key:"shock", label:"Shock", format:pct}},
      {{key:"confidence", label:"신뢰도", format:pct}},
      {{key:"basis", label:"근거", format:escapeHtml}}
    ])}}
  `;
}}
function outlookCard(item) {{
  const directionClass = item.direction === "BEARISH" ? "risk" : "";
  const drivers = Array.isArray(item.drivers) ? item.drivers.slice(0, 5) : [];
  const driverHtml = drivers.length
    ? `<ul class="reason-list">${{drivers.map(driver => `<li><b>${{escapeHtml(driver.label || driver.kind || "-")}}</b>: ${{escapeHtml(outlookDriverText(driver))}}</li>`).join("")}}</ul>`
    : `<p class="muted">driver 데이터 부족</p>`;
  return `<article class="mini-card">
    <h3>${{escapeHtml(horizonLabel(item.horizon))}} · ${{escapeHtml(item.market || "-")}}</h3>
    <div class="row"><span>예상 등락률</span><b class="${{directionClass}}">${{pct(item.expected_return)}}</b></div>
    <div class="row"><span>예상 범위</span><b>${{pct(item.range_low)}} ~ ${{pct(item.range_high)}}</b></div>
    <div class="row"><span>상승확률</span><b>${{pct(item.up_probability)}}</b></div>
    <div class="row"><span>하락확률</span><b>${{pct(item.down_probability)}}</b></div>
    <div class="row"><span>-2% 충격확률</span><b>${{pct(item.shock_probability)}}</b></div>
    <div class="row"><span>방향</span><b>${{escapeHtml(directionLabel(item.direction))}}</b></div>
    <div class="row"><span>신뢰도</span><b>${{pct(item.confidence)}}</b></div>
    <div><b>주요 driver</b>${{driverHtml}}</div>
    <p class="muted">target ${{shortDate(item.target_date)}} · ${{escapeHtml(item.model_version || "-")}}</p>
  </article>`;
}}
function horizonLabel(value) {{
  const labels = {{ TODAY: "오늘", WEEK: "이번주", NEXT_TRADING_DAY: "다음 거래일" }};
  return labels[String(value || "").toUpperCase()] || String(value || "-");
}}
function outlookDriverText(driver) {{
  const value = driver?.value || {{}};
  switch (driver?.kind) {{
    case "index_model":
      return compactText([
        value.index_expected_return !== undefined ? `지수모델 ${{pct(value.index_expected_return)}}` : null,
        value.index_return_1d !== undefined ? `1D ${{pct(value.index_return_1d)}}` : null,
        value.index_volatility_20d !== undefined ? `20D 변동성 ${{pct(value.index_volatility_20d)}}` : null
      ]);
    case "breadth":
      return compactText([
        value.breadth_expected_return !== undefined ? `breadth ${{pct(value.breadth_expected_return)}}` : null,
        value.top50_up_share_21d !== undefined ? `상승 breadth ${{pct(value.top50_up_share_21d)}}` : null,
        value.prediction_up_probability_avg !== undefined ? `2M 상승확률 평균 ${{pct(value.prediction_up_probability_avg)}}` : null
      ]);
    case "koru":
      return compactText([
        value.koru_return_1d !== undefined ? `KORU ${{pct(value.koru_return_1d)}}` : null,
        value.ewy_return_1d !== undefined ? `EWY ${{pct(value.ewy_return_1d)}}` : null,
        value.koru_ewy_spread_1d !== undefined ? `괴리 ${{pct(value.koru_ewy_spread_1d)}}` : null
      ]);
    case "regime":
      return compactText([
        value.global_risk_score !== undefined ? `위험 ${{num1(value.global_risk_score)}}` : null,
        value.recommended_cash_ratio !== undefined ? `현금 ${{pct(value.recommended_cash_ratio)}}` : null,
        value.semiconductor_score !== undefined ? `반도체 ${{num1(value.semiconductor_score)}}` : null
      ]);
    case "news_telegram":
      return compactText([
        value.news_count_24h !== undefined ? `뉴스 ${{count(value.news_count_24h)}}건` : null,
        value.news_sentiment_score !== undefined ? `뉴스심리 ${{num(value.news_sentiment_score)}}` : null,
        value.telegram_risk_score !== undefined ? `Telegram risk ${{num(value.telegram_risk_score)}}` : null
      ]);
    default:
      return driver?.summary || objectSummary(value);
  }}
}}
function renderTodayMoveExplanations(payload) {{
  const quality = payload.data_quality || {{}};
  const freshness = payload.freshness || {{}};
  const market = payload.market || [];
  const top50 = payload.top50 || [];
  const status = payload.status || "not_collected";
  const qualityMessages = (quality.messages || []).slice(0, 4);
  const staleHtml = freshness.stale ? `<span class="chip warn">최신 학습 미완료</span>` : "";
  const marketHtml = market.length
    ? `<div class="card-grid">${{market.map(item => moveCard(item, true)).join("")}}</div>`
    : `<div class="empty">시장 요약이 아직 없습니다.</div>`;
  const topHtml = top50.length
    ? `<div class="card-grid">${{top50.slice(0, 12).map(item => moveCard(item, false)).join("")}}</div>`
    : `<div class="empty">Top50 내 2% 이상 변동 종목이 없습니다.</div>`;
  const qualityHtml = qualityMessages.length
    ? `<p class="muted">${{qualityMessages.map(escapeHtml).join(" · ")}}</p>`
    : "";
  document.getElementById("todayMoveExplanations").innerHTML = `
    <div class="inline"><span class="chip">${{escapeHtml(status)}}</span>${{staleHtml}}<span>${{shortDate(payload.asof_date)}}</span><span>2% 트리거 ${{payload.summary?.triggered_count ?? 0}}건</span></div>
    ${{marketHtml}}
    ${{topHtml}}
    ${{qualityHtml}}
  `;
}}
function moveCard(item, compact) {{
  const evidence = Array.isArray(item.evidence) ? item.evidence : [];
  const prediction = item.prediction_context || {{}};
  const directionClass = item.direction === "DOWN" ? "risk" : "";
  const evidenceHtml = evidence.slice(0, 5).map(formatEvidence).join("");
  const predictionHtml = Object.keys(prediction).length
    ? `<div class="row"><span>모델 예측 방향</span><b>${{escapeHtml(directionLabel(prediction.side))}}</b></div>
       <div class="row"><span>${{escapeHtml(prediction.horizon || "-")}} 상승/하락 확률</span><b>${{escapeHtml(probabilityPair(prediction))}}</b></div>
       <div class="row"><span>모델 기준일</span><b>${{shortDate(prediction.asof_date)}}</b></div>
       <div class="row"><span>gate 상태</span><b>${{escapeHtml(prediction.gate_status || "검증 대기")}}</b></div>`
    : `<div class="row"><span>모델 예측 방향</span><b>데이터 부족</b></div>`;
  return `<article class="mini-card">
    <h3>${{escapeHtml(item.name || item.symbol)}} <span class="muted">${{escapeHtml(item.symbol || "")}}</span></h3>
    <div class="row"><span>변동률</span><b class="${{directionClass}}">${{pct(item.move_pct)}}</b></div>
    <div class="row"><span>방향</span><b>${{escapeHtml(item.direction || "-")}}</b></div>
    <p><b>실제 변동 원인</b><br>${{escapeHtml(item.primary_reason || "-")}}</p>
    <ul class="reason-list">${{evidenceHtml}}</ul>
    ${{predictionHtml}}
    <p class="muted">신뢰도 ${{pct(item.confidence)}}</p>
  </article>`;
}}
function formatEvidence(ev) {{
  const label = evidenceLabel(ev);
  const value = evidenceValue(ev);
  return `<li><b>${{escapeHtml(label)}}</b>${{value ? `: ${{escapeHtml(truncateText(value))}}` : ""}}</li>`;
}}
function evidenceLabel(ev) {{
  const labels = {{
    price: "1D 변동률",
    breadth: "Top50 변동 분포",
    market_index_trigger: "KOSPI/KOSDAQ -2% 시장충격",
    koru: "KORU 레버리지 심리",
    us_sector: "미국 유사섹터",
    prediction: "모델 전망",
    global_tech: "글로벌 기술주",
    regime: "글로벌 위험 레짐",
    fx: "환율 부담",
    market_structure: "시장 구조",
    macro_news: "거시 뉴스",
    data_quality: "데이터 품질"
  }};
  return labels[ev?.kind] || ev?.label || ev?.kind || "-";
}}
function evidenceValue(ev) {{
  const value = ev?.value;
  switch (ev?.kind) {{
    case "price":
      return pct(value);
    case "breadth":
      return `상승 ${{count(value?.up)}} · 하락 ${{count(value?.down)}}`;
    case "market_index_trigger":
      return formatMarketTrigger(value);
    case "koru":
      return compactText([
        value?.koru_return_1d !== undefined ? `KORU ${{pct(value.koru_return_1d)}}` : null,
        value?.ewy_return_1d !== undefined ? `EWY ${{pct(value.ewy_return_1d)}}` : null,
        value?.koru_ewy_spread_1d !== undefined ? `괴리 ${{pct(value.koru_ewy_spread_1d)}}` : null,
        value?.koru_impact_score !== undefined ? `impact ${{num(value.koru_impact_score)}}` : null
      ]);
    case "us_sector":
      return compactText([
        value?.primary_proxy ? `${{value.primary_proxy}}` : null,
        value?.us_sector_return_1d !== undefined ? `1D ${{pct(value.us_sector_return_1d)}}` : null,
        value?.us_sector_return_5d !== undefined ? `5D ${{pct(value.us_sector_return_5d)}}` : null,
        value?.us_sector_impact_score !== undefined ? `impact ${{pct(value.us_sector_impact_score)}}` : null
      ]);
    case "prediction":
      return formatPredictionSummary(value);
    case "global_tech":
      return Array.isArray(value)
        ? value.slice(0, 4).map(item => `${{item.symbol || "-"}} ${{pct(item.return_1d)}}`).join(" · ")
        : objectSummary(value);
    case "regime":
      return compactText([
        value?.risk_score !== undefined ? `위험점수 ${{num1(value.risk_score)}}` : null,
        Array.isArray(value?.reasons) ? value.reasons.slice(0, 2).join(" · ") : null
      ]);
    case "fx":
      return value?.keyword || objectSummary(value);
    case "data_quality":
      return Array.isArray(value) ? value.slice(0, 4).join(" · ") : objectSummary(value);
    default:
      if (typeof value === "number") return num(value);
      if (Array.isArray(value)) return value.slice(0, 4).join(" · ");
      if (typeof value === "object" && value !== null) return objectSummary(value);
      return value === undefined || value === null ? "" : String(value);
  }}
}}
function formatMarketTrigger(value) {{
  const markets = value?.markets || {{}};
  return compactText([
    markets.KOSPI ? `KOSPI ${{pct(markets.KOSPI.return_1d)}}` : null,
    markets.KOSDAQ ? `KOSDAQ ${{pct(markets.KOSDAQ.return_1d)}}` : null,
    value?.triggered ? "트리거 발생" : "미발생"
  ]);
}}
function formatPredictionSummary(value) {{
  if (!value || typeof value !== "object") return "데이터 부족";
  return compactText([
    value.horizon,
    value.side ? directionLabel(value.side) : null,
    value.pred_return !== null && value.pred_return !== undefined ? `예상 ${{pct(value.pred_return)}}` : null,
    probabilityPair(value)
  ]) || "데이터 부족";
}}
function probabilityPair(value) {{
  const hasTop = value?.pred_prob_top20 !== null && value?.pred_prob_top20 !== undefined && !Number.isNaN(Number(value.pred_prob_top20));
  const hasBottom = value?.pred_prob_bottom20 !== null && value?.pred_prob_bottom20 !== undefined && !Number.isNaN(Number(value.pred_prob_bottom20));
  return hasTop || hasBottom ? `${{pct(value.pred_prob_top20)}} / ${{pct(value.pred_prob_bottom20)}}` : "데이터 부족";
}}
function objectSummary(value) {{
  if (!value || typeof value !== "object") return "";
  return Object.entries(value)
    .slice(0, 4)
    .map(([key, item]) => `${{key}} ${{typeof item === "number" ? num(item) : String(item)}}`)
    .join(" · ");
}}
function renderTodayKoru(payload) {{
  const item = payload.item || {{}};
  const quality = item.data_quality || payload.data_quality || {{}};
  const trigger = item.market_index_trigger || {{}};
  const markets = trigger.markets || {{}};
  const weights = payload.weight_decisions || {{}};
  const messages = (quality.messages || []).slice(0, 3);
  const components = quality.components || {{}};
  const source = quality.signal_sources || {{}};
  const sourceText = source.koru === "intraday_snapshot_current_price"
    ? "현재가 snapshot"
    : (source.koru ? "전일 미국장 종가 fallback" : "데이터 부족");
  const rows = [
    ["KORU 1D", pct(item.koru_return_1d)],
    ["EWY 1D", pct(item.ewy_return_1d)],
    ["KORU-EWY 괴리", pct(item.koru_ewy_spread_1d)],
    ["KOSPI 1D", pct(item.kospi_return_1d ?? markets.KOSPI?.return_1d)],
    ["KOSDAQ 1D", pct(item.kosdaq_return_1d ?? markets.KOSDAQ?.return_1d)],
    ["시장충격", trigger.triggered ? "발생" : "미발생"],
    ["2M/3M weight", `${{num(weights["2M"] ?? 0)}} / ${{num(weights["3M"] ?? 0)}}`],
    ["기준", sourceText]
  ];
  const causeHtml = (item.causes || []).length
    ? `<ul class="reason-list">${{item.causes.slice(0, 4).map(cause => `<li>${{escapeHtml(cause.title || cause.type || "-")}}</li>`).join("")}}</ul>`
    : `<p class="muted">KORU 원인 후보가 아직 부족합니다.</p>`;
  const messageHtml = messages.length
    ? `<p class="muted">${{messages.map(escapeHtml).join(" · ")}}</p>`
    : "";
  document.getElementById("todayKoru").innerHTML = `
    <div class="inline"><span class="chip">${{escapeHtml(payload.status || "not_collected")}}</span><span>${{shortDate(payload.asof_date)}}</span><span>${{escapeHtml(components.koru || "missing")}}</span></div>
    <div class="card-grid">
      <article class="mini-card">
        ${{rows.map(([label, value]) => `<div class="row"><span>${{escapeHtml(label)}}</span><b>${{escapeHtml(value)}}</b></div>`).join("")}}
      </article>
      <article class="mini-card">
        <h3>원인 후보</h3>
        ${{causeHtml}}
        ${{messageHtml}}
      </article>
    </div>
    <p class="notice">${{escapeHtml(payload.leverage_warning || "KORU는 일간 3배 레버리지 ETF이며 장기 누적수익률은 단순 3배와 다를 수 있습니다.")}}</p>
  `;
}}
function renderTodaySectorLinkage(payload) {{
  const items = payload.items || [];
  const messages = (payload.data_quality?.messages || []).slice(0, 3);
  const rows = items.slice(0, 8);
  const cards = rows.length
    ? `<div class="card-grid">${{rows.map(sectorLinkageCard).join("")}}</div>`
    : `<div class="empty">미국 유사섹터 linkage가 아직 없습니다.</div>`;
  const messageHtml = messages.length ? `<p class="muted">${{messages.map(escapeHtml).join(" · ")}}</p>` : "";
  document.getElementById("todaySectorLinkage").innerHTML = `
    <div class="inline"><span class="chip">${{escapeHtml(statusLabel(payload.status || "not_collected"))}}</span><span>기준 ${{shortDate(payload.asof_date)}}</span></div>
    ${{cards}}
    ${{messageHtml}}
  `;
}}
function sectorLinkageCard(item) {{
  const quality = item.data_quality || {{}};
  return `<article class="mini-card">
    <h3>${{escapeHtml(sectorLabel(item.domestic_sector))}} <span class="muted">${{escapeHtml(item.primary_proxy || "-")}}</span></h3>
    <div class="row"><span>미국 섹터 1D</span><b>${{pct(item.us_sector_return_1d)}}</b></div>
    <div class="row"><span>미국 섹터 5D</span><b>${{pct(item.us_sector_return_5d)}}</b></div>
    <div class="row"><span>20D z-score</span><b>${{num1(item.us_sector_zscore_20d)}}</b></div>
    <div class="row"><span>60D beta/corr</span><b>${{num(item.us_sector_beta_60d)}} / ${{num(item.us_sector_corr_60d)}}</b></div>
    <div class="row"><span>impact</span><b>${{pct(item.us_sector_impact_score)}}</b></div>
    <p class="muted">${{escapeHtml((item.proxy_symbols || []).slice(0, 6).join(", "))}}</p>
    ${{(quality.messages || []).length ? `<p class="muted">${{escapeHtml(quality.messages.slice(0, 2).join(" · "))}}</p>` : ""}}
  </article>`;
}}
function sectorLabel(value) {{
  const labels = {{
    semiconductor: "반도체",
    auto: "자동차/부품",
    industrial: "산업재",
    financial: "금융",
    healthcare: "헬스케어/바이오",
    energy_materials: "에너지/소재",
    broad: "시장 전체"
  }};
  return labels[String(value || "")] || String(value || "-");
}}
function renderTodayNews(items, macroNews, marketContext, quality) {{
  const macroHtml = (macroNews || []).length
    ? `<section class="mini-card"><h3>거시·수급 뉴스</h3><div class="stack">${{(macroNews || []).slice(0, 8).map(item => `<div class="row"><span><a href="${{escapeHtml(item.link || "#")}}" target="_blank" rel="noreferrer">${{escapeHtml(item.title || "제목 없음")}}</a><br><small class="muted">${{shortDate(item.pub_date)}} · ${{escapeHtml(item.source || item.category || "-")}}</small></span></div>`).join("")}}</div></section>`
    : "";
  if (items.length) {{
    const grouped = items.reduce((acc, item) => {{
      const key = item.symbol || "unknown";
      if (!acc[key]) acc[key] = [];
      acc[key].push(item);
      return acc;
    }}, {{}});
    document.getElementById("todayNews").innerHTML = macroHtml + Object.entries(grouped).map(([symbol, rows]) => `
      <section class="mini-card">
        <h3>${{escapeHtml(rows[0].name || symbol)}} <span class="muted">${{escapeHtml(symbol)}}</span></h3>
        <div class="stack">
          ${{rows.slice(0, 5).map(item => `<div class="row"><span><a href="${{escapeHtml(item.originallink || item.link || "#")}}" target="_blank" rel="noreferrer">${{escapeHtml(item.title || "제목 없음")}}</a><br><small class="muted">${{shortDate(item.pub_date)}} · ${{escapeHtml(item.source_name || "-")}}</small></span></div>`).join("")}}
        </div>
      </section>
    `).join("");
    return;
  }}
  const messages = (quality.messages || []).filter(message => String(message).includes("뉴스"));
  const notice = messages.length
    ? `${{escapeHtml(messages.join(" · "))}} · .env에 NAVER_CLIENT_ID/SECRET 설정 후 run_today_market_update.py 실행`
    : "수집된 뉴스가 없습니다.";
  const contextHtml = marketContext.length
    ? `<section class="mini-card"><h3>시장 맥락</h3><div class="stack">${{marketContext.slice(0, 8).map(item => `<div class="row"><span>${{escapeHtml(item.title || "-")}}</span><b>${{escapeHtml(item.source_name || "-")}}</b></div>`).join("")}}</div></section>`
    : "";
  document.getElementById("todayNews").innerHTML = `${{macroHtml}}<div class="empty">${{notice}}</div>${{contextHtml}}`;
}}
function renderTodayRegime(regime) {{
  document.getElementById("todayRegime").innerHTML = renderRegimeDetail(regime);
}}
function renderTodayGlobalMarkets(globalMarkets) {{
  const items = globalMarkets.items || [];
  document.getElementById("todayGlobalMarkets").innerHTML = table(items.slice(0, 16), [
    {{key:"display_name", label:"지표"}},
    {{key:"symbol", label:"심볼"}},
    {{key:"trade_date", label:"일자", format:shortDate}},
    {{key:"close", label:"종가", format:money}},
    {{key:"return_1d", label:"1D", format:pct}},
    {{key:"return_5d", label:"5D", format:pct}},
    {{key:"source_name", label:"소스"}}
  ]);
}}
async function bootStock(symbol) {{
  const horizon = document.querySelector(".segmented button.active")?.dataset.horizon || "3M";
  const encoded = encodeURIComponent(symbol);
  const query = "?horizon=" + encodeURIComponent(horizon);
  const results = await Promise.allSettled([
    json("/api/stocks/" + encoded + query),
    json("/api/stocks/" + encoded + "/prediction-history" + query),
    json("/api/stocks/" + encoded + "/cluster" + query)
  ]);

  if (results[0].status === "fulfilled") renderStockDetail(results[0].value);
  else {{
    document.getElementById("stockInfo").innerHTML = errorState("추천 정보");
    document.getElementById("factorScores").innerHTML = errorState("Factor");
    document.getElementById("stockMetrics").innerHTML = metric("상세 API", "오류", "서버 로그를 확인하세요");
    document.getElementById("reports").innerHTML = errorState("애널리스트 리포트");
    document.getElementById("stockBacktest").innerHTML = errorState("Backtest");
  }}

  if (results[1].status === "fulfilled") renderPredictionHistory(results[1].value.items || []);
  else {{
    document.getElementById("history").innerHTML = errorState("예측 이력");
    document.getElementById("predictionChartState").innerHTML = errorState("예측 차트");
  }}

  if (results[2].status === "fulfilled") renderStockCluster(results[2].value);
  else {{
    document.getElementById("clusterSummary").innerHTML = errorState("클러스터");
    document.getElementById("clusterPeers").innerHTML = "";
  }}
}}
function selectStockHorizon(button) {{
  document.querySelectorAll(".segmented button").forEach(item => item.classList.remove("active"));
  button.classList.add("active");
  const page = document.getElementById("stockPage");
  if (page) bootStock(page.dataset.symbol);
}}
function renderStockDetail(detail) {{
  const rec = detail.recommendation || {{}};
  const pred = detail.prediction || {{}};
  const latest = detail.latest_price || {{}};
  const features = detail.features || {{}};
  document.getElementById("stockTitle").textContent = `${{detail.name || detail.symbol}} ${{detail.symbol || ""}}`;
  document.getElementById("stockMetrics").innerHTML = [
    metric("최근 종가", money(latest.close), shortDate(latest.date)),
    metric("추천/예측 순위", rec.rank ? rec.rank + "위" : (pred.rank ? pred.rank + "위" : "-"), detail.horizon || "-"),
    metric("상승 확률", pct(rec.pred_prob_top20 ?? pred.pred_prob_top20), rec.model_version || pred.model_version || "모델 없음"),
    metric("예상 상대수익률", pct(rec.pred_return ?? pred.pred_return), "모델 추정치"),
    metric("최종 점수", num(rec.final_score ?? pred.pred_prob_top20), rec.final_score === undefined ? "예측 확률 기준" : "복합 추천 점수"),
    metric("위험 점수", pct(rec.risk_score ?? pred.pred_risk), "낮을수록 안정적")
  ].join("");

  const reasons = jsonList(rec.reason_json);
  const risks = jsonList(rec.risk_flags_json);
  document.getElementById("stockInfo").innerHTML = `
    <div class="row"><span>시장</span><b>${{escapeHtml(detail.market || "-")}}</b></div>
    <div class="row"><span>섹터</span><b>${{escapeHtml(detail.sector || "-")}}</b></div>
    <div><b>추천 근거</b>${{reasons.length
      ? `<ul class="reason-list">${{reasons.map(item => `<li>${{escapeHtml(item)}}</li>`).join("")}}</ul>`
      : '<p class="empty">생성된 추천 근거가 없습니다.</p>'}}</div>
    <div><b>위험 신호</b>${{risks.length
      ? `<ul class="reason-list risk">${{risks.map(item => `<li>${{escapeHtml(item)}}</li>`).join("")}}</ul>`
      : '<p class="muted">감지된 별도 위험 신호가 없습니다.</p>'}}</div>
  `;

  const factors = [
    ["모멘텀", rec.momentum_score ?? features.momentum_score],
    ["수급", rec.supply_demand_score ?? features.supply_demand_score],
    ["목표가", rec.target_upside_score ?? features.target_upside_score],
    ["유동성", features.liquidity_score],
    ["가치", features.value_score],
    ["미국 유사섹터", features.us_sector_impact_score],
    ["리스크", rec.risk_score ?? features.risk_score]
  ];
  document.getElementById("factorScores").innerHTML = factors.map(([label, value]) => factorLine(label, value)).join("");
  const sectorItem = detail.sector_linkage?.item || {{}};
  const sectorHtml = Object.keys(sectorItem).length
    ? `<div><b>미국 유사섹터 영향</b>
        <div class="row"><span>섹터 / proxy</span><b>${{escapeHtml(sectorLabel(sectorItem.domestic_sector))}} / ${{escapeHtml(sectorItem.primary_proxy || "-")}}</b></div>
        <div class="row"><span>1D / 5D</span><b>${{pct(sectorItem.us_sector_return_1d)}} / ${{pct(sectorItem.us_sector_return_5d)}}</b></div>
        <div class="row"><span>beta / corr</span><b>${{num(sectorItem.us_sector_beta_60d)}} / ${{num(sectorItem.us_sector_corr_60d)}}</b></div>
        <div class="row"><span>impact</span><b>${{pct(sectorItem.us_sector_impact_score)}}</b></div>
      </div>`
    : "";
  document.getElementById("stockInfo").insertAdjacentHTML("beforeend", sectorHtml);
  document.getElementById("reports").innerHTML = detail.analyst_reports?.length
    ? table(detail.analyst_reports, [
        {{key:"report_date", label:"발행일", format:shortDate}},
        {{key:"broker_name", label:"증권사"}},
        {{key:"analyst_name", label:"애널리스트"}},
        {{key:"investment_rating", label:"의견"}},
        {{key:"target_price", label:"목표가", format:money}}
      ])
    : '<div class="empty">수집된 애널리스트 리포트가 없습니다.</div>';
  document.getElementById("stockDisclaimer").textContent = detail.disclaimer || "";
  renderPriceChart(detail.chart || []);
  renderStockBacktest(detail.backtest || []);
}}
function factorLine(label, value) {{
  const score = value === null || value === undefined || Number.isNaN(Number(value))
    ? null
    : Math.max(0, Math.min(1, Number(value)));
  const width = score === null ? 0 : score * 100;
  return `<div class="factor-line"><span>${{escapeHtml(label)}}</span><div class="bar"><span style="width:${{width}}%"></span></div><b>${{score === null ? "-" : Math.round(width)}}</b></div>`;
}}
function renderPredictionHistory(items) {{
  const ordered = [...items].sort((a, b) => String(b.prediction_date).localeCompare(String(a.prediction_date)));
  document.getElementById("history").innerHTML = table(ordered.slice(0, 100), [
    {{key:"prediction_date", label:"예측일", format:shortDate}},
    {{key:"horizon", label:"기간"}},
    {{key:"model_name", label:"모델"}},
    {{key:"source", label:"구분"}},
    {{key:"predicted_probability", label:"확률", format:pct}},
    {{key:"predicted_return", label:"예상수익", format:pct}},
    {{key:"risk_score", label:"위험", format:pct}},
    {{key:"rank_no", label:"순위"}}
  ]);
  renderPredictionChart(ordered);
}}
function renderStockCluster(data) {{
  const cluster = data.cluster || {{}};
  let featureRows = "";
  if (cluster.feature_values_json) {{
    try {{
      const parsed = JSON.parse(cluster.feature_values_json);
      featureRows = Object.entries(parsed).slice(0, 8)
        .map(([key, value]) => `<div class="row"><span>${{escapeHtml(key)}}</span><b>${{num(value)}}</b></div>`).join("");
    }} catch (_) {{
      featureRows = "";
    }}
  }}
  document.getElementById("clusterSummary").innerHTML = cluster.symbol
    ? `<div class="row"><span>클러스터</span><b>${{escapeHtml(cluster.cluster_label || "-")}}</b></div>
       <div class="row"><span>중심 거리</span><b>${{num(cluster.distance_to_centroid)}}</b></div>${{featureRows}}`
    : '<div class="empty">생성된 클러스터가 없습니다.</div>';
  document.getElementById("clusterPeers").innerHTML = table(data.peers || [], [
    {{key:"symbol", label:"코드", format:(value, item) => `<a class="peer-link" href="/stock/${{encodeURIComponent(item.symbol)}}">${{escapeHtml(value)}}</a>`}},
    {{key:"name", label:"종목"}},
    {{key:"sector", label:"산업"}},
    {{key:"distance_to_centroid", label:"유사거리", format:num}}
  ]);
}}
function renderStockBacktest(items) {{
  const completed = items.filter(item => item.actual_return !== null && item.actual_return !== undefined);
  const pending = items.filter(item => item.actual_return === null || item.actual_return === undefined);
  const average = (values) => values.length ? values.reduce((sum, value) => sum + Number(value || 0), 0) / values.length : null;
  const hitRatio = completed.length ? completed.filter(item => Number(item.actual_return) > 0).length / completed.length : null;
  document.getElementById("stockBacktestSummary").innerHTML = [
    metric("검증 완료", completed.length, "실제 수익률 확인"),
    metric("검증 대기", pending.length, "목표일 미도래"),
    metric("적중률", pct(hitRatio), "실제 수익률 양수"),
    metric("평균 실제수익", pct(average(completed.map(item => item.actual_return))), "완료 표본"),
    metric("평균 초과수익", pct(average(completed.map(item => item.excess_return))), "벤치마크 대비")
  ].join("");
  document.getElementById("stockBacktest").innerHTML = table(items, [
    {{key:"prediction_date", label:"예측일", format:shortDate}},
    {{key:"target_date", label:"목표일", format:shortDate}},
    {{key:"model_name", label:"모델"}},
    {{key:"rank_no", label:"순위"}},
    {{key:"predicted_return", label:"예상수익", format:pct}},
    {{key:"actual_return", label:"실제수익", format:(value) => value === null || value === undefined ? "검증 대기" : pct(value)}},
    {{key:"excess_return", label:"초과수익", format:pct}}
  ]);
}}
function prepareCanvas(canvas) {{
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(320, canvas.clientWidth);
  const height = Math.max(220, canvas.clientHeight);
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  return {{context, width, height}};
}}
function renderPriceChart(items) {{
  const state = document.getElementById("priceChartState");
  const canvas = document.getElementById("priceChart");
  const data = items.filter(item => Number.isFinite(Number(item.close))).slice(-180);
  if (!data.length) {{
    state.textContent = "수집된 가격 데이터가 없습니다.";
    canvas.style.display = "none";
    return;
  }}
  state.textContent = `${{shortDate(data[0].date)}} ~ ${{shortDate(data[data.length - 1].date)}} · ${{data.length}} 거래일`;
  canvas.style.display = "block";
  const {{context: ctx, width, height}} = prepareCanvas(canvas);
  ctx.clearRect(0, 0, width, height);
  const pad = {{left: 54, right: 18, top: 18, bottom: 28}};
  const priceHeight = height * 0.68;
  const closes = data.map(item => Number(item.close));
  const volumes = data.map(item => Number(item.volume || 0));
  const min = Math.min(...closes);
  const max = Math.max(...closes);
  const maxVolume = Math.max(...volumes, 1);
  const x = index => pad.left + index * (width - pad.left - pad.right) / Math.max(1, data.length - 1);
  const y = value => pad.top + (max - value) * (priceHeight - pad.top) / Math.max(1, max - min);
  ctx.strokeStyle = "#5f6678";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.left, priceHeight);
  ctx.lineTo(width - pad.right, priceHeight);
  ctx.stroke();
  ctx.fillStyle = "rgba(102,217,242,.32)";
  data.forEach((item, index) => {{
    const barHeight = Number(item.volume || 0) / maxVolume * (height - priceHeight - pad.bottom - 8);
    ctx.fillRect(x(index), height - pad.bottom - barHeight, Math.max(1, (width - pad.left - pad.right) / data.length - 1), barHeight);
  }});
  ctx.strokeStyle = "#ffd96a";
  ctx.lineWidth = 2;
  ctx.beginPath();
  closes.forEach((value, index) => index ? ctx.lineTo(x(index), y(value)) : ctx.moveTo(x(index), y(value)));
  ctx.stroke();
  ctx.fillStyle = "#bac1cf";
  ctx.font = "12px sans-serif";
  ctx.fillText(money(max), 4, pad.top + 5);
  ctx.fillText(money(min), 4, priceHeight);
  ctx.fillText(shortDate(data[0].date), pad.left, height - 6);
  const endLabel = shortDate(data[data.length - 1].date);
  ctx.fillText(endLabel, width - pad.right - ctx.measureText(endLabel).width, height - 6);
}}
function renderPredictionChart(items) {{
  const state = document.getElementById("predictionChartState");
  const canvas = document.getElementById("predictionChart");
  const data = [...items]
    .filter(item => Number.isFinite(Number(item.predicted_probability)))
    .sort((a, b) => String(a.prediction_date).localeCompare(String(b.prediction_date)))
    .slice(-180);
  if (!data.length) {{
    state.textContent = "생성된 예측 이력이 없습니다.";
    canvas.style.display = "none";
    return;
  }}
  state.textContent = `${{data.length}}개 예측 · 파랑: 상승확률 · 노랑: 예상수익률`;
  canvas.style.display = "block";
  const {{context: ctx, width, height}} = prepareCanvas(canvas);
  ctx.clearRect(0, 0, width, height);
  const pad = {{left: 48, right: 18, top: 18, bottom: 28}};
  const x = index => pad.left + index * (width - pad.left - pad.right) / Math.max(1, data.length - 1);
  const y = value => pad.top + (1 - Math.max(0, Math.min(1, value))) * (height - pad.top - pad.bottom);
  const draw = (key, color) => {{
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    data.forEach((item, index) => {{
      const raw = Number(item[key]);
      const value = key === "predicted_return" ? Math.max(0, Math.min(1, (raw + 0.5) / 1.5)) : raw;
      index ? ctx.lineTo(x(index), y(value)) : ctx.moveTo(x(index), y(value));
    }});
    ctx.stroke();
  }};
  draw("predicted_probability", "#66d9f2");
  draw("predicted_return", "#ffd96a");
  ctx.fillStyle = "#bac1cf";
  ctx.font = "12px sans-serif";
  ctx.fillText("100%", 5, pad.top + 4);
  ctx.fillText("0%", 20, height - pad.bottom);
  ctx.fillText(shortDate(data[0].prediction_date), pad.left, height - 6);
  const endLabel = shortDate(data[data.length - 1].prediction_date);
  ctx.fillText(endLabel, width - pad.right - ctx.measureText(endLabel).width, height - 6);
}}
document.addEventListener("DOMContentLoaded", () => {{
  if (document.getElementById("dashboardPage")) bootDashboard();
  if (document.getElementById("backtestPage")) bootBacktest();
  if (document.getElementById("twoStockDemoPage")) bootTwoStockDemo();
  if (document.getElementById("focusStocksDemoPage")) bootFocusStocksDemo();
  if (document.getElementById("fourStocksDemoPage")) bootFourStocksDemo();
  if (document.getElementById("top50Page")) bootTop50Universe();
  if (document.getElementById("longShortPage")) bootLongShort();
  if (document.getElementById("marketUpDownPage")) bootMarketUpDown();
  if (document.getElementById("top20UpsidePage")) bootTop20Upside();
  if (document.getElementById("todayMarketPage")) bootTodayMarket();
  if (document.getElementById("tomorrowMarketPage")) bootTomorrowMarket();
  const stockPage = document.getElementById("stockPage");
  if (stockPage) bootStock(stockPage.dataset.symbol);
}});
</script>
</body>
</html>"""
