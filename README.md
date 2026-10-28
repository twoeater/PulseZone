# PulseZone

**English** | [한국어](README.ko.md)

PulseZone reads BIND DNS zone files and performs HTTP/HTTPS health checks against **each A record and IP address**. It runs on Python 3.9+ and provides a web interface for managing domain and record settings. Configuration is stored in SQLite; check history and outage state stay in memory to minimize disk writes.

The web interface and generated alert messages currently use Korean labels.

## Quick start

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
cp .env.example .env
```

Set `ZONE_PATH` in `.env` to your zone directory, then start the application:

```bash
python -m pulsezone
# To use a different environment file:
python -m pulsezone --env-file /etc/pulsezone.env
```

Open `http://localhost:8080` when running locally, or use your server's address. The default bind address is `0.0.0.0:8080`. **There is no application login**; deploy on an internal network or behind a proxy that controls access. No frontend build or CDN is required.

On Windows:

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install .
Copy-Item .env.example .env
.\.venv\Scripts\python -m pulsezone
```

Sample zones are in [examples/zones](examples/zones). Their A records use loopback addresses and will fail checks unless a web server is listening. Set `ZONE_PATH=./examples/zones` to try zone parsing and the dashboard.

## Environment configuration

Process environment variables take precedence over `.env`. Environment changes require a restart; settings saved through the web interface take effect immediately.

| Variable | Default | Description |
| --- | --- | --- |
| `ZONE_PATH` | `./zones` | Zone root, scanned recursively |
| `ZONE_REFRESH_TIME` | `3600` | Seconds between zone scans |
| `CHECK_INTERVAL` | `10` | Delay in seconds after a successful check |
| `TIMEOUT` | `10` | Request timeout in seconds, per protocol |
| `RETRY` | `10` | Consecutive failed checks before an alert, including the first attempt |
| `RETRY_DELAY` | `10` | Delay in seconds after a failed check |
| `CHECK_PROTOCOL` | `auto` | `auto`, `http`, or `https` |
| `HTTPS_INSECURE` | `false` | Skip HTTPS certificate verification, like curl `-k`; `true` or `false` |
| `HTTP_ALLOW_2XX` | `true` | Treat HTTP 200–299 as healthy |
| `HTTP_ALLOW_3XX` | `true` | Treat HTTP 300–399 as healthy |
| `HTTP_ALLOW_4XX` | `false` | Treat HTTP 400–499 as healthy |
| `HTTP_ALLOW_5XX` | `false` | Treat HTTP 500–599 as healthy |
| `MAINTENANCE` | `false` | Global default for suppressing additional alerts on the same calendar day; `true` or `false` |
| `DATABASE_PATH` | `./data/pulsezone.sqlite3` | SQLite configuration database |
| `WEB_HOST` | `0.0.0.0` | Web server bind address |
| `WEB_PORT` | `8080` | Web server port |
| `MAX_CONCURRENCY` | `100` | Maximum concurrent checks |
| `HISTORY_SIZE` | `60` | Number of recent results retained in memory per record/IP |
| `ALERT_SLACK` | Empty | Slack incoming webhook URL |
| `ALERT_DISCORD` | Empty | Discord webhook URL |
| `ALERT_NTFY` | Empty | Full topic URL, such as `https://ntfy.sh/topic` |
| `ALERT_GOTIFY` | Empty | Full publish URL, such as `https://gotify.example/message` |
| `ALERT_CUSTOM` | Empty | Generic JSON POST webhook URL |
| `ALERT_NTFY_TOKEN` | Empty | No authentication when empty; `username:password` if it contains `:`, otherwise an access token |
| `ALERT_GOTIFY_TOKEN` | Empty | Optional Gotify application token |

Time values must be positive and may be fractional. `RETRY` must be an integer of at least 1. Checks can wait longer when the concurrency limit is reached.

## Zone files and inheritance

