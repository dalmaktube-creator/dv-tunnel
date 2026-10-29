//go:build !linux

package main

import (
	"errors"
	"os"
	"syscall"
)

func openTUN(string, bool) (*os.File, error) { return nil, errors.New("TUN mode is Linux-only") }

func lowatControl(int) func(string, string, syscall.RawConn) error { return nil }
