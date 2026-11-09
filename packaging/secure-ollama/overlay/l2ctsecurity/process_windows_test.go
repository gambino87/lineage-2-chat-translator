//go:build windows

package l2ctsecurity

import (
	"encoding/binary"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

func TestKernelSocketPinAndAuthorization(t *testing.T) {
	key := strings.Repeat("b", 64)
	srv := httptest.NewServer(Handler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte("accepted")) }), key))
	defer srv.Close()
	self, _ := os.FindProcess(os.Getpid())
	client, err := NewRunnerClient(self, key)
	if err != nil { t.Fatal(err) }
	defer CloseRunnerClient(client)
	r, err := client.Get(srv.URL + "/completion")
	if err != nil { t.Fatal(err) }
	defer r.Body.Close()
	data, _ := io.ReadAll(r.Body)
	if r.StatusCode != 200 || string(data) != "accepted" { t.Fatal("pinned authorized request failed") }
	CloseRunnerClient(client)
	if _, err := client.Get(srv.URL); err == nil { t.Fatal("released identity accepted") }
}

func TestWrongProcessReceivesNoHTTPBytes(t *testing.T) {
	ln, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil { t.Fatal(err) }
	defer ln.Close()
	readBytes := make(chan int, 1)
	go func() {
		conn, err := ln.Accept()
		if err != nil { readBytes <- -1; return }
		defer conn.Close()
		_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
		buffer := make([]byte, 4096)
		n, _ := conn.Read(buffer)
		readBytes <- n
	}()
	// Parent is live, but does not own this test process's TCP listener.
	parent, _ := os.FindProcess(os.Getppid())
	client, err := NewRunnerClient(parent, strings.Repeat("c", 64))
	if err != nil { t.Fatal(err) }
	defer CloseRunnerClient(client)
	if _, err := client.Post("http://"+ln.Addr().String()+"/completion", "application/json", strings.NewReader("private prompt")); err == nil { t.Fatal("wrong process accepted") }
	select { case n := <-readBytes: if n != 0 { t.Fatalf("untrusted endpoint received %d bytes", n) }; case <-time.After(6*time.Second): t.Fatal("connection was not closed") }
}

func TestRunnerRejectsRedirect(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, "http://example.invalid/", 302) }))
	defer srv.Close()
	self, _ := os.FindProcess(os.Getpid())
	client, err := NewRunnerClient(self, strings.Repeat("d", 64))
	if err != nil { t.Fatal(err) }
	defer CloseRunnerClient(client)
	if r, err := client.Get(srv.URL); err == nil { if r != nil { r.Body.Close() }; t.Fatal("redirect accepted") }
}

func TestConnectionTupleRejectsTruncationAndListenerRows(t *testing.T) {
	local := &net.TCPAddr{IP: net.IPv4(127,0,0,1), Port: 40000}
	remote := &net.TCPAddr{IP: net.IPv4(127,0,0,1), Port: 50000}
	if _, _, err := connectionPID([]byte{1,0,0,0}, local, remote); err == nil { t.Fatal("truncated rows accepted") }
	table := make([]byte, 28)
	binary.LittleEndian.PutUint32(table[:4], 1)
	row := table[4:]
	binary.LittleEndian.PutUint32(row[:4], tcpStateEstablished)
	copy(row[4:8], remote.IP.To4()); binary.BigEndian.PutUint16(row[8:10], uint16(remote.Port))
	copy(row[12:16], local.IP.To4()); binary.BigEndian.PutUint16(row[16:18], uint16(local.Port))
	binary.LittleEndian.PutUint32(row[20:24], 1234)
	if pid, found, err := connectionPID(table, local, remote); err != nil || !found || pid != 1234 { t.Fatal("exact server tuple not found") }
	binary.LittleEndian.PutUint32(row[:4], 2)
	if _, found, _ := connectionPID(table, local, remote); found { t.Fatal("listener-only row accepted") }
}
