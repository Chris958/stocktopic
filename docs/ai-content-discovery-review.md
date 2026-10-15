# AI short-drama / IP discovery review — 2026-10-15

## Evidence boundary

Public `/health` at 10:00:30 China time reported successful 4,413-stock collection,
no integration warnings, and zero new candidates in the Oct 14 end-of-day replay
for Oct 14 and Oct 13. This does not prove any particular stock was in strong input,
which filter rejected it, or whether an older candidate already existed.

Production authenticated candidate/trace data is not available in this workspace.
Do not attribute Oct 9/12/13/14's specific miss to one gate until exporting it.
The diagnostic module only reads SQLite in mode=ro/query_only, never re-runs AI or
admission, and keeps persisted past decisions separate from current reconstruction.
KPL event/tag updates are mutable: a reconstruction from current rows is not a
point-in-time market replay. Later news must not be used as earlier evidence.

## Confirmed code gaps (reproduced with synthetic inputs)

1. EventBridgeDetector only accepts nodes with two/three members; a four-member
   concrete node cannot bridge with a two/three-member sibling even if its own
   formal AI verification rejects a too-broad explanation.
2. Clusters are built with >=2 members. A 3+1 structure never reaches bridge AI.
3. Bridge nodes must be disjoint; overlapping concrete roles fail despite >=4
   **unique** stocks. Existing overlap family logic covers some, not all, overlaps.
4. Time score >=45 is a separate hard gate. With the 45-minute spread formula it
   requires all launch times within 24.75 minutes. A 30-minute relay sample scores
   65.75 overall and still fails.
5. Pairwise Chinese-bigram/Latin-token similarity does not encode complementary
   business roles. AIGC production and literary-IP adaptation can be causally
   connected while sharing no tokens. Every pair in a multi-node candidate must
   pass, further limiting supply-chain recall.
6. kpl_concept_memberships stores description, but limit_touch_events selects only
   tag/source/confidence. Concept reasons are not forwarded to scoring; the graph
   concept-tag branch also discards reasons. The graph reason class is present,
   but its enrich_events method is not wired into this discovery read path.
7. Pure local two/three-stock observations persist as pending/early_observation.
   The default frontend watching filter only selects status=watching. Such records
   are hidden in the reviewing tab, lack a specific early_observation label, and
   do not call the existing post-admission early-watch notification path.
8. Successful negative AI results can be cached indefinitely under unchanged market
   signatures, even after catalyst evidence changes. Concept descriptions are
   absent from the signature too. Failed requests retry, but valid negative
   decisions are not the same as failed requests.
9. Current trace exclusion text groups multiple gates; single-node verifications
   do not preserve per-anchor rejected evidence. Current metadata/run rows also
   overwrite signatures on retry, so they are not a complete historical audit.

Existing admission already supports new_theme/new_cycle/new_catalyst/old_rotation.
Changing the prompt to mention new_catalyst again alone will not repair missing
input evidence or the upstream candidate universe.

## Correct business model for diagnosis

Relevant concrete hypothesis: AI-assisted audiovisual content production and IP
adaptation commercialization, with upstream IP/script rights, production/tools,
and distribution roles. Broad media/publishing labels alone do not establish a
specific event or membership. A rights holder is not automatically an AI-drama
beneficiary without its business/evidence connection. Canonical event identity,
causal role and company exposure must be distinct fields.

Prior information (company platform, copyright assets) supports membership; the
current market starts the detector. Current platform/product/overseas growth data
may verify a new catalyst. Small current revenue is a risk/exposure fact, not proof
that market resonance did not happen. Formal admission evidence gates still apply.

Sources reviewed:

