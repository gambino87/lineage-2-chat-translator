"""Apply narrow, fail-closed changes to the exactly pinned upstream source."""
from __future__ import annotations

from pathlib import Path
import shutil

HERE = Path(__file__).resolve().parent
IMPORT = '\t"github.com/ollama/ollama/l2ctsecurity"\n'


def replace_once(root: Path, name: str, old: str, new: str) -> None:
    path = root / name
    data = path.read_text(encoding="utf-8")
    if data.count(old) != 1:
        raise RuntimeError(f"Pinned source patch context changed: {name}")
    path.write_text(data.replace(old, new), encoding="utf-8", newline="\n")


def apply(root: Path) -> None:
    for name in ("cmd/cmd.go", "server/routes.go", "llm/llama_server.go", "discover/llama_server.go"):
        replace_once(root, name, 'import (\n', 'import (\n' + IMPORT)
    replace_once(root, "cmd/cmd.go", 'func RunServer(_ *cobra.Command, _ []string) error {\n', '''func RunServer(_ *cobra.Command, _ []string) error {
	// Validate before opening any listener or creating upstream auth files.
	if _, err := l2ctsecurity.RequiredKey(); err != nil { return err }
	host, _, hostErr := net.SplitHostPort(envconfig.Host().Host)
	if hostErr != nil || host != "127.0.0.1" { return errors.New("private backend requires IPv4 loopback") }
''')
    replace_once(root, "server/routes.go", 'func Serve(ln net.Listener) error {\n', '''func Serve(ln net.Listener) error {
	privateKey, privateKeyErr := l2ctsecurity.RequiredKey()
	if privateKeyErr != nil { return privateKeyErr }
''')
    replace_once(root, "server/routes.go", '\t\tHandler: nil,\n', '''		// Wrap the entire mux, including pprof, before any upstream route executes.
		Handler: l2ctsecurity.Handler(http.DefaultServeMux, privateKey),
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout: 30 * time.Second,
		MaxHeaderBytes: 16 * 1024,
''')
    replace_once(root, "llm/llama_server.go", '''func newLlamaServerHTTPClient() *http.Client {
	return &http.Client{
		Transport: &http.Transport{
			DisableKeepAlives: true,
			Proxy:             nil,
		},
	}
}''', '''func newLlamaServerHTTPClient() *http.Client {
	return l2ctsecurity.UnstartedClient()
}''')
    replace_once(root, "llm/llama_server.go", '''func (s *llamaServerRunner) startProcess() error {
	cmd, port, err := startLlamaServer(s.launch, s.output)''', '''func (s *llamaServerRunner) startProcess() error {
	runnerKey, keyErr := l2ctsecurity.NewKey()
	if keyErr != nil { return keyErr }
	// A copied environment map prevents sharing credentials across runner restarts.
	launch := s.launch
	launch.extraEnvs = cloneStringMap(s.launch.extraEnvs)
	if launch.extraEnvs == nil { launch.extraEnvs = make(map[string]string) }
	launch.extraEnvs["LLAMA_API_KEY"] = runnerKey
	cmd, port, err := startLlamaServer(launch, s.output)''')
    replace_once(root, "llm/llama_server.go", '''	s.cmd = cmd
	s.port = port
	s.done = make(chan struct{})''', '''	privateClient, clientErr := l2ctsecurity.NewRunnerClient(cmd.Process, runnerKey)
	if clientErr != nil {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		return clientErr
	}
	s.client = privateClient
	s.cmd = cmd
	s.port = port
	s.done = make(chan struct{})''')
    replace_once(root, "llm/llama_server.go", '''	go func(cmd *exec.Cmd, done chan struct{}) {
		err := cmd.Wait()''', '''	go func(cmd *exec.Cmd, done chan struct{}) {
		defer l2ctsecurity.CloseRunnerClient(privateClient)
		err := cmd.Wait()''')
    replace_once(root, "mlxrunner/client.go", 'func NewClient(modelName string, softContextLength int) (*Client, error) {\n', '''func NewClient(modelName string, softContextLength int) (*Client, error) {
	if runtime.GOOS == "windows" { return nil, errors.New("MLX models are not supported by the private Windows backend; use a GGUF model") }
''')
    replace_once(root, "mlxrunner/server.go", 'import (\n', 'import (\n\t"runtime"\n')
    replace_once(root, "mlxrunner/server.go", 'func Execute(args []string) error {\n', '''func Execute(args []string) error {
	if runtime.GOOS == "windows" { return errors.New("MLX runner is disabled in the private Windows backend") }
''')
    replace_once(root, "discover/llama_server.go", '\tllm.SetupLlamaServerCommandEnv(cmd, llamaServer, libDirs, extraEnvs)\n', '''	// Device discovery can briefly open a llama-server listener without a model.
	// Authenticate that listener too; no unauthenticated transient exception.
	discoveryKey, keyErr := l2ctsecurity.NewKey()
	if keyErr != nil { return nil, status, keyErr }
	privateEnvs := make(map[string]string, len(extraEnvs)+2)
	for key, value := range extraEnvs { privateEnvs[key] = value }
	privateEnvs["LLAMA_API_KEY"] = discoveryKey
	llm.SetupLlamaServerCommandEnv(cmd, llamaServer, libDirs, privateEnvs)
''')
    replace_once(root, "llm/llama_server.go", '''	for key, val := range envUpdates {
		if !applied[key] {
			cmd.Env = append(cmd.Env, key+"="+val)
		}
	}
}''', '''	for key, val := range envUpdates {
		if !applied[key] {
			cmd.Env = append(cmd.Env, key+"="+val)
		}
	}
	cmd.Env = l2ctsecurity.SanitizeRunnerEnv(cmd.Env)
}''')
    replace_once(root, "discover/llama_server.go", '''	// Read stderr until we see system_info or timeout
''', '''	// The frontend owns this transient child too; forced exit must not orphan it.
	if err := llm.AddRunnerToJob(cmd.Process.Pid); err != nil {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		return nil, status, fmt.Errorf("assign discovery runner to private job: %w", err)
	}
	// Read stderr until we see system_info or timeout
''')
    shutil.copytree(HERE / "overlay" / "l2ctsecurity", root / "l2ctsecurity")
