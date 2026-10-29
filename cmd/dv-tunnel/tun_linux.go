//go:build linux

package main

import (
	"encoding/binary"
	"os"
	"syscall"
	"unsafe"
)

const (
	iffTUN        = 0x0001
	iffNoPI       = 0x1000
	iffVnetHdr    = 0x4000
	tunSetIff     = 0x400454ca
	tunSetOffload = 0x400454d0
	tunFCsum      = 0x01
	tunFTSO4      = 0x02
	tunFTSO6      = 0x04
)

// openTUN attaches to (or creates) a layer-3 TUN device without packet info.
// Addressing and routes are configured by the caller, never by peer input.
func openTUN(name string, offload bool) (*os.File, error) {
	fd, err := syscall.Open("/dev/net/tun", syscall.O_RDWR|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	var req [40]byte
	copy(req[:15], name)
	flags := uint16(iffTUN | iffNoPI)
	if offload {
		flags |= iffVnetHdr
	}
	binary.NativeEndian.PutUint16(req[16:18], flags)
	if _, _, e := syscall.Syscall(syscall.SYS_IOCTL, uintptr(fd), tunSetIff, uintptr(unsafe.Pointer(&req[0]))); e != 0 {
		syscall.Close(fd)
		return nil, e
	}
	if offload {
		if _, _, e := syscall.Syscall(syscall.SYS_IOCTL, uintptr(fd), tunSetOffload, tunFCsum|tunFTSO4|tunFTSO6); e != 0 {
			syscall.Close(fd)
			return nil, e
		}
	}
	if err = syscall.SetNonblock(fd, true); err != nil {
		syscall.Close(fd)
		return nil, err
	}
	return os.NewFile(uintptr(fd), "/dev/net/tun"), nil
}

const tcpNotSentLowat = 25

// lowatControl sets TCP_NOTSENT_LOWAT; accepted sockets inherit it from the
// listener on Linux.
func lowatControl(v int) func(string, string, syscall.RawConn) error {
	if v <= 0 {
		return nil
	}
	return func(_, _ string, c syscall.RawConn) error {
		var serr error
		if err := c.Control(func(fd uintptr) {
			serr = syscall.SetsockoptInt(int(fd), syscall.IPPROTO_TCP, tcpNotSentLowat, v)
		}); err != nil {
			return err
		}
		return serr
	}
}
