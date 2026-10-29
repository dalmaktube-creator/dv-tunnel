// Experimental bounded carrier over WSS: AWG/WG UDP datagrams, or (lab-only)
// raw IP packets from a TUN device. Not browser impersonation.
package main

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"crypto/tls"
	"crypto/x509"
	"encoding/binary"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"github.com/gorilla/websocket"
	"io"
	"log"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

const (
	maxPayload = 1800
	maxPadding = 256
	headerSize = 12
	maxFlows   = 128
	queueSize  = 64
	// Batch frames coalesce already-queued datagrams into one WebSocket
	// message (one TLS record and write syscall) without waiting for more.
	batchType    = 2
	batchHeader  = 4
	entryHeader  = 10
	maxBatch     = 16384
	maxBatchMsgs = 32
	tunFlow      = 1
	// Offload frames carry TCP super-packets (TSO/GRO, up to 64 KiB) from a
	// vnet-header TUN; the peer rebuilds the kernel header from the packet.
	offloadType   = 3
	offloadEntry  = 7
	maxSuper      = 65535
	maxOffloadMsg = 131072
	metaNeedsCsum = 1
	metaECN       = 2
	minGSOSize    = 64
)

var version = "development"

type config struct {
	Role       string `json:"role"`
	Mode       string `json:"mode"`
	Listen     string `json:"listen,omitempty"`
	URL        string `json:"url,omitempty"`
	UDP        string `json:"udp"`
	Path       string `json:"path"`
	Token      string `json:"token"`
	CA         string `json:"ca_pem,omitempty"`
	ServerName string `json:"server_name,omitempty"`
	Cert       string `json:"cert_pem,omitempty"`
	Key        string `json:"key_pem,omitempty"`
	Padding    int    `json:"padding_bytes,omitempty"`
	Stats      string `json:"stats_file,omitempty"`
	// Lab-only TUN mode: carry IP packets directly, without an inner WG/AWG.
	TUN     string   `json:"tun,omitempty"`
	Sources []string `json:"allowed_sources,omitempty"`
	Batch   bool     `json:"batch,omitempty"`
	Offload bool     `json:"tun_offload,omitempty"`
	// Keeps the kernel TCP backlog small so the bounded, dropping queue
	// (not the socket) absorbs bursts; 0 leaves the kernel default.
	NotSentLowat int `json:"notsent_lowat,omitempty"`
	// Independent packet cap; omitted/0 preserves the legacy 64-packet limit.
	QueuePackets int `json:"queue_packets,omitempty"`
	// Byte cap of the outgoing queue; 0 leaves only the packet cap active.
	QueueBytes int `json:"queue_bytes,omitempty"`
	// Opt-in CLI feature; never written into legacy private configurations.
	SplitUDP bool `json:"-"`
}

func (c config) packetQueueSize() int {
	if c.QueuePackets == 0 {
		return queueSize
	}
	return c.QueuePackets
}

func validIfName(n string) bool {
	if len(n) == 0 || len(n) > 15 {
		return false
	}
	for _, r := range n {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_') {
			return false
		}
	}
	return true
}

// sources parses the TUN anti-spoofing allowlist (the cryptokey-routing
// AllowedIPs check that WireGuard would otherwise perform).
func (c config) sources() ([]netip.Prefix, error) {
	if len(c.Sources) == 0 || len(c.Sources) > 64 {
		return nil, errors.New("TUN mode requires 1..64 allowed_sources")
	}
	out := make([]netip.Prefix, 0, len(c.Sources))
	for _, v := range c.Sources {
		p, e := netip.ParsePrefix(v)
		if e != nil {
			return nil, errors.New("invalid allowed_sources prefix")
		}
		out = append(out, p.Masked())
	}
	return out, nil
}

