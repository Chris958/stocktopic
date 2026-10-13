# Diagnose dashboard latency in the running service

The October 13 production trace attributed 32.046s to list_themes, 4.001s to
fund-flow attachment, and 18.960s to event-loop resume after an 88ms health query.
The default health executor queue was only 0.653ms; this is not evidence of thread
pool saturation. Middleware overlap peak was one, so that particular request was
not proof of two concurrent dashboard requests.

The follow-up fix keeps the complete dashboard JSON, all historical/rejected
records, admission rules and AI scheduling, with no database migration:

- Scheduler retry checks use only pending/watching ID, admission state and review
  time in a background thread. The old scheduler hydrated every theme on the event
  loop during its retry checks, a concrete path capable of delaying health resume.
  Retry intervals and admission decisions remain unchanged.
- Run dashboard query assembly and JSON encoding in one dedicated worker instead
  of synchronous work on the event loop. Serialize heavy dashboard reads to avoid
  multiplying database contention. `/api/v1/themes` also offloads its list read.
- Share board context by unique stock codes and query the unindexed global
  MAX(anomaly_events.trade_date) once per list, rather than once per theme.
- Load fund histories with one connection and bounded 400-owner batches; preserve
  latest close/intraday semantics and missing values.
- Keep follower counts and leader ordering local to each theme even though input
  context is shared across themes.
- Main-page refreshes share one in-flight Promise, and the intelligence panel
  consumes the authenticated result instead of refetching dashboard.
- Keep stage timing and request IDs for production before/after comparison.

The full roughly 8.8MiB response remains; this change does not promise a particular
production latency or remove histories from the UI. Other background synchronous
work and SQLite writer contention can still cause delay; inspect the new timings.

Each dashboard request produces one INFO `dashboard_timing` JSON log in
`logs/stocktopic.err.log`, including failed and unauthorized requests. Records contain
only server-generated request ID, numeric timing/count/size data, HTTP status and
exception **type**. No credentials, request headers, query strings, SQL parameters,
error messages or response contents are logged. Other endpoints are not instrumented.
Concurrent requests have separate trackers and an overlap peak count. No database
write or network request is added. Logging uses the existing application log level;
ensure `LOG_LEVEL=INFO` if your configuration suppresses INFO records.

## Timing fields

All durations are milliseconds, measured with a monotonic clock.

| Field | Meaning |
| --- | --- |
| pre_handler | Application ASGI entry to FastAPI handler; includes authentication and dispatch |
| flow_context | Display-date lookup |
| list_themes | Existing complete theme/member query |
| attach_fund_flows | Existing theme/member money-flow attachment |
| health_exec | Actual health query inside the dashboard worker |
| dashboard_queue | Submission until the dedicated dashboard worker starts |
| dashboard_exec | Query assembly and JSON encoding inside the worker |
| dashboard_resume | Worker finishes until the event loop resumes the endpoint |
| dashboard_total | Queue + execution + event-loop resume |
| jsonable_encoder | FastAPI recursive conversion inside the worker |
| json_response | JSON response encoding inside the worker |
| alerts | Alert query |
| backtest | Test-pool dashboard query |
| endpoint_total | Complete endpoint execution |
| framework_response | Endpoint exits to framework response ready; includes FastAPI encoding/response construction |
| headers_ready | Application entry to ASGI response headers ready |
| app_total | Application entry through ASGI application completion; can include send backpressure |

`dashboard_total` contains queue, worker execution and resume; `endpoint_total` includes stages;
`headers_ready` includes dispatch, endpoint and response creation. Do not sum these
nested timings. `thread_count` is process-wide and does **not** establish executor
saturation by itself. `active_peak` counts overlapping dashboard ASGI requests in
this one process. `response_bytes` counts emitted body bytes.

These measurements cannot observe TCP/Uvicorn waiting **before application entry**
or time spent downloading after ASGI sends. If curl time is much larger than
`headers_ready`, compare server ingress/event-loop scheduling and network timing;
do not label that gap as a measured middleware queue.

The response has `X-StockTopic-Request-ID`. Successful dashboard responses also
have `Server-Timing`, containing phases available before response headers. Browser
Network can correlate this ID with the log. Authentication/CSRF/security headers
and the JSON body remain unchanged. Error responses may lack the timing header.

## Deploy the diagnostic PR branch before merge

On the Mac mini (existing repository):

```bash
cd /Users/chris/stocktopic
git fetch origin
git switch --track origin/diag/dashboard-latency
bash ./scripts/install_macos.sh
bash ./scripts/doctor.sh
```

If that local branch already exists, use `git switch diag/dashboard-latency` and
`git pull --ff-only origin diag/dashboard-latency` instead. No reset/stash is needed.
After restarting, close browser pages and let the startup backfill finish before
comparing timings. Do not manually duplicate backfill jobs.

In one terminal, watch only diagnostic lines:

```bash
cd /Users/chris/stocktopic
tail -f logs/stocktopic.err.log | rg --line-buffered 'dashboard_timing'
```

`rg` is optional; macOS's built-in alternative is:

```bash
tail -f logs/stocktopic.err.log | awk '/dashboard_timing/ { print; fflush() }'
```

In a second terminal, use the existing successful local basic login. curl prompts
for the password; do not put it on the command line:

```bash
curl -sS --user chris -o /dev/null --max-time 90 \
  -w 'HTTP=%{http_code} first_byte=%{time_starttransfer}s total=%{time_total}s bytes=%{size_download}\n' \
  http://127.0.0.1:8765/api/v1/dashboard
```

The timing record is emitted after response completion. Share those diagnostic
lines plus curl output. Keep cookies, `.env` and tokens private.

## Interpretation

- Large `list_themes`/`attach_fund_flows`: query costs, DB lock waits or contention.
- Large `dashboard_queue`: preceding dashboard requests occupy the single worker.
- Large `dashboard_resume`: event loop was delayed after the worker finished.
- Earlier diagnostic-only builds used `health_queue/health_resume/health_total`;
  the new build runs health inside the dedicated dashboard worker instead.
- Large `pre_handler`: application auth/dispatch scheduling delayed the handler.
- Large `framework_response`: encoding/response creation or scheduling inside it.
- Large overlap peak: multiple requests are live together, not necessarily all
  actively executing. Correlate IDs and frontend requests.

Validate the fix with the same local curl and timings before claiming speedup.
Regression tests verify HTTP health stays responsive during a blocked dashboard
query, SQL batching/equivalence, frontend request sharing and all original tests. To return to merged main, switch to main, pull with
`--ff-only`, then run the existing installation script again.
