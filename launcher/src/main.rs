use std::env;
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use clap::Parser;
use serde::Deserialize;
use tracing::{error, info, warn};

// ---------------------------------------------------------------------------
// ContextConfig — corresponds to Go's ContextConfig struct
// ---------------------------------------------------------------------------

/// ContextConfig represents context window configuration
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ContextConfig {
    warning_threshold: f64,
    target_threshold: f64,
    sleep_trigger_minutes: i32,
    context_window_size: i32,
}

/// DefaultContextConfig returns default context configuration
fn default_context_config() -> ContextConfig {
    ContextConfig {
        warning_threshold: 0.80,
        target_threshold: 0.50,
        sleep_trigger_minutes: 5,
        context_window_size: 200_000,
    }
}

/// Helper struct for deserializing preferences.json
#[derive(Debug, Deserialize)]
struct Preferences {
    context: Option<ContextConfigOverrides>,
}

/// Partial context config from preferences.json (all fields optional)
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ContextConfigOverrides {
    warning_threshold: Option<f64>,
    target_threshold: Option<f64>,
    sleep_trigger_minutes: Option<i32>,
    context_window_size: Option<i32>,
}

/// LoadContextConfig loads context config from preferences.json
fn load_context_config() -> ContextConfig {
    let mut cfg = default_context_config();

    let home_dir = match dirs::home_dir() {
        Some(d) => d,
        None => return cfg,
    };

    let prefs_path = home_dir.join(".niu").join("preferences.json");
    let data = match fs::read_to_string(&prefs_path) {
        Ok(d) => d,
        Err(_) => return cfg,
    };

    let prefs: Preferences = match serde_json::from_str(&data) {
        Ok(p) => p,
        Err(_) => return cfg,
    };

    if let Some(ctx) = prefs.context {
        if let Some(v) = ctx.warning_threshold {
            if v > 0.0 {
                cfg.warning_threshold = v;
            }
        }
        if let Some(v) = ctx.target_threshold {
            if v > 0.0 {
                cfg.target_threshold = v;
            }
        }
        if let Some(v) = ctx.sleep_trigger_minutes {
            if v > 0 {
                cfg.sleep_trigger_minutes = v;
            }
        }
        if let Some(v) = ctx.context_window_size {
            if v > 0 {
                cfg.context_window_size = v;
            }
        }
    }

    cfg
}

// ---------------------------------------------------------------------------
// detectPython — corresponds to Go's detectPython()
// ---------------------------------------------------------------------------

/// detectPython finds the project's self-contained Python executable.
/// Primary: based on executable directory (works when running built binary from any cwd).
/// Fallback: current working directory (supports development).
fn detect_python() -> String {
    let python_rel_path: PathBuf = if cfg!(target_os = "windows") {
        PathBuf::from("python").join("Scripts").join("python.exe")
    } else {
        PathBuf::from("python").join("bin").join("python3")
    };

    // Primary: executable directory
    let exe_path = match env::current_exe() {
        Ok(p) => p,
        Err(e) => {
            error!("Failed to determine executable path: {}", e);
            std::process::exit(1);
        }
    };
    let exe_dir = exe_path.parent().unwrap_or_else(|| {
        error!("Executable path has no parent");
        std::process::exit(1);
    });
    let candidate = exe_dir.join(&python_rel_path);
    if Command::new(&candidate)
        .arg("--version")
        .output()
        .is_ok()
    {
        let abs_path = fs::canonicalize(&candidate).unwrap_or_else(|_| candidate.clone());
        info!("Found project Python (exeDir): {}", abs_path.display());
        return abs_path.to_string_lossy().to_string();
    }

    // Fallback: current working directory (cargo run scenario)
    let candidate = PathBuf::from(".").join(&python_rel_path);
    if Command::new(&candidate)
        .arg("--version")
        .output()
        .is_ok()
    {
        let abs_path = fs::canonicalize(&candidate).unwrap_or_else(|_| candidate.clone());
        info!("Found project Python (cwd fallback): {}", abs_path.display());
        return abs_path.to_string_lossy().to_string();
    }

    error!(
        "Project Python not found, checked_exeDir: {}, checked_cwd: {}",
        exe_dir.join(&python_rel_path).display(),
        python_rel_path.display()
    );
    std::process::exit(1);
}