func (c config) validate() error {
	if c.SplitUDP && (c.TUN == "" || c.packetQueueSize() < 2 || c.QueueBytes == 1) {
		return errors.New("split-udp requires TUN mode, at least 2 queue packets, and byte budget 0 or >=2")
	}
	if c.Role != "ingress" && c.Role != "egress" {
		return errors.New("role must be ingress or egress")
	}
	if c.Mode != "dial" && c.Mode != "listen" {
		return errors.New("mode must be dial or listen")
	}
	if len(c.Token) < 32 || len(c.Token) > 256 || strings.ContainsAny(c.Token, "\r\n") {
		return errors.New("token must be 32..256 characters")
	}
	if !strings.HasPrefix(c.Path, "/") || strings.ContainsAny(c.Path, "?#\r\n") || len(c.Path) > 128 {
		return errors.New("invalid path")
	}
	if c.TUN != "" {
		if c.UDP != "" || !validIfName(c.TUN) {
			return errors.New("TUN mode needs a valid interface name and no UDP endpoint")
		}
		if _, e := c.sources(); e != nil {
			return e
		}
	} else if c.Offload {
		return errors.New("tun_offload requires TUN mode")
	} else {
		host, _, splitErr := net.SplitHostPort(c.UDP)
		ip := net.ParseIP(host)
		a, e := net.ResolveUDPAddr("udp", c.UDP)
		if splitErr != nil || ip == nil || !ip.IsLoopback() || e != nil || a.Port == 0 {
			return errors.New("UDP must use an explicit loopback IP and port")
		}
	}
	if c.NotSentLowat < 0 || c.NotSentLowat > 1<<24 || c.QueueBytes < 0 || c.QueueBytes > 1<<24 {
		return errors.New("notsent_lowat/queue_bytes must be 0..16777216")
	}
	if c.QueuePackets < 0 || c.QueuePackets > 256 {
		return errors.New("queue_packets must be 0 (default 64) or 1..256")
	}
	if c.Padding < 0 || c.Padding > maxPadding {
		return errors.New("padding must be 0..256")
	}
	if c.Mode == "dial" {
		u, e := url.Parse(c.URL)
		if e != nil || u.Scheme != "wss" || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Path != c.Path {
			return errors.New("WSS URL must match path without credentials or query")
		}
	} else {
		_, p, e := net.SplitHostPort(c.Listen)
		if e != nil || p == "0" || p == "" {
			return errors.New("invalid listen address")
		}
	}
	return nil
}
func (c config) tlsConfig() (*tls.Config, error) {
	t := &tls.Config{MinVersion: tls.VersionTLS12}
	if c.Mode == "listen" {
		cert, e := tls.X509KeyPair([]byte(c.Cert), []byte(c.Key))
		if e != nil {
			return nil, errors.New("invalid listener certificate/key")
		}
		t.Certificates = []tls.Certificate{cert}
	} else {
		t.ServerName = c.ServerName
		if c.CA != "" {
			p := x509.NewCertPool()
			if !p.AppendCertsFromPEM([]byte(c.CA)) {
				return nil, errors.New("invalid CA")
			}
			t.RootCAs = p
		}
	}
	return t, nil
}

type metrics struct {
	Connected   atomic.Int64
	Sent        atomic.Uint64
	Received    atomic.Uint64
	Dropped     atomic.Uint64
	DropReasons [dropReasonCount]atomic.Uint64
	Padding     atomic.Uint64
	Sessions    atomic.Uint64
	ActiveFlows atomic.Int64
	Messages    atomic.Uint64
	Spoofed     atomic.Uint64
}

type dropReason uint8

const (
	dropInvalidSize dropReason = iota
	dropCancelledSession
	dropQueueBytes
	dropQueuePackets
	dropFlowLimit
	dropTUNWrite
	dropUnknownFlow
	dropQueueExpired
	dropNoSessionOrOversize
	dropInvalidVnetMetadata
	dropUnsupportedOffload
	dropReasonCount
)

var dropNames = [dropReasonCount]string{"invalid_size", "cancelled_session", "queue_bytes", "queue_packets", "flow_limit", "tun_write", "unknown_flow", "queue_expired", "no_session_or_oversize", "invalid_vnet_metadata", "unsupported_offload"}

func (m *metrics) drop(reason dropReason) { m.Dropped.Add(1); m.DropReasons[reason].Add(1) }
func (m *metrics) snapshot() map[string]interface{} {
	reasons := map[string]uint64{}
	for i, name := range dropNames {
		reasons[name] = m.DropReasons[i].Load()
	}
	return map[string]interface{}{"drop_reasons": reasons, "version": version, "connected": m.Connected.Load(), "tx_datagrams": m.Sent.Load(), "rx_datagrams": m.Received.Load(), "drops": m.Dropped.Load(), "padding_bytes": m.Padding.Load(), "sessions": m.Sessions.Load(), "flows": m.ActiveFlows.Load(), "ws_messages": m.Messages.Load(), "spoofed_drops": m.Spoofed.Load()}
}
func encode(id uint64, p []byte, pad int) ([]byte, error) {
	if id == 0 || len(p) == 0 || len(p) > maxPayload || pad < 0 || pad > maxPadding {
		return nil, errors.New("invalid datagram")
	}
	b := make([]byte, headerSize+len(p)+pad)
	b[0] = 1
	b[1] = 1
	binary.BigEndian.PutUint64(b[2:10], id)
	binary.BigEndian.PutUint16(b[10:12], uint16(len(p)))
	copy(b[headerSize:], p)
	if pad > 0 {
		if _, e := rand.Read(b[headerSize+len(p):]); e != nil {
			return nil, e
		}
	}
	return b, nil
}
func decode(b []byte) (uint64, []byte, error) {
	if len(b) < headerSize || b[0] != 1 || b[1] != 1 {
		return 0, nil, errors.New("invalid frame")
	}
	id := binary.BigEndian.Uint64(b[2:10])
	n := int(binary.BigEndian.Uint16(b[10:12]))
	pad := len(b) - headerSize - n
	if id == 0 || n == 0 || n > maxPayload || pad < 0 || pad > maxPadding {
		return 0, nil, errors.New("invalid length or flow")
	}
	return id, b[headerSize : headerSize+n], nil
}

