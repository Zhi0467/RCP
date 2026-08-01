use std::time::Duration;

use semver::Version;
use serde::Serialize;
use tauri::{AppHandle, Emitter};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
use tauri_plugin_updater::UpdaterExt;
use url::Url;

use crate::backend::{self, BackendState};

#[derive(Clone, Debug, Serialize)]
pub struct UpdateStatus {
    pub enabled: bool,
    pub available: bool,
    pub version: Option<String>,
    pub current_version: String,
    pub reason: Option<String>,
    pub active_agent_tasks: u64,
}

pub async fn check(app: &AppHandle, backend_state: &BackendState) -> Result<UpdateStatus, String> {
    let current = app.package_info().version.to_string();
    let active = refresh_active_tasks(backend_state).await.unwrap_or(0);
    let Some((endpoint, pubkey)) = configuration()? else {
        return Ok(UpdateStatus {
            enabled: false,
            available: false,
            version: None,
            current_version: current,
            reason: Some(
                "updates are disabled in this build because no signed endpoint and public key were configured"
                    .into(),
            ),
            active_agent_tasks: active,
        });
    };
    let update = app
        .updater_builder()
        .endpoints(vec![endpoint])
        .map_err(|error| format!("invalid updater endpoint: {error}"))?
        .pubkey(pubkey)
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|error| format!("could not configure updater: {error}"))?
        .check()
        .await
        .map_err(|error| format!("update check failed: {error}"))?;

    let version = update.as_ref().map(|candidate| candidate.version.clone());
    if let Some(candidate) = version.as_deref() {
        reject_downgrade(&current, candidate)?;
        app.emit(
            "rcp://update-ready",
            serde_json::json!({"version": candidate}),
        )
        .map_err(|error| format!("could not announce update: {error}"))?;
    }
    Ok(UpdateStatus {
        enabled: true,
        available: version.is_some(),
        version,
        current_version: current,
        reason: None,
        active_agent_tasks: active,
    })
}

pub async fn apply(
    app: &AppHandle,
    backend_state: &BackendState,
    confirm_active_work: bool,
) -> Result<(), String> {
    let active = refresh_active_tasks(backend_state).await?;
    if active > 0 && !confirm_active_work {
        return Err(format!(
            "update deferred: {active} agent task{} still running",
            if active == 1 { " is" } else { "s are" }
        ));
    }
    let (endpoint, pubkey) = configuration()?.ok_or_else(|| {
        "updates are disabled because this build has no signed endpoint and public key".to_string()
    })?;
    let updater = app
        .updater_builder()
        .endpoints(vec![endpoint])
        .map_err(|error| format!("invalid updater endpoint: {error}"))?
        .pubkey(pubkey)
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|error| format!("could not configure updater: {error}"))?;
    let update = updater
        .check()
        .await
        .map_err(|error| format!("update check failed: {error}"))?
        .ok_or_else(|| "no update is available".to_string())?;
    reject_downgrade(&app.package_info().version.to_string(), &update.version)?;

    // Network and signature verification finish before RCP pauses any owned work.
    let bytes = update
        .download(|_, _| {}, || {})
        .await
        .map_err(|error| format!("update download or signature verification failed: {error}"))?;
    let shutdown = backend::graceful_stop(backend_state).await?;
    if shutdown.forced {
        app.dialog()
            .message(shutdown.reason.unwrap_or_else(|| {
                "The owned backend required forced termination before updating.".into()
            }))
            .title("RCP shutdown")
            .buttons(MessageDialogButtons::Ok)
            .blocking_show();
    }
    update
        .install(bytes)
        .map_err(|error| format!("update installation failed: {error}"))?;
    app.restart();
}

async fn refresh_active_tasks(state: &BackendState) -> Result<u64, String> {
    let status = state.status()?;
    let health = backend::health(&status).await?;
    if health.instance_id != status.instance_id || health.data_dir_id != status.data_dir_id {
        return Err("backend identity changed while checking active work".into());
    }
    state.update_health(&health);
    Ok(health.active_agent_tasks)
}

fn configuration() -> Result<Option<(Url, String)>, String> {
    let endpoint = option_env!("RCP_UPDATE_ENDPOINT")
        .map(str::trim)
        .filter(|value| !value.is_empty());
    let pubkey = option_env!("RCP_UPDATE_PUBKEY")
        .map(str::trim)
        .filter(|value| !value.is_empty());
    match (endpoint, pubkey) {
        (None, None) => Ok(None),
        (Some(_), None) | (None, Some(_)) => Err(
            "updater build configuration requires both RCP_UPDATE_ENDPOINT and RCP_UPDATE_PUBKEY"
                .into(),
        ),
        (Some(endpoint), Some(pubkey)) => {
            let endpoint = Url::parse(endpoint)
                .map_err(|error| format!("RCP_UPDATE_ENDPOINT is invalid: {error}"))?;
            if endpoint.scheme() != "https" {
                return Err("RCP_UPDATE_ENDPOINT must use HTTPS".into());
            }
            Ok(Some((endpoint, pubkey.to_string())))
        }
    }
}

fn reject_downgrade(current: &str, candidate: &str) -> Result<(), String> {
    let current = Version::parse(current)
        .map_err(|error| format!("current app version is invalid: {error}"))?;
    let candidate =
        Version::parse(candidate).map_err(|error| format!("update version is invalid: {error}"))?;
    if candidate <= current {
        Err(format!("refusing non-upgrade version {candidate}"))
    } else {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn disabled_updater_config_is_an_object() {
        let config: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        assert!(config["plugins"]["updater"].is_object());
        assert_eq!(config["plugins"]["updater"]["pubkey"], "disabled");
    }

    #[test]
    fn rejects_downgrades_and_equal_versions() {
        assert!(reject_downgrade("1.2.3", "1.2.2").is_err());
        assert!(reject_downgrade("1.2.3", "1.2.3").is_err());
        assert!(reject_downgrade("1.2.3", "1.2.4").is_ok());
    }
}
