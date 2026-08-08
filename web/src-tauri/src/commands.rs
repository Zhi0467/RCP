use std::{io::Write, path::Path, time::Duration};

use serde::Serialize;
use tauri::{AppHandle, Emitter, State};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;
use url::Url;

use crate::{
    backend::{self, BackendState},
    dictation,
    lifecycle::DesktopStatus,
    navigation, updates, windows,
};

#[derive(Serialize)]
pub struct ShowResult {
    shown: bool,
}

#[derive(Serialize)]
pub struct OpenResult {
    opened: bool,
}

#[derive(Serialize)]
pub struct DownloadResult {
    saved: bool,
    path: Option<String>,
}

#[derive(Serialize)]
pub struct QuitResult {
    quitting: bool,
    #[serde(flatten)]
    shutdown: backend::ShutdownResult,
}

#[derive(Serialize)]
pub struct ApplyUpdateResult {
    started: bool,
}

#[tauri::command]
pub fn desktop_start_dictation(app: AppHandle, session_id: String) -> Result<(), String> {
    dictation::start(&app, &session_id)
}

#[tauri::command]
pub fn desktop_stop_dictation(session_id: String) -> Result<(), String> {
    dictation::stop(&session_id)
}

#[tauri::command]
pub async fn desktop_status(state: State<'_, BackendState>) -> Result<DesktopStatus, String> {
    let mut status = state.status()?;
    if let Ok(health) = backend::health(&status).await {
        if status.matches_health(&health) {
            state.update_health(&health);
            status.active_agent_tasks = health.active_agent_tasks;
            status.owner_kind = health.owner_kind;
        }
    }
    Ok(status)
}

#[tauri::command]
pub async fn desktop_reconnect_backend(
    app: AppHandle,
    state: State<'_, BackendState>,
) -> Result<DesktopStatus, String> {
    backend::connect(&app, &state, "Leave it running").await
}

#[tauri::command]
pub async fn desktop_show_ready(
    app: AppHandle,
    state: State<'_, BackendState>,
) -> Result<ShowResult, String> {
    let status = state.status()?;
    let health = backend::health(&status).await?;
    if !status.matches_health(&health) {
        let message = "backend identity changed before the desktop window was shown";
        app.emit_to(
            "main",
            "rcp://backend-mismatch",
            serde_json::json!({"message": message}),
        )
        .map_err(|error| error.to_string())?;
        return Err(message.into());
    }
    state.update_health(&health);
    windows::show_main(&app)?;
    Ok(ShowResult { shown: true })
}

#[tauri::command]
pub async fn open_artifact_preview(
    app: AppHandle,
    state: State<'_, BackendState>,
    project_id: String,
    task_id: String,
    artifact_id: String,
) -> Result<OpenResult, String> {
    let status = state.status()?;
    let url = artifact_url(
        &status.base_url,
        &project_id,
        &task_id,
        &artifact_id,
        "preview",
    )?;
    ensure_available(&url).await?;
    backend::reverify_identity(&state, &status).await?;
    windows::open_preview(&app, url, status.base_url)?;
    Ok(OpenResult { opened: true })
}