// appendEntry adds one datagram to a batch message under construction.
func appendEntry(b []byte, id uint64, p []byte) []byte {
	var h [entryHeader]byte
	binary.BigEndian.PutUint64(h[0:8], id)
	binary.BigEndian.PutUint16(h[8:10], uint16(len(p)))
	return append(append(b, h[:]...), p...)
}

// decodeBatch validates a whole batch before yielding any datagram.
func decodeBatch(b []byte, fn func(uint64, []byte) error) error {
	if len(b) < batchHeader || b[0] != 1 || b[1] != batchType {
		return errors.New("invalid batch")
	}
	count := int(binary.BigEndian.Uint16(b[2:4]))
	if count == 0 || count > maxBatchMsgs {
		return errors.New("invalid batch count")
	}
	type ent struct {
		id uint64
		p  []byte
	}
	var ents [maxBatchMsgs]ent
	off := batchHeader
	for i := 0; i < count; i++ {
		if len(b)-off < entryHeader {
			return errors.New("truncated batch")
		}
		id := binary.BigEndian.Uint64(b[off : off+8])
		n := int(binary.BigEndian.Uint16(b[off+8 : off+10]))
		off += entryHeader
		if id == 0 || n == 0 || n > maxPayload || len(b)-off < n {
			return errors.New("invalid batch entry")
		}
		ents[i] = ent{id, b[off : off+n]}
		off += n
	}
	if len(b)-off > maxPadding {
		return errors.New("invalid batch padding")
	}
	for i := 0; i < count; i++ {
		if e := fn(ents[i].id, ents[i].p); e != nil {
			return e
		}
	}
	return nil
}

type packet struct {
	id    uint64
	data  []byte
	at    time.Time
	flags uint8
	gso   uint16
}

// appendOffload adds one IP packet plus sender offload hints.
func appendOffload(b []byte, p packet) []byte {
	var h [offloadEntry]byte
	h[0] = p.flags
	binary.BigEndian.PutUint16(h[1:3], p.gso)
	binary.BigEndian.PutUint32(h[3:7], uint32(len(p.data)))
	return append(append(b, h[:]...), p.data...)
}

// decodeOffload validates a whole offload message before yielding packets.
func decodeOffload(b []byte, fn func(uint8, uint16, []byte) error) error {
	if len(b) < batchHeader || b[0] != 1 || b[1] != offloadType {
		return errors.New("invalid offload frame")
	}
	count := int(binary.BigEndian.Uint16(b[2:4]))
	if count == 0 || count > maxBatchMsgs {
		return errors.New("invalid offload count")
	}
	type ent struct {
		flags uint8
		gso   uint16
		p     []byte
	}
	var ents [maxBatchMsgs]ent
	off := batchHeader
	for i := 0; i < count; i++ {
		if len(b)-off < offloadEntry {
			return errors.New("truncated offload frame")
		}
		f, g := b[off], binary.BigEndian.Uint16(b[off+1:off+3])
		n := binary.BigEndian.Uint32(b[off+3 : off+7])
		off += offloadEntry
		if f&^(metaNeedsCsum|metaECN) != 0 || n < 20 || n > maxSuper || uint32(len(b)-off) < n {
			return errors.New("invalid offload entry")
		}
		ents[i] = ent{f, g, b[off : off+int(n)]}
		off += int(n)
	}
	if len(b)-off > maxPadding {
		return errors.New("invalid offload padding")
	}
	for i := 0; i < count; i++ {
		if e := fn(ents[i].flags, ents[i].gso, ents[i].p); e != nil {
			return e
		}
	}
	return nil
}

type flow struct {
	id   uint64
	addr *net.UDPAddr
	udp  *net.UDPConn
	seen time.Time
}
type engine struct {
	cfg      config
	m        metrics
	mu       sync.Mutex
	current  *session
	datagram *session
	lanes    [2]laneMetrics
	udp      *net.UDPConn
	tun      *os.File
	allowed  []netip.Prefix
}

