use std::{
    env,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};

use tauri::AppHandle;
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
use tauri_plugin_shell::{
    process::{CommandEvent, TerminatedPayload},
    ShellExt,
};
use tokio::{sync::Notify, time};

use crate::lifecycle::{DesktopStatus, Health, LaunchOutcome};

const BACKEND_HOST: &str = "127.0.0.1";
const BACKEND_PORT: &str = "8421";
const HEALTH_READY_TIMEOUT: Duration = Duration::from_secs(12);

#[derive(Clone, Default)]
pub struct BackendState {
    inner: Arc<Mutex<BackendRuntime>>,
    connect_lock: Arc<tokio::sync::Mutex<()>>,
}

#[derive(Default)]
struct BackendRuntime {
    status: Option<DesktopStatus>,
    process: Option<BackendProcess>,
    startup_error: Option<String>,
}

impl BackendState {
    pub fn set_ready(&self, status: DesktopStatus, process: BackendProcess) {
        let mut inner = self.inner.lock().expect("backend state poisoned");
        inner.status = Some(status);
        inner.process = Some(process);
        inner.startup_error = None;
    }

    pub fn set_error(&self, message: String) {
        self.inner
            .lock()
            .expect("backend state poisoned")
            .startup_error = Some(message);
    }

    pub fn status(&self) -> Result<DesktopStatus, String> {
        let inner = self
            .inner
            .lock()
            .map_err(|_| "backend state is unavailable")?;
        inner.status.clone().ok_or_else(|| {
            inner
                .startup_error
                .clone()
                .unwrap_or_else(|| "RCP is still starting".into())
        })
    }

    pub fn process(&self) -> Option<BackendProcess> {
        self.inner.lock().ok()?.process.clone()
    }

    pub fn update_health(&self, health: &Health) {
        if let Ok(mut inner) = self.inner.lock() {
            if let Some(status) = inner.status.as_mut() {
                if health.instance_id == status.instance_id {
                    status.active_agent_tasks = health.active_agent_tasks;
                    status.owner_kind = health.owner_kind.clone();
                }
            }
        }
    }
}

#[derive(Clone)]
pub struct BackendProcess {
    pid: u32,
    exit: Arc<ProcessExit>,
}

#[derive(Default)]
struct ProcessExit {
    result: Mutex<Option<TerminatedPayload>>,
    changed: Notify,
}

impl BackendProcess {
    pub fn pid(&self) -> u32 {
        self.pid
    }

    pub fn has_exited(&self) -> bool {
        self.exit.result.lock().is_ok_and(|result| result.is_some())
    }

    pub async fn wait(&self, timeout: Duration) -> bool {
        let deadline = time::Instant::now() + timeout;
        loop {
            if self.has_exited() {
                return true;
            }
            let changed = self.exit.changed.notified();
            if self.has_exited() {
                return true;
            }
            if time::timeout_at(deadline, changed).await.is_err() {
                return self.has_exited();
            }
        }
    }
}

pub struct StartedBackend {
    pub status: DesktopStatus,
    pub process: BackendProcess,
}

pub const CONNECT_CANCELLED: &str = "the existing RCP backend was left running";

pub async fn connect(
    app: &AppHandle,
    state: &BackendState,
    cancel_label: &str,
) -> Result<DesktopStatus, String> {
    let _guard = state.connect_lock.lock().await;
    if let Ok(mut status) = state.status() {
        if let Ok(current) = health(&status).await {
            if current.instance_id == status.instance_id
                && current.data_dir_id == status.data_dir_id
                && current.version == status.version
            {
                state.update_health(&current);
                status.active_agent_tasks = current.active_agent_tasks;
                status.owner_kind = current.owner_kind;
                return Ok(status);
            }
        }
    }
    let started = match start(app, false).await {
        Ok(started) => started,
        Err(error) => {
            let refusal = serde_json::from_str::<LaunchOutcome>(&error).ok();
            let Some(refusal) = refusal else {
                return Err(error);
            };
            let reason = refusal
                .reason
                .unwrap_or_else(|| "the current backend cannot be reused".into());
            let replace = app
                .dialog()
                .message(format!(
                    "RCP could not use the current backend.\n\n{reason}"
                ))
                .title("RCP backend needs attention")
                .buttons(MessageDialogButtons::OkCancelCustom(
                    "Replace gracefully".into(),
                    cancel_label.into(),
                ))
                .blocking_show();
            if !replace {
                return Err(CONNECT_CANCELLED.into());
            }
            start(app, true).await?
        }
    };
    let status = started.status.clone();
    state.set_ready(started.status, started.process);
    Ok(status)
}

