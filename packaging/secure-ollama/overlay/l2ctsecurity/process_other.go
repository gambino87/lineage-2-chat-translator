//go:build !windows

package l2ctsecurity

import (
	"context"
	"errors"
	"net"
)

type processPin struct{}
func pinProcess(_ int) (*processPin, error) { return nil, errors.New("this private backend build supports Windows only") }
func (*processPin) alive() error { return errors.New("unsupported platform") }
func (*processPin) close() {}
func (*processPin) verifyConnection(context.Context, net.Conn) error { return errors.New("unsupported platform") }