#[tauri::command]
pub async fn download_artifact(
    app: AppHandle,
    state: State<'_, BackendState>,
    project_id: String,
    task_id: String,
    artifact_id: String,
    suggested_name: String,
) -> Result<DownloadResult, String> {
    let status = state.status()?;
    let url = artifact_url(
        &status.base_url,
        &project_id,
        &task_id,
        &artifact_id,
        "download",
    )?;
    let name = safe_filename(&suggested_name);
    let Some(chosen) = app
        .dialog()
        .file()
        .set_title("Save RCP artifact")
        .set_file_name(name)
        .blocking_save_file()
    else {
        return Ok(DownloadResult {
            saved: false,
            path: None,
        });
    };
    let path = chosen
        .into_path()
        .map_err(|error| format!("selected destination is not a local file: {error}"))?;
    backend::reverify_identity(&state, &status).await?;
    let response = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|error| error.to_string())?
        .get(url)
        .send()
        .await
        .map_err(|error| format!("artifact download failed: {error}"))?
        .error_for_status()
        .map_err(|error| format!("artifact download failed: {error}"))?;
    let bytes = response
        .bytes()
        .await
        .map_err(|error| format!("artifact download was interrupted: {error}"))?;
    let parent = path
        .parent()
        .ok_or_else(|| "selected destination has no parent directory".to_string())?;
    let mut temporary = tempfile::Builder::new()
        .prefix(".rcp-download-")
        .tempfile_in(parent)
        .map_err(|error| format!("cannot create a temporary download file: {error}"))?;
    temporary
        .write_all(&bytes)
        .and_then(|()| temporary.as_file().sync_all())
        .map_err(|error| format!("cannot save artifact: {error}"))?;
    temporary
        .persist(&path)
        .map_err(|error| format!("cannot finish artifact download: {}", error.error))?;
    Ok(DownloadResult {
        saved: true,
        path: Some(path.display().to_string()),
    })
}

#[tauri::command]
pub fn open_external(app: AppHandle, url: String) -> Result<OpenResult, String> {
    let url = Url::parse(&url).map_err(|error| format!("invalid reference URL: {error}"))?;
    if !navigation::is_external_reference(&url) {
        return Err("only HTTP or HTTPS references may open externally".into());
    }
    app.opener()
        .open_url(url.as_str(), None::<&str>)
        .map_err(|error| format!("could not open system browser: {error}"))?;
    Ok(OpenResult { opened: true })
}

#[tauri::command]
pub async fn request_quit(
    app: AppHandle,
    state: State<'_, BackendState>,
) -> Result<QuitResult, String> {
    dictation::stop_active();
    let shutdown = backend::graceful_stop(&state).await?;
    if shutdown.forced {
        app.dialog()
            .message(
                shutdown
                    .reason
                    .clone()
                    .unwrap_or_else(|| "The owned backend required forced termination.".into()),
            )
            .title("RCP shutdown")
            .buttons(tauri_plugin_dialog::MessageDialogButtons::Ok)
            .blocking_show();
    }
    let exit_handle = app.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(Duration::from_millis(150)).await;
        exit_handle.exit(0);
    });
    Ok(QuitResult {
        quitting: true,
        shutdown,
    })
}

#[tauri::command]
pub async fn check_for_update(
    app: AppHandle,
    state: State<'_, BackendState>,
) -> Result<updates::UpdateStatus, String> {
    updates::check(&app, &state).await
}

#[tauri::command]
pub async fn apply_update(
    app: AppHandle,
    state: State<'_, BackendState>,
    confirm_active_work: bool,
) -> Result<ApplyUpdateResult, String> {
    updates::apply(&app, &state, confirm_active_work).await?;
    Ok(ApplyUpdateResult { started: true })
}

fn artifact_url(
    base_url: &str,
    project_id: &str,
    task_id: &str,
    artifact_id: &str,
    action: &str,
) -> Result<Url, String> {
    let mut url = Url::parse(base_url).map_err(|error| format!("invalid backend URL: {error}"))?;
    url.path_segments_mut()
        .map_err(|_| "backend URL cannot contain path segments".to_string())?
        .extend([
            "api",
            "projects",
            project_id,
            "tasks",
            task_id,
            "artifacts",
            artifact_id,
            action,
        ]);
    Ok(url)
}

async fn ensure_available(url: &Url) -> Result<(), String> {
    reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .map_err(|error| error.to_string())?
        .head(url.clone())
        .send()
        .await
        .map_err(|error| format!("artifact is unavailable: {error}"))?
        .error_for_status()
        .map_err(|error| format!("artifact is unavailable: {error}"))?;
    Ok(())
}

fn safe_filename(suggested: &str) -> &str {
    Path::new(suggested)
        .file_name()
        .and_then(|value| value.to_str())
        .filter(|value| !value.is_empty())
        .unwrap_or("artifact")
}
