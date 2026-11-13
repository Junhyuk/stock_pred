# 일일 연구 리포트 자동 갱신

기본 브랜치 `master`의 `Premarket research forecast` GitHub Actions가 매일 07:50 KST
(22:50 UTC)에 시작되도록 예약됩니다. GitHub의 예약 실행은 지연되거나 누락될 수
있으므로 정확한 시각의 갱신을 보장하지 않습니다. Actions 실행 결과와 리포트의
자동 확인 시각 및 각 카드의 기준 종가일을 확인하세요. 수동 실행도 기본 브랜치에서
할 수 있습니다. 기본 브랜치를 변경하면 workflow의 브랜치 지정도 함께 변경해야 합니다.

XKRX 거래일에는 공개 Yahoo/yfinance 가격을 갱신하고 한국과 미국 연구 예측을
각각 실행합니다. 미국 입력은 실행 시점에 완료된 미국 정규장 종가를 사용하므로,
휴장과 시차에 따라 한국 기준일과 다를 수 있습니다. 한국 휴장일에는 미국을
포함한 예측을 다시 실행하지 않고 이전 공개 결과의 검증된 발행 시각과 기준일을
표시합니다. 검증할 수 없는 발행 기록은 확인 불가로 남습니다. 자동 실행은 뉴스를
수집하지 않으며 과거 사건 파일을 현재 뉴스로 사용하지 않습니다.

Pages 설정의 Build and deployment → Source는 GitHub Actions여야 합니다.
실행이 끝나면 compact JSON과 일일 상태를 기본 브랜치에 push하고 같은 workflow가
Pages를 배포합니다. Actions `GITHUB_TOKEN` push가 별도 push workflow를 자동
기동하지 않기 때문입니다. 기본 브랜치가 실행 중 다른 커밋으로 전진하면 push가
실패하며 강제 push하지 않습니다. Actions에서 실패 원인을 확인한 후 기본 브랜치의
수동 실행을 재시도하세요. 캘린더나 수집·예측·push 실패 시 새로운 성공 상태를
게시하지 않습니다. Pages 배포가 실패하면 Actions에서 별도로 실패로 표시됩니다.

DB와 원시 CSV는 Git에 저장하지 않습니다. GitHub 호스팅의 새 runner가 공개 시세로
만든 SQLite와 미국 관측 CSV만 Actions cache에 저장해 증분 갱신합니다. 개인 로컬
DB나 참가자 데이터를 업로드하지 마세요. cache는 영구 저장소가 아니며 제거될 수
있습니다. cache가 없으면 공개 시세의 2019년 이후 이력을 다시 요청합니다. 이 경우
수집 시각과 공급자 수정 이력이 원래 cache와 같다는 보장은 없습니다. OOF 아카이브는
Actions artifact에 14일 보관하고, compact forecast와 publication manifest는 함께
Git에 저장합니다. 화면은 manifest의 run ID와 SHA-256을 검사합니다.