// sourceAllowed drops IP packets whose source the peer may not use.
func (e *engine) sourceAllowed(p []byte) bool {
	var a netip.Addr
	switch {
	case len(p) >= 20 && p[0]>>4 == 4:
		a = netip.AddrFrom4([4]byte(p[12:16]))
	case len(p) >= 40 && p[0]>>4 == 6:
		a = netip.AddrFrom16([16]byte(p[8:24])).Unmap()
	default:
		return false
	}
	for _, pre := range e.allowed {
		if pre.Contains(a) {
			return true
		}
	}
	return false
}

type session struct {
	e         *engine
	ws        *websocket.Conn
	ctx       context.Context
	cancel    context.CancelFunc
	q         chan packet
	mu        sync.Mutex
	flows     map[uint64]*flow
	addrs     map[string]uint64
	next      uint64
	wg        sync.WaitGroup
	finished  chan struct{}
	scratch   []byte
	qbytes    atomic.Int64
	dgram     bool
	byteLimit int
}

// took releases a dequeued packet's share of the byte budget.
func (s *session) took(p packet) packet { s.qbytes.Add(-int64(len(p.data))); return p }

func (e *engine) reserve(ws *websocket.Conn, ctx context.Context) *session {
	return e.reserveLane(ws, ctx, false, nil)
}

// slot and readiness require e.mu. An upgrade placeholder is never ready.
func (e *engine) slot(dgram bool) **session {
	if dgram {
		return &e.datagram
	}
	return &e.current
}
func (e *engine) readiness() {
	ready := func(s *session) bool { return s != nil && s.q != nil && s.ctx.Err() == nil }
	if ready(e.current) && (!e.cfg.SplitUDP || ready(e.datagram)) {
		e.m.Connected.Store(1)
	} else {
		e.m.Connected.Store(0)
	}
}
func (e *engine) reserveLane(ws *websocket.Conn, ctx context.Context, dgram bool, hold *session) *session {
	e.mu.Lock()
	defer e.mu.Unlock()
	slot := e.slot(dgram)
	if *slot != hold {
		return nil
	}
	if ctx.Err() != nil {
		*slot = nil
		e.readiness()
		return nil
	}
	ctx, cancel := context.WithCancel(ctx)
	packets, budget := e.cfg.packetQueueSize(), e.cfg.QueueBytes
	if e.cfg.SplitUDP {
		packets /= 2
		budget /= 2
		if !dgram {
			packets += e.cfg.packetQueueSize() % 2
			budget += e.cfg.QueueBytes % 2
		}
	}
	s := &session{e: e, ws: ws, ctx: ctx, cancel: cancel, q: make(chan packet, packets), flows: map[uint64]*flow{}, addrs: map[string]uint64{}, finished: make(chan struct{}), dgram: dgram, byteLimit: budget}
	*slot = s
	e.readiness()
	e.m.Sessions.Add(1)
	return s
}

