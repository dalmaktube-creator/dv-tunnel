package main

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/binary"
	"encoding/pem"
	"fmt"
	"github.com/gorilla/websocket"
	"io"
	"math/big"
	"net"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"
)

func certificate(t testing.TB) (string, string) {
	t.Helper()
	key, e := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if e != nil {
		t.Fatal(e)
	}
	c := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "webtcp.test"}, DNSNames: []string{"webtcp.test"}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), KeyUsage: x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, IsCA: true, BasicConstraintsValid: true}
	der, e := x509.CreateCertificate(rand.Reader, c, c, &key.PublicKey, key)
	if e != nil {
		t.Fatal(e)
	}
	k, _ := x509.MarshalECPrivateKey(key)
	return string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})), string(pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: k}))
}
func port(t testing.TB, network string) string {
	t.Helper()
	if network == "tcp" {
		l, e := net.Listen("tcp", "127.0.0.1:0")
		if e != nil {
			t.Fatal(e)
		}
		defer l.Close()
		return l.Addr().String()
	}
	l, e := net.ListenPacket("udp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	defer l.Close()
	return l.LocalAddr().String()
}
func wait(t testing.TB, fn func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if fn() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("timed out")
}
func start(t testing.TB, e *engine, ctx context.Context) {
	t.Helper()
	done := make(chan error, 1)
	go func() { done <- e.run(ctx) }()
	t.Cleanup(func() {
		select {
		case err := <-done:
			if err != nil {
				t.Error(err)
			}
		case <-time.After(3 * time.Second):
			t.Error("engine leaked after cancellation")
		}
	})
}
func echo(t testing.TB) string {
	t.Helper()
	a, _ := net.ResolveUDPAddr("udp", "127.0.0.1:0")
	u, e := net.ListenUDP("udp", a)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { u.Close() })
	go func() {
		b := make([]byte, 8192)
		for {
			n, a, e := u.ReadFromUDP(b)
			if e != nil {
				return
			}
			u.WriteToUDP(b[:n], a)
		}
	}()
	return u.LocalAddr().String()
}
func exchange(t testing.TB, c *net.UDPConn, p []byte) {
	t.Helper()
	c.SetDeadline(time.Now().Add(3 * time.Second))
	if _, e := c.Write(p); e != nil {
		t.Fatal(e)
	}
	b := make([]byte, 4096)
	n, e := c.Read(b)
	if e != nil {
		t.Fatal(e)
	}
	if !bytes.Equal(p, b[:n]) {
		t.Fatal("cross-flow corruption")
	}
}
func TestTLSRoundTripModes(t *testing.T) {
	for _, reverse := range []bool{false, true} {
		t.Run(fmt.Sprint(reverse), func(t *testing.T) {
			cert, key := certificate(t)
			ctx, cancel := context.WithCancel(context.Background())
			t.Cleanup(cancel)
			addr := port(t, "tcp")
			udp := port(t, "udp")
			base := config{Path: "/relay", Token: strings.Repeat("x", 40), Padding: 64}
			in, out := &engine{cfg: base}, &engine{cfg: base}
			in.cfg.Role = "ingress"
			in.cfg.UDP = udp
			out.cfg.Role = "egress"
			out.cfg.UDP = echo(t)
			listener, dialer := out, in
			if reverse {
				listener, dialer = in, out
			}
			listener.cfg.Mode = "listen"
			listener.cfg.Listen = addr
			listener.cfg.Cert = cert
			listener.cfg.Key = key
			dialer.cfg.Mode = "dial"
			dialer.cfg.URL = "wss://" + addr + "/relay"
			dialer.cfg.CA = cert
			dialer.cfg.ServerName = "webtcp.test"
			start(t, listener, ctx)
			start(t, dialer, ctx)
			defer cancel()
			wait(t, func() bool { return in.m.Connected.Load() == 1 && out.m.Connected.Load() == 1 })
			a, _ := net.ResolveUDPAddr("udp", udp)
			var peers []*net.UDPConn
			for i := 0; i < 4; i++ {
				c, e := net.DialUDP("udp", nil, a)
				if e != nil {
					t.Fatal(e)
				}
				defer c.Close()
				peers = append(peers, c)
			}
			for i := 0; i < 12; i++ {
				for j, c := range peers {
					exchange(t, c, bytes.Repeat([]byte{byte(i), byte(j)}, 800))
				}
			}
			if in.m.ActiveFlows.Load() != 4 || out.m.ActiveFlows.Load() != 4 {
				t.Fatal("association multiplexing failed")
			}
			if in.m.Padding.Load() != 8*64 {
				t.Fatal("padding not capped to initial eight frames")
			}
			// Kill one generation and prove fresh sessions recover without retained flows.
			before := in.m.Sessions.Load()
			in.mu.Lock()
			s := in.current
			in.mu.Unlock()
			s.cancel()
			wait(t, func() bool { return in.m.Sessions.Load() > before && in.m.Connected.Load() == 1 })
			for _, c := range peers {
				exchange(t, c, []byte("after reconnect"))
			}
			cancel()
			wait(t, func() bool { return in.m.Connected.Load() == 0 && out.m.Connected.Load() == 0 })
			if in.m.ActiveFlows.Load() != 0 || out.m.ActiveFlows.Load() != 0 {
				t.Fatal("flow leak")
			}
		})
	}
}
func TestAuthenticationAndTLS(t *testing.T) {
	cert, key := certificate(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	e := &engine{cfg: config{Role: "egress", Mode: "listen", UDP: "127.0.0.1:12345", Listen: port(t, "tcp"), Path: "/relay", Token: strings.Repeat("a", 40), Cert: cert, Key: key}}
	start(t, e, ctx)
	good := config{Mode: "dial", CA: cert, ServerName: "webtcp.test"}
	tlsConfig, _ := good.tlsConfig()
	client := &http.Client{Transport: &http.Transport{TLSClientConfig: tlsConfig}, Timeout: time.Second}
	t.Cleanup(func() { client.CloseIdleConnections() })
	wait(t, func() bool {
		r, err := client.Get("https://" + e.cfg.Listen + "/")
		if err != nil {
			return false
		}
		defer r.Body.Close()
		return r.StatusCode == 200
	})
	d := websocket.Dialer{TLSClientConfig: tlsConfig, HandshakeTimeout: time.Second}
	_, res, err := d.Dial("wss://"+e.cfg.Listen+"/relay", http.Header{"Authorization": []string{"Bearer wrong"}})
	if err == nil || res.StatusCode != 200 {
		t.Fatal("invalid token admitted")
	}
	b, _ := io.ReadAll(res.Body)
	res.Body.Close()
	if !bytes.Contains(b, []byte("Welcome")) {
		t.Fatal("fallback missing")
	}
	bad := websocket.Dialer{HandshakeTimeout: time.Second}
	if ws, _, err := bad.Dial("wss://"+e.cfg.Listen+"/relay", nil); err == nil {
		ws.Close()
		t.Fatal("untrusted certificate admitted")
	}
	headers := http.Header{"Authorization": []string{"Bearer " + e.cfg.Token}}
	ws, _, err := d.Dial("wss://"+e.cfg.Listen+"/relay", headers)
	if err != nil {
		t.Fatal(err)
	}
	defer ws.Close()
	if extra, _, err := d.Dial("wss://"+e.cfg.Listen+"/relay", headers); err == nil {
		extra.Close()
		t.Fatal("second session admitted")
	}
	ws.WriteMessage(websocket.BinaryMessage, []byte("malformed"))
	ws.SetReadDeadline(time.Now().Add(time.Second))
	if _, _, err := ws.ReadMessage(); err == nil {
		t.Fatal("malformed frame accepted")
	}
}
func TestCodecAndBounds(t *testing.T) {
	for _, pad := range []int{0, 64, 256} {
		b, err := encode(7, []byte("test"), pad)
		if err != nil {
			t.Fatal(err)
		}
		id, p, err := decode(b)
		if err != nil || id != 7 || string(p) != "test" {
			t.Fatal("codec")
		}
	}
	for _, b := range [][]byte{nil, {1}, {2, 1}, make([]byte, 3000)} {
		if _, _, err := decode(b); err == nil {
			t.Fatal("invalid frame accepted")
		}
	}
	if _, err := encode(1, make([]byte, maxPayload+1), 0); err == nil {
		t.Fatal("oversized datagram")
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	e := &engine{}
	s := e.reserve(nil, ctx)
	for i := 0; i < queueSize+10; i++ {
		s.enqueue(1, []byte("x"))
	}
	if len(s.q) != queueSize || e.m.Dropped.Load() != 10 {
		t.Fatal("unbounded queue")
	}
	for i := 0; i < maxFlows+10; i++ {
		s.ingress(&net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: i + 1}, []byte("x"))
	}
	if len(s.flows) != maxFlows {
		t.Fatal("unbounded flows")
	}
	s.reap(time.Now().Add(3 * time.Minute))
	if len(s.flows) != 0 || len(s.addrs) != 0 || e.m.ActiveFlows.Load() != 0 {
		t.Fatal("expiry leak")
	}
}
func TestConfigFailClosed(t *testing.T) {
	c := config{Role: "egress", Mode: "dial", URL: "ws://example.com/relay", UDP: "8.8.8.8:53", Path: "/relay", Token: strings.Repeat("a", 40)}
	if c.validate() == nil {
		t.Fatal("unsafe config accepted")
	}
	c.UDP = "127.0.0.1:53"
	if c.validate() == nil {
		t.Fatal("plaintext allowed")
	}
	c.URL = "wss://example.com/relay"
	if c.validate() != nil {
		t.Fatal("valid config rejected")
	}
	c.URL += "?token=secret"
	if c.validate() == nil {
		t.Fatal("query credentials")
	}
}
func FuzzFrame(f *testing.F) {
	b, _ := encode(1, []byte("test"), 64)
	f.Add(b)
	f.Add([]byte{})
	f.Fuzz(func(t *testing.T, b []byte) {
		id, p, e := decode(b)
		if e == nil {
			out, e := encode(id, p, 0)
			if e != nil {
				t.Fatal(e)
			}
			_, again, e := decode(out)
			if e != nil || !bytes.Equal(p, again) {
				t.Fatal("roundtrip")
			}
		}
	})
}
func BenchmarkFrame(b *testing.B) {
	p := make([]byte, 1400)
	b.ReportAllocs()
	b.SetBytes(int64(len(p)))
	for i := 0; i < b.N; i++ {
		v, _ := encode(1, p, 0)
		decode(v)
	}
}

// Repeated concurrent flow inserts exercise mutex ownership under -race.
func TestConcurrentQueue(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	e := &engine{}
	s := e.reserve(nil, ctx)
	var wg sync.WaitGroup
	for n := 0; n < 8; n++ {
		wg.Add(1)
		go func(n int) {
			defer wg.Done()
			for j := 0; j < 100; j++ {
				s.ingress(&net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: n + 1}, []byte("x"))
			}
		}(n)
	}
	wg.Wait()
	if len(s.flows) != 8 {
		t.Fatal("flow ownership")
	}
}