- SQLite contains a `zones` management table and **one record table per zone file**. For example, `a/db.example.com` maps to `zone:a/db.example.com`. Identical filenames in different directories remain separate.
- `id` is the internal index. Filenames and relative paths are read-only; display names, connection domains, descriptions, tooltips, and check settings are editable.
- A new zone's display name and connection `domain` initially equal **the complete filename**. For `db.example.com`, the initial HTTP Host is also `db.example.com`. Change the domain to `example.com` in the editor if that is the intended service hostname.
- The parsing origin is selected from an initial `$ORIGIN`, an absolute SOA owner name, or the filename with its `db.` prefix removed, in that order. Later `$ORIGIN` changes are supported. The parsing origin does not change the configured connection domain.
- Apex records use `@` and request the configured domain without an `@` prefix. A manually added record with an empty name checks its IP directly and initially defaults to HTTP.
- Only A records become monitoring targets. PTR, AAAA, and CNAME records are not checked. Wildcard `*` labels are replaced with `pulsezone-probe` in request hostnames.
- The parser supports `$TTL`, omitted owner names, multiple IPs per owner, parentheses, comments, absolute names, and `$GENERATE`. A-record fragments without SOA/NS or TTL declarations are accepted; an omitted TTL uses 300 seconds for parsing only.
- `$INCLUDE` paths are relative to **the including file's directory** and must remain within the zone root. Origin, owner, and TTL context is inherited and restored. Cycles and nesting at the 32-level limit are reported as errors.
- UTF-8 and UTF-8 BOM files are supported. Regular files are scanned regardless of extension, so an include fragment can also be registered independently if it parses on its own. Symbolic links are excluded from the scan.
- Settings resolve in this order: **record → zone → environment**. NULL/empty timing, protocol, `https_insecure`, `http_allow_2xx`/`3xx`/`4xx`/`5xx`, and `maintenance` values inherit from their parent. A disabled domain stops all its records, including records explicitly enabled individually.

### Rescanning

Aliases, intervals, descriptions, and other settings are preserved for each `(record name, IP)` pair. When a source record disappears, it becomes `MISSING` and stops checking, while its settings remain intact. It resumes with the saved settings if it returns. A new filename or relative path creates a separate zone.

A parse error retains the last successfully registered contents of that file. A failed directory scan is not interpreted as deletion and does not apply synchronization for that scan. Errors appear on the dashboard.

Editing a record name or IP in the web interface converts that row into a **manual record**. If the original entry remains in the zone file, the next scan registers it again. Editing only an alias, description, or check setting preserves the source link. Manual records survive source-file removal, but can still be stopped by disabling their domain. Duplicate record/IP pairs are not created. Source zone files are never modified.

## HTTP checks and alerts

Each check connects directly to the row's IP while using the configured hostname for HTTP Host and HTTPS SNI. Certificate chains, hostnames, and validity periods are verified by default. Connections are not shared between different target IPs, and system HTTP proxy environment variables are not used.

With `https_insecure=true`, verification is skipped as with curl `-k`. Expired, self-signed, or hostname-mismatched certificates can pass if the HTTPS response belongs to an allowed status class. Encryption, Host/SNI, and IP pinning remain in use. Connection failures, timeouts, and disallowed HTTP statuses still fail. In `auto` mode this setting affects the HTTPS attempt only; it does not affect HTTP checks or alert webhook TLS settings.

The environment default is `HTTPS_INSECURE=false`. Zone, record, and bulk editors offer inherit, `false` (verify), and `true` (skip verification). The API accepts `{"https_insecure": true}`, `false`, or `null`; `null` means inherit. Existing databases gain the column automatically at startup without losing settings.

Checks use `GET /`. **By default, HTTP 2xx and 3xx are healthy; 4xx and 5xx fail.** Each class can be allowed or rejected independently. Redirects are not followed and response bodies are not stored. Ports are fixed at HTTP 80 and HTTPS 443; custom ports and paths are not currently configurable.

`auto` tries HTTPS first and falls back to HTTP on failure. A success with either protocol counts as one successful check. An invalid HTTPS certificate can therefore still result in UP if HTTP succeeds; select `https` to require HTTPS to pass. With `TIMEOUT=10`, both attempts together can take about 20 seconds. The next interval starts after the check finishes.

With `RETRY=10`, outage alerts repeat on the 10th, 20th, 30th, and subsequent multiples of consecutive failures. The first success after DOWN sends one recovery alert. Failure counters reset on restart, so alerts can occur again once the threshold is reached. Switching to OFF or MISSING does not send a recovery alert. Maintenance can suppress these alerts as described below.

### Allowed HTTP status classes

