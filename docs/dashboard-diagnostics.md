# Diagnose dashboard latency in the running service

This instrumentation compares the **real HTTP request** with standalone profiling.
It does not change dashboard membership, filters, queries, serialization, AI calls,
scheduling or database schema. In particular it intentionally preserves synchronous
queries so their existing blocking behavior can be diagnosed before optimizing it.

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
| health_queue | Submission until default executor worker starts |
| health_exec | Actual health function inside the worker |
| health_resume | Worker finishes until await resumes on the event loop |
| health_total | Entire health await, including queue, execution and resume |
| alerts | Alert query |
| backtest | Test-pool dashboard query |
| endpoint_total | Complete endpoint execution |
| framework_response | Endpoint exits to framework response ready; includes FastAPI encoding/response construction |
| headers_ready | Application entry to ASGI response headers ready |
| app_total | Application entry through ASGI application completion; can include send backpressure |

`health_total` contains the three health parts; `endpoint_total` includes stages;
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
- Large `health_queue` with small `health_exec`: investigate shared executor jobs.
- Large `health_resume`: event loop was delayed after the worker finished.
- Large `pre_handler`: application auth/dispatch scheduling delayed the handler.
- Large `framework_response`: encoding/response creation or scheduling inside it.
- Large overlap peak: multiple requests are live together, not necessarily all
  actively executing. Correlate IDs and frontend requests.

This patch establishes attribution; it does not claim to fix the 30–40s delay.
Use the recorded stages to choose subsequent batch-query, threading, payload and
frontend-request fixes. To return to merged main, switch to main, pull with
`--ff-only`, then run the existing installation script again.
