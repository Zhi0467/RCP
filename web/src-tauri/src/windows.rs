use std::{
    sync::{
        atomic::{AtomicU64, Ordering},
        OnceLock,
    },
    time::Duration,
};

use tauri::{
    webview::{NewWindowResponse, WebviewWindowBuilder},
    AppHandle, Emitter, Manager, WebviewUrl,
};
use tauri_plugin_opener::OpenerExt;
use url::Url;

use crate::backend::BackendState;
use crate::{lifecycle::DesktopStatus, navigation};

static PREVIEW_SEQUENCE: AtomicU64 = AtomicU64::new(1);
static INITIAL_URL: OnceLock<Url> = OnceLock::new();

/// How long the hidden window waits for the frontend handshake before showing
/// itself anyway. A window that only ever appears on success turns any failure
/// into an app that silently does not open.
const HANDSHAKE_SHOW_TIMEOUT: Duration = Duration::from_secs(8);

/// The placeholder the main window holds while a cold start waits for its
/// backend. It is never the resting state: `finish_startup` navigates away from
/// it, and the handshake timeout still reveals the window if that never happens.
const BLANK_URL: &str = "about:blank";
const BACKEND_URL: &str = "http://127.0.0.1:8421";
const FRONTEND_URL_VARIABLE: &str = "RCP_DESKTOP_FRONTEND_URL";

#[derive(Debug, PartialEq)]
enum InitialNavigation {
    Eager(Url),
    AfterBackendReady(Url),
}

pub fn create_main(app: &AppHandle) -> Result<(), String> {
    let configured_url = std::env::var(FRONTEND_URL_VARIABLE).ok();
    let initial_navigation = initial_navigation(uses_vite_dev_server(), configured_url.as_deref())?;
    let start_url = match initial_navigation {
        InitialNavigation::Eager(url) => {
            eprintln!("[rcp] main window loading {url}");
            let _ = INITIAL_URL.set(url.clone());
            url
        }
        InitialNavigation::AfterBackendReady(url) => {
            eprintln!("[rcp] main window waiting for the backend at {url}");
            Url::parse(BLANK_URL).map_err(|error| format!("unusable blank page: {error}"))?
        }
    };
    let app_for_navigation = app.clone();
    let app_for_popup = app.clone();
    WebviewWindowBuilder::new(app, "main", WebviewUrl::External(start_url))
        .title("RCP")
        .inner_size(1320.0, 860.0)
        .min_inner_size(880.0, 600.0)
        .visible(false)
        .zoom_hotkeys_enabled(false)
        .on_navigation(move |url| {
            if url.as_str() == BLANK_URL {
                return true;
            }
            let current_base = app_for_navigation
                .state::<BackendState>()
                .status()
                .ok()
                .map(|status| status.base_url);
            if navigation::is_main_window_url(url, current_base.as_deref(), uses_vite_dev_server())
            {
                true
            } else {
                if navigation::is_external_reference(url) {
                    let _ = app_for_navigation
                        .opener()
                        .open_url(url.as_str(), None::<&str>);
                }
                false
            }
        })
        .on_new_window(move |url, _features| {
            if navigation::is_external_reference(&url) {
                let _ = app_for_popup.opener().open_url(url.as_str(), None::<&str>);
            }
            NewWindowResponse::Deny
        })
        .build()
        .map_err(|error| format!("could not create the RCP window: {error}"))?;
    Ok(())
}

pub fn prepare_show(app: &AppHandle, status: &DesktopStatus, reason: &str) -> Result<(), String> {
    app.emit_to(
        "main",
        "rcp://prepare-show",
        serde_json::json!({"reason": reason, "instanceId": status.instance_id}),
    )
    .map_err(|error| format!("could not request a window refresh: {error}"))
}

/// The backend origin the window must move to, or `None` when it is already
/// there — or when `tauri dev` is serving the frontend from Vite.
pub fn navigation_target(base_url: &str) -> Option<Url> {
    if uses_vite_dev_server() {
        return None;
    }
    let target = Url::parse(base_url).ok()?;
    match INITIAL_URL.get() {
        Some(initial) if *initial == target => None,
        _ => Some(target),
    }
}

