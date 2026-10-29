# Focused queue diagnostic (runtime remains preview.4)

This is an observation-only checker change, NOT a carrier performance fix.
The preview.4 field soak passed the full 120-second 1.92 Mbps-per-initiator
mixed phase, then stopped approximately seven seconds into the 4.8 Mbps step:
43 new Iran and 72 new Turkey datagram-lane packet-cap drops. One scheduler
yield was insufficient. No new queue-byte/expiry drops or session change.

Do not repeat the accepted 120-second step or escalate load yet. Run only the
failing load: one 30-second phase at 500 UDP packets/s (1200 bytes), independently
initiated by each endpoint, plus the existing four paced TCP integrity workers.
Echoes add traffic: 4.8 Mbps per initiating sender is not total wire load.
The same zero-drop pass gate and immediate safety guards are retained. A queue
hard-stop may end this diagnostic early. Partial counts are preserved, not
reported as final packet loss percentages.

## Why another observation, not another blind tuning change

Datagram writer cumulative maximum durations reached 82.012 ms on Iran and
104.914 ms on Turkey, including scheduling time. These do not prove retransmission
or isolate WAN, receiver, host scheduler or kernel backpressure as the cause.
Only 5 of 48 full-queue yield attempts on Iran, and 0 of 72 on Turkey, accepted
the retry. Do not increase caps or replace the transport based on this alone.

The focused mode reads numeric TCP fields from `ss -Htinp` at half-second
intervals, retaining ONLY established sockets owned by the already-verified
carrier process and using port 42443. The service MainPID is the Python runner,
not the Go binary: the observer resolves its unique direct dv-tunnel child,
checks parent PID and matching cgroup, and guards kernel process start ticks
against PID reuse. Saved data include socket FD, send/receive
queue sizes, and a fixed allowlist of numeric TCP fields such as RTT, cwnd,
retransmission and notsent counters. Addresses, process arguments, unrelated
sockets and raw command output are NOT saved. Interface-wide TUN packet/drop/error
counters are also read; they are not per-probe counters.

Two owned established TCP sockets are required before load begins. Missing ss,
wrong port or an incompatible deployment fails closed. No private config is read
by this observer. No shell is invoked, no sysctl or socket settings are changed.
The checker never starts/stops/restarts/enables the carrier, and changes no
firewall, NAT, address or route. Only its own application sockets are cleaned up.

Samples are bounded to 900; each ss subprocess has a half-second timeout, and
sampling ends with the existing checker. Polling itself has overhead. Half-second
samples can miss a shorter stall; two-second published carrier samples are not
high-resolution queue traces. FD is NOT a verified lane label. Compare cumulative
socket counters only within the same connection, not across endpoints or after
FD reuse/reconnect. Correlation alone is not causal attribution. Sampling errors
and retention-cap exhaustion are explicit; absence of a metric is not zero.

## Operator sequence

Download the SAME source revision of tools/soak_check.py on both peers to
/root/dv-queue-check.py. Do NOT update or restart the carrier and do not change
its split-udp-v1 profile. Runtime expected: v0.1.0-preview.4.

Turkey first:

```sh
python3 /root/dv-queue-check.py server --queue-diagnostic --expect-version v0.1.0-preview.4
```

After SOAK_SERVER_READY, Iran:

```sh
python3 /root/dv-queue-check.py client --queue-diagnostic --expect-version v0.1.0-preview.4
```

The dedicated handshake prevents a focused checker client from mixing with the
original staged-soak server (and vice versa). This mode does not run the later
9.6/19.2 Mbps steps. Server wait remains bounded by the original 420-second limit.

Send `/root/dv-queue-diag-results/latest-client.zip` from Iran and
`/root/dv-queue-diag-results/latest-server.zip` from Turkey. This separate default
output directory preserves the earlier soak aliases. Do not repeat automatically
if a guard stops the test. Keep the production link/client configuration unchanged.

Local parser/privacy/retention and synthetic two-role tests are separate from real
Linux ss/TUN acceptance. No live-server access was used while preparing this mode.
