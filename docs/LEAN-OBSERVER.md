# Lean observer — unchanged mixed load and safety gates

This is an observer-overhead experiment, not a runtime performance fix. The
CPU-attribution run stopped at the host CPU guard. Unassigned CPU includes
checker subprocesses as well as other host work; it is not all production load.
No attribution to periodic ss is claimed until measured.

On both existing preview.6 / split-udp-v1 peers, use the same pinned checker with
--queue-diagnostic --lean-observer --cpu-baseline-seconds 10
--expect-version v0.1.0-preview.6 --expect-lane-packets 32 --expect-udp-wait-ms 200.
Turkey server first; Iran client only after SOAK_SERVER_READY. No update or
restart is needed. The two endpoints must use the same checker mode; a distinct
lean suite handshake rejects mixing with the previous observer.

The two carrier-owned established TCP sockets are verified at preflight using
the same PID/cgroup/start-time checks and numeric-only ss extraction. Afterwards
ss is not spawned on each half-second sample. The report explicitly marks TCP
observation as not sampled in lean mode; there are no interim socket Send-Q or
retransmission measurements. This reduced diagnostic visibility must not be
described as zero socket backlog or zero TCP retransmissions.

CPU/TUN reads and carrier identity checks remain half-second, carrier stats
monitoring remains one-second, host CPU/memory guard thresholds and all delivery,
latency, achieved-rate and drop gates stay unchanged. The same four TCP workers,
2 Mbps aggregate TCP pacers and 30-second 4.8 Mbps-per-UDP-initiator mixed phase
remain. Ten-second no-test-load baseline still runs under guards. A stopped
baseline starts no test services/load. No production load is paused.

Samples additionally store resource.RUSAGE_CHILDREN user/system seconds, covering
reaped subprocesses of this checker collectively, not other services. A child
still running is not included until it is reaped. No names/argv/environments or
process inventory are stored. This does not capture every kernel/network cost.

Output stays /root/dv-queue-diag-results/latest-client.zip (Iran) and
latest-server.zip (Turkey). Stop on failure and send the newly produced report(s),
not a stale peer report from a run that never started. Even a completed pass at
this load would not certify higher capacity or prove previous runtime changes
caused the difference: WAN/host conditions vary.
