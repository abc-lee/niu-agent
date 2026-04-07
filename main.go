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

// detectPython finds a suitable Python executable for launching the API server
func detectPython() string {
	exePath, _ := os.Executable()
	exeDir := filepath.Dir(exePath)

	var candidates []string

	// 根据操作系统选择不同的路径格式
	if runtime.GOOS == "windows" {
		// Windows 路径
		candidates = []string{
			filepath.Join(exeDir, "python", "Scripts", "python.exe"),
			filepath.Join(".", "python", "Scripts", "python.exe"),
			"E:/opencode/venv/Scripts/python.exe",
			filepath.Join(exeDir, ".venv", "Scripts", "python.exe"),
			"C:/Python311/python.exe",
			"C:/Python310/python.exe",
			"python",
			"python3",
		}
	} else {
		// Mac/Linux 路径 (使用 bin 而不是 Scripts，无 .exe 扩展名)
		homeDir, _ := os.UserHomeDir()
		candidates = []string{
			filepath.Join(homeDir, ".niu-venv", "bin", "python3"),
			filepath.Join(homeDir, ".venv", "bin", "python3"),
			filepath.Join(exeDir, "python", "bin", "python3"),
			filepath.Join(exeDir, ".venv", "bin", "python3"),
			filepath.Join(".", "python", "bin", "python3"),
			"/usr/local/bin/python3",
			"/usr/bin/python3",
			"python3",
			"python",
		}
	}

	for _, candidate := range candidates {
		cmd := exec.Command(candidate, "--version")
		if err := cmd.Run(); err == nil {
			if absPath, err := filepath.Abs(candidate); err == nil {
				return absPath
			}
			return candidate
		}
	}

	slog.Warn("No Python found, using 'python' as fallback")
	return "python"
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

	// Get project root
	execPath, _ := os.Executable()
	projectRoot := filepath.Dir(execPath)

	// Start Python API server as background process
	slog.Info("Starting Python API server...")
	apiServerCmd := exec.Command(pythonPath, "-m", "niu_api")
	apiServerCmd.Dir = projectRoot
	apiServerCmd.Env = append(os.Environ(),
		fmt.Sprintf("NIU_API_PORT=%d", *port),
		"PYTHONUNBUFFERED=1",
	)

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

	// Shutdown API server
	slog.Info("Stopping Python API server...")
	if err := apiServerCmd.Process.Kill(); err != nil {
		slog.Warn("Failed to kill API server", "error", err)
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