func BenchmarkTLSUDPRelay(b *testing.B) {
	cert, key := certificate(b)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	addr := port(b, "tcp")
	udp := port(b, "udp")
	base := config{Path: "/relay", Token: strings.Repeat("b", 40)}
	in := &engine{cfg: base}
	in.cfg.Role = "ingress"
	in.cfg.Mode = "dial"
	in.cfg.UDP = udp
	in.cfg.URL = "wss://" + addr + "/relay"
	in.cfg.CA = cert
	in.cfg.ServerName = "webtcp.test"
	out := &engine{cfg: base}
	out.cfg.Role = "egress"
	out.cfg.Mode = "listen"
	out.cfg.UDP = echo(b)
	out.cfg.Listen = addr
	out.cfg.Cert = cert
	out.cfg.Key = key
	start(b, out, ctx)
	start(b, in, ctx)
	wait(b, func() bool { return in.m.Connected.Load() == 1 && out.m.Connected.Load() == 1 })
	a, _ := net.ResolveUDPAddr("udp", udp)
	c, err := net.DialUDP("udp", nil, a)
	if err != nil {
		b.Fatal(err)
	}
	defer c.Close()
	p := make([]byte, 1400)
	exchange(b, c, p)
	b.SetBytes(int64(len(p)))
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		exchange(b, c, p)
	}
	b.StopTimer()
}
func TestUDPReaderFailureDoesNotPoisonAssociation(t *testing.T) {
	target := port(t, "udp")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	e := &engine{cfg: config{Role: "egress", UDP: target}}
	s := e.reserve(nil, ctx)
	if err := s.deliver(1, []byte("early")); err != nil {
		t.Fatal(err)
	}
	wait(t, func() bool { s.mu.Lock(); defer s.mu.Unlock(); return len(s.flows) == 0 })
	a, _ := net.ResolveUDPAddr("udp", target)
	u, err := net.ListenUDP("udp", a)
	if err != nil {
		t.Fatal(err)
	}
	defer u.Close()
	go func() {
		b := make([]byte, 100)
		n, a, err := u.ReadFromUDP(b)
		if err == nil {
			u.WriteToUDP(b[:n], a)
		}
	}()
	if err := s.deliver(1, []byte("ready")); err != nil {
		t.Fatal(err)
	}
	select {
	case p := <-s.q:
		if string(p.data) != "ready" {
			t.Fatal("bad reply")
		}
	case <-time.After(time.Second):
		t.Fatal("dead UDP association retained")
	}
	s.mu.Lock()
	for _, f := range s.flows {
		f.udp.Close()
	}
	s.mu.Unlock()
	s.wg.Wait()
	if e.m.ActiveFlows.Load() != 0 {
		t.Fatal("leak")
	}
}

