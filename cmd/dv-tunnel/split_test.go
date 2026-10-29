package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/binary"
	"io"
	"net"
	"net/http"
	"os"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func ipPacket(proto byte) []byte {
	p := make([]byte, 40)
	p[0] = 0x45
	p[9] = proto
	binary.BigEndian.PutUint16(p[2:4], uint16(len(p)))
	copy(p[12:16], []byte{10, 254, 253, 1})
	copy(p[16:20], []byte{10, 254, 253, 2})
	return p
}
func TestSplitClassifier(t *testing.T) {
	for _, proto := range []byte{1, 6, 17, 50} {
		p := ipPacket(proto)
		if datagramIP(p) != (proto == 1 || proto == 17) {
			t.Fatalf("IPv4 %d", proto)
		}
		p[6] = 0x20
		if datagramIP(p) != (proto == 1 || proto == 17) {
			t.Fatal("IPv4 fragment")
		}
	}
	p := make([]byte, 56)
	p[0] = 0x60
	p[6] = 0
	p[40] = 17
	if !datagramIP(p) {
		t.Fatal("IPv6 UDP extension")
	}
	p[40] = 58
	if !datagramIP(p) {
		t.Fatal("IPv6 ICMP extension")
	}
	p[40] = 6
	if datagramIP(p) {
		t.Fatal("IPv6 TCP extension")
	}
	p[6] = 44
	p[40] = 17
	p[43] = 8
	if !datagramIP(p) {
		t.Fatal("IPv6 nonfirst UDP fragment")
	}
	p[40] = 6
	if datagramIP(p) {
		t.Fatal("IPv6 nonfirst TCP fragment")
	}
	for n := 0; n < 40; n++ {
		if datagramIP(make([]byte, n)) {
			t.Fatal("short packet")
		}
	}
	p[6] = 0
	p[41] = 255
	if datagramIP(p) {
		t.Fatal("unbounded extension")
	}
}
func TestSplitBudgetAndReadiness(t *testing.T) {
	for _, cap := range []int{2, 3, 64, 255, 256} {
		e := &engine{cfg: config{SplitUDP: true, QueuePackets: cap, QueueBytes: 262145}}
		a := e.reserveLane(nil, context.Background(), false, nil)
		if e.m.Connected.Load() != 0 {
			t.Fatal("half-ready")
		}
		b := e.reserveLane(nil, context.Background(), true, nil)
		defer a.cancel()
		defer b.cancel()
		if e.m.Connected.Load() != 1 || len(a.q)+len(b.q) != 0 || capOf(a.q)+capOf(b.q) != cap || a.byteBudget()+b.byteBudget() != 262145 {
			t.Fatal("budget or readiness")
		}
		if e.reserveLane(nil, context.Background(), true, nil) != nil {
			t.Fatal("duplicate accepted")
		}
		for n := 0; n < cap+1; n++ {
			e.enqueueIP(ipPacket(6), 0, 0)
			e.enqueueIP(ipPacket(17), 0, 0)
		}
		if len(a.q) != capOf(a.q) || len(b.q) != capOf(b.q) {
			t.Fatal("wrong lane classification")
		}
		a.cancel()
		d := e.snapshot()
		if d["connected"] != int64(0) {
			t.Fatal("cancelled ready")
		}
		if e.lanes[0].drops[dropQueuePackets].Load() == 0 || e.lanes[1].drops[dropQueuePackets].Load() == 0 {
			t.Fatal("no lane attribution")
		}
	}
}
func capOf(q chan packet) int { return cap(q) }
func TestSplitRejectsWrongLane(t *testing.T) {
	e := &engine{cfg: config{SplitUDP: true}}
	s := e.reserveLane(nil, context.Background(), false, nil)
	defer s.cancel()
	if s.deliver(tunFlow, ipPacket(17)) == nil || s.deliverOffload(0, 0, ipPacket(17)) == nil {
		t.Fatal("wrong lane delivered")
	}
}
func TestSplitConfigBounds(t *testing.T) {
	c := config{Role: "ingress", Mode: "dial", URL: "wss://webtcp.test:42443/relay", Path: "/relay", Token: strings.Repeat("x", 40), TUN: "wft0", Sources: []string{"10.254.253.2/32"}, SplitUDP: true}
	if err := c.validate(); err != nil {
		t.Fatal(err)
	}
	for _, q := range []int{1} {
		c.QueuePackets = q
		if c.validate() == nil {
			t.Fatal("packet budget")
		}
	}
	c.QueuePackets = 64
	c.QueueBytes = 1
	if c.validate() == nil {
		t.Fatal("one-byte budget")
	}
	c.QueueBytes = 0
	c.TUN = ""
	c.UDP = "127.0.0.1:42442"
	if c.validate() == nil {
		t.Fatal("split relay accepted")
	}
}