// ---------------------------------------------------------------------------
// initNiuDir — corresponds to Go's initNiuDir()
// ---------------------------------------------------------------------------

/// initNiuDir ensures ~/.niu/ directory exists and copies template files if needed
fn init_niu_dir(project_root: &str) {
    let home_dir = match dirs::home_dir() {
        Some(d) => d,
        None => {
            error!("Failed to get home directory");
            return;
        }
    };

    let niu_dir = home_dir.join(".niu");

    // Create ~/.niu/ directory if it doesn't exist
    if let Err(e) = fs::create_dir_all(&niu_dir) {
        error!("Failed to create ~/.niu directory: {}", e);
        return;
    }

    // Template files to copy if they don't exist in ~/.niu/
    let template_files = ["memory.json", "preferences.json"];
    let template_dir = PathBuf::from(project_root).join("memory");

    for filename in &template_files {
        let dst_path = niu_dir.join(filename);
        // Only copy if destination doesn't exist
        if dst_path.exists() {
            continue;
        }
        let src_path = template_dir.join(filename);
        let src_data = match fs::read(&src_path) {
            Ok(d) => d,
            Err(e) => {
                warn!("Template file not found, skipping: {} ({})", filename, e);
                continue;
            }
        };
        if let Err(e) = fs::write(&dst_path, &src_data) {
            error!(
                "Failed to copy template file: src={}, dst={}, error={}",
                src_path.display(),
                dst_path.display(),
                e
            );
            continue;
        }
        info!("Copied template file: {} -> {}", filename, dst_path.display());
    }
}

// ---------------------------------------------------------------------------
// loadMemory — corresponds to Go's loadMemory()
// ---------------------------------------------------------------------------

/// loadMemory loads user memory from ~/.niu/memory.json
fn load_memory() -> Option<serde_json::Value> {
    let home_dir = dirs::home_dir()?;

    let memory_path = home_dir.join(".niu").join("memory.json");
    let data = fs::read_to_string(&memory_path).ok()?;

    serde_json::from_str(&data).ok()
}

// ---------------------------------------------------------------------------
// formatMemoryForPrompt — corresponds to Go's formatMemoryForPrompt()
// ---------------------------------------------------------------------------

/// formatMemoryForPrompt formats memory for system prompt injection
fn format_memory_for_prompt(memory: &Option<serde_json::Value>) -> String {
    let memory = match memory {
        Some(v) => v,
        None => return String::new(),
    };

    let mut sb = String::new();
    sb.push_str("\n\n# 我的重要记忆\n\n");

    // Identity
    if let Some(identity) = memory.get("identity").and_then(|v| v.as_object()) {
        sb.push_str("## 我的身份\n\n");
        if let Some(name) = identity.get("name").and_then(|v| v.as_str()) {
            if !name.is_empty() {
                sb.push_str(&format!("我的名字是 {}。\n", name));
            }
        }
        if let Some(personality) = memory
            .get("identity")
            .and_then(|v| v.get("personality"))
            .and_then(|v| v.as_array())
        {
            if !personality.is_empty() {
                let traits: Vec<&str> = personality
                    .iter()
                    .filter_map(|t| t.as_str())
                    .collect();
                if !traits.is_empty() {
                    sb.push_str(&format!("我的性格：{}。\n", traits.join("、")));
                }
            }
        }
        sb.push('\n');
    }

    // Workspace
    if let Some(workspace) = memory.get("workspace").and_then(|v| v.as_object()) {
        if let Some(path) = workspace.get("path").and_then(|v| v.as_str()) {
            if !path.is_empty() && !path.starts_with("请询问") {
                sb.push_str("## 工作目录\n\n");
                sb.push_str(&format!("我的知识库存储在：{}\n\n", path));
            }
        }
    }

    // User
    if let Some(user) = memory.get("user").and_then(|v| v.as_object()) {
        if let Some(name) = user.get("name").and_then(|v| v.as_str()) {
            if !name.is_empty() {
                sb.push_str("## 用户信息\n\n");
                sb.push_str(&format!("用户称呼：{}\n", name));
            }
        }
    }

    sb
}

// ---------------------------------------------------------------------------
// notifyShutdown — corresponds to Go's notifyShutdown()
// ---------------------------------------------------------------------------

