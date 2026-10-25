//go:build unix

// Package sysutil holds small OS helpers.
package sysutil

import "syscall"

// RaiseFileLimit raises RLIMIT_NOFILE to the hard limit (the freezer keeps
// one handle per data file it touches; a full arb1 freezer has thousands).
func RaiseFileLimit() {
	var r syscall.Rlimit
	if err := syscall.Getrlimit(syscall.RLIMIT_NOFILE, &r); err != nil {
		return
	}
	if r.Cur < r.Max {
		r.Cur = r.Max
		if syscall.Setrlimit(syscall.RLIMIT_NOFILE, &r) != nil {
			r.Cur = 10240 // darwin caps below RLIM_INFINITY
			syscall.Setrlimit(syscall.RLIMIT_NOFILE, &r)
		}
	}
}