type gatedConn struct {
	net.Conn
	blocked atomic.Bool
	entered chan struct{}
	release chan struct{}
	once    sync.Once
}

func (g *gatedConn) Write(p []byte) (int, error) {
	if g.blocked.Load() {
		g.once.Do(func() { close(g.entered) })
		<-g.release
	}
	return g.Conn.Write(p)
}

// Real TLS/WSS sockets, but fake pipe TUNs. One outer writer is intentionally
// stalled; UDP delivery in BOTH directions must continue without that writer.
func TestSplitStalledBulkIsolationAndReconnect(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cert, key := certificate(t)
	cfg := config{Role: "egress", Mode: "listen", Listen: "127.0.0.1:0", Path: "/relay", Token: strings.Repeat("x", 40), Cert: cert, Key: key, TUN: "wft0", Sources: []string{"10.254.253.1/32"}, Offload: true, SplitUDP: true, QueueBytes: 262144}
	server := &engine{cfg: cfg}
	server.allowed, _ = cfg.sources()
	client := &engine{cfg: cfg}
	client.allowed, _ = cfg.sources()
	sr, sw, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer sr.Close()
	defer sw.Close()
	server.tun = sw
	cr, cw, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer cr.Close()
	defer cw.Close()
	client.tun = cw
	tlsCfg, err := cfg.tlsConfig()
	if err != nil {
		t.Fatal(err)
	}
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	served := make(chan error, 1)
	go func() { served <- server.serve(ctx, l, tlsCfg) }()
	defer func() {
		cancel()
		l.Close()
		select {
		case <-served:
		case <-time.After(3 * time.Second):
			t.Error("server leak")
		}
	}()
	roots := x509.NewCertPool()
	roots.AppendCertsFromPEM([]byte(cert))
	var gate *gatedConn
	dial := func(lane string) (*websocket.Conn, *http.Response, error) {
		d := websocket.Dialer{TLSClientConfig: &tls.Config{RootCAs: roots, ServerName: "webtcp.test", MinVersion: tls.VersionTLS12}, HandshakeTimeout: time.Second}
		if lane == "tcp" {
			d.NetDialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
				c, err := (&net.Dialer{}).DialContext(ctx, network, address)
				if err != nil {
					return nil, err
				}
				gate = &gatedConn{Conn: c, entered: make(chan struct{}), release: make(chan struct{})}
				return gate, nil
			}
		}
		h := http.Header{}
		h.Set("Authorization", "Bearer "+cfg.Token)
		h.Set(modeHeader, splitMode)
		h.Set(laneHeader, lane)
		return d.DialContext(ctx, "wss://"+l.Addr().String()+cfg.Path, h)
	}
	bulk, res, err := dial("tcp")
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	var releaseOnce sync.Once
	unblock := func() { gate.blocked.Store(false); gate.Conn.Close(); releaseOnce.Do(func() { close(gate.release) }) }
	defer unblock()
	a := client.reserveLane(bulk, ctx, false, nil)
	go a.run()
	defer func() { unblock(); a.cancel(); <-a.finished }()
	if client.m.Connected.Load() != 0 {
		t.Fatal("one lane ready")
	}
	udp, res, err := dial("udp")
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	b := client.reserveLane(udp, ctx, true, nil)
	go b.run()
	defer func() { b.cancel(); <-b.finished }()
	wait(t, func() bool { return server.m.Connected.Load() == 1 && client.m.Connected.Load() == 1 })
	duplicate, r, err := dial("udp")
	if duplicate != nil {
		duplicate.Close()
	}
	if err == nil || r.StatusCode != 503 {
		t.Fatal("duplicate lane admitted")
	}
	r.Body.Close()
	gate.blocked.Store(true)
	client.enqueueIP(ipPacket(6), 0, 0)
	select {
	case <-gate.entered:
	case <-time.After(time.Second):
		t.Fatal("writer did not stall")
	}
	for n := 0; n < 100; n++ {
		client.enqueueIP(ipPacket(6), 0, 0)
	}
	want := ipPacket(17)
	client.enqueueIP(want, 0, 0)
	if err = sr.SetReadDeadline(time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	got := make([]byte, vnetHdrLen+len(want))
	if _, err = io.ReadFull(sr, got); err != nil || !bytes.Equal(got[vnetHdrLen:], want) {
		t.Fatalf("UDP blocked behind bulk: %v", err)
	}
	server.enqueueIP(want, 0, 0)
	cr.SetReadDeadline(time.Now().Add(time.Second))
	if _, err = io.ReadFull(cr, got); err != nil || !bytes.Equal(got[vnetHdrLen:], want) {
		t.Fatalf("reverse UDP blocked: %v", err)
	}
	if client.lanes[0].drops[dropQueuePackets].Load() == 0 || client.lanes[1].drops[dropQueuePackets].Load() != 0 {
		t.Fatal("isolation counters")
	}
	// Reconnect only the datagram lane. Bulk connection identity must survive.
	server.mu.Lock()
	oldServerBulk := server.current
	server.mu.Unlock()
	b.cancel()
	<-b.finished
	wait(t, func() bool { server.mu.Lock(); defer server.mu.Unlock(); return server.datagram == nil })
	udp, res, err = dial("udp")
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	newer := client.reserveLane(udp, ctx, true, nil)
	go newer.run()
	defer func() { newer.cancel(); <-newer.finished }()
	wait(t, func() bool { return server.m.Connected.Load() == 1 && client.m.Connected.Load() == 1 })
	server.mu.Lock()
	unchanged := server.current == oldServerBulk
	server.mu.Unlock()
	if !unchanged {
		t.Fatal("bulk replaced by datagram reconnect")
	}
	client.enqueueIP(want, 0, 0)
	sr.SetReadDeadline(time.Now().Add(time.Second))
	if _, err = io.ReadFull(sr, got); err != nil {
		t.Fatal("UDP after reconnect", err)
	}
	// Unblock BEFORE deferred run cleanup, which waits for the writer.
	unblock()
}