pub async fn start(app: &AppHandle, force: bool) -> Result<StartedBackend, String> {
    let (mut events, child) = backend_command(app, force)?
        .spawn()
        .map_err(|error| format!("could not start the RCP backend process: {error}"))?;
    let process = BackendProcess {
        pid: child.pid(),
        exit: Arc::new(ProcessExit::default()),
    };
    drop(child);

    let (startup_tx, mut startup_rx) = tokio::sync::mpsc::unbounded_channel();
    let exit = process.exit.clone();
    tauri::async_runtime::spawn(async move {
        while let Some(event) = events.recv().await {
            if let CommandEvent::Terminated(payload) = event {
                *exit.result.lock().expect("process exit state poisoned") = Some(payload);
                exit.changed.notify_waiters();
                break;
            }
            let _ = startup_tx.send(event);
        }
    });

    let mut stderr = Vec::new();
    let mut stdout_pending = Vec::new();
    let outcome = loop {
        let event = match time::timeout(HEALTH_READY_TIMEOUT, startup_rx.recv()).await {
            Ok(Some(event)) => event,
            Ok(None) => {
                return Err(launch_error(
                    &stderr,
                    "backend ended before reporting launch status",
                ));
            }
            Err(_) => {
                let error = "timed out waiting for the backend launch result".to_string();
                let cleanup = stop_process(&process, process.pid()).await;
                return Err(match cleanup {
                    Ok(false) => error,
                    Ok(true) => format!(
                        "{error}; the unidentified backend launcher required forced termination"
                    ),
                    Err(cleanup_error) => {
                        format!("{error}; its launcher could not be cleaned up: {cleanup_error}")
                    }
                });
            }
        };
        match event {
            CommandEvent::Stdout(bytes) => {
                if let Some(outcome) = parse_launch_stdout(&mut stdout_pending, &mut stderr, &bytes)
                {
                    break outcome;
                }
            }
            CommandEvent::Stderr(bytes) => stderr.extend_from_slice(&bytes),
            CommandEvent::Error(message) => stderr.extend_from_slice(message.as_bytes()),
            _ => {}
        }
    };

    if outcome.is_refusal() {
        return Err(serde_json::to_string(&outcome).unwrap_or_else(|_| {
            outcome
                .reason
                .clone()
                .unwrap_or_else(|| "backend refused to start".into())
        }));
    }

    let health = match wait_for_health(&outcome).await {
        Ok(health) => health,
        Err(error) => {
            if outcome.owned {
                let cleanup = stop_process(&process, process.pid()).await;
                return Err(match cleanup {
                    Ok(false) => error,
                    Ok(true) => {
                        format!("{error}; the unready owned backend required forced termination")
                    }
                    Err(cleanup_error) => {
                        format!(
                            "{error}; its owned process could not be cleaned up: {cleanup_error}"
                        )
                    }
                });
            }
            return Err(error);
        }
    };
    let status = DesktopStatus::from_ready(&outcome, &health)?;
    Ok(StartedBackend { status, process })
}