// Select under e.mu, then copy into a bounded independent writer queue.
func (e *engine) enqueueIP(p []byte, flags uint8, gso uint16) {
	e.mu.Lock()
	defer e.mu.Unlock()
	s := e.current
	if e.cfg.SplitUDP && datagramIP(p) {
		s = e.datagram
	}
	if s == nil || s.q == nil {
		e.m.drop(dropNoSessionOrOversize)
		return
	}
	s.enqueueMeta(tunFlow, p, flags, gso)
}
func (s *session) byteBudget() int {
	if s.e.cfg.SplitUDP {
		return s.byteLimit
	}
	return s.e.cfg.QueueBytes
}
func (s *session) enqueue(id uint64, p []byte) { s.enqueueMeta(id, p, 0, 0) }
func (s *session) enqueueMeta(id uint64, p []byte, flags uint8, gso uint16) {
	limit := maxPayload
	if s.e.cfg.Offload {
		limit = maxSuper
	}
	if len(p) == 0 || len(p) > limit {
		s.drop(dropInvalidSize)
		return
	}
	select {
	case <-s.ctx.Done():
		s.drop(dropCancelledSession)
		return
	default:
	}
	for {
		old := s.qbytes.Load()
		next := old + int64(len(p))
		if c := int64(s.byteBudget()); c > 0 && next > c {
			s.drop(dropQueueBytes)
			return
		}
		if s.qbytes.CompareAndSwap(old, next) {
			break
		}
	}
	v := packet{id: id, data: append([]byte(nil), p...), at: time.Now(), flags: flags, gso: gso}
	select {
	case s.q <- v:
	default:
		s.qbytes.Add(-int64(len(p)))
		s.drop(dropQueuePackets)
	}
}
func (s *session) ingress(a *net.UDPAddr, p []byte) {
	s.mu.Lock()
	key := a.String()
	id, ok := s.addrs[key]
	if !ok {
		if len(s.flows) >= maxFlows {
			s.mu.Unlock()
			s.drop(dropFlowLimit)
			return
		}
		s.next++
		id = s.next
		s.flows[id] = &flow{id: id, addr: a}
		s.addrs[key] = id
		s.e.m.ActiveFlows.Add(1)
	}
	s.flows[id].seen = time.Now()
	s.mu.Unlock()
	s.enqueue(id, p)
}
func (s *session) deliver(id uint64, p []byte) error {
	if s.e.cfg.SplitUDP && datagramIP(p) != s.dgram {
		return errors.New("wrong transport lane")
	}
	if s.e.tun != nil {
		if id != tunFlow || !s.e.sourceAllowed(p) {
			s.e.m.Spoofed.Add(1)
			return nil
		}
		_, err := s.e.tun.Write(p)
		if err != nil {
			s.drop(dropTUNWrite)
		}
		return nil
	}
	s.mu.Lock()
	f := s.flows[id]
	if s.e.cfg.Role == "ingress" {
		if f != nil {
			f.seen = time.Now()
		}
		s.mu.Unlock()
		if f == nil {
			s.drop(dropUnknownFlow)
			return nil
		}
		s.e.udp.SetWriteDeadline(time.Now().Add(time.Second))
		_, err := s.e.udp.WriteToUDP(p, f.addr)
		return err
	}
	if f == nil {
		if len(s.flows) >= maxFlows {
			s.mu.Unlock()
			s.drop(dropFlowLimit)
			return nil
		}
		a, err := net.ResolveUDPAddr("udp", s.e.cfg.UDP)
		if err != nil {
			s.mu.Unlock()
			return err
		}
		u, err := net.DialUDP("udp", nil, a)
		if err != nil {
			s.mu.Unlock()
			return err
		}
		f = &flow{id: id, udp: u}
		s.flows[id] = f
		s.e.m.ActiveFlows.Add(1)
		s.wg.Add(1)
		go s.replies(f)
	}
	f.seen = time.Now()
	s.mu.Unlock()
	f.udp.SetWriteDeadline(time.Now().Add(time.Second))
	_, err := f.udp.Write(p)
	return err
}
func (s *session) replies(f *flow) {
	defer s.wg.Done()
	defer func() {
		f.udp.Close()
		s.mu.Lock()
		if s.flows[f.id] == f {
			delete(s.flows, f.id)
			s.e.m.ActiveFlows.Add(-1)
		}
		s.mu.Unlock()
	}()
	b := make([]byte, maxPayload+1)
	for {
		n, err := f.udp.Read(b)
		if err != nil {
			return
		}
		s.mu.Lock()
		live := s.flows[f.id] == f
		if live {
			f.seen = time.Now()
		}
		s.mu.Unlock()
		if live {
			s.enqueue(f.id, b[:n])
		}
	}
}
func (s *session) reap(now time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for id, f := range s.flows {
		if now.Sub(f.seen) > 2*time.Minute {
			if f.udp != nil {
				f.udp.Close()
			}
			if f.addr != nil {
				delete(s.addrs, f.addr.String())
			}
			delete(s.flows, id)
			s.e.m.ActiveFlows.Add(-1)
		}
	}
}
func (s *session) writeLoop() {
	defer s.wg.Done()
	defer s.cancel()
	ping := time.NewTicker(20 * time.Second)
	defer ping.Stop()
	reap := time.NewTicker(15 * time.Second)
	defer reap.Stop()
	count := 0
	batchBuf := make([]byte, 0, maxBatch+maxPadding)
	if s.e.cfg.Offload {
		batchBuf = make([]byte, 0, maxOffloadMsg+maxPadding)
	}
	for {
		select {
		case <-s.ctx.Done():
			return
		case now := <-reap.C:
			s.reap(now)
		case <-ping.C:
			if s.ws.WriteControl(websocket.PingMessage, nil, time.Now().Add(5*time.Second)) != nil {
				return
			}
		case p := <-s.q:
			p = s.took(p)
			if time.Since(p.at) > time.Second {
				s.drop(dropQueueExpired)
				continue
			}
			pad := 0
			if count < 8 {
				pad = s.e.cfg.Padding
			}
			count++
			var b []byte
			sent := uint64(1)
			if s.e.cfg.Offload || s.e.cfg.Batch {
				b, sent = s.batch(batchBuf[:0], p)
				if pad > 0 {
					n := len(b)
					b = append(b, make([]byte, pad)...)
					if _, err := rand.Read(b[n:]); err != nil {
						return
					}
				}
				batchBuf = b
			} else {
				var err error
				if b, err = encode(p.id, p.data, pad); err != nil {
					return
				}
			}
			s.ws.SetWriteDeadline(time.Now().Add(5 * time.Second))
			started := time.Now()
			err := s.ws.WriteMessage(websocket.BinaryMessage, b)
			s.recordWrite(time.Since(started))
			if err != nil {
				return
			}
			s.laneMetrics().sent.Add(sent)
			s.e.m.Messages.Add(1)
			s.e.m.Sent.Add(sent)
			s.e.m.Padding.Add(uint64(pad))
		}
	}
}