func TestSplitNegotiationMismatch(t *testing.T) {
	for _, enabled := range []bool{false, true} {
		e := &engine{cfg: config{SplitUDP: enabled}}
		for _, mode := range []string{"", splitMode, "future-mode"} {
			for _, lane := range []string{"", "tcp", "udp", "oops"} {
				r, _ := http.NewRequest("GET", "https://example.test/", nil)
				r.Header.Set(modeHeader, mode)
				r.Header.Set(laneHeader, lane)
				_, h, err := e.negotiate(r)
				valid := (!enabled && mode == "" && lane == "") || (enabled && mode == splitMode && (lane == "tcp" || lane == "udp"))
				if (err == nil) != valid {
					t.Fatal("negotiation mismatch", enabled, mode, lane)
				}
				if valid && enabled && h.Get(laneHeader) != lane {
					t.Fatal("no confirmation")
				}
			}
		}
	}
}

func TestSplitDialLifecycle(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cert, key := certificate(t)
	server := &engine{cfg: config{Mode: "listen", SplitUDP: true, Cert: cert, Key: key, Token: strings.Repeat("x", 40), Path: "/relay"}}
	cfg, _ := server.cfg.tlsConfig()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	serverDone := make(chan error, 1)
	go func() { serverDone <- server.serve(ctx, l, cfg) }()
	roots := x509.NewCertPool()
	roots.AppendCertsFromPEM([]byte(cert))
	client := &engine{cfg: config{SplitUDP: true, Token: server.cfg.Token, URL: "wss://" + l.Addr().String() + "/relay"}}
	done := make(chan error, 1)
	go func() {
		done <- client.dial(ctx, &tls.Config{RootCAs: roots, ServerName: "webtcp.test", MinVersion: tls.VersionTLS12})
	}()
	defer func() {
		cancel()
		l.Close()
		select {
		case <-done:
		case <-time.After(3 * time.Second):
			t.Error("dial lifecycle leak")
		}
		<-serverDone
	}()
	wait(t, func() bool { return server.m.Connected.Load() == 1 && client.m.Connected.Load() == 1 })
	if client.m.Sessions.Load() != 2 {
		t.Fatal("not two outer TCP connections")
	}
	server.mu.Lock()
	bulk := server.current
	udp := server.datagram
	udp.cancel()
	server.mu.Unlock()
	wait(t, func() bool {
		return client.m.Sessions.Load() >= 3 && client.m.Connected.Load() == 1 && server.m.Connected.Load() == 1
	})
	server.mu.Lock()
	same := server.current == bulk
	server.mu.Unlock()
	if !same {
		t.Fatal("bulk restarted on UDP reconnect")
	}
	cancel()
	select {
	case <-done:
		done <- nil
	case <-time.After(3 * time.Second):
		t.Fatal("cancelled dial leaked")
	}
	client.mu.Lock()
	defer client.mu.Unlock()
	if client.current != nil || client.datagram != nil {
		t.Fatal("sessions not cleaned up")
	}
}

