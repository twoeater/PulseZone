# PulseZone

BIND DNS 존 파일의 **각 A 레코드·IP**를 HTTP/HTTPS로 점검하는 Python 3.9+ 모니터입니다. 웹에서 도메인과 개별 IP의 점검 설정을 관리합니다. 설정은 SQLite, 점검 이력과 장애 상태는 메모리에 저장합니다.

## 빠른 시작

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
cp .env.example .env
```

`.env`의 `ZONE_PATH`를 실제 존 파일 디렉터리로 지정한 후 실행합니다.

```bash
python -m pulsezone
# 다른 환경 파일을 사용하려면:
python -m pulsezone --env-file /etc/pulsezone.env
```

브라우저에서 `http://서버주소:8080`에 접속합니다. 기본 바인딩은 `0.0.0.0:8080`이며 **애플리케이션 로그인은 없습니다**. 내부망 또는 접근을 제어하는 프록시 뒤에서 운영하는 구성을 전제로 합니다. 별도 웹 프론트엔드 빌드나 CDN이 필요하지 않습니다.

Windows에서도 실행할 수 있습니다.

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install .
Copy-Item .env.example .env
.\.venv\Scripts\python -m pulsezone
```

샘플은 `examples/zones`에 있습니다. 샘플의 모든 A 주소는 루프백이며, 웹 서버가 없으면 실패로 표시됩니다. 존 파싱과 화면만 살펴보려면 `.env`에서 `ZONE_PATH=./examples/zones`로 지정하세요.

## 환경 설정

실제 환경변수가 `.env`보다 우선합니다. 환경변수 변경은 재시작해야 적용되며, 웹에서 저장한 설정은 즉시 적용됩니다.

| 변수 | 기본값 | 의미 |
| --- | --- | --- |
| `ZONE_PATH` | `./zones` | 하위 폴더까지 스캔할 존 루트 |
| `ZONE_REFRESH_TIME` | `3600` | 존 다시 읽기 간격, 초 |
| `CHECK_INTERVAL` | `10` | 성공 후 다음 점검까지 대기, 초 |
| `TIMEOUT` | `10` | 프로토콜별 요청 제한, 초 |
| `RETRY` | `10` | 최초 포함 연속 실패 횟수, 정수 |
| `RETRY_DELAY` | `10` | 실패 후 다음 점검까지 대기, 초 |
| `CHECK_PROTOCOL` | `auto` | `auto`, `http`, `https` |
| `HTTPS_INSECURE` | `false` | `true`이면 curl `-k`처럼 HTTPS 인증서 검증 생략. `true` / `false` |
| `DATABASE_PATH` | `./data/pulsezone.sqlite3` | SQLite 설정 파일 |
| `WEB_HOST` | `0.0.0.0` | 웹 바인딩 주소 |
| `WEB_PORT` | `8080` | 웹 포트 |
| `MAX_CONCURRENCY` | `100` | 최대 동시 점검 수 |
| `HISTORY_SIZE` | `60` | 행별 메모리 점검 이력 개수 |
| `ALERT_SLACK` | 빈 값 | Slack incoming webhook URL |
| `ALERT_DISCORD` | 빈 값 | Discord webhook URL |
| `ALERT_NTFY` | 빈 값 | `https://ntfy.sh/토픽` 등 완전한 토픽 URL |
| `ALERT_GOTIFY` | 빈 값 | `https://gotify.example/message` publish URL |
| `ALERT_CUSTOM` | 빈 값 | 다목적 JSON POST URL |
| `ALERT_NTFY_TOKEN` | 빈 값 | ntfy 인증값. 빈 값은 인증 헤더 생략, `:` 포함 시 `아이디:비밀번호`, 없으면 토큰 |
| `ALERT_GOTIFY_TOKEN` | 빈 값 | 선택적 애플리케이션 토큰 |