pub fn show_when_handshake_does_not_arrive(app: &AppHandle) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(HANDSHAKE_SHOW_TIMEOUT).await;
        let Some(window) = app.get_webview_window("main") else {
            return;
        };
        if !window.is_visible().unwrap_or(false) {
            eprintln!("[rcp] the frontend handshake did not arrive; showing the window anyway");
            if let Err(error) = show_main(&app) {
                eprintln!("[rcp] the window could not be shown: {error}");
            }
        }
    });
}

pub fn show_main(app: &AppHandle) -> Result<(), String> {
    eprintln!("[rcp] showing the RCP window");
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "the RCP window is unavailable".to_string())?;
    window.show().map_err(|error| error.to_string())?;
    window.unminimize().map_err(|error| error.to_string())?;
    window.set_focus().map_err(|error| error.to_string())
}

pub fn open_preview(app: &AppHandle, url: Url, base_url: String) -> Result<(), String> {
    if !navigation::is_loopback_rcp_url(&url, &base_url, false) {
        return Err("artifact preview URL is outside the RCP backend".into());
    }
    let label = format!(
        "artifact-preview-{}",
        PREVIEW_SEQUENCE.fetch_add(1, Ordering::Relaxed)
    );
    let app_for_navigation = app.clone();
    let app_for_popup = app.clone();
    WebviewWindowBuilder::new(app, label, WebviewUrl::External(url))
        .title("RCP artifact preview")
        .inner_size(1040.0, 760.0)
        .min_inner_size(520.0, 400.0)
        .on_navigation(move |candidate| {
            if navigation::is_loopback_rcp_url(candidate, &base_url, false) {
                true
            } else {
                if navigation::is_external_reference(candidate) {
                    let _ = app_for_navigation
                        .opener()
                        .open_url(candidate.as_str(), None::<&str>);
                }
                false
            }
        })
        .on_new_window(move |candidate, _features| {
            if navigation::is_external_reference(&candidate) {
                let _ = app_for_popup
                    .opener()
                    .open_url(candidate.as_str(), None::<&str>);
            }
            NewWindowResponse::Deny
        })
        .build()
        .map_err(|error| format!("could not open artifact preview: {error}"))?;
    Ok(())
}

pub fn uses_vite_dev_server() -> bool {
    cfg!(debug_assertions) && !crate::backend::is_bundled_dev_app()
}

/// Resolve the requested frontend without reading process-global state, then
/// decide whether it is safe to navigate before backend readiness. A backend
/// URL is always deferred, including when it was explicitly configured.
fn initial_navigation(
    uses_vite_dev_server: bool,
    configured_url: Option<&str>,
) -> Result<InitialNavigation, String> {
    let default = if uses_vite_dev_server {
        "http://127.0.0.1:5173"
    } else {
        BACKEND_URL
    };
    let raw = configured_url.unwrap_or(default);
    let url =
        Url::parse(raw).map_err(|error| format!("invalid {FRONTEND_URL_VARIABLE}: {error}"))?;
    if !navigation::is_loopback_rcp_url(&url, BACKEND_URL, cfg!(debug_assertions)) {
        return Err("RCP_DESKTOP_FRONTEND_URL must be an approved loopback RCP origin".into());
    }
    let backend = Url::parse(BACKEND_URL).expect("the built-in backend URL must be valid");
    if url.origin() == backend.origin() {
        Ok(InitialNavigation::AfterBackendReady(url))
    } else {
        Ok(InitialNavigation::Eager(url))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn url(raw: &str) -> Url {
        Url::parse(raw).unwrap()
    }

    #[test]
    fn initial_navigation_defers_only_the_backend_origin() {
        assert_eq!(
            initial_navigation(false, None).unwrap(),
            InitialNavigation::AfterBackendReady(url("http://127.0.0.1:8421")),
        );
        assert_eq!(
            initial_navigation(true, None).unwrap(),
            InitialNavigation::Eager(url("http://127.0.0.1:5173")),
        );
        assert_eq!(
            initial_navigation(false, Some("http://127.0.0.1:5173")).unwrap(),
            InitialNavigation::Eager(url("http://127.0.0.1:5173")),
        );
        assert_eq!(
            initial_navigation(true, Some("http://127.0.0.1:8421")).unwrap(),
            InitialNavigation::AfterBackendReady(url("http://127.0.0.1:8421")),
        );
    }
}
