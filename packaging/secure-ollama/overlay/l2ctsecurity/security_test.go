package l2ctsecurity

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestRequiredKeyRejectsMissingShortUppercase(t *testing.T) {
	for _, value := range []string{"", "short", strings.Repeat("A", 64), strings.Repeat("g", 64)} {
		t.Setenv(KeyEnvironment, value)
		if _, err := RequiredKey(); err == nil { t.Fatal("invalid key accepted") }
	}
	t.Setenv(KeyEnvironment, strings.Repeat("a", 64))
	if _, err := RequiredKey(); err != nil { t.Fatal(err) }
}

func TestAuthenticationPrecedesAllRouting(t *testing.T) {
	key := strings.Repeat("a", 64)
	for _, path := range []string{"/api/chat", "/api/delete", "/api/pull", "/api/version", "/debug/pprof/"} {
		calls := 0
		h := Handler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls++; w.WriteHeader(204) }), key)
		for _, header := range []string{"", "Bearer wrong", "Bearer " + key} {
			r := httptest.NewRequest(http.MethodPost, path, strings.NewReader("untrusted body"))
			r.Header.Set("Authorization", header)
			w := httptest.NewRecorder()
			h.ServeHTTP(w, r)
			if header != "Bearer " + key && w.Code != 401 { t.Fatal("unauthenticated request accepted") }
		}
		if calls != 1 { t.Fatal("unauthenticated route was invoked") }
	}
}

func TestUnstartedClientCannotSend(t *testing.T) {
	if _, err := UnstartedClient().Get("http://127.0.0.1:1/health"); err == nil { t.Fatal("unstarted runner allowed a request") }
}

func TestNewKeyIsDistinctAndValid(t *testing.T) {
	a, err := NewKey(); if err != nil { t.Fatal(err) }
	b, err := NewKey(); if err != nil { t.Fatal(err) }
	if a == b || len(a) != 64 || len(b) != 64 { t.Fatal("invalid generated keys") }
	t.Setenv(KeyEnvironment, a)
	if _, err := RequiredKey(); err != nil { t.Fatal(err) }
}

func TestRunnerEnvironmentRemovesAlternativeCredentials(t *testing.T) {
	env := []string{"PATH=normal", "LLAMA_API_KEY=runner", "llama_arg_api_key_file=untrusted", "l2ct_ollama_api_key=frontend"}
	got := SanitizeRunnerEnv(env)
	if len(got) != 2 || got[0] != "PATH=normal" || got[1] != "LLAMA_API_KEY=runner" { t.Fatal("runner credential isolation failed") }
}