시간 설정은 양수이며 소수를 허용합니다. RETRY는 1 이상의 정수입니다. 다수 대상이 동시에 점검 중이면 동시 실행 한도에 따라 추가 대기할 수 있습니다.

## 존 파일과 설정 상속

- `zones` 관리 테이블 1개와 **존 파일별 실제 레코드 테이블**을 생성합니다. `a/db.example.com`은 `zone:a/db.example.com` 테이블입니다. 다른 폴더의 동명 파일도 독립적으로 관리합니다.
- `id`가 화면의 index입니다. 파일명과 상대경로는 읽기 전용이며, 표시 이름·domain·설명·tip·점검 설정을 편집할 수 있습니다.
- 최초 표시 이름과 `domain`은 **파일명 그대로**입니다. `db.example.com`의 HTTP Host도 초기에는 `db.example.com`입니다. 실제 서비스가 `example.com`이라면 도메인 편집 화면에서 변경하세요.
- 파싱 origin은 파일 앞부분의 `$ORIGIN`, SOA의 절대 소유자명, `db.`를 제거한 파일명 순으로 결정합니다. 도중의 `$ORIGIN` 변경도 처리합니다. 파싱 origin과 웹 점검 domain은 서로 독립적입니다.
- apex는 `@`, IP 전용 수동 항목은 빈 레코드명입니다. `@`의 요청 호스트에서는 `@`를 제외합니다. 빈 레코드는 처음 등록할 때 HTTP를 적용하며 IP로 접속합니다.
- A만 점검합니다. PTR·AAAA·CNAME 등은 점검 대상으로 등록하지 않습니다. 와일드카드 `*`는 요청 시 `pulsezone-probe`로 바꿉니다.
- `$TTL`, 생략된 소유자명, 복수 IP, 괄호·주석·절대 이름, `$GENERATE`를 처리합니다. SOA/NS 없는 A 조각과 TTL 없는 조각도 읽으며 생략된 기본 TTL은 파싱에만 쓰는 300초입니다.
- `$INCLUDE`는 **포함하는 파일의 디렉터리 기준**으로 해석하고 존 루트 안에 있어야 합니다. origin·소유자·TTL 상속 및 복원을 지원합니다. 순환 참조와 32단계 이상 중첩은 오류로 표시합니다.
- UTF-8/UTF-8 BOM 파일을 지원합니다. 확장자와 무관하게 일반 파일을 대상으로 하므로, include 조각도 독립 파일로 해석 가능한 경우 별도 등록됩니다. 심볼릭 링크는 루트 스캔에서 제외합니다.
- 설정 우선순위는 **레코드 → 도메인 → 환경변수**입니다. NULL/빈 시간·프로토콜·`https_insecure` 필드는 상속입니다. 단, **도메인 off는 모든 하위 레코드를 중지**합니다.

### 다시 읽을 때

같은 `(레코드명, IP)`의 별명·주기·설명을 보존합니다. 원본에서 사라지면 `MISSING`으로 점검을 중지하지만 on/off와 관리 설정은 덮어쓰지 않습니다. 다시 나타나면 기존 설정으로 재개합니다. 파일명이나 경로가 바뀌면 새로운 존으로 등록됩니다.

구문 오류는 해당 파일의 마지막 정상 등록 상태를 유지합니다. 루트 또는 하위 디렉터리 스캔 자체가 실패하면 그 회차의 동기화를 적용하지 않습니다. 오류는 화면에 표시됩니다.

웹에서 레코드명이나 IP를 변경하면 그 행은 **수동 항목**이 됩니다. 다음 스캔에 파일의 원본이 그대로 있으면 다시 등록됩니다. 별명·설명·주기만 변경하면 파일 연결을 유지합니다. 수동 항목은 파일 소실로 중지되지 않으며 도메인 off로 중지할 수 있습니다. 원본과 동일한 수동 레코드·IP가 있으면 중복 생성하지 않습니다. 원본 존 파일에는 쓰지 않습니다.

## HTTP 판정과 알림

