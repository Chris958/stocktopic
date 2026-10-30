# Event Bridge P0

Formal pool identity now follows [the direction-pool policy](direction-pool.md):
robotics is the direction; a Tesla product event is an attached catalyst. The bridge
still validates fixed local nodes and members. New directions retain novelty gates;
existing/archived directions may continue or reactivate without a novelty-only veto,
while retaining four-stock, relationship, evidence and persistence requirements.

## Problem and scope

The old graph verifier ran when the **whole market** supplied four strong stocks,
even if each concrete local node had only two or three. Its output replaced local
early observations, including when it returned an empty list. Family clustering
requires semantic/name agreement corroborated by overlap (including 猪肉/生猪/猪周期), but not
disjoint sub-industry nodes connected by the same new event.

This patch keeps Market First, News Second. It adds no Level-2 signals, success-rate
model, role migration, spread acceleration, trading plan or database migration.
No real API calls are used in regression tests.

## Flow

1. Existing structured/family clustering fixes the candidate universe locally.
2. Local two/three-stock nodes remain early observations without AI. They survive
   empty AI results, cached results and verification outages. Storage/UI/alerts
   are updated before research starts, without downgrading existing formal candidates.
3. Existing single-node >=4 graph verification remains in place; output members
   are constrained to that anchor's member list. Parent-only 文化传媒/传媒 do not
   enter that verifier. Existing other-parent verification behavior is retained.
4. EventBridgeDetector evaluates concrete nodes with >=2 members, including >=4
   nodes and overlaps counted by unique code. Parent taxonomies remain context.
   Every pair must pass bridge_score>=60 and an evidence route described below.
   A third node cannot join through just one good neighbor.
5. Bridges have 2–4 nodes, >=4 unique members, >=2 per node and <=15 members. At most eight
   bridge candidates are verified per pass, with at most three concurrent requests.
   Results retain deterministic local-score order. This is a bounded P0 cap;
   overlapping hypotheses may exist and the AI can reject or narrow them. Verified
   hypotheses sharing an anchor retain the highest locally scored result; later
   duplicates are traced as superseded rather than overwriting the same Theme ID.
6. The existing gpt-5.6-sol semantic task route verifies only the fixed payload,
   with web_search allowed to confirm common catalysts. Decisions are merge,
   partial_merge or reject. Invalid/expanded members, <4 members, <2 per accepted
   node, broad names, missing per-member reasons or missing catalysts with an actual search URL fail closed.
7. An accepted event_bridge goes through ThemeDiscovery's >=4 formal_candidate
   gate and **admission_analysis**, including evidence and persistence gates.
   New directions retain novelty gates; an existing direction's continuation or
   reactivation records the real novelty mode without a novelty-only veto.

## Deterministic score and identity

Weights are time .35, market strength .25, reason-text similarity .25, graph
relation .15. Missing components are omitted and remaining weights normalized.
The normal route requires time_sync>=45 and text_similarity>=25. Restricted semantic
review can instead use two substantive business reasons per node and either:

- Observed shared context, known synchronization, and complementary AI-content
  production / IP-rights roles. An industry label alone does not establish a role.
- Strong shared text (>=60) and a measured launch span <=60 minutes for relay review.
- Strong shared text (>=60) plus shared context with unknown launch times.

Every route retains bridge_score>=60 and all breadth/whitelist/admission gates.
Unknown time stays null; a cumulative-high first snapshot cannot fabricate a
launch or synchronization score. Review hypotheses are not locally approved themes.
Time uses the observed launch-time spread (45-minute scale). Market strength uses
existing cluster_metrics board quality and penalizes differences across nodes.
Text similarity uses Chinese bigrams/Latin tokens from limit_reason and graph/KPL
member_reasons, removing common boilerplate. Graph context counts only observed
shared parent_tags; no historical relationship is fabricated.

Anchor priority: earliest known launch, largest member list, highest graph weight
(or cluster confidence), lexical tag as final deterministic tie-breaker. A partial
uses only accepted nodes when selecting the anchor. AI cannot rename the anchor.
The existing event-cluster-v4|trade_date|anchor fingerprint remains unchanged;
canonical_name changes do not create an extra same-day Theme ID. The existing
admission/history policy handles cross-day novelty; daily fingerprints still
include trade_date, as before.

Cache inputs now include launch times, graph reasons/weights, and board quality
for bounded bridge nodes. Signature v3 prevents reuse of older coverage decisions.
Single-node board changes outside bridge scope retain the old cache behavior.
Verifier failures are marked failed, so the existing 30-minute retry cooldown
applies while local early observations remain available.

Successful negative results are reconsidered after 30 minutes during the same
trading date's sessions, with at most eight extra reviews per input signature.
Only existing >=4 candidates qualify. When some nodes already passed, only traced
rejected anchors/bridges are resubmitted; accepted clusters retain their previous
evidence rather than being described as freshly reverified. A fully successful
cache is not polled on a timer. Historical replays do not trigger timed rereview.

## Decision trace (existing metadata table)

- discovery_trace_latest:{trade_date}: current input signature.
- discovery_trace:{trade_date}:{signature}: strong-stock count, local nodes and
  touch counts, candidate count, pair scores/components/available weights and
  exclusion reasons, AI decision, accepted nodes/confidence, canonical name,
  final anchor and verification failure/rejection reason.
- discovery_node_trace:{trade_date}:{anchor_tag}: actual ThemeDiscovery stage,
  touch count, bridge trace and synchronization rejection reason.
- admission_trace:{theme_id}: admission result, novelty_mode, gate checks and
  final rejection reason. Existing admission reviews retain detailed evidence.
- discovery_review_history:{date}:{signature}:{count}: prior trace before each
  negative recheck. discovery_negative_reviews:{date}:{signature} tracks the count;
  service_runs.discovery_ai_recheck records completion/failure. Current traces
  identify the review scope, retained cache entries and semantic-review route.

To diagnose a date, read its latest signature and corresponding discovery trace,
then node traces and each created theme's admission trace/review. This distinguishes
no concrete nodes, insufficient local bridge evidence, AI rejection, discovery
noise and admission rejection. No new schema is required for P0. The follow-up
[AI-content omission repair](ai-content-discovery-review.md) also makes local
observations visible, adds deduplicated notifications, restores business reasons,
and provides a deterministic official-price main-board input fallback.

Example read-only query using the existing SQLite database (substitute date):

```sql
SELECT value FROM metadata WHERE key = 'discovery_trace_latest:20261009';
SELECT key, value FROM metadata
WHERE key LIKE 'discovery_trace:20261009:%'
   OR key LIKE 'discovery_node_trace:20261009:%';
SELECT key, value FROM metadata WHERE key LIKE 'admission_trace:%';
```

## Validation and limits

Regression coverage includes A–G bridge scenarios, existing pork family tests,
existing no-AI early layer tests and existing >=4 graph tests, plus whitelist,
missing data, bounded workload, deterministic anchors, same-day identity, cache
and timeout traces. Ruff, full pytest and static JavaScript syntax are CI gates.

Conservative text/time gates can miss genuine events with sparse reasons or late
launches. Daily source times may be unavailable: this is explicitly recorded and
not guessed. Bridge AI confidence and news still do not bypass admission evidence.
Metadata grows with signatures; retention policy is unchanged in this P0 patch.
Historical production attribution for October 9/12 requires their stored market
inputs/replay; this code fix alone is not proof of that day's specific rejection.

Ruff, full pytest, JavaScript node --check and git diff --check are validation gates.
