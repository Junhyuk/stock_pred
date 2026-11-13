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
예측 CLI가 정상 종료하더라도 `blocked_*` 등 신규 발행이 없는 결과는 성공으로
취급하지 않습니다. 한국과 미국 각각의 실행 결과, 발행 run ID·시각 및 compact
파일 체크섬이 일치하는 실제 신규 발행을 확인한 뒤에만 상태와 결과를 게시합니다.

DB와 원시 CSV는 Git에 저장하지 않습니다. GitHub 호스팅의 새 runner가 공개 시세로
만든 SQLite와 미국 관측 CSV만 Actions cache에 저장해 증분 갱신합니다. 개인 로컬
DB나 참가자 데이터를 업로드하지 마세요. cache는 영구 저장소가 아니며 제거될 수
있습니다. cache가 없으면 공개 시세의 2019년 이후 이력을 다시 요청합니다. 이 경우
수집 시각과 공급자 수정 이력이 원래 cache와 같다는 보장은 없습니다. OOF 아카이브는
Actions artifact에 14일 보관하고, compact forecast와 publication manifest는 함께
Git에 저장합니다. 화면은 manifest의 run ID와 SHA-256을 검사합니다.

공개 저장소의 표준 GitHub-hosted runner 사용은 무료입니다. 이 workflow는
`ubuntu-latest`를 유지하며 유료 larger runner나 캐시 용량 증액을 사용하지 않습니다.
저장소를 private으로 바꾸면 실행 시간의 무료 정책도 다시 확인해야 합니다.
Artifact/Packages 저장 공간은 계정의 공유 allowance를 확인해야 하며, GitHub Free의
포함량은 500MB입니다. 다른 저장소의 사용량까지 포함한 계정 전체 무료 여부는 이
저장소의 측정만으로 보장할 수 없습니다.
[GitHub Actions 결제 정책](https://docs.github.com/en/billing/concepts/product-billing/github-actions)

2026-11-13 측정에서 OOF artifact는 5,201,975 bytes였습니다. 매일 한 번 생성하는
상한으로 계산해도 14일 보관량은 약 72.8MB이며, 한국 거래일에만 생성하므로 보통은
그보다 작습니다. Pages artifact는 약 0.25MB이고 기본 보관은 1일입니다.
실제 크기는 데이터 증가와 수동 재실행 횟수에 따라 달라집니다.
[Pages artifact 보관 설정](https://github.com/actions/upload-pages-artifact#inputs)

같은 날 이 저장소의 cache 사용량은 104,834,962 bytes였고 대부분은 pip cache였습니다.
기본 저장소 cache 한도 10GB 안에서 운영하며, 장기간 미사용 cache 제거와 한도 도달 시
퇴출이 있으므로 장기 데이터 보존 장치로 간주하지 않습니다.
[Cache 용량과 퇴출 정책](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#usage-limits-and-eviction-policy)

수동 실행 `43431083091`은 Pages 배포까지 9분 42초가 걸렸습니다. 한국 예측 6분
18초, 미국 예측 2분 19초, 두 의존성 설치 합계 21초, 가격 갱신 6초였으므로
예측 반복문의 중복 행 변환과 동일 모델의 중복 추론만 줄였습니다. 300행·두 feature
group·세 모델 family의 결측/지연 label fixture에서 경량화 전후 전체 OOF와 평가
JSON은 동일했고, 3회 중앙값 기준 평가 시간은 5.796초에서 5.233초로 약 9.7%
줄었습니다. 이는 합성 fixture 측정이며 실제 일일 실행 시간의 감소율을 보장하지
않습니다. 실측 9분 42초를 매일 반복하는 보수적 가정은 월 31일 약 301분의 경과
시간입니다. 실제 runner 집계는 job별이며 휴장일 실행은 예측을 생략합니다.

핵심 정확성 검토에서는 알려진 과거 가격의 공개 시각을 해당 가격에 의존하는
21세션 feature 창의 각 예측 cutoff와 비교하도록 보완했습니다. 현재 snapshot
이후에 수신한 가격은 과거 행이라도 이번 실행에서 제외하며, 08:00 이후 수신한
자료로 08:00 snapshot을 발행하지 않습니다. 제외된 가격은 0이나 직전 가격으로
채우지 않습니다. 원래 공개 시각이 없는 역사 데이터는 계속 retrospective close
reconstruction으로 표시하며, 과거에 실제 수신한 데이터였다고 주장하지 않습니다.
이전 vintage 자체가 없는 자료의 모든 수정 이력을 복원하는 보장은 없습니다.

실제 예측 재학습에도 OOF와 같은 positional purge를 maturity 검사보다 먼저
적용하며, 중복 모델 family 설정은 OOF 표본을 중복 집계하지 않도록 거부합니다.
첫 feature group이 비어 있어도 다른 유효 group의 진단을 계속할 수 있습니다.
이 정확성 수정은 일부 예측값을 바꿀 수 있습니다. 새로운 feature/model, 검증
기준 완화, 성능 향상 주장이나 production 승인은 포함하지 않습니다.

검증은 KR/US pipeline·입력·가격 갱신·캘린더·공개 상태·workflow를 포함한
10개 test module에서 117개 통과했고, 지연 label과 purge의 순서를 확인하는
4개 조합도 별도로 통과했습니다. 독립 코드 검토에서 추가 차단 결함은 발견하지
못했습니다. 이 결과가 가격 예측력 향상이나 production 승인을 의미하지는 않습니다.