// batch drains datagrams that are already queued; it never waits for more,
// so it adds no latency, only fewer records and syscalls under load.
func (s *session) batch(b []byte, first packet) ([]byte, uint64) {
	if s.e.cfg.Offload {
		return s.batchOffload(b, first)
	}
	b = append(b, 1, batchType, 0, 0)
	b = appendEntry(b, first.id, first.data)
	n := 1
	for n < maxBatchMsgs {
		select {
		case p := <-s.q:
			p = s.took(p)
			if time.Since(p.at) > time.Second {
				s.drop(dropQueueExpired)
				continue
			}
			b = appendEntry(b, p.id, p.data)
			n++
			if len(b)+entryHeader+maxPayload > maxBatch {
				binary.BigEndian.PutUint16(b[2:4], uint16(n))
				return b, uint64(n)
			}
			continue
		default:
		}
		break
	}
	binary.BigEndian.PutUint16(b[2:4], uint16(n))
	return b, uint64(n)
}
func (s *session) batchOffload(b []byte, first packet) ([]byte, uint64) {
	b = append(b, 1, offloadType, 0, 0)
	b = appendOffload(b, first)
	n := 1
	for n < maxBatchMsgs && len(b)+offloadEntry+maxSuper <= maxOffloadMsg {
		select {
		case p := <-s.q:
			p = s.took(p)
			if time.Since(p.at) > time.Second {
				s.drop(dropQueueExpired)
				continue
			}
			b = appendOffload(b, p)
			n++
			continue
		default:
		}
		break
	}
	binary.BigEndian.PutUint16(b[2:4], uint16(n))
	return b, uint64(n)
}
func (s *session) run() {
	defer func() {
		s.cancel()
		s.ws.Close()
		s.mu.Lock()
		for _, f := range s.flows {
			if f.udp != nil {
				f.udp.Close()
			}
		}
		s.e.m.ActiveFlows.Add(-int64(len(s.flows)))
		s.flows = map[uint64]*flow{}
		s.mu.Unlock()
		s.wg.Wait()
		s.e.mu.Lock()
		slot := s.e.slot(s.dgram)
		if *slot == s {
			*slot = nil
		}
		s.e.readiness()
		s.e.mu.Unlock()
		close(s.finished)
	}()
	if s.e.cfg.Offload {
		s.ws.SetReadLimit(maxOffloadMsg + maxPadding)
	} else if s.e.cfg.Batch {
		s.ws.SetReadLimit(maxBatch + maxPadding)
	} else {
		s.ws.SetReadLimit(headerSize + maxPayload + maxPadding)
	}
	s.ws.SetReadDeadline(time.Now().Add(60 * time.Second))
	s.ws.SetPongHandler(func(string) error { return s.ws.SetReadDeadline(time.Now().Add(60 * time.Second)) })
	s.wg.Add(2)
	go s.writeLoop()
	go func() { defer s.wg.Done(); <-s.ctx.Done(); s.ws.Close() }()
	for {
		kind, b, err := s.ws.ReadMessage()
		if err != nil {
			return
		}
		if kind != websocket.BinaryMessage {
			return
		}
		if s.e.cfg.Offload {
			if len(b) < 2 || b[1] != offloadType || decodeOffload(b, func(f uint8, g uint16, p []byte) error {
				s.e.m.Received.Add(1)
				s.laneMetrics().received.Add(1)
				return s.deliverOffload(f, g, p)
			}) != nil {
				return
			}
			continue
		}
		if s.e.cfg.Batch && len(b) > 1 && b[1] == batchType {
			if decodeBatch(b, func(id uint64, p []byte) error {
				s.e.m.Received.Add(1)
				s.laneMetrics().received.Add(1)
				return s.deliver(id, p)
			}) != nil {
				return
			}
			continue
		}
		id, p, err := decode(b)
		if err != nil {
			return
		}
		if err = s.deliver(id, p); err != nil {
			return
		}
		s.e.m.Received.Add(1)
		s.laneMetrics().received.Add(1)
	}
}
func (e *engine) handler(ctx context.Context) http.Handler {
	up := websocket.Upgrader{ReadBufferSize: 2048, WriteBufferSize: 2048, HandshakeTimeout: 5 * time.Second, EnableCompression: false, CheckOrigin: func(r *http.Request) bool { return r.Header.Get("Origin") == "" }}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth := r.Header.Get("Authorization")
		want := "Bearer " + e.cfg.Token
		if r.URL.Path != e.cfg.Path || r.Method != "GET" || len(auth) != len(want) || subtle.ConstantTimeCompare([]byte(auth), []byte(want)) != 1 {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			w.Header().Set("Cache-Control", "no-store")
			w.Write([]byte("<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>Welcome</title><h1>Welcome</h1><p>This site is online.</p></html>"))
			return
		}
		dgram, headers, err := e.negotiate(r)
		if err != nil {
			http.Error(w, "transport mode mismatch", http.StatusConflict)
			return
		}
		// Hold the specific lane across Upgrade and reservation: no slot race.
		e.mu.Lock()
		slot := e.slot(dgram)
		if *slot != nil {
			e.mu.Unlock()
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
			return
		}
		hold := &session{}
		*slot = hold
		e.mu.Unlock()
		ws, err := up.Upgrade(w, r, headers)
		if err != nil {
			e.mu.Lock()
			if *e.slot(dgram) == hold {
				*e.slot(dgram) = nil
			}
			e.readiness()
			e.mu.Unlock()
			return
		}
		s := e.reserveLane(ws, ctx, dgram, hold)
		if s == nil {
			ws.Close()
			return
		}
		s.run()
	})
}

