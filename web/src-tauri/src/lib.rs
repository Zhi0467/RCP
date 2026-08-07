mod backend;
mod commands;
mod lifecycle;
mod navigation;
mod updates;
mod windows;

use std::time::Duration;

use backend::BackendState;
use tauri::{
    menu::{Menu, MenuItem, Submenu},
    Emitter, Manager, RunEvent, WindowEvent,
};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};

const QUIT_MENU_ID: &str = "rcp-quit";

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let backend_state = BackendState::default();
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            verify_then_prepare_show(app.clone(), "second-launch");
        }))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(
            tauri_plugin_opener::Builder::new()
                .open_js_links_on_click(false)
                .build(),
        )
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(backend_state)
        .menu(|app| {
            let quit = MenuItem::with_id(app, QUIT_MENU_ID, "Quit RCP", true, Some("CmdOrCtrl+Q"))?;
            let application = Submenu::with_items(app, "RCP", true, &[&quit])?;
            Menu::with_items(app, &[&application])
        })
        .on_menu_event(|app, event| {
            if event.id() == QUIT_MENU_ID {
                quit_from_menu(app.clone());
            }
        })
        .invoke_handler(tauri::generate_handler![
            commands::desktop_status,
            commands::desktop_reconnect_backend,
            commands::desktop_show_ready,
            commands::open_artifact_preview,
            commands::download_artifact,
            commands::open_external,
            commands::request_quit,
            commands::check_for_update,
            commands::apply_update,
        ])
        .setup(|app| {
            windows::create_main(app.handle())?;
            start_backend(app.handle().clone());
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() == "main" {
                if let WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building RCP desktop app");

    app.run(|app, event| {
        #[cfg(target_os = "macos")]
        if matches!(event, RunEvent::Reopen { .. }) {
            verify_then_prepare_show(app.clone(), "dock-reopen");
        }
    });
}

fn start_backend(app: tauri::AppHandle) {
    tauri::async_runtime::spawn(async move {
        let state = app.state::<BackendState>().inner().clone();
        eprintln!("[rcp] connecting to a backend");
        match backend::connect(&app, &state, "Quit").await {
            Ok(status) => {
                eprintln!(
                    "[rcp] backend ready at {} (owned={})",
                    status.base_url, status.owned
                );
                finish_startup(&app, status)
            }
            Err(error) => {
                eprintln!("[rcp] backend connection failed: {error}");
                if error == backend::CONNECT_CANCELLED {
                    app.exit(1);
                    return;
                }
                app.state::<BackendState>().set_error(error.clone());
                app.dialog()
                    .message(format!("RCP could not start.\n\n{error}"))
                    .title("RCP could not start")
                    .buttons(MessageDialogButtons::Ok)
                    .blocking_show();
                app.exit(1);
            }
        }
    });
}

fn finish_startup(app: &tauri::AppHandle, status: lifecycle::DesktopStatus) {
    if let Some(target) = windows::navigation_target(&status.base_url) {
        if let Some(window) = app.get_webview_window("main") {
            eprintln!("[rcp] main window navigating to {target}");
            if let Err(error) = window.navigate(target) {
                eprintln!("[rcp] the main window could not navigate: {error}");
            }
        }
    }
    let _ = windows::prepare_show(app, &status, "startup");
    windows::show_when_handshake_does_not_arrive(app);
}

fn verify_then_prepare_show(app: tauri::AppHandle, reason: &'static str) {
    tauri::async_runtime::spawn(async move {
        let state = app.state::<BackendState>().inner().clone();
        let status = match state.status() {
            Ok(status) => status,
            Err(error) => {
                let _ = app.emit_to(
                    "main",
                    "rcp://backend-mismatch",
                    serde_json::json!({"message": error}),
                );
                return;
            }
        };
        match backend::health(&status).await {
            Ok(health)
                if health.instance_id == status.instance_id
                    && health.data_dir_id == status.data_dir_id =>
            {
                state.update_health(&health);
                let _ = windows::prepare_show(&app, &status, reason);
            }
            Ok(_) => {
                let _ = app.emit_to(
                    "main",
                    "rcp://backend-mismatch",
                    serde_json::json!({"message": "the backend identity changed"}),
                );
            }
            Err(error) => {
                let _ = app.emit_to(
                    "main",
                    "rcp://backend-mismatch",
                    serde_json::json!({"message": error}),
                );
            }
        }
    });
}

fn quit_from_menu(app: tauri::AppHandle) {
    tauri::async_runtime::spawn(async move {
        let state = app.state::<BackendState>().inner().clone();
        let result = backend::graceful_stop(&state).await;
        if let Ok(shutdown) = &result {
            if shutdown.forced {
                app.dialog()
                    .message(
                        shutdown.reason.clone().unwrap_or_else(|| {
                            "The owned backend required forced termination.".into()
                        }),
                    )
                    .title("RCP shutdown")
                    .buttons(MessageDialogButtons::Ok)
                    .blocking_show();
            }
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
        app.exit(if result.is_ok() { 0 } else { 1 });
    });
}
