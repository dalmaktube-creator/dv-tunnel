package main

import (
	"context"
	"sync/atomic"
	"testing"
	"time"
)

func TestUDPWaitAbsorbsFiniteByteBurstWithoutGrowingCaps(t *testing.T) {
	run := func(ms int) (uint64, uint64) {
		e := &engine{cfg: config{SplitUDP: true, QueuePackets: 256, QueueBytes: 262144, UDPWaitMS: ms}}
		s := e.reserveLane(nil, context.Background(), true, nil)
		defer s.cancel()
		done := make(chan struct{})
		received := make(chan uint64, 1)
		go func() {
			for i := 0; i < 160; i++ {
				s.enqueueMeta(tunFlow, makeUDPPacketForQueueTest(), 0, 0)
			}
			close(done)
		}()
		go func() {
			time.Sleep(120 * time.Millisecond)
			var n uint64
			for {
				select {
				case p := <-s.q:
					s.took(p)
					n++
				case <-done:
					for len(s.q) > 0 {
						s.took(<-s.q)
						n++
					}
					received <- n
					return
				}
			}
		}()
		var n uint64
		select {
		case n = <-received:
		case <-time.After(3 * time.Second):
			t.Fatal("bounded burst hung")
		}
		drops := e.m.Dropped.Load()
		if n+drops != 160 || s.qbytes.Load() != 0 || cap(s.q) != 128 || s.byteBudget() != 131072 {
			t.Fatal("accounting or caps changed")
		}
		if ms > 0 && (e.lanes[1].waitSuccess.Load() == 0 || e.lanes[1].waitTimeouts.Load() != 0) {
			t.Fatal("finite wait not observed")
		}
		return n, drops
	}
	_, baseline := run(0)
	if baseline == 0 {
		t.Fatal("baseline did not reproduce byte pressure")
	}
	n, drops := run(200)
	if n != 160 || drops != 0 {
		t.Fatal("bounded recoverable burst was dropped", n, drops)
	}
}
func TestUDPWaitPacketPressureCancellationAndTimeout(t *testing.T) {
	for _, budget := range []int{80, 4096} {
		e := &engine{cfg: config{SplitUDP: true, QueuePackets: 2, QueueBytes: budget, UDPWaitMS: 200}}
		s := e.reserveLane(nil, context.Background(), true, nil)
		s.enqueue(tunFlow, ipPacket(17))
		done := make(chan struct{})
		go func() { s.enqueue(tunFlow, ipPacket(17)); close(done) }()
		wait(t, func() bool { return e.lanes[1].queueWaits.Load() == 1 })
		s.cancel()
		select {
		case <-done:
		case <-time.After(time.Second):
			t.Fatal("cancellation did not release admission")
		}
		if s.qbytes.Load() != 40 || e.m.DropReasons[dropCancelledSession].Load() != 1 || e.lanes[1].waitTimeouts.Load() != 0 {
			t.Fatal("cancel accounting")
		}
	}
	e := &engine{cfg: config{SplitUDP: true, QueuePackets: 2, QueueBytes: 4096, UDPWaitMS: 20}}
	s := e.reserveLane(nil, context.Background(), true, nil)
	defer s.cancel()
	s.enqueue(tunFlow, ipPacket(17))
	start := time.Now()
	s.enqueue(tunFlow, ipPacket(17))
	if time.Since(start) < 15*time.Millisecond || e.lanes[1].waitTimeouts.Load() != 1 || e.m.DropReasons[dropQueuePackets].Load() != 1 || s.qbytes.Load() != 40 {
		t.Fatal("timeout or budget incorrect")
	}
}
func TestUDPWaitDoesNotHoldEngineLockAndLeavesBulkNonwaiting(t *testing.T) {
	e := &engine{cfg: config{SplitUDP: true, QueuePackets: 2, QueueBytes: 4096, UDPWaitMS: 200}}
	a := e.reserveLane(nil, context.Background(), false, nil)
	b := e.reserveLane(nil, context.Background(), true, nil)
	defer a.cancel()
	defer b.cancel()
	e.enqueueIP(ipPacket(17), 0, 0)
	done := make(chan struct{})
	go func() { e.enqueueIP(ipPacket(17), 0, 0); close(done) }()
	wait(t, func() bool { return e.lanes[1].queueWaits.Load() > 0 })
	var observed atomic.Bool
	go func() { e.snapshot(); e.enqueueIP(ipPacket(6), 0, 0); observed.Store(true) }()
	until := time.After(80 * time.Millisecond)
	for !observed.Load() {
		select {
		case <-until:
			t.Fatal("engine lock held while admission waits")
		default:
			time.Sleep(time.Millisecond)
		}
	}
	b.cancel()
	<-done
	if e.lanes[0].queueWaits.Load() != 0 || len(a.q) != 1 {
		t.Fatal("bulk wait enabled")
	}
}
func TestUDPWaitDeadlineIsNotRenewedByQueueActivity(t *testing.T) {
	e := &engine{cfg: config{SplitUDP: true, QueuePackets: 2, UDPWaitMS: 20}}
	s := e.reserveLane(nil, context.Background(), true, nil)
	defer s.cancel()
	w := admissionWait{s: s}
	defer w.finish()
	s.space <- struct{}{}
	if !w.next() {
		t.Fatal("initial notification")
	}
	time.Sleep(25 * time.Millisecond)
	s.space <- struct{}{}
	if w.next() || e.lanes[1].waitTimeouts.Load() != 1 {
		t.Fatal("deadline renewed")
	}
}
