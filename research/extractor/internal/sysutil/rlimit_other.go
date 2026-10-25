//go:build !unix

package sysutil

// RaiseFileLimit is a no-op on non-unix platforms.
func RaiseFileLimit() {}