각 행의 IP에 직접 연결하며 Host와 HTTPS SNI는 실제 요청 호스트를 사용합니다. 기본적으로 인증서의 체인·호스트·유효기간을 검증합니다. 서로 다른 IP 사이에서 연결을 공유하지 않습니다. 시스템 HTTP 프록시 환경변수를 사용하지 않습니다.

`https_insecure=true`이면 curl `-k`처럼 인증서 검증을 생략합니다. 만료·자체 서명·도메인 불일치 인증서도 HTTPS 응답이 200~399이면 정상으로 처리합니다. HTTPS 암호화, Host/SNI와 IP 고정은 유지하며, 연결 실패·타임아웃·비정상 HTTP 상태는 계속 실패로 처리합니다. `auto`에서는 HTTPS 시도에 적용하고, HTTP 점검과 알림 웹훅의 TLS 설정에는 영향을 주지 않습니다.

환경변수 기본값은 `HTTPS_INSECURE=false`입니다. 웹의 도메인·레코드 편집 및 일괄 설정에서 `상속 / false (검증) / true (검증 생략)`을 선택할 수 있습니다. API는 `{"https_insecure": true}`, `false`, `null`을 받으며 `null`은 상속입니다. 기존 SQLite는 시작 시 새 컬럼을 NULL로 추가하고 기존 설정을 보존합니다. 웹 저장은 다음 점검에 즉시 반영되며, 환경변수 변경은 재시작 후 적용됩니다.

`GET /`에서 **200~399를 정상**으로 보고 리다이렉트를 따라가지 않습니다. 응답 본문은 저장하지 않습니다. 기본 포트는 HTTP 80, HTTPS 443입니다. 임의 포트·경로 설정은 현재 제공하지 않습니다.

`auto`는 HTTPS 실패 시 HTTP도 시도하며 한쪽이 정상이면 점검 1회 성공입니다. HTTPS가 실패했어도 HTTP가 정상이면 UP이므로, HTTPS 자체를 확인하려면 `https`를 선택하세요. TIMEOUT=10이면 auto의 총 요청 시간은 최대 약 20초입니다. 대기는 요청 완료 후 시작합니다.

RETRY=10이면 10·20·30…번째 연속 실패에 장애 알림을 반복합니다. DOWN 이후 첫 성공에 복구 알림을 한 번 보냅니다. 재시작 시 장애 카운터도 초기화되므로 새 실패 횟수가 임계값에 도달하면 다시 알립니다. off/MISSING 전환은 복구 알림을 보내지 않습니다.

### 마지막 알림과 maintenance

각 파일의 레코드 테이블에 다음 컬럼을 저장합니다. 기존 DB는 시작할 때 자동 확장하며 존 파일과 기존 설정을 보존합니다.

| 컬럼 | 기본값 | 의미 |
| --- | --- | --- |
| `alerted_at` | `NULL` | 마지막 장애·복구 알림 발생 시각. 시간대 오프셋을 포함한 ISO 8601 TEXT, 예: `2026-10-28T14:30:00.000000+09:00`. API에서는 읽기 전용 |
| `maintenance` | `false` | 레코드별 당일 중복 알림 생략. SQLite에는 0/1, API에는 JSON boolean으로 저장·표시 |

`alerted_at`은 알림 발생 조건을 충족하고 전송 대기열에 정상 등록됐을 때만 갱신합니다. 채널이 없거나 대기열이 가득 찼거나 maintenance로 생략한 경우, 일반 점검·조회·재스캔에서는 갱신하지 않습니다. **알림 발생 시각이며 수신 성공 시각은 아닙니다.** 채널별 전송 성공·실패는 기존 알림 전송 상태에서 확인합니다. 여러 채널과 채널별 전송 재시도는 하나의 발생 시각을 공유합니다.

