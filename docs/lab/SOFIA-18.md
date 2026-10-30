# Iran ↔ Sofia mother-local: 18 functional tests

## Outcome
18/18 bounded integrity gates passed: three sequential repeats of the same six cases. Total 90 verified HTTP responses, 168MiB receiver TCP payload, 15000/15000 unique UDP echo replies. No UDP missing/corrupt/duplicate replies in these finite phases. Not a capacity or production-readiness claim.

## What changed
- DV-Lab preview.8 mother-local migration was completed by the operator. Real heartbeat, capsule execution and TUN were verified before traffic tests.
- DV-Lab main commit: `e7554ae08d6313f63b719ffe441433c75e7f585d` (PR#1).
- No DV Tunnel carrier source, Moory service or host routes/NAT was changed. Only temporary lab test files and new private runtime credentials were used.
- Mother-local registration initially had no heartbeat and the first exec was rejected offline; the actual ready heartbeat arrived later. No data test was counted before that.

## Startup retry (not hidden in PASS count)
Round2 first start failed before any application case: Sofia startup returned Errno98 address already in use; Iran consequently timed out waiting for the encrypted link. Both roles exited1 and cleaned up. The harness preflight outer-port probe was changed to SO_REUSEADDR for normal repeated socket use (TIME_WAIT is a plausible mechanism, not a packet-captured diagnosis). Retried the same64 cap and workloads; no carrier code changed. This extra failed startup is recorded separately from the18 completed application cases. Runtime harness SHA256 for round1 and rounds2/3 are retained in evidence.

## Conditions
- Published DV Tunnel `v0.1.0-preview.6`, verified stripped ELF SHA256 `b7b5f02da2227ec6bdd2a1ba2698f44d6c8ba5afa65908d1109358683dad9062`. Archive SHA256 `aac2bd25d287282e21bc5a88ed8acb1e29ae84a90fe47f220fca31166849bcc2`.
- Direct private /30, MTU1336, fresh generated EC certificate/token, verified hostname/CA, offload-lowlat and `--split-udp`. No padding, camouflage, NAT or persistent tunnel service.
- Aggregate packet cap64/64/128 in rounds1/2/3 (32/32/64 per split lane); byte cap262144 and lowat131072 unchanged; no UDP admission wait. Both default64 rounds retained; final128 is experimental, not a proven fix.
- Same loads as the Turkey checkpoint. All Sofia rounds use2.2s stats settling before/after every phase; this differs from the first Turkey round instrumentation.
- Iran account/capsule:1.5/1.35CPU,384/256MiB. Sofia account/capsule:0.5/0.4CPU,192/128MiB (initial default, unchanged). Turkey used0.75/0.65CPU,240/176MiB: do not call route differences a speedup or efficiency improvement.
- Binary:38 independent verified compressed chunks uploaded to Iran, bounded one-shot TCP copy to Sofia, both final versions/hashes verified. Workload code was reused unchanged; only endpoint/result metadata adaptation and the startup-probe fix above were made.

## Gates
| Gate | Workload per round | Passing rounds |
|---|---|---:|
| HTTP |30 real GETs; status200/body/length verified |3/3|
| TCP Iran → Sofia |8MiB,8Mbps paced, SHA256 |3/3|
| TCP Sofia → Iran |8MiB,8Mbps paced, SHA256 |3/3|
| Four TCP streams |4×2MiB,2Mbps each, all SHA256 |3/3|
| UDP echo |2500×1000B at125pps; unique sequence/payload |3/3|
| Mixed TCP + UDP |32MiB TCP at8Mbps + same20s UDP phase |3/3|

Receiver goodput uses received bytes×8/monotonic receiver seconds; it is not an unpaced capacity estimate. RTT is client-clock nearest-rank, not one-way latency. Mixed UDP starts after the first TCP data; TCP goodput specifically within the UDP-only window was not measured.

## Internal counters and latency
| Round | Aggregate packet cap | Iran / Sofia queue-packet drops | UDP p95 / p99 ms | Mixed UDP p95 / p99 ms |
|---|---:|---:|---:|---:|
|1|64|0 / 0|183.398 / 280.959|184.807 / 256.444|
|2|64|0 / 20|140.169 / 265.856|170.749 / 282.897|
|3|128|0 / 0|107.168 / 118.388|176.693 / 257.221|

Internal drops and application delivery are separate denominators. Do not interpret internal counters as physical-WAN loss. Full other drop reasons, phase snapshots, lane caps, RTT quantiles, process CPU/RSS and harness CPU are retained in JSON. CPU is own carrier process from /proc/PID/stat divided by monotonic phase duration, including settling; not host/account CPU. RSS is sampled every0.25s. These are diagnostic values, not efficiency proof.

## Completion and continuation
Both roles independently exited0 after every round, stopped their own carrier and confirmed the nonpersistent test TUN absent. No persistent tunnel was installed. A single server short-header diagnostic per round is the closed readiness probe, not a failed data gate.

Next engineering gates: matched-quota/direct baseline, unpaced capacity, controlled queue-cap A/B, reconnect/fault injection, longer mixed-load soak and many-flow/user integration. Passing these integrity tests alone does not justify a carrier rewrite.

Sanitized source data: `sofia-18-evidence.json`. Previous18 Iran↔Turkey tests remain in `ROUND-2.md` and `round-2-evidence.json`. No private config, token, key or carrier-private log is committed.
