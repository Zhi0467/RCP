//! Native notifications for the spaces this Mac has turned on.
//!
//! The shell pulls each enabled space's outbox, posts every item through
//! `UNUserNotificationCenter` (see `notifications.m`), and acknowledges an item
//! only after macOS accepts its request. A click routes the window to the item.

use std::{
    collections::{HashMap, HashSet},
    ffi::{c_char, CStr, CString},
    path::PathBuf,
    sync::{Arc, Mutex, MutexGuard, OnceLock},
    time::Duration,
};

use reqwest::Method;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{AppHandle, Manager, WebviewWindow};
use tokio::sync::oneshot;
use url::Url;

use crate::{
    backend::BackendState, team_connections::TeamConnectionState, team_session::TeamSessionState,
    windows,
};

const POLL_INTERVAL: Duration = Duration::from_secs(10);
const PERSONAL_REQUEST_TIMEOUT: Duration = Duration::from_secs(10);
const ADAPTER_REPLY_TIMEOUT: Duration = Duration::from_secs(30);
/// More than this many items waiting at launch post as one summary.
const BACKLOG_SUMMARY_LIMIT: usize = 3;
const SETTINGS_FILENAME: &str = "notifications.json";
const TEST_ID: &str = "test";
const AUTHORIZE_ID: &str = "authorize";

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
enum Space {
    Personal,
    Team(String),
}

impl Space {
    /// Prefix of the adapter id, so a click names the space it belongs to.
    fn key(&self) -> String {
        match self {
            Space::Personal => "personal".into(),
            Space::Team(connection_id) => format!("team:{connection_id}"),
        }
    }

    fn from_key(key: &str) -> Option<Self> {
        match key {
            "personal" => Some(Space::Personal),
            _ => key.strip_prefix("team:").map(|id| Space::Team(id.into())),
        }
    }
}

#[derive(Default, Serialize, Deserialize)]
struct Settings {
    personal: bool,
    teams: Vec<String>,
}

#[derive(Deserialize)]
struct Item {
    notification_id: String,
    reason: String,
    project_name: String,
    deep_link: String,
}

#[derive(Deserialize)]
struct Device {
    device_id: String,
}

/// What one accepted adapter request acknowledges.
struct Posting {
    space: Space,
    device_id: String,
    notification_ids: Vec<String>,
}

#[derive(Default)]
struct Inner {
    settings: Settings,
    devices: HashMap<Space, String>,
    launched: HashSet<Space>,
    posting: HashMap<String, Posting>,
    replies: HashMap<String, oneshot::Sender<Result<(), String>>>,
    pending_click: Option<(Space, String)>,
}

#[derive(Clone)]
pub struct NotificationState {
    inner: Arc<Mutex<Inner>>,
    settings_path: PathBuf,
}

static APP: OnceLock<AppHandle> = OnceLock::new();

impl NotificationState {
    pub fn for_app(app: &AppHandle) -> Result<Self, String> {
        let settings_path = app
            .path()
            .app_config_dir()
            .map_err(|error| format!("cannot locate RCP desktop configuration: {error}"))?
            .join(SETTINGS_FILENAME);
        let settings = std::fs::read(&settings_path)
            .ok()
            .and_then(|bytes| serde_json::from_slice(&bytes).ok())
            .unwrap_or_default();
        Ok(Self {
            inner: Arc::new(Mutex::new(Inner {
                settings,
                ..Inner::default()
            })),
            settings_path,
        })
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn enabled(&self, space: &Space) -> bool {
        let inner = self.lock();
        match space {
            Space::Personal => inner.settings.personal,
            Space::Team(id) => inner.settings.teams.contains(id),
        }
    }

    fn set_enabled(&self, space: &Space, enabled: bool) -> Result<(), String> {
        let bytes = {
            let mut inner = self.lock();
            match space {
                Space::Personal => inner.settings.personal = enabled,
                Space::Team(id) => {
                    inner.settings.teams.retain(|item| item != id);
                    if enabled {
                        inner.settings.teams.push(id.clone());
                    }
                }
            }
            if !enabled {
                inner.devices.remove(space);
            }
            serde_json::to_vec(&inner.settings).map_err(|error| error.to_string())?
        };
        if let Some(parent) = self.settings_path.parent() {
            std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
        }
        let temporary = self.settings_path.with_extension("json.tmp");
        std::fs::write(&temporary, bytes).map_err(|error| error.to_string())?;
        std::fs::rename(&temporary, &self.settings_path).map_err(|error| error.to_string())
    }
}

fn reason_text(reason: &str) -> &'static str {
    match reason {
        "proposal" => "A Proposal is waiting for you",
        "decision" => "A Decision is waiting for a choice",
        "blocker" => "A Blocker is open",
        "episode_needs_action" => "An episode needs you",
        "episode_finished" => "An episode finished",
        _ => "Something needs you in RCP",
    }
}

