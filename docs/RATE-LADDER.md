# Bounded mixed-traffic rate ladder

Precondition: the existing preview.6, split-udp-v1, 32-slot/lane, UDP-wait200ms
link passed a full 120-second mixed phase at 4.8 Mbps per UDP initiator.
The Turkish host is near its CPU guard at times. This is a controlled short
probe with unchanged guards, not permission for unbounded load or an 800 Mbps
claim. No server settings, runtime, queue sizes or production service changes.

Use the same pinned tools/soak_check.py on BOTH peers, with:

```bash
# Turkey first
python3 /root/dv-ladder-check.py server --queue-diagnostic --lean-observer \
  --rate-ladder --cpu-baseline-seconds 10 \
  --expect-version v0.1.0-preview.6 --expect-lane-packets 32 --expect-udp-wait-ms 200
# Iran only after SOAK_SERVER_READY
python3 /root/dv-ladder-check.py client --queue-diagnostic --lean-observer \
  --rate-ladder --cpu-baseline-seconds 10 \
  --expect-version v0.1.0-preview.6 --expect-lane-packets 32 --expect-udp-wait-ms 200
```

Fixed plan, no user-supplied unbounded rates:
- Phase 1: 30 seconds, 700 probes/s = 6.72 Mbps payload per UDP initiating side.
- Phase 2: 30 seconds, 1000 probes/s = 9.6 Mbps per UDP initiating side.

Echo replies add load; these are not total wire rates or independent one-way
capacity measurements. Existing four TCP workers and 2 Mbps aggregate payload
pacers per endpoint remain. Distinct ladder-suite handshake rejects mixing
with the prior fixed-rate checker before phase load. Report stores the plan.

The existing client loop blocks escalation after a failed phase gate; this has
a dedicated test. Local cumulative carrier drops also veto the phase gate.
Any carrier drop fails acceptance; the same >=64 packet / any byte-or-expiry
hard stop remains. CPU85% twice, memory192MiB, TUN errors/drops, process/session
changes, no echo and all payload/rate/latency gates remain unchanged. Do not
combine --rate-ladder with --diagnostic-seconds 120: rejected before test load.

Ten-second guarded baseline precedes services; no production load is paused.
Lean observer keeps half-second CPU/TUN checks without periodic ss discovery.
Outputs remain /root/dv-queue-diag-results/latest-client.zip on Iran and
latest-server.zip on Turkey. If Turkey stops before READY, do not run Iran.
Send only fresh reports. Do not retry failures, increase limits, or stop other
services to force a pass. A CPU-guard abort is a host/test limitation, not a
measurement of the carrier's absolute capacity.
