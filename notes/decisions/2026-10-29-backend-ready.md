# backend 준비 완료와 재분배 채택

| 항목 | 값 |
|---|---|
| 날짜 | 2026-10-29 |
| 상태 | 결정 |
| 관련 | 서비스 저장소 Jira S15P31E101-342, MR !249(2026-10-29 병합, 운영 반영), `docs/design/auth.md` 5.5·5.6절 |

## 결정

- **Master는 `--enable-rebalancing`으로 띄운다.** 근거는 [로컬 테스트](2026-10-29-local-tests.md) 결과 1이다. 이 옵션 없이 Master가 뜨면 노트북 Worker가 끊길 때 실행기가 조용히 줄어든다.
- **구분 헤더는 `X-Synthetic-Traffic: 1`이다.** 로그인을 포함한 모든 요청에 붙인다. 설정은 `config/default.toml`의 `[synthetic]`이다.
- **인공 회원 이메일은 `lt-<번호>@synthetic.kkugi.test`로 확정되었다.** 발생기의 지금 형식과 같다.
- **탈퇴된 계정은 새 번호로 다시 만든다.** 서비스 저장소 문서(`auth.md` 5.5절 7항)의 규칙이다.

## backend 쪽 상태 (2026-10-29 보고)

- 운영에서 dev 로그인(`POST /api/v1/auth/login/dev`)이 켜졌다.
- 헤더가 붙은 요청은 지표(`http.server.requests`의 `synthetic` 태그), 로그(JSON 필드 `synthetic`), 추적(span 속성 `http.request.header.x-synthetic-traffic`) 세 곳에서 구분된다. 세 곳 모두 운영에서 확인했다고 보고되었다.
- 인공 회원은 provider `EMAIL_DEV`이고 이메일이 `lt-%@synthetic.kkugi.test`인 회원이다.
- 후속 백로그: 비동기 이벤트 로그의 `synthetic` 필드(S15P31E101-346), 운영 관리자 지정 방법(S15P31E101-347), 대시보드에서 인공·실제 요청 나눠 보기(S15P31E101-348).

## 탈퇴가 발생기에 닿는 곳

인공 회원의 이메일 형식이 공개되어 있어, 누구나 인공 회원으로 로그인해 탈퇴시킬 수 있다(`auth.md` 5.5절 6항, 팀이 감수한 위험). 탈퇴되면 발생기는 다음에서 401 `MEMBER_WITHDRAWN`을 받는다(서비스 저장소 `da239cf8`의 OpenAPI와 `auth.md`에서 확인).

- 보호 API 전부: 온보딩 게이트가 탈퇴 회원을 막는다
- `POST /auth/refresh`: 탈퇴 회원의 리프레시 토큰을 거절한다
- `POST /members/me/onboarding`

지금 발생기는 이 응답을 일반 실패로 처리해, 그 계정의 세션이 매번 실패한다. 처리 방식은 [구현 계획](../plan.md) 단계 0에 있다.