/// Install the notification-center delegate. Call it during setup, before the
/// app finishes launching, so a click that launches the app is delivered.
pub fn install(app: &AppHandle) {
    let _ = APP.set(app.clone());
    platform::install();
}

/// Start the outbox poller for every enabled space.
pub fn start(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        loop {
            poll_all(&app).await;
            tokio::time::sleep(POLL_INTERVAL).await;
        }
    });
}

async fn poll_all(app: &AppHandle) {
    let state = app.state::<NotificationState>().inner().clone();
    let pending_click = state.lock().pending_click.take();
    if let Some((space, link)) = pending_click {
        open(app, &space, &link);
    }
    let spaces: Vec<Space> = {
        let inner = state.lock();
        let mut spaces: Vec<Space> = inner
            .settings
            .teams
            .iter()
            .cloned()
            .map(Space::Team)
            .collect();
        if inner.settings.personal {
            spaces.insert(0, Space::Personal);
        }
        spaces
    };
    for space in spaces {
        if let Err(error) = poll(app, &state, &space).await {
            eprintln!(
                "[rcp] notifications for {} could not be delivered: {error}",
                space.key()
            );
        }
    }
}

async fn request(
    app: &AppHandle,
    space: &Space,
    method: Method,
    path: &str,
    body: Option<&Value>,
    renew: bool,
) -> Result<(u16, Value), String> {
    let response = match space {
        Space::Personal => {
            let status = app.state::<BackendState>().status()?;
            let mut builder = reqwest::Client::new()
                .request(method, format!("{}{path}", status.base_url))
                .timeout(PERSONAL_REQUEST_TIMEOUT);
            if let Some(body) = body {
                builder = builder.json(body);
            }
            builder.send().await.map_err(|error| error.to_string())?
        }
        Space::Team(connection_id) => {
            app.state::<TeamSessionState>()
                .notification_request(
                    app.state::<TeamConnectionState>().inner(),
                    connection_id,
                    method,
                    path,
                    body,
                    renew,
                )
                .await?
        }
    };
    let status = response.status().as_u16();
    let value = response.json::<Value>().await.unwrap_or(Value::Null);
    Ok((status, value))
}

/// `renew` is true only for a human tap; see `notification_request`.
async fn device_id(
    app: &AppHandle,
    state: &NotificationState,
    space: &Space,
    renew: bool,
) -> Result<String, String> {
    if let Some(id) = state.lock().devices.get(space).cloned() {
        return Ok(id);
    }
    let (status, value) = request(
        app,
        space,
        Method::POST,
        "/api/notifications/devices/desktop",
        Some(&json!({})),
        renew,
    )
    .await?;
    if status != 200 {
        return Err(format!("device registration returned HTTP {status}"));
    }
    let device: Device = serde_json::from_value(value).map_err(|error| error.to_string())?;
    state
        .lock()
        .devices
        .insert(space.clone(), device.device_id.clone());
    Ok(device.device_id)
}

