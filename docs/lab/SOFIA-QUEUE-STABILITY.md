# Iran–Sofia: queue budget repetitions and short stability

## Outcome
15/15 finite gates passed;225MiB TCP verified; UDP29000/29000; HTTP143 verified.
No tunnel-source change and no production/default queue-size recommendation.

## Method
- binary: unchanged v0.1.0-preview.6, same verified ELF
- path: Iran to Sofia mother-local capsules, same quotas as prior checkpoint
- queue: Both endpoints configured64 then256 total queue_packets; split-UDP gives TCP packet limits32 vs128, observed from stats. queue_bytes remains262144 total,131072 per lane; no other tuning knob changed. No default promoted.
- workload: 32x256KiB TCP at0.5Mbps per flow,500x1000byte UDP echoes at125pps,8HTTP per matched gate
- order: queue64: baseline,combined,combined,baseline,baseline,combined; queue256 opposite sequence. Not randomized or simultaneous; time/order and unseeded netem variation remain confounders
- impairment: combined40±20ms delay and1% random loss independently per direction, actual IFB packet/drop deltas checked on Iran tap0 and Sofia forwarded loopback ingress; capsule-stack rather than physical WAN emulator
- stats: per-gate before/after cumulative deltas with2.2second settling for stats snapshots; control/settling may contribute; no attribution to a specific connection
- limits: Three trials per cell, only8HTTP per gate, no statistical significance claim. Paced generator, not capacity benchmark or independent users. Complete UDP delivery over reliable WSS/TCP does not imply raw UDP loss immunity.
- soak: A three-minute mixed window only; not hours-long soak. RSS snapshots alone cannot rule out leaks.

## Matched comparison
UDP metric below is median of three trial-specific p95s; range preserves trial variability, not confidence bounds.
| Total packet budget | Condition | Pass | UDP trial-p95 median (min–max) ms | HTTP trial-p95 median ms | Packet / byte queue drops summed across both roles |
|---:|---|---|---:|---:|---|
|64|baseline|3/3|164.8 (102.1–165.2)|450.7|360 /0|
|64|combined|3/3|322.8 (270.5–337.9)|3875.0|82 /0|
|256|baseline|3/3|198.4 (186.5–249.5)|208.9|0 /38|
|256|combined|3/3|1559.8 (1523.8–2601.8)|4324.9|0 /85|

## Queue findings
With the byte budget held fixed, larger packet queues removed packet-limit drops in this sample but did not remove drops: the byte-limit reason became active. More buffering is not established as a latency fix. Compare both UDP and HTTP: the direction of the effect may differ by protocol and network condition. These two sequential queue batches, three trials per cell and unseeded netem do not establish a causal or statistically significant improvement.

## Short stability window
```json
{
  "actual_application_seconds": 180.75982870801818,
  "observation_errors": [],
  "memory": {
    "client": {
      "samples": 35,
      "first_rss_kib": 7936.0,
      "last_rss_kib": 13304.0,
      "min_rss_kib": 7936.0,
      "max_rss_kib": 13544.0,
      "warm_first_30s_median_rss_kib": 13146.0,
      "final_30s_median_rss_kib": 13304.0
    },
    "server": {
      "samples": 35,
      "first_rss_kib": 6544.0,
      "last_rss_kib": 11608.0,
      "min_rss_kib": 6544.0,
      "max_rss_kib": 12068.0,
      "warm_first_30s_median_rss_kib": 11842.0,
      "final_30s_median_rss_kib": 11888.0
    }
  }
}
```

## Individual gates
| Group | Gate | Pass | TCP MiB | UDP replies / sent | HTTP verified |
|---|---|---|---:|---|---:|
|queue64|baseline-repeat-1|True|8|500/500|8|
|queue64|combined-repeat-1|True|8|500/500|8|
|queue64|combined-repeat-2|True|8|500/500|8|
|queue64|baseline-repeat-2|True|8|500/500|8|
|queue64|baseline-repeat-3|True|8|500/500|8|
|queue64|combined-repeat-3|True|8|500/500|8|
|queue256|combined-repeat-1|True|8|500/500|8|
|queue256|baseline-repeat-1|True|8|500/500|8|
|queue256|baseline-repeat-2|True|8|500/500|8|
|queue256|combined-repeat-2|True|8|500/500|8|
|queue256|combined-repeat-3|True|8|500/500|8|
|queue256|baseline-repeat-3|True|8|500/500|8|
|soak|mixed-continuous-180-seconds|True|128|22500/22500|36|
|soak|idle-20-seconds-resume|True|0|—|3|
|soak|post-soak-mixed-integrity|True|1|500/500|8|

## Interpretation
Larger packet queues can trade drops for waiting and may encounter the unchanged byte budget. Results must be evaluated by condition, with the three trial values and actual counter deltas, not a single best run. Tests with random loss are not a deterministic dose-response curve. No configuration is promoted by this report.
All owned carriers, TUNs, IFBs and ingress rules cleaned up, original root qdiscs preserved and private configs restored, with independent role exits.
Raw RTT arrays are retained locally with source SHA256s; published evidence includes validated quantiles, per-trial exact fixture hashes, queue/counter deltas and every stability observation.
Next: investigate bounded TCP backpressure rather than blindly growing queues; repeat matched direct/carrier baselines and run hours-long soak in a suitable sustained environment.
