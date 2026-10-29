package main

import (
	"encoding/binary"
	"errors"
	"net/http"
	"sync/atomic"
	"time"
)

const modeHeader = "X-DV-Transport"
const laneHeader = "X-DV-Lane"
const splitMode = "split-udp-v1"

func laneName(dgram bool) string {
	if dgram {
		return "udp"
	}
	return "tcp"
}

// UDP and ICMP get their own TCP connection, not an unreliable outer transport.
// Unknown protocols, ESP and unparseable extension chains use the bulk lane.
func datagramIP(p []byte) bool {
	if len(p) >= 20 && p[0]>>4 == 4 {
		ihl := int(p[0]&15) * 4
		return ihl >= 20 && ihl <= len(p) && (p[9] == 17 || p[9] == 1)
	}
	if len(p) < 40 || p[0]>>4 != 6 {
		return false
	}
	next, off := p[6], 40
	for depth := 0; depth < 8; depth++ {
		switch next {
		case 17, 58:
			return true
		case 0, 43, 60, 51:
			if off+2 > len(p) {
				return false
			}
			n := (int(p[off+1]) + 1) * 8
			if next == 51 {
				n = (int(p[off+1]) + 2) * 4
			}
			if n < 8 || off+n > len(p) {
				return false
			}
			next = p[off]
			off += n
		case 44:
			if off+8 > len(p) {
				return false
			}
			n := p[off]
			if binary.BigEndian.Uint16(p[off+2:off+4])&0xfff8 != 0 {
				return n == 17 || n == 58
			}
			next = n
			off += 8
		default:
			return false
		}
	}
	return false
}

// Auth is checked by the handler BEFORE this function is called. Both sides
// confirm the profile and lane. Missing negotiation cannot silently fall back.
func (e *engine) negotiate(r *http.Request) (bool, http.Header, error) {
	mode, lane := r.Header.Get(modeHeader), r.Header.Get(laneHeader)
	if !e.cfg.SplitUDP {
		if mode != "" || lane != "" {
			return false, nil, errors.New("split mode disabled")
		}
		return false, nil, nil
	}
	if mode != splitMode || (lane != "tcp" && lane != "udp") {
		return false, nil, errors.New("split negotiation required")
	}
	h := http.Header{}
	h.Set(modeHeader, mode)
	h.Set(laneHeader, lane)
	return lane == "udp", h, nil
}

type laneMetrics struct {
	drops      [dropReasonCount]atomic.Uint64
	sent       atomic.Uint64
	received   atomic.Uint64
	maxWriteUS atomic.Uint64
	slowWrites atomic.Uint64
}

func (s *session) laneMetrics() *laneMetrics {
	if s.dgram {
		return &s.e.lanes[1]
	}
	return &s.e.lanes[0]
}
func (s *session) drop(reason dropReason) {
	s.e.m.drop(reason)
	s.laneMetrics().drops[reason].Add(1)
}
func (s *session) recordWrite(d time.Duration) {
	m := s.laneMetrics()
	us := uint64(d / time.Microsecond)
	for old := m.maxWriteUS.Load(); us > old; old = m.maxWriteUS.Load() {
		if m.maxWriteUS.CompareAndSwap(old, us) {
			break
		}
	}
	if d >= 100*time.Millisecond {
		m.slowWrites.Add(1)
	}
}
func (e *engine) snapshot() map[string]interface{} {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.readiness()
	d := e.m.snapshot()
	d["transport_profile"] = "single-stream"
	if e.cfg.SplitUDP {
		d["transport_profile"] = splitMode
	}
	lanes := map[string]interface{}{}
	for i, s := range []*session{e.current, e.datagram} {
		if i == 1 && !e.cfg.SplitUDP {
			continue
		}
		m := &e.lanes[i]
		reasons := map[string]uint64{}
		for j, name := range dropNames {
			reasons[name] = m.drops[j].Load()
		}
		info := map[string]interface{}{"connected": false, "tx_datagrams": m.sent.Load(), "rx_datagrams": m.received.Load(), "drop_reasons": reasons, "max_write_us": m.maxWriteUS.Load(), "writes_over_100ms": m.slowWrites.Load()}
		if s != nil && s.q != nil {
			info["connected"] = s.ctx.Err() == nil
			info["queued_packets"] = len(s.q)
			info["queued_bytes"] = s.qbytes.Load()
			info["packet_limit"] = cap(s.q)
			info["byte_limit"] = s.byteBudget()
		}
		lanes[laneName(i == 1)] = info
	}
	d["lanes"] = lanes
	return d
}