Use `HTTP_ALLOW_2XX`, `HTTP_ALLOW_3XX`, `HTTP_ALLOW_4XX`, and `HTTP_ALLOW_5XX` for global defaults (`true`/`false`). Zone, record, and bulk editors provide an independent inherit/allow/fail selector for each class. API and SQLite field names are `http_allow_2xx`, `http_allow_3xx`, `http_allow_4xx`, and `http_allow_5xx`. The API accepts booleans or `null`; SQLite stores 0/1/NULL. Each setting inherits separately, and an explicit `false` overrides a parent `true`.

For example, this patch rejects redirects, permits 4xx responses, and inherits the other two classes:

```json
{"http_allow_2xx": null, "http_allow_3xx": false, "http_allow_4xx": true, "http_allow_5xx": null}
```

An allowed 404 or 503 counts as a successful check and retains its actual status code in history. A rejected 200 counts as a failure and uses the normal retry/alert rules. Setting all four to `false` makes every HTTP response fail; it does not disable checking. Responses outside 200–599 do not count as success. Connection errors, timeouts, and TLS verification failures still fail regardless of these switches. Alert webhook delivery rules are unaffected.

In `auto` mode, a disallowed HTTPS status triggers the HTTP fallback; an allowed HTTPS status completes the check immediately. For example, with 5xx allowed, HTTPS 503 is UP and HTTP is not attempted. Select `https` to require the HTTPS attempt itself to pass.

Existing databases gain four nullable columns on both zone and record tables at startup, preserving settings and the previous default behavior. Saving a change to a target's effective status policy restarts its check and resets its in-memory history and failure count so old results cannot overwrite the new policy. Changes that leave the effective policy unchanged preserve its history. Last-alert timestamps remain stored. Record tooltips show the effective allowed classes.

### Last alert and maintenance

The following columns are stored in each file's record table. `maintenance` is also available on the zone management table and defaults globally to `.env`'s `MAINTENANCE=false`. Existing databases are migrated automatically at startup, preserving source files and settings.

| Column | Default | Description |
| --- | --- | --- |
| `alerted_at` | `NULL` | Last outage or recovery alert trigger, stored as ISO 8601 TEXT with a timezone offset, e.g. `2026-10-28T14:30:00.000000+09:00`. Read-only through the API |
| `maintenance` | `NULL` | Suppress additional alerts on the same day. SQLite stores 0/1/NULL; the API uses boolean/null. NULL inherits from the parent |

Set `MAINTENANCE=true` to enable the global default. Resolution is **record → zone → environment**; an explicit child `false` overrides a parent `true`. Values from older versions, including record `true`/`false` settings and `alerted_at`, are preserved. To apply the global default to existing records, select them in the web interface and bulk-set maintenance to inherit. New zones and records inherit automatically.

`alerted_at` updates only when an alert meets its trigger conditions and is accepted into the delivery queue. It does not update for ordinary checks, reads, rescans, maintenance-suppressed alerts, an absent channel configuration, or a full queue. **It records the alert trigger, not successful delivery.** Channel delivery results are reported separately. All channels and their delivery retries share the same trigger timestamp.

If the effective `maintenance` value is `true` and the last alert's date is today, further **outage and recovery alerts** for that record are skipped. Checks, failure counts, and in-memory history continue. If there is no previous alert or its date differs, the normal retry/recovery rules can emit an alert and update `alerted_at`. Maintenance settings and the timestamp survive restarts.

Automatic release uses the **server's local timezone**. Configure the runtime timezone, such as `TZ=Asia/Seoul` for a container using Korean time. The rule is equivalent to:

```python
cutoff = now.replace(hour=13, minute=0, second=0, microsecond=0)
active = effective_maintenance(env, zone, record)
if active and record.alerted_at is not None:
    if now >= cutoff and record.alerted_at < cutoff:
        record.maintenance = False  # Persist an explicit record override.
        active = False
if active and record.alerted_at is not None and record.alerted_at.date() == now.date():
    return  # Skip this alert; keep the monitoring loop running.
```

An alert at 10:00 is eligible for release from 13:00 that day. An alert at 14:00 remains under maintenance for that day and becomes eligible at 13:00 the next day. A new alert after midnight but before 13:00 still becomes eligible at 13:00 the same day. `alerted_at=NULL` is excluded from automatic release.

