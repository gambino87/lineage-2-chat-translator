// Package l2ctsecurity protects the translator's private Ollama transport.
// It is a maintained patch, not part of upstream Ollama.
package l2ctsecurity

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"strings"
)

const KeyEnvironment = "L2CT_OLLAMA_API_KEY"

// RequiredKey fails closed: this dedicated build has no unauthenticated mode.
func RequiredKey() (string, error) {
	key := os.Getenv(KeyEnvironment)
	decoded, err := hex.DecodeString(key)
	if err != nil || len(decoded) != 32 || key != strings.ToLower(key) {
		return "", errors.New("private backend requires a 32-byte lowercase hexadecimal session key")
	}
	return key, nil
}

// NewKey keeps the runner credential separate from the frontend credential.
func NewKey() (string, error) {
	var value [32]byte
	if _, err := rand.Read(value[:]); err != nil { return "", err }
	return hex.EncodeToString(value[:]), nil
}

// SanitizeRunnerEnv removes alternative keys and the unneeded frontend secret.
// An empty key-file variable is NOT equivalent to absence in llama.cpp: it tries
// to open an empty filename, so remove the entry instead of assigning "".
func SanitizeRunnerEnv(env []string) []string {
	filtered := make([]string, 0, len(env))
	for _, entry := range env {
		name, _, _ := strings.Cut(entry, "=")
		if strings.EqualFold(name, "LLAMA_ARG_API_KEY_FILE") || strings.EqualFold(name, KeyEnvironment) { continue }
		filtered = append(filtered, entry)
	}
	return filtered
}

// Handler authenticates before upstream routing or request-body processing.
func Handler(next http.Handler, key string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		expected := "Bearer " + key
		if len(key) != 64 || subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte(expected)) != 1 {
			w.Header().Set("Cache-Control", "no-store")
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}

type authenticatedTransport struct {
	base *http.Transport
	key string
	pin *processPin
}

type unstartedTransport struct{}
func (unstartedTransport) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, errors.New("private runner has not started")
}

// UnstartedClient prevents any accidental unauthenticated fallback request.
func UnstartedClient() *http.Client { return &http.Client{Transport: unstartedTransport{}} }

func (t *authenticatedTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	if req.URL.Scheme != "http" || req.URL.Hostname() != "127.0.0.1" {
		return nil, errors.New("runner request must use IPv4 loopback HTTP")
	}
	if err := t.pin.alive(); err != nil { return nil, err }
	copy := req.Clone(req.Context())
	copy.Header = req.Header.Clone()
	copy.Header.Set("Authorization", "Bearer " + t.key)
	return t.base.RoundTrip(copy)
}

// NewRunnerClient pins the actual connected TCP socket, not a preceding probe.
// Every fresh connection is checked before net/http can write headers or prompts.
func NewRunnerClient(process *os.Process, key string) (*http.Client, error) {
	if process == nil || len(key) != 64 { return nil, errors.New("missing private runner identity") }
	pin, err := pinProcess(process.Pid)
	if err != nil { return nil, err }
	base := &http.Transport{DisableKeepAlives: true, Proxy: nil}
	base.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
		if err := pin.alive(); err != nil { return nil, err }
		host, _, err := net.SplitHostPort(address)
		if err != nil || host != "127.0.0.1" { return nil, errors.New("invalid runner address") }
		conn, err := (&net.Dialer{}).DialContext(ctx, "tcp4", address)
		if err != nil { return nil, err }
		if err := pin.verifyConnection(ctx, conn); err != nil {
			_ = conn.Close()
			return nil, fmt.Errorf("private runner connection rejected: %w", err)
		}
		return conn, nil
	}
	return &http.Client{
		Transport: &authenticatedTransport{base: base, key: key, pin: pin},
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return errors.New("runner redirects are forbidden") },
	}, nil
}

// CloseRunnerClient releases the retained kernel identity after its child exits.
func CloseRunnerClient(client *http.Client) {
	if client == nil { return }
	if transport, ok := client.Transport.(*authenticatedTransport); ok {
		transport.base.CloseIdleConnections()
		transport.pin.close()
	}
}