/// notifyShutdown sends shutdown request to Python API
fn notify_shutdown(port: u16) -> Result<(), Box<dyn std::error::Error>> {
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()?;
    let url = format!("http://127.0.0.1:{}/api/shutdown", port);
    client.post(&url).header("Content-Type", "application/json").send()?;
    Ok(())
}

// ---------------------------------------------------------------------------
// isAPIRunning — corresponds to Go's isAPIRunning()
// ---------------------------------------------------------------------------

/// isAPIRunning checks if the Python API is already running on the given port
fn is_api_running(port: u16) -> bool {
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(2))
        .build();
    let client = match client {
        Ok(c) => c,
        Err(_) => return false,
    };
    let url = format!("http://127.0.0.1:{}/health", port);
    match client.get(&url).send() {
        Ok(resp) => resp.status().is_success(),
        Err(_) => false,
    }
}

// ---------------------------------------------------------------------------
// killStaleAPIProcess — corresponds to Go's killStaleAPIProcess()
// ---------------------------------------------------------------------------

/// killStaleAPIProcess checks if the API port is occupied and kills the stale process
fn kill_stale_api_process(port: u16) {
    // Check if port is occupied by trying to connect
    let url = format!("http://127.0.0.1:{}/health", port);
    let resp = reqwest::blocking::get(&url);
    let conn = match resp {
        Ok(r) => r,
        Err(_) => {
            // Port not occupied, safe to start
            return;
        }
    };
    // Drain the body (Go's resp.Body.Close())
    let _ = conn.text();

    warn!("Port already occupied, attempting to stop stale API process (port={})", port);

    // Try graceful shutdown via HTTP endpoint
    if notify_shutdown(port).is_ok() {
        info!("Sent shutdown request to stale API process, waiting 2s...");
        thread::sleep(Duration::from_secs(2));
        // Check if port is now free
        let url2 = format!("http://127.0.0.1:{}/health", port);
        let resp2 = reqwest::blocking::get(&url2);
        match resp2 {
            Err(_) => {
                info!("Stale API process exited gracefully");
                return;
            }
            Ok(r) => {
                let _ = r.text();
            }
        }
    }

    // Still alive, force kill with pkill
    warn!("Stale API process still alive, force killing with pkill");
    #[cfg(unix)]
    {
        let kill_result = Command::new("pkill")
            .arg("-f")
            .arg("python.*niu_api")
            .status();
        match kill_result {
            Ok(status) if status.success() => {
                info!("Sent SIGTERM to stale API process via pkill");
            }
            _ => {
                warn!("pkill failed (process may already be gone)");
            }
        }
    }
    #[cfg(windows)]
    {
        let kill_result = Command::new("taskkill")
            .args(["/F", "/FI", "WINDOWTITLE eq niu_api*"])
            .status();
        match kill_result {
            Ok(status) if status.success() => {
                info!("Sent kill to stale API process via taskkill");
            }
            _ => {
                warn!("taskkill failed (process may already be gone)");
            }
        }
    }
    // Wait for process to exit
    thread::sleep(Duration::from_secs(1));
}

// ---------------------------------------------------------------------------
// launchWindow — corresponds to Go's launchWindow()
// ---------------------------------------------------------------------------

/// launchWindow launches an Electron window via npm start
fn launch_window(name: &str) -> Result<std::process::Child, Box<dyn std::error::Error>> {
    // Use executable directory as base, not current working directory
    let exe_path = env::current_exe()?;
    let exe_dir = exe_path.parent().unwrap_or_else(|| {
        error!("Executable path has no parent");
        std::process::exit(1);
    });
    let window_dir = exe_dir.join("ui").join(name);

    let mut cmd = Command::new("npm");
    cmd.arg("start");
    cmd.current_dir(&window_dir);
    cmd.stdout(std::process::Stdio::inherit());
    cmd.stderr(std::process::Stdio::inherit());
    cmd.stdin(std::process::Stdio::inherit());

    let child = cmd.spawn()?;
    Ok(child)
}

// ---------------------------------------------------------------------------
// CLI args — corresponds to Go's flag package
// ---------------------------------------------------------------------------

#[derive(Parser, Debug)]
#[command(name = "niu-launcher", about = "Niu launcher")]
struct Args {
    /// path to configuration directory (kept for compatibility)
    #[arg(long, default_value = "./config")]
    config: String,

