# ENV_AND_SECURITY.md

## Secret handling

`.env`:
```text
DART_API_KEY=
KRX_API_KEY=
KIS_APP_KEY=
KIS_APP_SECRET=
NAVER_CLIENT_ID=
NAVER_CLIENT_SECRET=
```

`.env`는 git에 commit하지 않는다.

`.env.example`:
값 없이 key 이름만 저장한다.

## Security
- API key logging 금지
- secret masking
- least privilege
- paper account 우선
- production trading credentials 분리
- credential rotation 가능 구조