fn parse_launch_stdout(
    pending: &mut Vec<u8>,
    diagnostics: &mut Vec<u8>,
    bytes: &[u8],
) -> Option<LaunchOutcome> {
    pending.extend_from_slice(bytes);
    while let Some(newline) = pending.iter().position(|byte| *byte == b'\n') {
        let line = pending.drain(..=newline).collect::<Vec<_>>();
        let text = String::from_utf8_lossy(&line);
        let text = text.trim();
        if text.is_empty() {
            continue;
        }
        if let Ok(outcome) = LaunchOutcome::parse(text) {
            return Some(outcome);
        }
        diagnostics.extend_from_slice(b"[stdout] ");
        diagnostics.extend_from_slice(text.as_bytes());
        diagnostics.push(b'\n');
    }
    None
}

fn backend_command(
    app: &AppHandle,
    force: bool,
) -> Result<tauri_plugin_shell::process::Command, String> {
    let command = if cfg!(debug_assertions) {
        let bundled = dev_bundle_settings()?;
        let (checkout, uv) = match bundled {
            Some(settings) => (
                canonical_directory(&settings.checkout, "RCPDevCheckout in Info.plist")?,
                canonical_file(&settings.uv, "RCPDevUvExecutable in Info.plist")?,
            ),
            None => (dev_checkout()?, dev_uv()?),
        };
        app.shell()
            .command(uv)
            .current_dir(checkout)
            .args(["run", "rcp", "serve"])
    } else {
        app.shell()
            .sidecar("rcp-backend")
            .map_err(|error| format!("packaged backend is unavailable: {error}"))?
            .args(["serve"])
    };

    let mut args = vec![
        "--machine-readable",
        "--owner",
        "desktop",
        "--web-assets",
        if cfg!(debug_assertions) {
            "source"
        } else {
            "prebuilt"
        },
        "--host",
        BACKEND_HOST,
        "--port",
        BACKEND_PORT,
    ];
    args.push(if force { "--force" } else { "--reuse-existing" });
    Ok(command.args(args).env("PATH", repaired_path()?))
}

