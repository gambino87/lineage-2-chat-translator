//go:build windows

package l2ctsecurity

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"sync"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

var getExtendedTCPTable = windows.NewLazySystemDLL("iphlpapi.dll").NewProc("GetExtendedTcpTable")

const (
	tcpTableOwnerPIDAll = 5
	tcpStateEstablished = 5
	tcpRowSize = 24
	tcpTableHeaderSize = 4
	maxTCPTableSize = 16 * 1024 * 1024
)

type processPin struct {
	mu sync.Mutex
	handle windows.Handle
	pid uint32
}

func pinProcess(pid int) (*processPin, error) {
	if pid <= 0 { return nil, errors.New("invalid runner process") }
	handle, err := windows.OpenProcess(windows.SYNCHRONIZE | windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil { return nil, fmt.Errorf("retain runner process identity: %w", err) }
	p := &processPin{handle: handle, pid: uint32(pid)}
	if err := p.alive(); err != nil { p.close(); return nil, err }
	return p, nil
}

func (p *processPin) alive() error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.handle == 0 { return errors.New("runner identity has been released") }
	state, err := windows.WaitForSingleObject(p.handle, 0)
	if err != nil || state != uint32(windows.WAIT_TIMEOUT) { return errors.New("expected runner process has exited") }
	return nil
}

func (p *processPin) close() {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.handle != 0 { _ = windows.CloseHandle(p.handle); p.handle = 0 }
}

func readTCPTable() ([]byte, error) {
	var size uint32
	result, _, _ := getExtendedTCPTable.Call(0, uintptr(unsafe.Pointer(&size)), 0, windows.AF_INET, tcpTableOwnerPIDAll, 0)
	if result != uintptr(windows.ERROR_INSUFFICIENT_BUFFER) { return nil, fmt.Errorf("TCP identity table unavailable (%d)", result) }
	for attempt := 0; attempt < 3; attempt++ {
		if size < tcpTableHeaderSize || size > maxTCPTableSize { return nil, errors.New("invalid TCP identity table size") }
		table := make([]byte, size)
		result, _, _ = getExtendedTCPTable.Call(uintptr(unsafe.Pointer(&table[0])), uintptr(unsafe.Pointer(&size)), 0, windows.AF_INET, tcpTableOwnerPIDAll, 0)
		if result == 0 { return table[:size], nil }
		if result != uintptr(windows.ERROR_INSUFFICIENT_BUFFER) { return nil, fmt.Errorf("TCP identity table read failed (%d)", result) }
	}
	return nil, errors.New("TCP identity table kept changing")
}

// The reverse full tuple identifies this connection's SERVER endpoint. Checking
// a listening port alone would reintroduce the probe/request substitution race.
func connectionPID(table []byte, local, remote *net.TCPAddr) (uint32, bool, error) {
	if len(table) < tcpTableHeaderSize { return 0, false, errors.New("truncated TCP identity table") }
	count := binary.LittleEndian.Uint32(table[:4])
	if uint64(count)*tcpRowSize+tcpTableHeaderSize > uint64(len(table)) { return 0, false, errors.New("truncated TCP identity rows") }
	for i := uint32(0); i < count; i++ {
		offset := tcpTableHeaderSize + int(i)*tcpRowSize
		row := table[offset:offset+tcpRowSize]
		if binary.LittleEndian.Uint32(row[:4]) != tcpStateEstablished { continue }
		if net.IP(row[4:8]).Equal(remote.IP) && int(binary.BigEndian.Uint16(row[8:10])) == remote.Port &&
			net.IP(row[12:16]).Equal(local.IP) && int(binary.BigEndian.Uint16(row[16:18])) == local.Port {
			return binary.LittleEndian.Uint32(row[20:24]), true, nil
		}
	}
	return 0, false, nil
}

func (p *processPin) verifyConnection(ctx context.Context, conn net.Conn) error {
	local, ok := conn.LocalAddr().(*net.TCPAddr)
	if !ok { return errors.New("runner connection has no TCP source") }
	remote, ok := conn.RemoteAddr().(*net.TCPAddr)
	if !ok || !remote.IP.Equal(net.IPv4(127, 0, 0, 1)) { return errors.New("runner connection is not loopback") }
	for attempt := 0; attempt < 10; attempt++ {
		if err := p.alive(); err != nil { return err }
		table, err := readTCPTable()
		if err != nil { return err }
		pid, found, err := connectionPID(table, local, remote)
		if err != nil { return err }
		if found {
			if pid != p.pid { return errors.New("connected socket belongs to a different process") }
			return p.alive()
		}
		select { case <-ctx.Done(): return ctx.Err(); case <-time.After(10 * time.Millisecond): }
	}
	return errors.New("connected socket owner could not be established")
}
