package main

import (
	"bufio"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"
)

// ContextConfig represents context window configuration
type ContextConfig struct {
	WarningThreshold    float64 `json:"warningThreshold"`
	TargetThreshold     float64 `json:"targetThreshold"`
	SleepTriggerMinutes int     `json:"sleepTriggerMinutes"`
	ContextWindowSize   int     `json:"contextWindowSize"`
}

// DefaultContextConfig returns default context configuration
func DefaultContextConfig() *ContextConfig {
	return &ContextConfig{
		WarningThreshold:    0.80,
		TargetThreshold:     0.50,
		SleepTriggerMinutes: 5,
		ContextWindowSize:   200000,
	}
}

// LoadContextConfig loads context config from preferences.json
func LoadContextConfig() *ContextConfig {
	cfg := DefaultContextConfig()

	homeDir, err := os.UserHomeDir()
	if err != nil {
		return cfg
	}

	prefsPath := filepath.Join(homeDir, ".niu", "preferences.json")
	data, err := os.ReadFile(prefsPath)
	if err != nil {
		return cfg
	}

	var prefs struct {
		Context *ContextConfig `json:"context"`
	}
	if err := json.Unmarshal(data, &prefs); err != nil {
		return cfg
	}

	if prefs.Context != nil {
		if prefs.Context.WarningThreshold > 0 {
			cfg.WarningThreshold = prefs.Context.WarningThreshold
		}
		if prefs.Context.TargetThreshold > 0 {
			cfg.TargetThreshold = prefs.Context.TargetThreshold
		}
		if prefs.Context.SleepTriggerMinutes > 0 {
			cfg.SleepTriggerMinutes = prefs.Context.SleepTriggerMinutes
		}
		if prefs.Context.ContextWindowSize > 0 {
			cfg.ContextWindowSize = prefs.Context.ContextWindowSize
		}
	}

	return cfg
}

// detectPython finds the project's self-contained Python executable.
// Primary: based on executable directory (works when running built binary from any cwd).
// Fallback: current working directory (supports `go run main.go` during development).
func detectPython() string {
	var pythonRelPath string
	if runtime.GOOS == "windows" {
		pythonRelPath = filepath.Join("python", "Scripts", "python.exe")
	} else {
		pythonRelPath = filepath.Join("python", "bin", "python3")
	}

	// Primary: executable directory
	exePath, err := os.Executable()
	if err != nil {
		slog.Error("Failed to determine executable path", "error", err)
		os.Exit(1)
	}
	exeDir := filepath.Dir(exePath)
	candidate := filepath.Join(exeDir, pythonRelPath)
	if cmd := exec.Command(candidate, "--version"); cmd.Run() == nil {
		absPath, _ := filepath.Abs(candidate)
		slog.Info("Found project Python (exeDir)", "path", absPath)
		return absPath
	}

	// Fallback: current working directory (go run scenario)
	candidate = filepath.Join(".", pythonRelPath)
	if cmd := exec.Command(candidate, "--version"); cmd.Run() == nil {
		absPath, _ := filepath.Abs(candidate)
		slog.Info("Found project Python (cwd fallback)", "path", absPath)
		return absPath
	}

	slog.Error("Project Python not found", "checked_exeDir", filepath.Join(exeDir, pythonRelPath), "checked_cwd", pythonRelPath)
	os.Exit(1)
	return ""
}

// loadMemory loads user memory from ~/.niu/memory.json
func loadMemory() map[string]any {
	homeDir, err := os.UserHomeDir()
	if err != nil {
		return nil
	}

	memoryPath := filepath.Join(homeDir, ".niu", "memory.json")
	data, err := os.ReadFile(memoryPath)
	if err != nil {
		return nil
	}

	var memory map[string]any
	if err := json.Unmarshal(data, &memory); err != nil {
		return nil
	}

	return memory
}