The condition is checked before alerts, when settings are applied, at startup, and every 30 seconds. This also covers OFF/MISSING records and records with long check intervals. A server that was stopped at the cutoff applies the same rule when restarted.

Automatic release writes an explicit `false` to that record only. A parent zone or environment value of `true` cannot immediately re-enable it, and other records are unaffected. Restore the record to inherit to follow its parent again. If its alert timestamp still meets the release condition, it is immediately released again.

Zone, record, and mixed bulk editors support inherit/`true`/`false`. The record editor shows the last alert time and effective maintenance state; active maintenance is marked `MAINT` beside the check result. The restore-inheritance action includes maintenance.

### Alert messages and delivery

Messages include a record/domain display name, IP address, failure count, estimated and actual downtime, descriptions, and the error. An illustrative English translation of an outage message is:

```text
record-display-name.domain-display-name(IP)
No response (10 consecutive failures × retry_delay 10s = reference 100s / actual down 290.0s)
Domain description / record description
Cause: https: timeout (10s); http: timeout (10s)
```

Actual downtime starts at the beginning of the first failed request. Ten failures contain nine delays between checks, plus request time, so actual downtime differs from `failure count × retry_delay`.

Every configured alert channel receives the notification; empty URLs are skipped. Custom webhooks receive the same message in each JSON field: `text`, `content`, `body`, and `alert`. Gotify accepts a token in the publish URL's `?token=...` query; the token is sent through the `X-Gotify-Key` header. `ALERT_GOTIFY_TOKEN`, when set, takes precedence over the URL token.

ntfy sends **no authentication header** when `ALERT_NTFY_TOKEN` is empty. Otherwise it uses HTTP Basic authentication, equivalent to curl's `-u`. An access token can be supplied as the password with an empty username, as described in the [ntfy documentation](https://docs.ntfy.sh/publish/#access-tokens). Set the topic URL in `ALERT_NTFY` and use the single `ALERT_NTFY_TOKEN` field for authentication:

| Authentication | Environment value | Equivalent curl option |
| --- | --- | --- |
| Public topic | `ALERT_NTFY_TOKEN=` | No authentication option; omit `-u` |
| Username/password | `ALERT_NTFY_TOKEN=username:password` | `-u 'username:password'` |
| Access token | `ALERT_NTFY_TOKEN=tk_...` | `-u ':tk_...'` |

Only the first colon is used as the separator: `user:secret:part` means username `user` and password `secret:part`. A nonempty value without a colon is treated as a token. An empty value behaves like `curl -d "test" https://ntfy.example.com/public`.

```dotenv
ALERT_NTFY=https://ntfy.example.com/public
ALERT_NTFY_TOKEN=
```

Environment changes require a process restart. An empty topic URL disables ntfy delivery even if a token is present.

Delivery runs separately from monitoring. Each channel has a 10-second request timeout and up to three attempts for network errors, HTTP 429, or 5xx responses. Delivery failures and overflow of the 1,000-message queue are shown on the dashboard. The queue is in memory and does not survive shutdown. Retries after an uncertain response can result in duplicate delivery. Discord messages are limited to 2,000 characters and automatic mentions are disabled.

## Web interface and API

The theme selector supports light, dark, and system modes. The default follows the operating system. The choice is saved in the browser and does not affect monitoring settings.

Enabled domains appear before disabled domains. Within each group, the existing relative-path order is preserved. Saving individual or bulk on/off changes immediately reorders the list. A record's OFF state or a DOWN check result does not change the zone order.

Use the arrow on the left of a zone to collapse or expand its records, or use the collapse-all/expand-all button. Collapsed zones retain live record counts and status totals. Collapse state is saved in the browser; monitoring and history collection continue while a zone is collapsed.

Click a domain title to edit its settings. Click a record to open that file's record editor, where you can switch records or add a manual entry. Checkboxes select domains and records for bulk updates. The restore-inheritance button clears the relevant fields; click Save to apply the changes.

Domain and record hover text shows configured tips. Record tooltips also include the request hostname and effective settings. Hover over each `v/X` history entry to see the timestamp, protocol, status code, latency, and error. The dashboard polls the read-only status API every two seconds.

