package main

import (
	"context"
	"encoding/binary"
	"errors"
	"os"
)

// Linux virtio_net_hdr (legacy, native-endian) used by IFF_VNET_HDR TUNs.
const (
	vnetHdrLen      = 10
	vnetNeedsCsum   = 1
	gsoNone         = 0
	gsoTCPv4        = 1
	gsoTCPv6        = 4
	gsoECN          = 0x80
	protoTCP        = 6
	protoUDP        = 17
	tcpCsumOffset   = 16
	udpCsumOffset   = 6
	errOffloadInput = "offload packet rejected"
)

// l4 returns the L3 header length and L4 protocol of an IP packet whose
// length field matches the buffer exactly. IPv6 extension headers are not
// accepted for offload.
func l4(p []byte) (int, byte, bool) {
	switch {
	case len(p) >= 20 && p[0]>>4 == 4:
		ihl := int(p[0]&0x0f) * 4
		if ihl < 20 || ihl > len(p) || int(binary.BigEndian.Uint16(p[2:4])) != len(p) {
			return 0, 0, false
		}
		return ihl, p[9], true
	case len(p) >= 40 && p[0]>>4 == 6:
		if int(binary.BigEndian.Uint16(p[4:6]))+40 != len(p) {
			return 0, 0, false
		}
		return 40, p[6], true
	}
	return 0, 0, false
}

// vnetHeader rebuilds the kernel header from the packet itself, taking only
// "needs checksum", "ECN" and the segment size from the untrusted peer, so a
// peer can never hand the kernel inconsistent GSO metadata.
func vnetHeader(h []byte, flags uint8, gso uint16, p []byte) error {
	for i := range h[:vnetHdrLen] {
		h[i] = 0
	}
	l3, proto, ok := l4(p)
	if !ok || flags&^(metaNeedsCsum|metaECN) != 0 || (flags&metaECN != 0 && gso == 0) {
		return errors.New(errOffloadInput)
	}
	if flags == 0 && gso == 0 {
		return nil
	}
	// Partial checksums and GSO are unsafe for fragmented IPv4 packets.
	if p[0]>>4 == 4 && binary.BigEndian.Uint16(p[6:8])&0x3fff != 0 {
		return errors.New(errOffloadInput)
	}
	if flags&metaNeedsCsum == 0 {
		return errors.New(errOffloadInput)
	}
	csumOff := 0
	switch proto {
	case protoTCP:
		csumOff = tcpCsumOffset
	case protoUDP:
		csumOff = udpCsumOffset
	default:
		return errors.New(errOffloadInput)
	}
	if l3+csumOff+2 > len(p) {
		return errors.New(errOffloadInput)
	}
	h[0] = vnetNeedsCsum
	binary.NativeEndian.PutUint16(h[6:8], uint16(l3))
	binary.NativeEndian.PutUint16(h[8:10], uint16(csumOff))
	if gso == 0 {
		return nil
	}
	if proto != protoTCP || len(p) < l3+20 {
		return errors.New(errOffloadInput)
	}
	thl := int(p[l3+12]>>4) * 4
	if thl < 20 || l3+thl > len(p) || int(gso) < minGSOSize || int(gso) > maxPayload || len(p) <= l3+thl {
		return errors.New(errOffloadInput)
	}
	t := byte(gsoTCPv4)
	if p[0]>>4 == 6 {
		t = gsoTCPv6
	}
	if flags&metaECN != 0 {
		t |= gsoECN
	}
	h[1] = t
	binary.NativeEndian.PutUint16(h[2:4], uint16(l3+thl))
	binary.NativeEndian.PutUint16(h[4:6], gso)
	return nil
}

// deliverOffload writes one validated (super-)packet to the vnet TUN.
func (s *session) deliverOffload(flags uint8, gso uint16, p []byte) error {
	if s.e.cfg.SplitUDP && datagramIP(p) != s.dgram {
		return errors.New("wrong transport lane")
	}
	if !s.e.sourceAllowed(p) {
		s.e.m.Spoofed.Add(1)
		return nil
	}
	if cap(s.scratch) < vnetHdrLen+len(p) {
		s.scratch = make([]byte, vnetHdrLen+maxSuper)
	}
	b := s.scratch[:vnetHdrLen+len(p)]
	if vnetHeader(b, flags, gso, p) != nil {
		s.drop(dropInvalidVnetMetadata)
		return nil
	}
	copy(b[vnetHdrLen:], p)
	if _, err := s.e.tun.Write(b); err != nil {
		s.drop(dropTUNWrite)
	}
	return nil
}

// readOffload turns kernel vnet headers into the three peer hints.
func (e *engine) readOffload(ctx context.Context) {
	b := make([]byte, vnetHdrLen+maxSuper)
	for {
		n, err := e.tun.Read(b)
		if err != nil {
			if ctx.Err() != nil || errors.Is(err, os.ErrClosed) {
				return
			}
			continue
		}
		if n <= vnetHdrLen {
			continue
		}
		var flags uint8
		if b[0]&vnetNeedsCsum != 0 {
			flags |= metaNeedsCsum
		}
		var gso uint16
		switch b[1] &^ gsoECN {
		case gsoNone:
		case gsoTCPv4, gsoTCPv6:
			gso = binary.NativeEndian.Uint16(b[4:6])
			if b[1]&gsoECN != 0 {
				flags |= metaECN
			}
		default:
			e.m.drop(dropUnsupportedOffload)
			continue
		}
		e.enqueueIP(b[vnetHdrLen:n], flags, gso)
	}
}