// formatMemoryForPrompt formats memory for system prompt injection
func formatMemoryForPrompt(memory map[string]any) string {
	if memory == nil {
		return ""
	}

	var sb strings.Builder
	sb.WriteString("\n\n# 我的重要记忆\n\n")

	// Identity
	if identity, ok := memory["identity"].(map[string]any); ok {
		sb.WriteString("## 我的身份\n\n")
		if name, ok := identity["name"].(string); ok && name != "" {
			sb.WriteString(fmt.Sprintf("我的名字是 %s。\n", name))
		}
		if personality, ok := identity["personality"].([]any); ok && len(personality) > 0 {
			var traits []string
			for _, t := range personality {
				if s, ok := t.(string); ok {
					traits = append(traits, s)
				}
			}
			if len(traits) > 0 {
				sb.WriteString(fmt.Sprintf("我的性格：%s。\n", strings.Join(traits, "、")))
			}
		}
		sb.WriteString("\n")
	}

	// Workspace
	if workspace, ok := memory["workspace"].(map[string]any); ok {
		if path, ok := workspace["path"].(string); ok && path != "" {
			sb.WriteString("## 工作目录\n\n")
			sb.WriteString(fmt.Sprintf("我的知识库存储在：%s\n\n", path))
		}
	}

	// User
	if user, ok := memory["user"].(map[string]any); ok {
		if name, ok := user["name"].(string); ok && name != "" {
			sb.WriteString("## 用户信息\n\n")
			sb.WriteString(fmt.Sprintf("用户称呼：%s\n", name))
		}
	}

	return sb.String()
}

var (
	contextConfig *ContextConfig
)