**Alert Test**, to the left of the theme selector, queues a message such as `UP 120 Down 30 OFF 1` to every configured alert channel. Counts come from the current server state across all zones, regardless of search filters or collapsed groups; RETRYING, UNKNOWN, and MISSING are not included in these three counts. This manual test bypasses maintenance and does not change record `alerted_at`, history, or settings. The confirmation means queued, not delivered; channel delivery failures appear on the dashboard. No configured channels or a full queue produces an error.

| API | Purpose |
| --- | --- |
| `GET /api/zones` | Settings, all records, and effective inherited values |
| `PATCH /api/zones/{id}` | Update zone settings |
| `GET /api/zones/{id}/records` | List records |
| `POST /api/zones/{id}/records` | Add a manual record; `ip` is required |
| `PATCH /api/zones/{id}/records/{record_id}` | Update a record |
| `POST /api/bulk-update` | Apply updates in one transaction |
| `GET /api/status` | In-memory state/history, scan errors, and alert delivery status |
| `POST /api/refresh` | Rescan zone files immediately; send `{}` |
| `POST /api/alert-test` | Queue the current UP/DOWN/OFF summary to all alert channels; send `{}`. Returns 202 when queued, 400 for no channels/invalid input, 503 for a full queue |
| `GET /healthz` | Process health, last scan time, and scan-error count |

Mutation requests require `Content-Type: application/json`. Invalid values return 400, unknown IDs return 404, and duplicate record/IP pairs return 409. Timing fields, protocol, `https_insecure`, the four `http_allow_*` fields, and `maintenance` accept JSON `null` for inheritance. Record `enabled` accepts 0/1/null; zone `enabled` accepts only 0/1. Zone and record `https_insecure`, `http_allow_*`, and `maintenance` accept `true`/`false`/`null`. `alerted_at` cannot be edited through the API.

```json
{"operations":[
  {"zone_id":1,"patch":{"interval":30}},
  {"zone_id":1,"record_id":2,"patch":{"enabled":0}},
  {"zone_id":1,"record_id":3,"patch":{"interval":null}}
]}
```

## Linux systemd

Place the project in `/opt/pulsezone` with its virtual environment and `.env` file. The example service runs as `pulsezone`, which needs read access to the zone files and write access to `/opt/pulsezone/data`.

```bash
sudo useradd --system --home-dir /opt/pulsezone --shell /usr/sbin/nologin pulsezone
sudo install -d -o pulsezone -g pulsezone /opt/pulsezone/data
sudo cp deploy/pulsezone.service /etc/systemd/system/pulsezone.service
sudo systemctl daemon-reload
sudo systemctl enable --now pulsezone
sudo systemctl status pulsezone
```

Adjust the paths in [deploy/pulsezone.service](deploy/pulsezone.service) for your installation. If the database location changes, update `ReadWritePaths` too. Run a single monitoring process per database; multiple processes using the same database cause duplicate checks and alerts.

## Docker Compose

```bash
cp .env.example .env
# Set ZONE_PATH in .env to the zone directory on the host.
docker compose up -d --build
docker compose logs --tail 30
```

The host's `ZONE_PATH` is mounted read-only at `/zones`. SQLite uses the persistent `pulsezone-data` volume, independently of `.env`'s `DATABASE_PATH`. The host's `WEB_PORT` maps to port 8080 in the container. UID 10001 must be able to read the zone directory and files. Inside the container, `127.0.0.1` refers to the container itself.

The default image is Python 3.13-slim. Build for the minimum supported version with `PYTHON_VERSION=3.9 docker compose build`. The root filesystem is read-only, `/tmp` uses tmpfs, and container logs are limited to two files of 1 MB each.

## Testing and maintenance

```bash
python -m pip install -e '.[test]'
python -m pytest -q
```

Tests use temporary SQLite databases and local HTTP/TLS receivers, without sending requests to production hosts or alert channels. GitHub Actions defines a Python 3.9–3.14 test matrix and a Linux container check. For local browser QA, run `python tests/ui_server.py`; it uses loopback targets and serves the dashboard at `http://127.0.0.1:18765`. QA data is stored under `output/playwright`.

SQLite uses WAL mode and writes only for configuration or zone changes, accepted alert trigger timestamps, and automatic maintenance release. Ordinary checks without alerts, unchanged rescans, and status reads do not write to the database. Back up using SQLite's backup API, or stop the process and preserve the database along with any remaining WAL file. Check history, failure counters, and the alert queue are not persisted.
