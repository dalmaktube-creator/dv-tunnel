# Iran–Sofia: outer-ingress impairment and pressure checkpoint

## Decision
13/13 bounded gates passed. This is finite capsule-stack fault/load evidence, not production readiness.

## Method and limitations
- TCP fixture repeats bytes0..255, SHA256 checked per stream; UDP sequence/payload checked exactly; load generators paced; netem queue limit2000skb; no deterministic netem seed
- Iran dialer to Sofia mother-local listener, same preview.6 ELF and unchanged default capsule quotas
- Linux netem on owned IFBs; Iran tap0 ingress src_port45100 and Sofia lo ingress dst_port45100; both directions independently verified from real counter deltas; only outer carrier TCP flows targeted
- Rootless user-space TCP forwarding is in path; this is capsule-stack impairment, not physical full-WAN link emulation. The initial Sofia tap0 filter carried almost no traffic; v2 targets the forwarded loopback listener path and independently checks both directions using actual counters.
- configured Bernoulli drop probability is not observed packet-loss rate; qdisc counters are packet/skb counters influenced by offloads; retransmitted traffic counted too
- UDP is tunneled over reliable WSS/TCP; complete delivery can coexist with large tail latency, not raw UDP loss immunity
- paced generators and changing offered aggregate loads; no unpaced maximum-capacity claim or matched direct baseline
- per-case netem after-minus-before only; exclusion of setup/control outside these windows; blackout reported separately
- one trial per setting; eight HTTP requests insufficient for stable population p95
- hours-long soak; physical WAN emulator; production-user scaling; resource-matched direct baseline

## Measured cases
| Group / case | Pass | TCP MiB | UDP replies / sent | UDP p95 RTT ms | HTTP p95 ms | Local / remote outer drops |
|---|---|---:|---|---:|---:|---|
| profiles-v2 / baseline | True | 2 | 500/500 | 107.0 | 241.6 | 0 / 0 |
| profiles-v2 / delay30-jitter10 | True | 2 | 500/500 | 178.8 | 381.1 | 0 / 0 |
| profiles-v2 / loss0.5 | True | 2 | 500/500 | 183.9 | 384.8 | 3 / 11 |
| profiles-v2 / loss2 | True | 2 | 500/500 | 201.9 | 1655.8 | 33 / 29 |
| profiles-v2 / combined40-20-loss1 | True | 2 | 500/500 | 2534.5 | 1685.9 | 11 / 16 |
| profiles-v2 / severe80-40-loss3 | True | 2 | 500/500 | 1512.1 | 2943.4 | 27 / 54 |
| stress-v2 / load-1 | True | 0.25 | 500/500 | 111.7 | 252.2 | 0 / 0 |
| stress-v2 / load-8 | True | 2 | 500/500 | 112.1 | 280.3 | 0 / 0 |
| stress-v2 / load-32 | True | 8 | 500/500 | 116.6 | 234.9 | 0 / 0 |
| stress-v2 / load-64 | True | 16 | 500/500 | 136.4 | 233.6 | 0 / 0 |
| stress-v2 / load32-with-combined-impairment | True | 8 | 500/500 | 1369.4 | 4468.2 | 17 / 30 |
| stress-v2 / three-second-outer-ingress-blackout | True | 8 | — | — | — | 29 / 0 |
| stress-v2 / mixed-60-seconds | True | 64 | 7500/7500 | 120.7 | 218.1 | 0 / 0 |

## Remaining pressure finding
The repaired stress batch ended with TCP queue_packets drop counters of 1018 on Iran and 434 on Sofia. These are cumulative across the batch, not attributable to a single gate. All application fixtures still arrived intact, but the queue drops are real and must not be described as a loss-free carrier. The 32-flow combined-fault gate had HTTP sample p95 of 4468.2 ms; latency and backpressure remain optimization priorities.

## Earlier attempts retained
Initial 6 profiles passed but only Iran ingress carried substantial traffic. Initial threaded pressure run completed 3 passes then hit the unchanged 64-task capsule ceiling at 64 streams (cannot start new thread; pids.events max 3); the next case timed out after server failure. The batch was explicitly interrupted, remaining gates were not counted, and owned IFB/ingress cleanup was verified separately. Both server and client fixture concurrency were converted to coroutines without raising quotas. Repaired trials are separately identified, not substituted silently.

## Interpretation
Complete application delivery does not mean zero induced network loss. Observed outer drops were retransmitted by the reliable carrier; increased latency is material. These single trials are not a monotonic loss-response curve: the combined profile had a higher UDP p95 than the severe profile. A paced concurrent-flow gate is not the same as independent authenticated users or maximum supported throughput.

All owned carriers, TUNs, IFBs and ingress rules were cleaned up and original root qdiscs retained, as independently confirmed by both role exits. No persistent host, firewall or tunnel-source changes.

Evidence: `impairment-evidence.json`. It includes exact fixture hashes, qdisc settings/counter deltas and raw-source SHA256 digests. High-volume raw RTT arrays are retained in local immutable source files; published evidence stores validated nearest-rank quantiles.

Next: investigate bounded TCP queue backpressure and loss-related tail latency; repeat both-direction impairment trials with matched conditions, long soak and matched direct/carrier capacity baselines.