type limitedListener struct {
	net.Listener
	slots chan struct{}
}
type limitedConn struct {
	net.Conn
	once    sync.Once
	release func()
}

func (c *limitedConn) Close() error { err := c.Conn.Close(); c.once.Do(c.release); return err }
func (l *limitedListener) Accept() (net.Conn, error) {
	c, e := l.Listener.Accept()
	if e != nil {
		return nil, e
	}
	select {
	case l.slots <- struct{}{}:
		return &limitedConn{Conn: c, release: func() { <-l.slots }}, nil
	default:
		c.Close()
		return nil, temporaryError{}
	}
}

type temporaryError struct{}

func (temporaryError) Error() string   { return "connection limit" }
func (temporaryError) Temporary() bool { return true }
func (temporaryError) Timeout() bool   { return false }
func (e *engine) serve(ctx context.Context, l net.Listener, t *tls.Config) error {
	srv := &http.Server{Handler: e.handler(ctx), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8192, TLSConfig: t, ErrorLog: log.New(io.Discard, "", 0)}
	done := make(chan struct{})
	defer close(done)
	go func() {
		select {
		case <-ctx.Done():
			srv.Close()
		case <-done:
		}
	}()
	err := srv.Serve(tls.NewListener(&limitedListener{Listener: l, slots: make(chan struct{}, 32)}, t))
	if ctx.Err() != nil {
		return nil
	}
	return err
}
func (e *engine) dial(ctx context.Context, t *tls.Config) error {
	if !e.cfg.SplitUDP {
		return e.dialLane(ctx, t, false)
	}
	var wg sync.WaitGroup
	wg.Add(1)
	go func() { defer wg.Done(); e.dialLane(ctx, t, true) }()
	err := e.dialLane(ctx, t, false)
	wg.Wait()
	return err
}
func (e *engine) dialLane(ctx context.Context, t *tls.Config, dgram bool) error {
	delay := time.Second
	for ctx.Err() == nil {
		started := time.Now()
		nd := &net.Dialer{Timeout: 10 * time.Second, Control: lowatControl(e.cfg.NotSentLowat)}
		d := websocket.Dialer{TLSClientConfig: t, HandshakeTimeout: 10 * time.Second, EnableCompression: false, Proxy: nil, NetDialContext: nd.DialContext}
		headers := http.Header{"Authorization": []string{"Bearer " + e.cfg.Token}}
		if e.cfg.SplitUDP {
			headers.Set(modeHeader, splitMode)
			headers.Set(laneHeader, laneName(dgram))
		}
		ws, res, err := d.DialContext(ctx, e.cfg.URL, headers)
		if err == nil && e.cfg.SplitUDP && (res == nil || res.Header.Get(modeHeader) != splitMode || res.Header.Get(laneHeader) != laneName(dgram)) {
			ws.Close()
			err = errors.New("peer did not confirm split transport")
		}
		if res != nil && res.Body != nil {
			res.Body.Close()
		}
		if err == nil {
			s := e.reserveLane(ws, ctx, dgram, nil)
			if s != nil {
				s.run()
			} else {
				ws.Close()
			}
		}
		if time.Since(started) > time.Minute {
			delay = time.Second
		}
		var b [1]byte
		rand.Read(b[:])
		jitter := time.Duration(b[0]) * delay / 512
		timer := time.NewTimer(delay + jitter)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil
		case <-timer.C:
		}
		if delay < 15*time.Second {
			delay *= 2
			if delay > 15*time.Second {
				delay = 15 * time.Second
			}
		}
	}
	return nil
}
func (e *engine) run(parent context.Context) error {
	ctx, cancel := context.WithCancel(parent)
	defer cancel()
	if err := e.cfg.validate(); err != nil {
		return err
	}
	t, err := e.cfg.tlsConfig()
	if err != nil {
		return err
	}
	var workers sync.WaitGroup
	defer func() {
		cancel()
		if e.udp != nil {
			e.udp.Close()
		}
		if e.tun != nil {
			e.tun.Close()
		}
		e.mu.Lock()
		ss := []*session{e.current, e.datagram}
		e.mu.Unlock()
		for _, s := range ss {
			if s != nil && s.cancel != nil {
				s.cancel()
			}
		}
		for _, s := range ss {
			if s != nil && s.cancel != nil {
				<-s.finished
			}
		}
		workers.Wait()
	}()
	if e.cfg.TUN != "" {
		if e.allowed, err = e.cfg.sources(); err != nil {
			return err
		}
		if e.tun, err = openTUN(e.cfg.TUN, e.cfg.Offload); err != nil {
			return errors.New("TUN open failed: " + err.Error())
		}
		workers.Add(1)
		go func() {
			defer workers.Done()
			if e.cfg.Offload {
				e.readOffload(ctx)
				return
			}
			b := make([]byte, maxPayload+1)
			for {
				n, err := e.tun.Read(b)
				if err != nil {
					if ctx.Err() != nil || errors.Is(err, os.ErrClosed) {
						return
					}
					continue
				}
				if n <= maxPayload {
					e.enqueueIP(b[:n], 0, 0)
				} else {
					e.m.drop(dropNoSessionOrOversize)
				}
			}
		}()
	} else if e.cfg.Role == "ingress" {
		a, _ := net.ResolveUDPAddr("udp", e.cfg.UDP)
		e.udp, err = net.ListenUDP("udp", a)
		if err != nil {
			return errors.New("UDP listen failed")
		}
		workers.Add(1)
		go func() {
			defer workers.Done()
			b := make([]byte, maxPayload+1)
			for {
				n, a, err := e.udp.ReadFromUDP(b)
				if err != nil {
					return
				}
				e.mu.Lock()
				s := e.current
				if s != nil && s.q != nil {
					s.ingress(a, b[:n])
				} else {
					e.m.drop(dropNoSessionOrOversize)
				}
				e.mu.Unlock()
			}
		}()
	}
	if e.cfg.Stats != "" {
		workers.Add(1)
		go func() {
			defer workers.Done()
			tick := time.NewTicker(2 * time.Second)
			defer tick.Stop()
			for {
				select {
				case <-ctx.Done():
					return
				case <-tick.C:
					b, _ := json.Marshal(e.snapshot())
					tmp := e.cfg.Stats + ".part"
					if os.WriteFile(tmp, b, 0600) == nil {
						os.Rename(tmp, e.cfg.Stats)
					}
				}
			}
		}()
	}
	if e.cfg.Mode == "dial" {
		return e.dial(ctx, t)
	}
	lc := net.ListenConfig{Control: lowatControl(e.cfg.NotSentLowat)}
	l, err := lc.Listen(ctx, "tcp", e.cfg.Listen)
	if err != nil {
		return errors.New("TCP listen failed")
	}
	return e.serve(ctx, l, t)
}
func main() {
	file := flag.String("config", "", "root-owned JSON configuration")
	show := flag.Bool("version", false, "show version")
	check := flag.Bool("check", false, "validate without opening sockets")
	split := flag.Bool("split-udp", false, "experimental separate TCP and datagram WSS lanes (both peers required)")
	flag.Parse()
	if *show {
		fmt.Println(version)
		return
	}
	if *file == "" {
		log.Fatal("--config is required")
	}
	fh, err := os.Open(*file)
	if err != nil {
		log.Fatal("cannot read configuration")
	}
	b, err := io.ReadAll(io.LimitReader(fh, 65537))
	fh.Close()
	if len(b) > 65536 {
		log.Fatal("configuration too large")
	}
	if err != nil {
		log.Fatal("cannot read configuration")
	}
	var c config
	d := json.NewDecoder(strings.NewReader(string(b)))
	d.DisallowUnknownFields()
	if d.Decode(&c) != nil || d.Decode(new(interface{})) != io.EOF {
		log.Fatal("invalid configuration")
	}
	c.SplitUDP = *split
	if err = c.validate(); err != nil {
		log.Fatal(err)
	}
	if _, err = c.tlsConfig(); err != nil {
		log.Fatal(err)
	}
	if *check {
		return
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	e := engine{cfg: c}
	if err = e.run(ctx); err != nil {
		log.Fatal("transport stopped: ", err)
	}
}