`maintenance=true`이고 마지막 알림의 날짜가 오늘이면 해당 레코드의 추가 **장애·복구 알림 모두** 건너뜁니다. 점검·실패 카운트·메모리 이력은 계속 갱신합니다. 마지막 알림이 없거나 다른 날짜이면 기존 RETRY/복구 조건에서 알림을 보내고 `alerted_at`을 갱신합니다. maintenance 설정과 마지막 알림 시각은 재시작 후에도 유지됩니다.

자동 해제는 **서버 로컬 시간대**로 아래 순서대로 처리합니다. 한국 시간으로 운영할 컨테이너는 `TZ=Asia/Seoul` 등 실행 환경의 시간대도 맞춰 주세요.

```python
cutoff = now.replace(hour=13, minute=0, second=0, microsecond=0)
if record.maintenance and record.alerted_at is not None:
    if now >= cutoff and record.alerted_at < cutoff:
        record.maintenance = False
if record.maintenance and record.alerted_at is not None and record.alerted_at.date() == now.date():
    return  # 알림만 생략, 점검 루프는 계속 실행
```

예를 들어 오늘 10시 알림은 오늘 13시부터 자동 해제 대상이고, 오늘 14시 알림은 오늘 유지하다 다음 날 13시 조건에서 해제됩니다. 자정 이후 새 알림이 생겨도 그 시각이 13시 전이면 같은 날 13시에 해제됩니다. `alerted_at=NULL`은 자동 해제 대상이 아닙니다. 알림 직전·설정 반영·시작 시점과 30초 주기로 확인하므로, 점검 간격이 길거나 OFF/MISSING인 항목도 해제됩니다. 서버가 꺼져 시각을 놓친 항목도 재시작 후 같은 조건으로 처리합니다.

레코드 편집 창에서 마지막 알림 시각과 현재 maintenance 상태를 확인하고 `true/false`를 선택할 수 있습니다. 여러 레코드를 선택한 일괄 설정도 지원합니다. 이 옵션은 도메인·환경변수에서 상속하지 않으며, 도메인 선택이 섞이면 maintenance 일괄 설정은 비활성화됩니다. 유지 중인 레코드는 점검 결과에 `MAINT`로 표시합니다.

```text
레코드표시명.도메인표시명(IP)
응답없음(연속 실패 10회 × retry_delay 10초 = 기준 100초 / 실제 down 290.0초)
도메인 설명 / 레코드 설명
원인: https: 타임아웃 (10초); http: 타임아웃 (10초)
```

실제 down은 첫 실패 요청 시작부터 측정합니다. 10회 실패 사이의 대기는 9회이며 요청 소요 시간도 포함하므로, `횟수 × 딜레이`와 다릅니다.

빈 알림 URL은 건너뛰고 나머지 채널에 모두 보냅니다. Custom JSON의 `text`, `content`, `body`, `alert`에는 동일한 본문을 넣습니다. Gotify URL의 `?token=...`도 허용하며 토큰은 `X-Gotify-Key` 헤더로 이동해 전송합니다. 별도 토큰 환경변수가 있으면 우선합니다.

