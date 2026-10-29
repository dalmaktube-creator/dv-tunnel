package main

import "time"

// One admission deadline covers byte and packet pressure together. It does not
// grow either budget or wait indefinitely for the outer TCP writer.
type admissionWait struct {
	s        *session
	timer    *time.Timer
	started  time.Time
	admitted bool
}

func (w *admissionWait) next() bool {
	s := w.s
	if !s.e.cfg.SplitUDP || !s.dgram || s.e.cfg.UDPWaitMS == 0 {
		return false
	}
	if w.timer == nil {
		w.started = time.Now()
		w.timer = time.NewTimer(time.Duration(s.e.cfg.UDPWaitMS) * time.Millisecond)
		s.laneMetrics().queueWaits.Add(1)
	}
	if time.Since(w.started) >= time.Duration(s.e.cfg.UDPWaitMS)*time.Millisecond {
		s.laneMetrics().waitTimeouts.Add(1)
		return false
	}
	select {
	case <-s.ctx.Done():
		return false
	case <-w.timer.C:
		s.laneMetrics().waitTimeouts.Add(1)
		return false
	case <-s.space:
		return true
	}
}
func (w *admissionWait) finish() {
	if w.timer == nil {
		return
	}
	w.timer.Stop()
	m := w.s.laneMetrics()
	if w.admitted {
		m.waitSuccess.Add(1)
	}
	us := uint64(time.Since(w.started) / time.Microsecond)
	for old := m.maxWaitUS.Load(); us > old; old = m.maxWaitUS.Load() {
		if m.maxWaitUS.CompareAndSwap(old, us) {
			break
		}
	}
}