fn repaired_path() -> Result<std::ffi::OsString, String> {
    let mut entries: Vec<PathBuf> = env::var_os("PATH")
        .map(|value| env::split_paths(&value).collect())
        .unwrap_or_default();
    if let Some(home) = env::var_os("HOME") {
        entries.push(Path::new(&home).join(".local/bin"));
        entries.push(Path::new(&home).join(".npm-global/bin"));
    }
    entries.extend(
        [
            "/opt/homebrew/bin",
            "/opt/homebrew/sbin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
        .into_iter()
        .map(PathBuf::from),
    );
    let mut deduplicated = Vec::with_capacity(entries.len());
    for entry in entries {
        if !entry.as_os_str().is_empty() && !deduplicated.contains(&entry) {
            deduplicated.push(entry);
        }
    }
    env::join_paths(deduplicated).map_err(|error| format!("cannot construct backend PATH: {error}"))
}

struct DevBundleSettings {
    checkout: PathBuf,
    uv: PathBuf,
}

/// A bundled `RCP Dev.app` serves its frontend from the backend it launches, the
/// same way the release app does; only `tauri dev` has a Vite server to load.
pub fn is_bundled_dev_app() -> bool {
    matches!(dev_bundle_settings(), Ok(Some(_)))
}

fn dev_bundle_settings() -> Result<Option<DevBundleSettings>, String> {
    let executable = env::current_exe()
        .map_err(|error| format!("cannot locate the RCP Dev executable: {error}"))?;
    let Some(contents) = executable.parent().and_then(Path::parent) else {
        return Ok(None);
    };
    let info_plist = contents.join("Info.plist");
    if !info_plist.is_file() {
        return Ok(None);
    }
    let value = plist::Value::from_file(&info_plist)
        .map_err(|error| format!("cannot read RCP Dev Info.plist: {error}"))?;
    let dictionary = value
        .as_dictionary()
        .ok_or_else(|| "RCP Dev Info.plist is not a dictionary".to_string())?;
    let checkout = dictionary
        .get("RCPDevCheckout")
        .and_then(plist::Value::as_string);
    let uv = dictionary
        .get("RCPDevUvExecutable")
        .and_then(plist::Value::as_string);
    match (checkout, uv) {
        (Some(checkout), Some(uv)) => Ok(Some(DevBundleSettings {
            checkout: PathBuf::from(checkout),
            uv: PathBuf::from(uv),
        })),
        (None, None) => Ok(None),
        _ => {
            Err("RCP Dev Info.plist must record both RCPDevCheckout and RCPDevUvExecutable".into())
        }
    }
}

fn dev_checkout() -> Result<PathBuf, String> {
    if let Some(path) = env::var_os("RCP_DEV_CHECKOUT") {
        return canonical_directory(Path::new(&path), "RCP_DEV_CHECKOUT");
    }
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    canonical_directory(
        manifest
            .parent()
            .and_then(Path::parent)
            .ok_or_else(|| "cannot resolve the RCP checkout from the Tauri manifest".to_string())?,
        "compiled RCP checkout",
    )
}

fn dev_uv() -> Result<PathBuf, String> {
    if let Some(path) = env::var_os("RCP_DEV_UV") {
        return canonical_file(Path::new(&path), "RCP_DEV_UV");
    }
    if let Some(path) = find_on_path("uv") {
        return canonical_file(&path, "uv on PATH");
    }
    if let Some(home) = env::var_os("HOME") {
        let candidate = Path::new(&home).join(".local/bin/uv");
        if candidate.is_file() {
            return canonical_file(&candidate, "uv in ~/.local/bin");
        }
    }
    Err("RCP Dev.app cannot find uv; set RCP_DEV_UV to its absolute path".into())
}

fn find_on_path(name: &str) -> Option<PathBuf> {
    env::var_os("PATH")?
        .to_string_lossy()
        .split(':')
        .map(Path::new)
        .map(|directory| directory.join(name))
        .find(|candidate| candidate.is_file())
}

fn canonical_file(path: &Path, label: &str) -> Result<PathBuf, String> {
    let resolved = path
        .canonicalize()
        .map_err(|error| format!("cannot resolve {label}: {error}"))?;
    if !resolved.is_file() {
        return Err(format!("{label} is not a file: {}", resolved.display()));
    }
    Ok(resolved)
}

fn canonical_directory(path: &Path, label: &str) -> Result<PathBuf, String> {
    let resolved = path
        .canonicalize()
        .map_err(|error| format!("cannot resolve {label}: {error}"))?;
    if !resolved.is_dir() {
        return Err(format!(
            "{label} is not a directory: {}",
            resolved.display()
        ));
    }
    Ok(resolved)
}

async fn wait_for_health(outcome: &LaunchOutcome) -> Result<Health, String> {
    let expected = outcome
        .instance_id
        .as_deref()
        .ok_or_else(|| "backend launch omitted its instance id".to_string())?;
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .map_err(|error| format!("cannot build health client: {error}"))?;
    let deadline = time::Instant::now() + HEALTH_READY_TIMEOUT;
    let mut last_error = "backend has not answered yet".to_string();
    while time::Instant::now() < deadline {
        match client
            .get(format!("{}/api/health", outcome.base_url))
            .send()
            .await
        {
            Ok(response) if response.status().is_success() => match response.json::<Health>().await
            {
                Ok(health) if health.instance_id == expected => return Ok(health),
                Ok(_) => last_error = "another process answered at the backend address".into(),
                Err(error) => last_error = format!("health response was invalid: {error}"),
            },
            Ok(response) => last_error = format!("health returned HTTP {}", response.status()),
            Err(error) => last_error = error.to_string(),
        }
        time::sleep(Duration::from_millis(120)).await;
    }
    Err(format!("backend did not become ready: {last_error}"))
}

pub async fn health(status: &DesktopStatus) -> Result<Health, String> {
    reqwest::Client::builder()
        .timeout(Duration::from_secs(3))
        .build()
        .map_err(|error| error.to_string())?
        .get(format!("{}/api/health", status.base_url))
        .send()
        .await
        .map_err(|error| format!("backend is unreachable: {error}"))?
        .error_for_status()
        .map_err(|error| format!("backend health failed: {error}"))?
        .json::<Health>()
        .await
        .map_err(|error| format!("backend health was invalid: {error}"))
}

#[derive(Clone, Debug, serde::Serialize)]
pub struct ShutdownResult {
    pub stopped_owned_backend: bool,
    pub forced: bool,
    pub reason: Option<String>,
}

pub async fn graceful_stop(state: &BackendState) -> Result<ShutdownResult, String> {
    let status = state.status()?;
    let current = match health(&status).await {
        Ok(value) => value,
        Err(error) => {
            return Ok(ShutdownResult {
                stopped_owned_backend: false,
                forced: false,
                reason: Some(format!(
                    "backend identity could not be re-verified, so it was left running: {error}"
                )),
            })
        }
    };
    if !status.still_owns(&current) {
        return Ok(ShutdownResult {
            stopped_owned_backend: false,
            forced: false,
            reason: Some(
                "the backend is reused or its identity changed; it was left running".into(),
            ),
        });
    }
    let process = state
        .process()
        .ok_or_else(|| "owned backend process information is unavailable".to_string())?;
    let forced = stop_process(&process, current.pid).await?;
    if !forced {
        return Ok(ShutdownResult {
            stopped_owned_backend: true,
            forced: false,
            reason: None,
        });
    }

    Ok(ShutdownResult {
        stopped_owned_backend: true,
        forced: true,
        reason: Some(
            "graceful shutdown timed out; the owned backend was forcibly terminated".into(),
        ),
    })
}

async fn stop_process(process: &BackendProcess, signal_pid: u32) -> Result<bool, String> {
    if process.has_exited() {
        return Ok(false);
    }
    send_signal(signal_pid, libc::SIGTERM)?;
    if process.wait(Duration::from_secs(20)).await {
        return Ok(false);
    }
    send_signal(signal_pid, libc::SIGKILL)?;
    let _ = process.wait(Duration::from_secs(2)).await;
    Ok(true)
}

fn send_signal(pid: u32, signal: libc::c_int) -> Result<(), String> {
    let result = unsafe { libc::kill(pid as libc::pid_t, signal) };
    if result == 0 {
        Ok(())
    } else {
        let error = std::io::Error::last_os_error();
        if error.kind() == std::io::ErrorKind::NotFound || error.raw_os_error() == Some(libc::ESRCH)
        {
            Ok(())
        } else {
            Err(format!("could not signal backend process {pid}: {error}"))
        }
    }
}

fn launch_error(stderr: &[u8], fallback: &str) -> String {
    let detail = String::from_utf8_lossy(stderr).trim().to_string();
    if detail.is_empty() {
        fallback.to_string()
    } else {
        format!("{fallback}: {detail}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn launch_stdout_skips_build_output_before_the_machine_result() {
        let mut pending = Vec::new();
        let mut diagnostics = Vec::new();
        let first = b"Building frontend...\n{\"outcome\":\"owned\",\"base_url\":\"http://127.0.0.1:8421\",\"instance_id\":\"instance-a\",\"version\":\"0.3.0\",";
        assert!(parse_launch_stdout(&mut pending, &mut diagnostics, first).is_none());

        let outcome = parse_launch_stdout(
            &mut pending,
            &mut diagnostics,
            b"\"owned\":true,\"reason\":null}\n",
        )
        .expect("machine result should be parsed after the build output");

        assert_eq!(outcome.outcome, "owned");
        assert_eq!(outcome.instance_id.as_deref(), Some("instance-a"));
        assert_eq!(diagnostics, b"[stdout] Building frontend...\n");
    }
}
