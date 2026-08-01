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

pub fn create_main(app: &AppHandle) -> Result<(), String> {
    let initial_url = initial_frontend_url()?;
    eprintln!("[rcp] main window loading {initial_url}");
    let _ = INITIAL_URL.set(initial_url.clone());
    let app_for_navigation = app.clone();
    let app_for_popup = app.clone();
    WebviewWindowBuilder::new(app, "main", WebviewUrl::External(initial_url))
        .title("RCP")
        .inner_size(1320.0, 860.0)
        .min_inner_size(880.0, 600.0)
        .visible(false)
        .zoom_hotkeys_enabled(false)
        .on_navigation(move |url| {
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

fn initial_frontend_url() -> Result<Url, String> {
    let default = if uses_vite_dev_server() {
        "http://127.0.0.1:5173"
    } else {
        "http://127.0.0.1:8421"
    };
    let raw = std::env::var("RCP_DESKTOP_FRONTEND_URL").unwrap_or_else(|_| default.into());
    let url =
        Url::parse(&raw).map_err(|error| format!("invalid RCP_DESKTOP_FRONTEND_URL: {error}"))?;
    if navigation::is_loopback_rcp_url(&url, "http://127.0.0.1:8421", cfg!(debug_assertions)) {
        Ok(url)
    } else {
        Err("RCP_DESKTOP_FRONTEND_URL must be an approved loopback RCP origin".into())
    }
}