func TestBatchCodecAndSourceFilter(t *testing.T) {
	b := []byte{1, batchType, 0, 2}
	b = appendEntry(b, 1, []byte("one"))
	b = appendEntry(b, 9, []byte("two"))
	var got []string
	if err := decodeBatch(append(b, make([]byte, 64)...), func(id uint64, p []byte) error {
		got = append(got, fmt.Sprint(id, string(p)))
		return nil
	}); err != nil || strings.Join(got, ",") != "1one,9two" {
		t.Fatal("batch roundtrip", err, got)
	}
	for _, bad := range [][]byte{b[:5], append(append([]byte(nil), b...), make([]byte, maxPadding+1)...), {1, batchType, 0, 0}} {
		calls := 0
		if decodeBatch(bad, func(uint64, []byte) error { calls++; return nil }) == nil || calls != 0 {
			t.Fatal("invalid batch accepted or partially delivered")
		}
	}
	e := &engine{}
	c := config{Role: "egress", Mode: "listen", Listen: "127.0.0.1:1", Path: "/r", Token: strings.Repeat("a", 40), TUN: "wgt0", Sources: []string{"10.9.0.0/24", "fd00::/64"}}
	if err := c.validate(); err != nil {
		t.Fatal(err)
	}
	e.allowed, _ = c.sources()
	v4 := make([]byte, 20)
	v4[0] = 0x45
	copy(v4[12:16], []byte{10, 9, 0, 7})
	v6 := make([]byte, 40)
	v6[0] = 0x60
	v6[8], v6[9] = 0xfd, 0
	if !e.sourceAllowed(v4) || !e.sourceAllowed(v6) {
		t.Fatal("allowed source rejected")
	}
	v4[12] = 11
	v6[8] = 0xfe
	if e.sourceAllowed(v4) || e.sourceAllowed(v6) || e.sourceAllowed([]byte{0x45}) {
		t.Fatal("spoofed source accepted")
	}
	for _, bad := range []config{{TUN: "bad name"}, {TUN: "wgt0", UDP: "127.0.0.1:1"}, {TUN: "wgt0"}} {
		bad.Role, bad.Mode, bad.Listen, bad.Path, bad.Token = c.Role, c.Mode, c.Listen, c.Path, c.Token
		if bad.Sources == nil && bad.TUN == "wgt0" && bad.UDP == "" {
			bad.Sources = nil
		}
		if bad.validate() == nil {
			t.Fatal("unsafe TUN config accepted")
		}
	}
}