    /// open settings window
    #[arg(long)]
    settings: bool,

    /// open knowledge graph window
    #[arg(long)]
    graph: bool,

    /// port for Python API server
    #[arg(long, default_value_t = 9876)]
    port: u16,
}

// ---------------------------------------------------------------------------
// main — corresponds to Go's main()
// ---------------------------------------------------------------------------

fn main() {
    // Initialize tracing (replaces Go's slog)
    tracing_subscriber::fmt().init();

    // Parse args (replaces Go's flag)
    let args = Args::parse();

    // Context cancellation via AtomicBool (replaces Go's context.WithCancel)
    let cancelled = Arc::new(AtomicBool::new(false));
    let cancelled_clone = cancelled.clone();

    // Handle shutdown signals (replaces Go's signal.Notify)
    ctrlc::set_handler(move || {
        info!("Shutdown signal received");
        cancelled_clone.store(true, Ordering::SeqCst);
    })
    .expect("Failed to set Ctrl-C handler");

    info!("Niu launcher starting...");

    // --settings and --graph modes: connect to existing API, do NOT start a new one
    // This prevents orphan Python API processes when these modes exit without shutdown
    if args.settings || args.graph {
        if is_api_running(args.port) {
            let window_name = if args.graph { "graph" } else { "settings" };
            info!(
                "API already running, launching window: window={}, port={}",
                window_name, args.port
            );
            if let Err(e) = launch_window(window_name) {
                error!("Failed to launch window: window={}, error={}", window_name, e);
                std::process::exit(1);
            }
            return;
        }
        error!(
            "API is not running, please start the main program first (port={})",
            args.port
        );
        println!(
            "Error: API is not running on port {}. Please start the main program (niu) first.",
            args.port
        );
        std::process::exit(1);
    }

    // Load context configuration
    let _context_config = load_context_config();

    // Detect Python
    let python_path = detect_python();
    info!("Using Python path: {}", python_path);

    // Get project root (needed for template file paths)
    // Primary: executable directory (works when running built binary from any cwd)
    // Fallback: current working directory (supports development)
    let exe_path = env::current_exe().unwrap_or_else(|_| PathBuf::from("."));
    let mut project_root = exe_path
        .parent()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|| ".".to_string());
    let memory_dir = PathBuf::from(&project_root).join("memory");
    if !memory_dir.exists() {
        // exeDir doesn't contain memory/ — likely development with temp build dir
        let cwd = env::current_dir()
            .map(|d| d.to_string_lossy().to_string())
            .unwrap_or_else(|_| ".".to_string());
        let cwd_memory_dir = PathBuf::from(&cwd).join("memory");
        if cwd_memory_dir.exists() {
            info!(
                "memory/ not found in exeDir, using cwd as project root: exeDir={}, cwd={}",
                project_root, cwd
            );
            project_root = cwd;
        } else {
            warn!(
                "memory/ not found in exeDir or cwd, template copy will be skipped: exeDir={}, cwd={}",
                project_root, cwd
            );
        }
    }

    // Initialize ~/.niu/ directory and copy template files if needed
    init_niu_dir(&project_root);

    // Load memory for injection (passed to Python API via environment)
    let memory = load_memory();
    let _ = format_memory_for_prompt(&memory); // Memory injection handled by Python API

    // Extract workspace.path from memory and set as WORKSPACE_PATH env var
    // so all child processes (Python API, MCP servers) use the correct vectors.db path
    // Skip placeholder values like "请询问用户指定工作目录" which are not real paths
    let mut workspace_path = String::new();
    if let Some(mem) = &memory {
        if let Some(ws) = mem.get("workspace").and_then(|v| v.as_object()) {
            if let Some(path) = ws.get("path").and_then(|v| v.as_str()) {
                if !path.is_empty() && !path.starts_with("请询问") {
                    workspace_path = path.to_string();
                }
            }
        }
    }

    // Start Python API server as background process
    info!("Starting Python API server...");
    let mut api_server_cmd = Command::new(&python_path);
    api_server_cmd.args(["-m", "niu_api"]);
    api_server_cmd.current_dir(&project_root);

    let mut env_vars: Vec<(String, String)> = Vec::new();
    env_vars.push((
        "NIU_API_PORT".to_string(),
        args.port.to_string(),
    ));
    env_vars.push(("PYTHONUNBUFFERED".to_string(), "1".to_string()));
    env_vars.push(("LITELLM_LOCAL_MODEL_COST_MAP".to_string(), "True".to_string()));
    env_vars.push(("LITELLM_NO_AIOHTTP_TRANSPORT".to_string(), "True".to_string()));

    if !workspace_path.is_empty() {
        if !PathBuf::from(&workspace_path).exists() {
            error!(
                "WORKSPACE_PATH directory does not exist, skipping: path={}, error=directory not found",
                workspace_path
            );
            workspace_path = String::new();
        }
    }
    if !workspace_path.is_empty() {
        env_vars.push(("WORKSPACE_PATH".to_string(), workspace_path.clone()));
        info!("Setting WORKSPACE_PATH for Python API: {}", workspace_path);
    }

    // Set environment: inherit current env + add our vars
    for (key, value) in env::vars() {
        api_server_cmd.env(&key, &value);
    }
    for (key, value) in &env_vars {
        api_server_cmd.env(key, value);
    }

    // Kill stale Python API process occupying the port before starting a new one
    kill_stale_api_process(args.port);

    // Capture output
    let mut api_server_child = api_server_cmd
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .expect("Failed to start Python API server command");

    // stdout goroutine -> thread
    if let Some(stdout) = api_server_child.stdout.take() {
        thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines() {
                match line {
                    Ok(text) => info!("niu_api output: {}", text),
                    Err(_) => break,
                }
            }
        });
    }

    // stderr goroutine -> thread
    if let Some(stderr) = api_server_child.stderr.take() {
        thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines() {
                match line {
                    Ok(line_text) => {
                        // Filter tqdm progress bar lines (Batches:, \r lines)
                        if line_text.starts_with("Batches:") || line_text.starts_with('\r') {
                            continue;
                        }
                        // Skip lines that are only ANSI escape sequences (tqdm control chars)
                        let stripped = line_text.replace('\x1b', "");
                        if stripped.is_empty() {
                            continue;
                        }
                        // Route log level based on Python logger markers
                        if line_text.contains("| INFO") || line_text.contains("| DEBUG") {
                            info!("niu_api stderr: {}", line_text);
                        } else if line_text.contains("| WARNING") || line_text.contains("| WARN") {
                            warn!("niu_api stderr: {}", line_text);
                        } else {
                            error!("niu_api stderr: {}", line_text);
                        }
                    }
                    Err(_) => break,
                }
            }
        });
    }

    // Wait for API server to be ready
    let mut api_ready = false;
    for i in 0..30 {
        thread::sleep(Duration::from_secs(1));
        let url = format!("http://127.0.0.1:{}/health", args.port);
        match reqwest::blocking::get(&url) {
            Ok(resp) => {
                if resp.status().is_success() {
                    api_ready = true;
                    break;
                }
            }
            Err(_) => {}
        }
        // Suppress unused variable warning for loop counter
        let _ = i;
    }

    if !api_ready {
        warn!("Python API server may not be ready");
    }
    info!("Python API server started");

    // Wait for preload to complete (embedding-service, MCP tools)
    info!("Waiting for preload to complete...");
    let mut preload_ready = false;
    for i in 0..120 {
        thread::sleep(Duration::from_millis(500));
        let url = format!("http://127.0.0.1:{}/api/preload-status", args.port);
        match reqwest::blocking::get(&url) {
            Ok(resp) => {
                let body = match resp.text() {
                    Ok(b) => b,
                    Err(_) => continue,
                };

                // Parse JSON
                #[derive(Debug, Deserialize)]
                struct PreloadStatus {
                    ready: bool,
                    uptime: String,
                }
                let status: PreloadStatus = match serde_json::from_str(&body) {
                    Ok(s) => s,
                    Err(e) => {
                        warn!("Failed to parse preload status: error={}, body={}", e, body);
                        continue;
                    }
                };

                // Log first response or when ready
                if i == 0 || status.ready {
                    info!(
                        "Preload status check: ready={}, uptime={}, attempt={}",
                        status.ready, status.uptime, i + 1
                    );
                }

                if status.ready {
                    preload_ready = true;
                    info!("Preload complete, launching window...");
                    break;
                }
            }
            Err(_) => {}
        }
    }

    if !preload_ready {
        warn!("Preload may not be complete, proceeding anyway");
    }

    // Launch assistant window
    let mut electron_child = match launch_window("assistant") {
        Ok(child) => Some(child),
        Err(e) => {
            error!("Failed to launch assistant window: {}", e);
            println!("\nPlease run manually: cd ui/assistant && npm start");
            None
        }
    };

    // Monitor Electron process - when it exits, trigger shutdown
    if let Some(mut child) = electron_child.take() {
        let cancelled_ref = cancelled.clone();
        thread::spawn(move || {
            let result = child.wait();
            match result {
                Err(e) => {
                    info!("Electron window exited: error={}", e);
                }
                Ok(status) => {
                    if status.success() {
                        info!("Electron window closed normally");
                    } else {
                        info!("Electron window exited with status: {}", status);
                    }
                }
            }
            cancelled_ref.store(true, Ordering::SeqCst); // Trigger shutdown
        });
    }

    // Wait for shutdown (either signal or Electron exit)
    // Corresponds to Go's <-ctx.Done()
    while !cancelled.load(Ordering::SeqCst) {
        thread::sleep(Duration::from_millis(100));
    }

    // Graceful shutdown: notify Python API first
    info!("Notifying Python API to shutdown...");
    if let Err(e) = notify_shutdown(args.port) {
        warn!("Failed to notify Python API shutdown: {}", e);
    }

    // Wait for graceful shutdown (increased from 500ms to 2s to allow subprocess cleanup)
    thread::sleep(Duration::from_secs(2));

    // Shutdown API server: SIGTERM first for graceful shutdown, then SIGKILL as fallback
    info!("Stopping Python API server...");
    #[cfg(unix)]
    {
        let pid = api_server_child.id() as i32;
        let send_sigterm = || {
            use nix::sys::signal::{self, Signal};
            use nix::unistd::Pid;
            signal::kill(Pid::from_raw(pid), Signal::SIGTERM)
        };

        if let Err(e) = send_sigterm() {
            warn!("Failed to send SIGTERM to API server, trying SIGKILL: {}", e);
            if let Err(e) = api_server_child.kill() {
                warn!("Failed to kill API server: {}", e);
            }
        } else {
            // Wait up to 5 seconds for graceful exit
            let grace_done = Arc::new(AtomicBool::new(false));
            let grace_done_clone = grace_done.clone();
            let wait_handle = thread::spawn(move || {
                let _ = api_server_child.wait();
                grace_done_clone.store(true, Ordering::SeqCst);
            });

            let start = std::time::Instant::now();
            loop {
                if grace_done.load(Ordering::SeqCst) {
                    info!("API server exited gracefully after SIGTERM");
                    break;
                }
                if start.elapsed() >= Duration::from_secs(5) {
                    warn!("API server did not exit in 5s, sending SIGKILL");
                    // We can't kill via the Child handle because it was moved into the thread.
                    // Use nix kill as fallback.
                    use nix::sys::signal::{self, Signal};
                    use nix::unistd::Pid;
                    let _ = signal::kill(Pid::from_raw(pid), Signal::SIGKILL);
                    break;
                }
                thread::sleep(Duration::from_millis(100));
            }
            let _ = wait_handle.join();
        }
    }
    #[cfg(windows)]
    {
        // On Windows, just kill the process (no SIGTERM equivalent)
        if let Err(e) = api_server_child.kill() {
            warn!("Failed to kill API server: {}", e);
        } else {
            // Wait up to 5 seconds for exit
            let grace_done = Arc::new(AtomicBool::new(false));
            let grace_done_clone = grace_done.clone();
            let wait_handle = thread::spawn(move || {
                let _ = api_server_child.wait();
                grace_done_clone.store(true, Ordering::SeqCst);
            });

            let start = std::time::Instant::now();
            loop {
                if grace_done.load(Ordering::SeqCst) {
                    info!("API server exited gracefully after kill");
                    break;
                }
                if start.elapsed() >= Duration::from_secs(5) {
                    warn!("API server did not exit in 5s");
                    break;
                }
                thread::sleep(Duration::from_millis(100));
            }
            let _ = wait_handle.join();
        }
    }

    info!("Niu launcher shutdown complete");
}
