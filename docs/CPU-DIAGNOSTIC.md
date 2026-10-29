# CPU-attribution diagnostic — existing preview.6 link

Purpose: the wait trial stopped at the unchanged host-CPU safety guard. There
were no observed new carrier drops, but the wait counters never increased. Do
not infer causality or claim success from that aborted run.

No runtime update/restart or configuration change is required. Download the
commit-pinned tools/soak_check.py to /root/dv-cpu-check.py on both endpoints.
Run Turkey first, then Iran after SOAK_SERVER_READY:

```bash
# Turkey
python3 /root/dv-cpu-check.py server --queue-diagnostic \
  --expect-version v0.1.0-preview.6 --expect-lane-packets 32 \
  --expect-udp-wait-ms 200 --cpu-baseline-seconds 10
# Iran
python3 /root/dv-cpu-check.py client --queue-diagnostic \
  --expect-version v0.1.0-preview.6 --expect-lane-packets 32 \
  --expect-udp-wait-ms 200 --cpu-baseline-seconds 10
```

Each endpoint first records ten seconds with no test-generated traffic and with
safety guards active; normal production traffic is not paused. If a guard stops
the baseline, test sockets/load are not started. Then only the same 30-second
4.8 Mbps/initiator mixed phase runs, with unchanged guard thresholds and gates.
The diagnostic can stop early again; send both results, do not keep repeating.

Reports remain /root/dv-queue-diag-results/latest-client.zip on Iran and
latest-server.zip on Turkey. These now add raw numeric /proc/stat first-eight
CPU counters, logical CPU count, clock ticks/second, and user/system/threads
counts for this checker, the known runner and its verified carrier child only.
PID parent/start-time identities are checked at each sample. No inventory or
command lines of other services, environments, secrets, private configs or
process names are saved. Existing half-second observations stay bounded.

Interpretation: delta process(user+system ticks) / CLK_TCK / elapsed_seconds
*100 is one-core CPU percent. For host-normalized percent use the compatible
whole-host tick delta, not a different denominator. Host busy includes steal:
high VM steal is not evidence of equivalent application CPU work. Sampling
adds overhead and may miss short spikes; unobserved host work cannot be assigned
to a particular production service. There is no claim of zero production impact.

No carrier/protocol/queue tuning, extra connections, relaxed safety stop or
production service changes are part of this diagnostic.
