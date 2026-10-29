# Bounded UDP admission trial — preview.6

Experimental opt-in only. The preview.5 128-slot lane trial failed on the
existing byte budget; expanding packet slots did not solve congestion. This
trial restores the original 32 slots per split lane and permits a shared
200 ms admission deadline when the datagram lane lacks byte or packet capacity.
It is not a measured WAN fix, a capacity claim, or a lossless guarantee.

## What changes

- Default `--udp-wait-ms 0` preserves nonblocking admission. Explicit 1..200 ms
  is valid only with `--split-udp`; the wire format and outer WSS/TCP are unchanged.
- One deadline covers both byte and packet pressure for an admission attempt.
  Dequeue wakes the producer; cancellation interrupts waiting. Repeated queue
  activity does not renew the deadline. Host scheduling can overrun wall-clock
  deadlines, so this is not a real-time latency guarantee.
- Queue slot/byte caps, one-second packet age, write deadlines and NOTSENT_LOWAT
  remain unchanged. Byte reservations include a pending packet waiting for a
  channel slot; a pending packet is not an extra channel slot. Packet age starts
  before admission, not after the wait. Bulk admission remains nonblocking.
- No global engine lifecycle/stats lock is held while waiting. However, the
  actual TUN reader is shared: waiting on UDP/ICMP admission can delay subsequent
  inner TCP packets too. Backpressure can move into kernel queues and increase
  latency or kernel drops. This does not eliminate TCP head-of-line blocking.
- Per-lane stats expose `queue_wait_ms`, `queue_waits`, `queue_wait_success`,
  `queue_wait_timeouts`, and `queue_wait_max_us`. Admission success alone does
  not establish application delivery.

## Operator procedure — existing managed main only

On BOTH peers, install preview.6 through the updater, then select:

```bash
dv-tunnel update --version v0.1.0-preview.6 --restart &&
dv-tunnel queue main --default --wait-ms 200 &&
dv-tunnel restart main --yes
```

These are two explicit restarts of the test link. `queue` only updates nonsecret
0600 link metadata, keeps the private configuration unchanged, and backs up
metadata. `--default --wait-ms 200` removes the failed preview.5 packet override
and selects the wait. Expected original configuration: 32 slots and 131072 bytes
per lane. The checker rejects mismatched caps/version/wait before test load.
Old target runners that cannot honor the wait reject an update/rollback until
it is cleared and the instance restarted.

Use the pinned `tools/soak_check.py` from the same release commit on both peers.
Turkey first, then Iran after SOAK_SERVER_READY:

```bash
# Turkey
python3 /root/dv-wait-check.py server --queue-diagnostic \
  --expect-version v0.1.0-preview.6 --expect-lane-packets 32 --expect-udp-wait-ms 200
# Iran — separate terminal/server
python3 /root/dv-wait-check.py client --queue-diagnostic \
  --expect-version v0.1.0-preview.6 --expect-lane-packets 32 --expect-udp-wait-ms 200
```

This repeats only the failing 30-second, 4.8 Mbps per UDP initiator phase with
mixed TCP. Echo return traffic adds load. It is not an 800 Mbps port benchmark.
Application integrity, achieved rate and latency gates remain unchanged. Any
new carrier drop fails the phase; byte/expiry drops or >=64 packet drops stop
load immediately. Half-second TUN drop/error counter increases or an observation
error during load now also stop test load. Counter sampling can miss events and
is not a universal no-loss guarantee. No carrier, route, firewall or production
service is stopped by the checker. Host guards reduce risk, not eliminate it.

Collect both `/root/dv-queue-diag-results/latest-client.zip` (Iran) and
`latest-server.zip` (Turkey), including failed results. Do not repeat a failed
run or increase the rate before review. Reports exclude private configs/tokens.

To disable the experiment on both endpoints:

```bash
dv-tunnel queue main --default && dv-tunnel restart main --yes
```

## Local validation, not server acceptance

Go race/vet, Linux amd64/arm64 builds, and Python manager/checker/package tests
cover finite-burst admission, deadline/cancellation accounting, unchanged caps,
nonblocking bulk admission, lock independence, metadata-only changes, target
runner guards, expected-wait preflight and kernel-counter load stop. The local
environment did not validate real TUN/systemd behavior. Field reports are still
required before any claim of improvement or integration into the WG panel.