async fn poll(app: &AppHandle, state: &NotificationState, space: &Space) -> Result<(), String> {
    if let Space::Team(connection_id) = space {
        if app
            .state::<TeamSessionState>()
            .established(connection_id)
            .is_err()
        {
            return Ok(());
        }
    }
    let device = device_id(app, state, space, false).await?;
    let (status, value) = request(
        app,
        space,
        Method::GET,
        &format!("/api/notifications/devices/{device}/pending"),
        None,
        false,
    )
    .await?;
    if status == 404 || status == 401 {
        // The device was detached; register again on the next pass.
        state.lock().devices.remove(space);
        return Ok(());
    }
    if status != 200 {
        return Err(format!("the outbox returned HTTP {status}"));
    }
    let items: Vec<Item> = serde_json::from_value(value).map_err(|error| error.to_string())?;
    let first_since_launch = state.lock().launched.insert(space.clone());
    if first_since_launch && items.len() > BACKLOG_SUMMARY_LIMIT {
        let id = format!("{}|summary", space.key());
        state.lock().posting.insert(
            id.clone(),
            Posting {
                space: space.clone(),
                device_id: device,
                notification_ids: items
                    .iter()
                    .map(|item| item.notification_id.clone())
                    .collect(),
            },
        );
        platform::post(&id, "RCP", &format!("{} items need you", items.len()), "#/");
        return Ok(());
    }
    for item in items {
        let id = format!("{}|{}", space.key(), item.notification_id);
        state.lock().posting.insert(
            id.clone(),
            Posting {
                space: space.clone(),
                device_id: device.clone(),
                notification_ids: vec![item.notification_id],
            },
        );
        platform::post(
            &id,
            &item.project_name,
            reason_text(&item.reason),
            &item.deep_link,
        );
    }
    Ok(())
}

fn acknowledge(app: &AppHandle, posting: Posting, posted: bool) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let status = if posted { "posted" } else { "failed" };
        for notification_id in posting.notification_ids {
            let path = format!(
                "/api/notifications/devices/{}/items/{notification_id}",
                posting.device_id
            );
            if let Err(error) = request(
                &app,
                &posting.space,
                Method::POST,
                &path,
                Some(&json!({ "status": status })),
                false,
            )
            .await
            {
                // An unacknowledged lease is retried by the backend with the same id.
                eprintln!("[rcp] a notification acknowledgment failed: {error}");
            }
        }
    });
}

fn handle_event(kind: &str, adapter_id: &str, text: &str) {
    let Some(app) = APP.get() else {
        return;
    };
    let state = app.state::<NotificationState>().inner().clone();
    match kind {
        "click" => {
            let Some(space) = adapter_id.split('|').next().and_then(Space::from_key) else {
                return;
            };
            if text.starts_with("#/") {
                if app.state::<BackendState>().status().is_ok() {
                    open(app, &space, text);
                } else {
                    // A click that launched the app waits for the backend.
                    state.lock().pending_click = Some((space, text.to_string()));
                }
            }
        }
        "posted" | "error" => {
            let posted = kind == "posted";
            let reply = state.lock().replies.remove(adapter_id);
            if let Some(reply) = reply {
                let _ = reply.send(if posted {
                    Ok(())
                } else {
                    Err(text.to_string())
                });
                return;
            }
            let posting = state.lock().posting.remove(adapter_id);
            if let Some(posting) = posting {
                acknowledge(app, posting, posted);
            }
        }
        _ => {}
    }
}

fn open(app: &AppHandle, space: &Space, link: &str) {
    let origin = match space {
        Space::Personal => app
            .state::<BackendState>()
            .status()
            .and_then(|status| windows::personal_root(&status.base_url)),
        Space::Team(connection_id) => app
            .state::<TeamSessionState>()
            .established(connection_id)
            .and_then(|session| {
                Url::parse(&session.connection.local_origin).map_err(|error| error.to_string())
            }),
    };
    if let (Ok(mut target), Some(window)) = (origin, app.get_webview_window("main")) {
        target.set_fragment(Some(link.trim_start_matches('#')));
        if let Err(error) = window.navigate(target) {
            eprintln!("[rcp] a notification could not open its item: {error}");
        }
    }
    crate::verify_then_prepare_show(app.clone(), "notification");
}

async fn adapter_reply(app: &AppHandle, id: &str, send: impl FnOnce()) -> Result<(), String> {
    let (sender, receiver) = oneshot::channel();
    app.state::<NotificationState>()
        .lock()
        .replies
        .insert(id.to_string(), sender);
    send();
    tokio::time::timeout(ADAPTER_REPLY_TIMEOUT, receiver)
        .await
        .map_err(|_| "macOS did not answer".to_string())?
        .map_err(|_| "macOS did not answer".to_string())?
}

/// The space the window is showing: the personal backend or one team.
fn window_space(app: &AppHandle, window: &WebviewWindow) -> Result<Space, String> {
    let url = window.url().map_err(|error| error.to_string())?;
    if let Ok(status) = app.state::<BackendState>().status() {
        if windows::personal_root(&status.base_url).is_ok_and(|root| root.origin() == url.origin())
        {
            return Ok(Space::Personal);
        }
    }
    app.state::<TeamSessionState>()
        .established_for_origin(&url)?
        .map(|session| Space::Team(session.connection.connection_id))
        .ok_or_else(|| "this window is not showing an RCP space".to_string())
}

