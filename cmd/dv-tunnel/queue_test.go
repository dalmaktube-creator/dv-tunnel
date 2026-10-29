package main

import (
	"context"
	"encoding/json"
	"strings"
	"sync"
	"testing"
	"time"
)

func queueConfig() config {
	return config{Role: "ingress", Mode: "listen", Listen: "127.0.0.1:1", UDP: "127.0.0.1:2", Path: "/test", Token: strings.Repeat("x", 32)}
}
func TestPacketQueueConfig(t *testing.T) {
	for _, n := range []int{0, 1, 64, 128, 256} {
		c := queueConfig()
		c.QueuePackets = n
		if err := c.validate(); err != nil {
			t.Fatal(n, err)
		}
		e := &engine{cfg: c}
		s := e.reserve(nil, context.Background())
		defer s.cancel()
		want := n
		if want == 0 {
			want = 64
		}
		if cap(s.q) != want {
			t.Fatal(n, cap(s.q))
		}
		for i := 0; i < want+3; i++ {
			s.enqueue(1, []byte("x"))
		}
		if len(s.q) != want || e.m.DropReasons[dropQueuePackets].Load() != 3 {
			t.Fatal(n, e.m.snapshot())
		}
		for len(s.q) > 0 {
			s.took(<-s.q)
		}
		if s.qbytes.Load() != 0 {
			t.Fatal("packet-full reservation leaked")
		}
	}
	for _, n := range []int{-1, 257, 1000000} {
		c := queueConfig()
		c.QueuePackets = n
		if c.validate() == nil {
			t.Fatal("invalid cap accepted", n)
		}
	}
	var c config
	if json.Unmarshal([]byte(`{"queue_packets":1.5}`), &c) == nil || json.Unmarshal([]byte(`{"queue_packets":true}`), &c) == nil {
		t.Fatal("noninteger cap accepted")
	}
}
func TestPacketCapStillHonorsByteCap(t *testing.T) {
	c := queueConfig()
	c.QueuePackets = 128
	c.QueueBytes = 5
	e := &engine{cfg: c}
	s := e.reserve(nil, context.Background())
	defer s.cancel()
	s.enqueue(1, []byte("1234"))
	s.enqueue(1, []byte("ab"))
	if len(s.q) != 1 || e.m.DropReasons[dropQueueBytes].Load() != 1 || e.m.DropReasons[dropQueuePackets].Load() != 0 {
		t.Fatal(e.m.snapshot())
	}
	s.took(<-s.q)
	if s.qbytes.Load() != 0 {
		t.Fatal("byte budget leaked")
	}
}
func TestQueueExpiryAcrossBatchModes(t *testing.T) {
	for _, offload := range []bool{false, true} {
		e := &engine{cfg: config{QueuePackets: 128, Offload: offload}}
		s := e.reserve(nil, context.Background())
		defer s.cancel()
		s.enqueue(1, []byte("fresh"))
		s.enqueue(1, []byte("stale"))
		first := s.took(<-s.q)
		stale := <-s.q
		stale.at = time.Now().Add(-2 * time.Second)
		s.q <- stale
		_, n := s.batch(nil, first)
		if n != 1 || e.m.DropReasons[dropQueueExpired].Load() != 1 || s.qbytes.Load() != 0 {
			t.Fatal(offload, n, e.m.snapshot())
		}
	}
}
func TestReasonCountersAndCancellation(t *testing.T) {
	m := &metrics{}
	var wg sync.WaitGroup
	for reason := dropReason(0); reason < dropReasonCount; reason++ {
		wg.Add(1)
		go func(r dropReason) {
			defer wg.Done()
			for i := 0; i < 100; i++ {
				m.drop(r)
			}
		}(reason)
	}
	wg.Wait()
	snap := m.snapshot()
	reasons := snap["drop_reasons"].(map[string]uint64)
	var total uint64
	for _, name := range dropNames {
		if reasons[name] != 100 {
			t.Fatal(name, reasons)
		}
		total += reasons[name]
	}
	if total != snap["drops"].(uint64) {
		t.Fatal(snap)
	}
	e := &engine{}
	s := e.reserve(nil, context.Background())
	s.enqueue(1, nil)
	s.cancel()
	s.enqueue(1, []byte("x"))
	if e.m.DropReasons[dropInvalidSize].Load() != 1 || e.m.DropReasons[dropCancelledSession].Load() != 1 || s.qbytes.Load() != 0 {
		t.Fatal(e.m.snapshot())
	}
}
