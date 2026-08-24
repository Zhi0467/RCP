use std::{
    io::Write,
    path::{Path, PathBuf},
    time::Duration,
};

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

const ARTIFACT_AVAILABILITY_TIMEOUT: Duration = Duration::from_secs(5);
const REPOSITORY_PREVIEW_AVAILABILITY_TIMEOUT: Duration = Duration::from_secs(35);

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

#[derive(Debug, PartialEq, Serialize)]
pub struct FolderSelectionResult {
    selected: bool,
    path: Option<String>,
}

#[derive(Serialize)]
pub struct QuitResult {
    quitting: bool,
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
pub fn choose_repository_folder(app: AppHandle) -> Result<FolderSelectionResult, String> {
    let selected = app
        .dialog()
        .file()
        .set_title("Choose repository folder")
        .blocking_pick_folder()
        .map(|folder| {
            folder
                .into_path()
                .map_err(|error| format!("selected repository is not a local folder: {error}"))
        })
        .transpose()?;
    folder_selection_result(selected)
}

fn folder_selection_result(path: Option<PathBuf>) -> Result<FolderSelectionResult, String> {
    let Some(path) = path else {
        return Ok(FolderSelectionResult {
            selected: false,
            path: None,
        });
    };
    if !path.is_absolute() {
        return Err("selected repository folder is not an absolute path".into());
    }
    let path = path
        .to_str()
        .ok_or_else(|| "selected repository folder path is not valid UTF-8".to_string())?;
    Ok(FolderSelectionResult {
        selected: true,
        path: Some(path.to_string()),
    })
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
    ensure_available(&url, "artifact", ARTIFACT_AVAILABILITY_TIMEOUT).await?;
    backend::reverify_identity(&state, &status).await?;
    windows::open_preview(&app, url, status.base_url)?;
    Ok(OpenResult { opened: true })
}

#[tauri::command]
pub async fn open_episode_report_preview(
    app: AppHandle,
    state: State<'_, BackendState>,
    project_id: String,
    episode_id: String,
) -> Result<OpenResult, String> {
    let status = state.status()?;
    let url = episode_report_preview_url(&status.base_url, &project_id, &episode_id)?;
    if !navigation::is_loopback_rcp_url(&url, &status.base_url, false) {
        return Err("episode report preview URL is outside the RCP backend".into());
    }
    backend::reverify_identity(&state, &status).await?;
    windows::open_preview(&app, url, status.base_url)?;
    Ok(OpenResult { opened: true })
}

#[tauri::command]
pub async fn open_repository_file_preview(
    app: AppHandle,
    state: State<'_, BackendState>,
    project_id: String,
    path: String,
    line: Option<u64>,
) -> Result<OpenResult, String> {
    let status = state.status()?;
    let url = repository_file_preview_url(&status.base_url, &project_id, &path, line)?;
    ensure_available(
        &url,
        "repository file",
        REPOSITORY_PREVIEW_AVAILABILITY_TIMEOUT,
    )
    .await?;
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
pub fn request_quit(app: AppHandle) -> Result<QuitResult, String> {
    let quitting = crate::request_app_quit(app);
    Ok(QuitResult { quitting })
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

fn episode_report_preview_url(
    base_url: &str,
    project_id: &str,
    episode_id: &str,
) -> Result<Url, String> {
    let mut url = Url::parse(base_url).map_err(|error| format!("invalid backend URL: {error}"))?;
    url.path_segments_mut()
        .map_err(|_| "backend URL cannot contain path segments".to_string())?
        .extend([
            "api", "projects", project_id, "episodes", episode_id, "report", "preview",
        ]);
    Ok(url)
}

fn repository_file_preview_url(
    base_url: &str,
    project_id: &str,
    path: &str,
    line: Option<u64>,
) -> Result<Url, String> {
    validate_repository_path(path)?;
    if line == Some(0) {
        return Err("repository file line must be a positive integer".into());
    }

    let mut url = Url::parse(base_url).map_err(|error| format!("invalid backend URL: {error}"))?;
    url.path_segments_mut()
        .map_err(|_| "backend URL cannot contain path segments".to_string())?
        .extend([
            "api",
            "projects",
            project_id,
            "repositories",
            "files",
            "preview",
        ]);
    {
        let mut query = url.query_pairs_mut();
        query.append_pair("path", path);
        if let Some(line) = line {
            query.append_pair("line", &line.to_string());
        }
    }
    Ok(url)
}

fn validate_repository_path(path: &str) -> Result<(), String> {
    if !path.starts_with('/')
        || path.contains('\\')
        || path.contains('\0')
        || path
            .split('/')
            .skip(1)
            .any(|segment| segment.is_empty() || matches!(segment, "." | ".."))
    {
        return Err(
            "repository file path must be an absolute POSIX path without empty or dot segments"
                .into(),
        );
    }
    Ok(())
}

async fn ensure_available(url: &Url, description: &str, timeout: Duration) -> Result<(), String> {
    reqwest::Client::builder()
        .timeout(timeout)
        .build()
        .map_err(|error| error.to_string())?
        .head(url.clone())
        .send()
        .await
        .map_err(|error| format!("{description} is unavailable: {error}"))?
        .error_for_status()
        .map_err(|error| format!("{description} is unavailable: {error}"))?;
    Ok(())
}

fn safe_filename(suggested: &str) -> &str {
    Path::new(suggested)
        .file_name()
        .and_then(|value| value.to_str())
        .filter(|value| !value.is_empty())
        .unwrap_or("artifact")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn folder_selection_result_preserves_cancel_and_path() {
        assert_eq!(
            folder_selection_result(None).unwrap(),
            FolderSelectionResult {
                selected: false,
                path: None,
            }
        );
        assert_eq!(
            folder_selection_result(Some(PathBuf::from("/Users/example/research project")))
                .unwrap(),
            FolderSelectionResult {
                selected: true,
                path: Some("/Users/example/research project".into()),
            }
        );
        assert!(folder_selection_result(Some(PathBuf::from("relative/repository"))).is_err());
    }

    #[test]
    fn repository_preview_url_encodes_identifiers_path_and_optional_line() {
        let url = repository_file_preview_url(
            "http://127.0.0.1:8421",
            "project id",
            "/Users/example/origin repo/src/a file.rs",
            Some(27),
        )
        .unwrap();

        assert_eq!(
            url.as_str(),
            "http://127.0.0.1:8421/api/projects/project%20id/repositories/files/preview?path=%2FUsers%2Fexample%2Forigin+repo%2Fsrc%2Fa+file.rs&line=27"
        );
    }

    #[test]
    fn episode_report_preview_url_is_same_origin_and_encodes_identifiers() {
        let base = "http://127.0.0.1:8421";
        let url = episode_report_preview_url(base, "project id", "episode/id").unwrap();

        assert_eq!(
            url.as_str(),
            "http://127.0.0.1:8421/api/projects/project%20id/episodes/episode%2Fid/report/preview"
        );
        assert!(navigation::is_loopback_rcp_url(&url, base, false));
    }

    #[test]
    fn repository_preview_url_omits_absent_line() {
        let url = repository_file_preview_url(
            "http://127.0.0.1:8421",
            "project",
            "/Users/example/repo/README.md",
            None,
        )
        .unwrap();

        assert_eq!(
            url.query(),
            Some("path=%2FUsers%2Fexample%2Frepo%2FREADME.md")
        );
    }

    #[test]
    fn repository_preview_rejects_unsafe_paths_and_zero_line() {
        for path in [
            "",
            "relative/path",
            "src\\main.rs",
            "/Users/example/repo/bad\0name",
            "/",
            "/Users/example/repo/./main.rs",
            "/Users/example/repo/../main.rs",
            "/Users/example/repo//main.rs",
            "/Users/example/repo/",
        ] {
            assert!(
                repository_file_preview_url("http://127.0.0.1:8421", "project", path, None,)
                    .is_err(),
                "accepted unsafe path {path:?}"
            );
        }
        assert!(repository_file_preview_url(
            "http://127.0.0.1:8421",
            "project",
            "/Users/example/repo/src/main.rs",
            Some(0),
        )
        .is_err());
    }
}