func TestBatchedTLSRelay(t *testing.T) {
	cert, key := certificate(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	addr, udp := port(t, "tcp"), port(t, "udp")
	base := config{Path: "/relay", Token: strings.Repeat("x", 40), Padding: 64, Batch: true}
	in, out := &engine{cfg: base}, &engine{cfg: base}
	in.cfg.Role, in.cfg.Mode, in.cfg.UDP = "ingress", "dial", udp
	in.cfg.URL, in.cfg.CA, in.cfg.ServerName = "wss://"+addr+"/relay", cert, "webtcp.test"
	out.cfg.Role, out.cfg.Mode, out.cfg.UDP = "egress", "listen", echo(t)
	out.cfg.Listen, out.cfg.Cert, out.cfg.Key = addr, cert, key
	start(t, out, ctx)
	start(t, in, ctx)
	defer cancel()
	wait(t, func() bool { return in.m.Connected.Load() == 1 && out.m.Connected.Load() == 1 })
	a, _ := net.ResolveUDPAddr("udp", udp)
	c, err := net.DialUDP("udp", nil, a)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	// Burst so several datagrams are queued together and must share messages.
	c.SetDeadline(time.Now().Add(5 * time.Second))
	for i := 0; i < 48; i++ {
		c.Write(bytes.Repeat([]byte{byte(i)}, 900))
	}
	seen := map[byte]bool{}
	buf := make([]byte, 2000)
	for len(seen) < 40 {
		n, err := c.Read(buf)
		if err != nil {
			break
		}
		if n != 900 || !bytes.Equal(buf[:n], bytes.Repeat(buf[:1], 900)) {
			t.Fatal("batched datagram corrupted")
		}
		seen[buf[0]] = true
	}
	if len(seen) < 40 {
		t.Fatal("too many batched datagrams lost", len(seen))
	}
	if in.m.Messages.Load() > in.m.Sent.Load() || in.m.Padding.Load() != 8*64 && in.m.Messages.Load() >= 8 {
		t.Fatal("batch accounting")
	}
}

func FuzzBatch(f *testing.F) {
	b := appendEntry([]byte{1, batchType, 0, 1}, 1, []byte("x"))
	f.Add(b)
	f.Fuzz(func(t *testing.T, b []byte) {
		total := 0
		decodeBatch(b, func(id uint64, p []byte) error {
			if id == 0 || len(p) == 0 || len(p) > maxPayload {
				t.Fatal("invalid entry yielded")
			}
			total += len(p)
			return nil
		})
		if total > len(b) {
			t.Fatal("overread")
		}
	})
}

func tcp4(total int) []byte {
	p := make([]byte, total)
	p[0], p[9] = 0x45, protoTCP
	binary.BigEndian.PutUint16(p[2:4], uint16(total))
	if total >= 40 {
		p[20+12] = 5 << 4
	}
	return p
}

func TestOffloadHeaderRebuild(t *testing.T) {
	h := make([]byte, vnetHdrLen)
	p := tcp4(20000)
	if err := vnetHeader(h, metaNeedsCsum, 1296, p); err != nil {
		t.Fatal(err)
	}
	if h[0] != vnetNeedsCsum || h[1] != gsoTCPv4 || binary.NativeEndian.Uint16(h[2:4]) != 40 || binary.NativeEndian.Uint16(h[4:6]) != 1296 || binary.NativeEndian.Uint16(h[6:8]) != 20 || binary.NativeEndian.Uint16(h[8:10]) != tcpCsumOffset {
		t.Fatal("rebuilt header", h)
	}
	if vnetHeader(h, 0, 0, p) != nil || !bytes.Equal(h, make([]byte, vnetHdrLen)) {
		t.Fatal("plain packet header")
	}
	bad := [][]byte{tcp4(30)[:29], append(tcp4(100), 0)}
	udp := tcp4(2000)
	udp[9] = protoUDP
	bad = append(bad, udp) // GSO is only accepted for TCP
	for _, b := range bad {
		if vnetHeader(h, metaNeedsCsum, 1296, b) == nil {
			t.Fatal("inconsistent super-packet accepted")
		}
	}
	for _, g := range []uint16{1, maxPayload + 1} {
		if vnetHeader(h, 0, g, p) == nil {
			t.Fatal("unsafe gso size accepted", g)
		}
	}
	msg := appendOffload([]byte{1, offloadType, 0, 1}, packet{data: p, flags: metaNeedsCsum, gso: 1296})
	n := 0
	if decodeOffload(msg, func(f uint8, g uint16, b []byte) error { n++; return nil }) != nil || n != 1 {
		t.Fatal("offload roundtrip")
	}
	msg[4] = 0x80
	if decodeOffload(msg, func(uint8, uint16, []byte) error { return nil }) == nil {
		t.Fatal("unknown offload flag accepted")
	}
	c := config{Role: "egress", Mode: "listen", Listen: "127.0.0.1:1", Path: "/r", Token: strings.Repeat("a", 40), UDP: "127.0.0.1:2", Offload: true}
	if c.validate() == nil {
		t.Fatal("offload without TUN accepted")
	}
}

func FuzzOffload(f *testing.F) {
	f.Add(appendOffload([]byte{1, offloadType, 0, 1}, packet{data: tcp4(3000), flags: 1, gso: 1200}))
	f.Fuzz(func(t *testing.T, b []byte) {
		h := make([]byte, vnetHdrLen)
		decodeOffload(b, func(fl uint8, g uint16, p []byte) error {
			if vnetHeader(h, fl, g, p) == nil && h[1] != gsoNone {
				l3, _, _ := l4(p)
				if int(binary.NativeEndian.Uint16(h[2:4])) > len(p) || int(binary.NativeEndian.Uint16(h[6:8])) != l3 {
					t.Fatal("inconsistent kernel header")
				}
			}
			return nil
		})
	})
}

func TestQueueByteCapConcurrent(t *testing.T) {
	e := &engine{cfg: config{QueueBytes: 4096}}
	s := &session{e: e, ctx: context.Background(), q: make(chan packet, 256)}
	var wg sync.WaitGroup
	for i := 0; i < 256; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			s.enqueue(1, make([]byte, 512))
		}()
	}
	wg.Wait()
	if n := s.qbytes.Load(); n != 4096 || len(s.q) != 8 {
		t.Fatalf("byte cap: bytes=%d packets=%d", n, len(s.q))
	}
	for len(s.q) > 0 {
		s.took(<-s.q)
	}
	if s.qbytes.Load() != 0 {
		t.Fatal("queue byte reservation leaked")
	}
}

func TestOffloadRejectsInconsistentMetadata(t *testing.T) {
	h := make([]byte, vnetHdrLen)
	p := tcp4(3000)
	for _, tt := range []struct {
		flags uint8
		gso   uint16
		p     []byte
	}{
		{0, 0, append(tcp4(100), 0)},
		{0, 1200, p},
		{metaECN, 0, p},
		{0x80, 1200, p},
	} {
		if vnetHeader(h, tt.flags, tt.gso, tt.p) == nil {
			t.Fatal("inconsistent metadata accepted")
		}
	}
	p[6] = 0x20 // MF: first fragment is still a fragment.
	if vnetHeader(h, metaNeedsCsum, 1200, p) == nil {
		t.Fatal("fragmented TCP GSO accepted")
	}
}