func main() {
	// Parse flags
	_ = flag.String("config", "./config", "path to configuration directory") // Kept for compatibility
	showSettings := flag.Bool("settings", false, "open settings window")
	showGraph := flag.Bool("graph", false, "open knowledge graph window")
	port := flag.Int("port", 9876, "port for Python API server")
	flag.Parse()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Handle shutdown signals
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		<-sigChan
		slog.Info("Shutdown signal received")
		cancel()
	}()

	slog.Info("Niu launcher starting...")

	// Load context configuration
	contextConfig = LoadContextConfig()

	// Detect Python
	pythonPath := detectPython()
	slog.Info("Using Python path", "path", pythonPath)

	// Load memory for injection (passed to Python API via environment)
	memory := loadMemory()
	_ = formatMemoryForPrompt(memory) // Memory injection handled by Python API

	// Extract workspace.path from memory and set as WORKSPACE_PATH env var
	// so all child processes (Python API, MCP servers) use the correct vectors.db path
	var workspacePath string
	if memory != nil {
		if ws, ok := memory["workspace"].(map[string]any); ok {
			if path, ok := ws["path"].(string); ok && path != "" {
				workspacePath = path
			}
		}
	}

	// Get project root
	execPath, _ := os.Executable()
	projectRoot := filepath.Dir(execPath)

	// Start Python API server as background process
	slog.Info("Starting Python API server...")
	apiServerCmd := exec.Command(pythonPath, "-m", "niu_api")
	apiServerCmd.Dir = projectRoot
	envVars := []string{
		fmt.Sprintf("NIU_API_PORT=%d", *port),
		"PYTHONUNBUFFERED=1",
	}
		envVars = append(envVars, "LITELLM_LOCAL_MODEL_COST_MAP=True")
		envVars = append(envVars, "LITELLM_NO_AIOHTTP_TRANSPORT=True")
	if workspacePath != "" {
		if _, err := os.Stat(workspacePath); err != nil {
			slog.Error("WORKSPACE_PATH directory does not exist, skipping", "path", workspacePath, "error", err)
			workspacePath = ""
		}
	}
	if workspacePath != "" {
		envVars = append(envVars, fmt.Sprintf("WORKSPACE_PATH=%s", workspacePath))
		slog.Info("Setting WORKSPACE_PATH for Python API", "path", workspacePath)
	}
	apiServerCmd.Env = append(os.Environ(), envVars...)

	// Capture output
	if stdout, err := apiServerCmd.StdoutPipe(); err == nil {
		go func() {
			scanner := bufio.NewScanner(stdout)
			for scanner.Scan() {
				slog.Info("niu_api", "output", scanner.Text())
			}
		}()
	}
	if stderr, err := apiServerCmd.StderrPipe(); err == nil {
		go func() {
			scanner := bufio.NewScanner(stderr)
			for scanner.Scan() {
				slog.Error("niu_api", "error", scanner.Text())
			}
		}()
	}

	// Set process group so we can kill the entire group on shutdown
	// (including child processes like multiprocessing.resource_tracker)
	apiServerCmd.SysProcAttr = &syscall.SysProcAttr{
		Setpgid: true,
	}

	if err := apiServerCmd.Start(); err != nil {
		slog.Error("Failed to start Python API server", "error", err)
		fmt.Printf("Failed to start API server: %v\n", err)
		os.Exit(1)
	}

	// Wait for API server to be ready
	apiReady := false
	for i := 0; i < 30; i++ {
		time.Sleep(1 * time.Second)
		resp, err := http.Get(fmt.Sprintf("http://127.0.0.1:%d/health", *port))
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				apiReady = true
				break
			}
		}
	}

	if !apiReady {
		slog.Warn("Python API server may not be ready")
	}
	slog.Info("Python API server started")

	// Wait for preload to complete (embedding-service, MCP tools)
	slog.Info("Waiting for preload to complete...")
	preloadReady := false
	for i := 0; i < 120; i++ {
		time.Sleep(500 * time.Millisecond)
		resp, err := http.Get(fmt.Sprintf("http://127.0.0.1:%d/api/preload-status", *port))
		if err == nil {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()

			// Parse JSON
			var status struct {
				Ready  bool   `json:"ready"`
				Uptime string `json:"uptime"`
			}
			if err := json.Unmarshal(body, &status); err != nil {
				slog.Warn("Failed to parse preload status", "error", err, "body", string(body))
				continue
			}

			// Log first response or when ready
			if i == 0 || status.Ready {
				slog.Info("Preload status check", "ready", status.Ready, "uptime", status.Uptime, "attempt", i+1)
			}

			if status.Ready {
				preloadReady = true
				slog.Info("Preload complete, launching window...")
				break
			}
		}
	}

	if !preloadReady {
		slog.Warn("Preload may not be complete, proceeding anyway")
	}

	// If --settings flag, just open settings and exit
	if *showSettings {
		if _, err := launchWindow("settings"); err != nil {
			slog.Error("Failed to launch settings window", "error", err)
		}
		return
	}

	// If --graph flag, just open graph and exit
	if *showGraph {
		if _, err := launchWindow("graph"); err != nil {
			slog.Error("Failed to launch graph window", "error", err)
		}
		return
	}

	// Launch assistant window
	electronCmd, err := launchWindow("assistant")
	if err != nil {
		slog.Error("Failed to launch assistant window", "error", err)
		fmt.Println("\nPlease run manually: cd ui/assistant && npm start")
	}

	// Monitor Electron process - when it exits, trigger shutdown
	if electronCmd != nil {
		go func() {
			err := electronCmd.Wait()
			if err != nil {
				slog.Info("Electron window exited", "error", err)
			} else {
				slog.Info("Electron window closed normally")
			}
			cancel() // Trigger shutdown
		}()
	}

	// Wait for shutdown (either signal or Electron exit)
	<-ctx.Done()

	// Graceful shutdown: notify Python API first
	slog.Info("Notifying Python API to shutdown...")
	if err := notifyShutdown(*port); err != nil {
		slog.Warn("Failed to notify Python API shutdown", "error", err)
	}

	// Wait for graceful shutdown (increased from 500ms to 2s to allow subprocess cleanup)
	time.Sleep(2 * time.Second)

	// Kill the entire process group (not just the main process)
	// This ensures child processes (resource_tracker, etc.) are also killed
	slog.Info("Stopping Python API server (process group)...")
	pgid, err := syscall.Getpgid(apiServerCmd.Process.Pid)
	if err != nil {
		slog.Warn("Failed to get process group, falling back to single process kill", "error", err)
		if err := apiServerCmd.Process.Kill(); err != nil {
			slog.Warn("Failed to kill API server", "error", err)
		}
	} else {
		// Send SIGKILL to the entire process group (negative pgid means process group)
		if err := syscall.Kill(-pgid, syscall.SIGKILL); err != nil {
			slog.Warn("Failed to kill process group, falling back to single process kill", "error", err)
			if err := apiServerCmd.Process.Kill(); err != nil {
				slog.Warn("Failed to kill API server", "error", err)
			}
		}
	}

	// Electron window should already be closed (triggered shutdown)
	// No need to kill it again

	slog.Info("Niu launcher shutdown complete")
}

// notifyShutdown sends shutdown request to Python API
func notifyShutdown(port int) error {
	client := http.Client{Timeout: 2 * time.Second}
	resp, err := client.Post(fmt.Sprintf("http://127.0.0.1:%d/api/shutdown", port), "application/json", nil)
	if err != nil {
		return err
	}
	resp.Body.Close()
	return nil
}

func launchWindow(name string) (*exec.Cmd, error) {
	// 使用可执行文件所在目录作为基准，而不是当前工作目录
	exePath, _ := os.Executable()
	exeDir := filepath.Dir(exePath)
	windowDir := filepath.Join(exeDir, "ui", name)

	cmd := exec.Command("npm", "start")
	cmd.Dir = windowDir
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Stdin = os.Stdin

	if err := cmd.Start(); err != nil {
		return nil, err
	}
	return cmd, nil
}