// A legacy peer may ignore headers and return a successful WebSocket upgrade.
// That is not sufficient: never mark a split client connected without confirmation.
func TestSplitRejectsLegacySilentFallback(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 400*time.Millisecond)
	defer cancel()
	cert, key := certificate(t)
	cfg := config{Mode: "listen", Cert: cert, Key: key}
	tc, _ := cfg.tlsConfig()
	var upgraded atomic.Uint64
	srv := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		up := websocket.Upgrader{}
		ws, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer ws.Close()
		upgraded.Add(1)
		ws.SetReadDeadline(time.Now().Add(time.Second))
		ws.ReadMessage()
	})}
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	go func() { defer close(done); srv.Serve(tls.NewListener(l, tc)) }()
	defer func() { srv.Close(); <-done }()
	roots := x509.NewCertPool()
	roots.AppendCertsFromPEM([]byte(cert))
	client := &engine{cfg: config{SplitUDP: true, Token: strings.Repeat("x", 40), URL: "wss://" + l.Addr().String() + "/relay"}}
	client.dial(ctx, &tls.Config{RootCAs: roots, ServerName: "webtcp.test", MinVersion: tls.VersionTLS12})
	if upgraded.Load() != 2 || client.m.Sessions.Load() != 0 || client.m.Connected.Load() != 0 {
		t.Fatal("legacy upgrade falsely accepted", upgraded.Load(), client.m.Sessions.Load())
	}
}

// Simulate the TUN producer outrunning a runnable consumer on one scheduler P.
// Same packet/byte bounds; a real blocked writer is covered separately.
func TestDatagramQueueSingleYieldFairness(t *testing.T) {
	old := runtime.GOMAXPROCS(1)
	defer runtime.GOMAXPROCS(old)
	var baselineDrops uint64
	for _, split := range []bool{false, true} {
		cap := 32
		if split {
			cap = 64
		} // split halves the same effective lane cap to 32
		e := &engine{cfg: config{SplitUDP: split, QueuePackets: cap, QueueBytes: 262144}}
		s := e.reserveLane(nil, context.Background(), true, nil)
		ready, done := make(chan struct{}), make(chan struct{})
		var received atomic.Uint64
		go func() {
			defer close(done)
			close(ready)
			for {
				select {
				case p := <-s.q:
					s.took(p)
					received.Add(1)
				case <-s.ctx.Done():
					return
				}
			}
		}()
		<-ready
		for i := 0; i < 200; i++ {
			s.enqueueMeta(tunFlow, ipPacket(17), 0, 0)
		}
		runtime.Gosched()
		wait(t, func() bool { return received.Load()+e.m.DropReasons[dropQueuePackets].Load() == 200 })
		drops := e.m.DropReasons[dropQueuePackets].Load()
		s.cancel()
		<-done
		if split {
			if drops >= baselineDrops || s.laneMetrics().retrySuccess.Load() == 0 || received.Load()+drops != 200 {
				t.Fatalf("yield did not preserve bounded burst: drops=%d received=%d retries=%d", drops, received.Load(), s.laneMetrics().retrySuccess.Load())
			}
		} else {
			baselineDrops = drops
			if drops == 0 {
				t.Fatal("fixture did not reproduce producer starvation")
			}
		}
	}
}
func TestDatagramQueueYieldDoesNotWaitForBlockedWriter(t *testing.T) {
	e := &engine{cfg: config{SplitUDP: true, QueuePackets: 2, QueueBytes: 4096}}
	s := e.reserveLane(nil, context.Background(), true, nil)
	defer s.cancel()
	for i := 0; i < 10; i++ {
		s.enqueueMeta(tunFlow, ipPacket(17), 0, 0)
	}
	if len(s.q) != 1 || s.qbytes.Load() != 40 || e.m.DropReasons[dropQueuePackets].Load() != 9 {
		t.Fatal("bounded queue or byte accounting changed")
	}
	if s.laneMetrics().queueYields.Load() != 9 || s.laneMetrics().retrySuccess.Load() != 0 {
		t.Fatal("unexpected retry accounting")
	}
}