- [Ciwen abnormal fluctuation announcement](https://static.cninfo.com.cn/finalpage/2026-10-13/1225602370.PDF):
  Oct 9/12 price deviation and the fact AIGC had not produced revenue as disclosed.
- [Ciwen platform announcement coverage, Sept 27](https://stcn.com/article/detail/4199845.html):
  script-to-film platform and IP development, available before Oct 9.
- [People's Daily, March 23](https://paper.people.com.cn/rmrbhwb/pc/content/202603/23/content_30146543.html):
  adaptation rights, platform incentives and IP monetization as pre-existing context.
- [Xinhua on AI comics](https://app.xinhuanet.com/news/article.html?articleId=bfd0572519e04e5917f61a606721f04e):
  existing reading-platform IP pools and distribution models.
- [Rongxin company-news coverage, Oct 13 evening](https://static.weeklyonstock.com/26/1013/qdy200839.html):
  a book-to-AI-animation adaptation example. This report cannot substantiate Oct 9
  detection using information not yet public then.
- [Oct 12 09:32 market report](https://www.caiwennews.com/article/1645163.shtml):
  contemporaneous market cluster; not a replacement for per-stock recorded input.

The user's “初版传媒” name is unresolved; don't silently equate it with 出版传媒,
新华文轩, 新经典 or 中信出版. Export requested names and report unresolved ones.

## Proposed repair sequence (not applied to runtime in this diagnostic PR)

1. Export the actual first-wave dates once. Establish for each stock/node whether
   failure was before strong-input, graph normalization, bridge, AI verification,
   admission, UI or notification. Keep API failures distinct from rejected logic.
2. Repair observation visibility and a deduplicated local-observation notification
   path. Two/three strong members still use only deterministic local logic and no
   AI. Explicitly distinguish this level from post-admission evidence watch.
3. Carry source descriptions/reasons, source date and knowledge validity through
   graph construction, scoring, verifier payload, signature and trace.
4. Add versioned local event/role relations with evidence and observed_at. Normalize
   AI comics/drama terminology cautiously; “publishing” alone never qualifies.
   A 3+1 industry split can form one locally evidenced concrete event node with
   four members, rather than violating the bridge's >=2-per-node rule. Three/one
   isolated stocks without that evidence stay outside formal AI.
5. For bridges, remove the **upper** three-member eligibility ceiling and dedupe
   overlaps by unique stock count, retaining >=2 per accepted node, >=4 total,
   <=4 nodes, bounded members and all original AI/admission safety gates. Extend
   the verifier and regression fixtures together; detector changes alone are not
   sufficient. Overlap cannot double-count market breadth.
6. Replace the accidental ~25-minute all-stock cutoff with separately measured
   simultaneous and relay patterns, with explicit bounded windows, board strength,
   validated role edges and causal corroboration. Calibrate on prior data and
   unrelated synchronized sectors; do not simply lower thresholds to fit this case.
7. Preserve market observations when formal verification/admission rejects. For
   genuinely new evidence, retry only existing >=4 candidates under a bounded
   budget/backoff and evidence-aware cache; never invoke AI for 2–3 observations.
8. Add coverage accounting: every strong input has a visible disposition. Missing
   graph evidence, lack of catalyst or unavailable AI creates a pending explanation,
   not a silently empty interface. Use existing metadata/service_runs initially.

Acceptance is first_seen -> observation_visible -> alert_created -> notification
result -> formal_candidate -> admission, each timestamped with input/rule version.
Use true historical inputs at 5-minute intervals where available. Do not reconstruct
an earlier trade opportunity using end-of-day tags/news/prices. Zero omissions
cannot be promised; visible reasons and bounded review of uncovered signals can.

## Read-only production export without changing the running branch

From the Mac mini repository, fetch only the audit branch, then execute the reviewed
module from that ref in a separate Python process. This does not checkout files,
restart the service, write to SQLite, ingest data, call AI or send notifications.

```bash
cd /Users/chris/stocktopic
git fetch origin diag/ai-content-discovery
.venv/bin/python - \
  --dates 20261009 20261012 20261013 20261014 20261015 \
  --names 慈文传媒 欢瑞世纪 掌阅科技 荣信文化 出版传媒 初版传媒 \
  > ai-content-discovery-audit.json <<'PY'
import subprocess
source = subprocess.check_output([
    'git', 'show',
    'origin/diag/ai-content-discovery:src/stocktopic/discovery_audit.py',
], text=True)
exec(compile(source, 'discovery_audit.py', 'exec'),
     {'__name__': '__main__', '__package__': 'stocktopic'})
PY
```

Share ai-content-discovery-audit.json. It omits credentials and raw AI responses;
it includes target business descriptions, candidate names/reasons and status.
Related theme records include earlier themes within 120 calendar days and show
current state; they are not historical per-date snapshots. The current exported
UI visibility flag follows the frontend filter at this
review's version; it must be updated if UI filtering changes later. Defaults cap
trace/runs at 64 per day and reviews at 3 per theme; a cap flag indicates truncation.