ntfy는 `ALERT_NTFY_TOKEN`이 비어 있으면 **인증 헤더 없이** 전송합니다. 값이 있으면 `curl -u 사용자:비밀번호`와 같은 Basic 인증을 사용합니다. 토큰을 빈 사용자명의 비밀번호 자리에 보내는 방법은 [ntfy 공식 문서](https://docs.ntfy.sh/publish/#access-tokens)에서도 지원합니다. `ALERT_NTFY`에는 토픽 URL을 넣고, 인증은 `ALERT_NTFY_TOKEN` 하나로 자동 판단합니다.

| 인증 방식 | 환경변수 | 대응하는 curl 인증 옵션 |
| --- | --- | --- |
| 공개 토픽 | `ALERT_NTFY_TOKEN=` | 인증 옵션 없음 (`-u` 생략) |
| 아이디·비밀번호 | `ALERT_NTFY_TOKEN=사용자:비밀번호` | `-u '사용자:비밀번호'` |
| 토큰 | `ALERT_NTFY_TOKEN=tk_...` | `-u ':tk_...'` |

값에 콜론(`:`)이 있으면 첫 번째 콜론만 기준으로 나누므로, `사용자:비밀:번호`는 아이디 `사용자`, 비밀번호 `비밀:번호`로 전송합니다. 값이 있고 콜론이 없으면 빈 사용자명과 토큰을 전송합니다. 빈 값이면 `Authorization` 헤더를 보내지 않아 `curl -d "test" https://ntfy.example.com/public`과 같은 방식으로 동작합니다. 예를 들어 공개 토픽은 아래처럼 설정합니다.

```dotenv
ALERT_NTFY=https://ntfy.example.com/public
ALERT_NTFY_TOKEN=
```

환경변수와 `.env` 변경은 프로세스 재시작 후 적용됩니다. 실제 환경변수가 `.env`보다 우선하며, 토픽 URL이 비어 있으면 인증값이 있어도 ntfy 전송을 건너뜁니다.

전송 작업은 점검과 분리됩니다. 채널당 timeout 10초, 네트워크 오류·429·5xx에는 최대 3회 시도합니다. 전송 실패와 1,000건 대기열 초과는 화면에 표시합니다. 전송 대기열도 메모리이며, 프로세스 종료 시 미전송 알림은 유지되지 않습니다. 전송 결과를 확인하지 못해 재시도하는 경우 수신 측에 중복 알림이 생길 수 있습니다. Discord 본문은 2,000자로 제한하며 자동 멘션을 막습니다.

## 웹 관리와 API

상단 `화면` 선택에서 라이트·다크 모드를 바꿀 수 있습니다. 기본값 `시스템`은 운영체제의 색상 모드를 따릅니다. 선택은 현재 브라우저에 저장되어 새로고침 후에도 유지되며, 점검 설정과 서버 동작에는 영향을 주지 않습니다.

존 제목 또는 화살표를 누르면 해당 존의 레코드 목록을 접고 펼칩니다. 상단 `모두 접기 / 모두 펼치기`로 전체 존을 한 번에 전환할 수 있습니다. 접힌 존에도 전체 레코드 수와 상태별 개수가 표시되며 계속 갱신됩니다. 접기 상태는 브라우저에 저장됩니다. 목록을 접어도 서버 점검과 history 수집은 계속됩니다.

도메인 클릭은 도메인 설정을, 레코드 클릭은 해당 파일의 전체 레코드 관리 창을 엽니다. 레코드 목록에서 다른 항목을 선택하거나 빈 레코드를 추가할 수 있습니다. 체크박스로 도메인·레코드를 선택해 일괄 설정합니다. `상속값으로 복원` 버튼은 입력값을 비우고 **저장**할 때 적용합니다.

도메인·레코드의 마우스오버는 tip을 표시하고, 레코드에는 실제 요청 호스트와 적용 설정을 함께 표시합니다. 각 `v/X`에 시각·프로토콜·상태코드·응답 시간이 있습니다. 화면은 2초마다 읽기 전용 상태 API를 조회합니다.

| API | 동작 |
| --- | --- |
| `GET /api/zones` | 관리 설정·전체 레코드·effective 상속 적용값 |
| `PATCH /api/zones/{id}` | 도메인 설정 수정 |
| `GET /api/zones/{id}/records` | 레코드 목록 |
| `POST /api/zones/{id}/records` | 수동 레코드 추가, `ip` 필수 |
| `PATCH /api/zones/{id}/records/{record_id}` | 레코드 수정 |
| `POST /api/bulk-update` | 트랜잭션으로 일괄 수정 |
| `GET /api/status` | 메모리 상태·이력·스캔 오류·알림 전송 상태 |
| `POST /api/refresh` | 즉시 파일 재스캔, 본문 `{}` |
| `GET /healthz` | 프로세스 응답·마지막 스캔·스캔 오류 수 |

수정 API는 `Content-Type: application/json`을 요구합니다. 잘못된 값은 400, 없는 ID는 404, 중복 레코드·IP는 409입니다. 시간·protocol은 JSON `null`로 상속을 지정할 수 있습니다. 레코드 enabled만 `null`을 허용하며 도메인은 0/1입니다. 레코드 `maintenance`는 `true/false`만 허용하고 `alerted_at` 수정은 거부합니다.

```json
{"operations":[
  {"zone_id":1,"patch":{"interval":30}},
  {"zone_id":1,"record_id":2,"patch":{"enabled":0}},
  {"zone_id":1,"record_id":3,"patch":{"interval":null}}
]}
```

## Linux systemd

프로젝트를 `/opt/pulsezone`에 두고 해당 위치에 가상환경과 `.env`를 준비합니다. 예제 서비스는 `pulsezone` 계정을 사용하며 이 계정에 존 읽기 권한과 `/opt/pulsezone/data` 쓰기 권한이 필요합니다.

```bash
sudo useradd --system --home-dir /opt/pulsezone --shell /usr/sbin/nologin pulsezone
sudo install -d -o pulsezone -g pulsezone /opt/pulsezone/data
sudo cp deploy/pulsezone.service /etc/systemd/system/pulsezone.service
sudo systemctl daemon-reload
sudo systemctl enable --now pulsezone
sudo systemctl status pulsezone
```

`deploy/pulsezone.service`의 설치 경로를 실제 위치에 맞추세요. DB 경로를 변경하면 `ReadWritePaths`도 변경해야 합니다. 서비스는 단일 프로세스로 실행하며, 같은 DB에 모니터를 여러 개 실행하면 중복 점검·알림이 발생합니다.

## Docker Compose

```bash
cp .env.example .env
# .env에서 ZONE_PATH를 호스트의 실제 존 디렉터리로 변경
docker compose up -d --build
docker compose logs --tail 30
```

호스트의 ZONE_PATH를 컨테이너 `/zones`에 읽기 전용으로 마운트합니다. 컨테이너 SQLite는 `pulsezone-data` 이름 볼륨에 저장되므로 `.env`의 DATABASE_PATH와는 별도입니다. 호스트 WEB_PORT를 내부 8080에 연결합니다. UID 10001 사용자가 존 파일과 하위 디렉터리를 읽을 수 있어야 합니다. 컨테이너 안의 127.0.0.1은 컨테이너 자신입니다.

기본 이미지는 Python 3.13-slim입니다. `PYTHON_VERSION=3.9 docker compose build`로 최소 버전 이미지도 빌드할 수 있습니다. 루트 파일시스템은 읽기 전용, `/tmp`는 tmpfs, 로그는 최대 1MB씩 2개로 제한합니다.

## 테스트 및 유지보수

```bash
python -m pip install -e '.[test]'
python -m pytest -q
```

테스트는 임시 SQLite와 로컬 HTTP/TLS 수신 서버를 사용하며 운영 주소나 알림 채널로 전송하지 않습니다. GitHub Actions에는 Python 3.9~3.14 테스트와 Linux 컨테이너 실행 검증이 있습니다. 브라우저 점검용 `python tests/ui_server.py`는 루프백 서버만 사용하고 `http://127.0.0.1:18765`에서 실행됩니다. 테스트 데이터는 `output/playwright`에 생성됩니다.

SQLite는 WAL 모드를 사용하며 설정·존 구성 변화, 실제 알림 발생 시각, maintenance 자동 해제에만 기록합니다. 알림 없는 점검 결과와 무변경 재스캔·상태 조회는 DB 쓰기를 발생시키지 않습니다. DB 백업은 프로세스를 중지한 후 DB 및 남아 있는 WAL을 함께 보존하거나 SQLite backup API를 사용하세요. 점검 이력·장애 카운터·알림 큐는 백업 대상이 아닙니다.
