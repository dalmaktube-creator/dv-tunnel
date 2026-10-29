# Fixed-load stability trial — no runtime change

The lean 30-second mixed diagnostic passed at 4.8 Mbps UDP payload per
initiating sender, while four TCP workers and the existing aggregate TCP
pacers overlapped. Next extend ONLY duration, to 120 seconds. No higher-rate
phase, carrier update/restart, config change or relaxed acceptance threshold.
This is not a line-rate or maximum-capacity benchmark.

Use the same pinned tools/soak_check.py on BOTH peers. Turkey first:

```bash
python3 /root/dv-steady-check.py server --queue-diagnostic --lean-observer \
  --diagnostic-seconds 120 --cpu-baseline-seconds 10 \
  --expect-version v0.1.0-preview.6 --expect-lane-packets 32 \
  --expect-udp-wait-ms 200
```

After SOAK_SERVER_READY, Iran:

```bash
python3 /root/dv-steady-check.py client --queue-diagnostic --lean-observer \
  --diagnostic-seconds 120 --cpu-baseline-seconds 10 \
  --expect-version v0.1.0-preview.6 --expect-lane-packets 32 \
  --expect-udp-wait-ms 200
```

Each endpoint first performs a ten-second guarded no-test-load baseline; normal
production traffic is not paused. If Turkey stops before READY, do not start
Iran; send the fresh Turkey report only. Do not repeat a failed run.

Duration choices are explicitly bounded to 30/default or 120 seconds; the UDP
rate remains 500 packets/second with 1200-byte application payloads. The long
trial therefore plans 60000 probes per initiating sender, within the existing
probe memory/packet bound. Reply/echo traffic adds load. Four TCP workers,
2 Mbps aggregate TCP pacers and all payload-integrity tests remain unchanged.
The report records planned phases; a distinct -120s suite handshake rejects
peers running the short mode before phase load.

CPU85% twice, memory192MiB, carrier PID/session/profile, queue/kernel-drop and
all delivery/latency/rate quality gates stay unchanged. Lean observer verifies
two owned TCP sockets at preflight, then observes CPU/TUN without repeated ss
process discovery. It does not provide periodic TCP backlog/retransmission
evidence. CPU child counters cover reaped checker subprocesses only.

Results: /root/dv-queue-diag-results/latest-client.zip on Iran and
latest-server.zip on Turkey. A full pass is required before any rate increase.
A pass would establish this longer bounded load, not indefinite uptime, user
application acceptance, causal performance improvement or a capacity guarantee.