pub fn enabled_for_window(app: &AppHandle, window: &WebviewWindow) -> Result<bool, String> {
    let space = window_space(app, window)?;
    Ok(app.state::<NotificationState>().enabled(&space))
}

pub async fn set_for_window(
    app: &AppHandle,
    window: &WebviewWindow,
    enabled: bool,
) -> Result<(), String> {
    let space = window_space(app, window)?;
    let state = app.state::<NotificationState>().inner().clone();
    if enabled {
        adapter_reply(app, AUTHORIZE_ID, platform::authorize).await?;
        state.set_enabled(&space, true)?;
        device_id(app, &state, &space, true).await?;
        return Ok(());
    }
    let device = device_id(app, &state, &space, true).await;
    state.set_enabled(&space, false)?;
    if let Ok(device) = device {
        request(
            app,
            &space,
            Method::DELETE,
            &format!("/api/notifications/devices/{device}"),
            None,
            true,
        )
        .await?;
    }
    Ok(())
}

pub async fn test(app: &AppHandle) -> &'static str {
    match adapter_reply(app, TEST_ID, || {
        platform::post(TEST_ID, "RCP", "Notifications are on", "")
    })
    .await
    {
        Ok(()) => "posted",
        Err(_) => "failed",
    }
}

extern "C" fn adapter_event(kind: *const c_char, id: *const c_char, text: *const c_char) {
    handle_event(
        &string_from_ptr(kind),
        &string_from_ptr(id),
        &string_from_ptr(text),
    );
}

fn string_from_ptr(value: *const c_char) -> String {
    if value.is_null() {
        return String::new();
    }
    // SAFETY: the Objective-C bridge passes NUL-terminated UTF-8 strings that
    // stay alive for the duration of the callback.
    unsafe { CStr::from_ptr(value) }
        .to_string_lossy()
        .into_owned()
}

#[cfg(target_os = "macos")]
mod platform {
    use super::*;

    extern "C" {
        fn rcp_notifications_install(
            callback: extern "C" fn(*const c_char, *const c_char, *const c_char),
        );
        fn rcp_notifications_authorize(notification_id: *const c_char);
        fn rcp_notifications_post(
            notification_id: *const c_char,
            title: *const c_char,
            body: *const c_char,
            link: *const c_char,
        );
    }

    fn c(value: &str) -> CString {
        CString::new(value.replace('\0', "")).unwrap_or_default()
    }

    pub(super) fn install() {
        // SAFETY: the bridge retains the function pointer for later callbacks.
        unsafe { rcp_notifications_install(adapter_event) };
    }

    pub(super) fn authorize() {
        let id = c(AUTHORIZE_ID);
        // SAFETY: the bridge copies the id before returning.
        unsafe { rcp_notifications_authorize(id.as_ptr()) };
    }

    pub(super) fn post(id: &str, title: &str, body: &str, link: &str) {
        let (id, title, body, link) = (c(id), c(title), c(body), c(link));
        // SAFETY: the bridge copies every string before returning.
        unsafe {
            rcp_notifications_post(id.as_ptr(), title.as_ptr(), body.as_ptr(), link.as_ptr())
        };
    }
}

#[cfg(not(target_os = "macos"))]
mod platform {
    use super::*;

    pub(super) fn install() {}

    pub(super) fn authorize() {
        adapter_event(
            c"error".as_ptr(),
            c"authorize".as_ptr(),
            c"notifications are available only on macOS".as_ptr(),
        );
    }

    pub(super) fn post(id: &str, _title: &str, _body: &str, _link: &str) {
        let id = CString::new(id).unwrap_or_default();
        adapter_event(
            c"error".as_ptr(),
            id.as_ptr(),
            c"notifications are available only on macOS".as_ptr(),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn adapter_ids_name_their_space() {
        for space in [Space::Personal, Space::Team("c-1".into())] {
            let id = format!("{}|abc", space.key());
            assert_eq!(id.split('|').next().and_then(Space::from_key), Some(space));
        }
        assert_eq!(Space::from_key("other"), None);
    }
}
