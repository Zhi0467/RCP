//! Native personal-to-team project transfer relay.
//!
//! This module deliberately keeps archive and proof bytes on the Rust side of
//! the desktop boundary.  JavaScript supplies only a source request id (and,
//! for the manual path, a local destination chosen by the human); it never
//! receives the archive or either transition proof.

use std::{
    fs::{self, File},
    io::{Read, Write},
    path::{Component, Path, PathBuf},
    time::Duration,
};

use reqwest::{
    header::{HeaderValue, CONTENT_LENGTH, CONTENT_TYPE},
    Client, Response,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tauri::ipc::Channel;
use uuid::{Uuid, Version as UuidVersion};

use crate::{
    backend::{self, BackendState},
    lifecycle::DesktopStatus,
    server_commands::{self, TerminalLaunchResult},
    team_connections::{TeamConnectionMetadata, TeamConnectionState},
    team_session::{ProjectTransferTargetReadback, TeamSessionState},
    team_tunnel::TeamTunnelState,
};

const PERSONAL_REQUEST_TIMEOUT: Duration = Duration::from_secs(30);
const TRANSFER_STREAM_IDLE_TIMEOUT: Duration = Duration::from_secs(120);
const MAX_PROOF_BYTES: usize = 32;
const COPY_BUFFER_BYTES: usize = 1024 * 1024;
const TRANSFER_ARCHIVE_CONTENT_TYPE: &str = "application/octet-stream";
const SOURCE_REQUEST_PATH_PREFIX: &str = "/api/project-transfers/requests/";
const SOURCE_ARCHIVE_PATH_PREFIX: &str = "/api/native/project-transfers/source-requests/";

#[derive(Clone, Debug, PartialEq, Eq)]
struct PinnedPersonalBackend {
    base_url: String,
    instance_id: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct SourceTransferRequest {
    request_id: String,
    phase: String,
    target_request_id: String,
    project_id: String,
    source_space_id: String,
    target_space_id: String,
    target_activation_proof_sha256: String,
    archive_sha256: String,
    archive_size_bytes: u64,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct ProjectTransferRunResult {
    pub request_id: String,
    pub target_request_id: String,
    pub target_space_id: String,
    pub connection_id: String,
    pub archive_sha256: String,
    pub archive_size_bytes: u64,
    pub exit_code: i32,
    pub event_count: usize,
    pub proof_verified: bool,
    pub cleanup_acknowledged: bool,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct ProjectTransferFinishResult {
    pub request_id: String,
    pub target_request_id: String,
    pub target_space_id: String,
    pub connection_id: String,
    pub proof_verified: bool,
    pub cleanup_acknowledged: bool,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct ProjectTransferExportResult {
    pub saved: bool,
    pub request_id: String,
    pub target_request_id: Option<String>,
    pub target_space_id: Option<String>,
    pub archive_sha256: Option<String>,
    pub archive_size_bytes: Option<u64>,
    pub path: Option<String>,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct ProjectTransferExportCleanupResult {
    pub request_id: String,
    pub removed: bool,
    pub path: String,
}

impl ProjectTransferExportResult {
    pub fn cancelled(request_id: &str) -> Self {
        Self {
            saved: false,
            request_id: request_id.to_string(),
            target_request_id: None,
            target_space_id: None,
            archive_sha256: None,
            archive_size_bytes: None,
            path: None,
        }
    }
}

/// The only cross-space value returned by the source proof endpoint.
///
/// It is parsed and re-serialized as this strict type before being sent to the
/// target cleanup endpoint.  In particular, the target never receives an
/// arbitrary JSON object supplied by the browser.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProjectTransferCleanupAcknowledgment {
    pub source_request_id: String,
    pub target_request_id: String,
    pub project_id: String,
    pub source_space_id: String,
    pub target_space_id: String,
    pub source_release_proof_sha256: String,
    pub target_activation_proof_sha256: String,
    pub archive_sha256: String,
    pub source_fence_head: TransferGraphHead,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TransferGraphHead {
    pub target: TransferGraphTarget,
    pub revision: u64,
    #[serde(default)]
    pub transition_id: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TransferGraphTarget {
    pub kind: String,
    #[serde(default)]
    pub branch_id: Option<String>,
}

impl ProjectTransferCleanupAcknowledgment {
    pub(crate) fn validate(&self) -> Result<(), String> {
        for (value, label) in [
            (&self.source_request_id, "source transfer request identity"),
            (&self.target_request_id, "target transfer request identity"),
            (&self.project_id, "transfer project identity"),
            (&self.source_space_id, "source transfer space identity"),
            (&self.target_space_id, "target transfer space identity"),
        ] {
            validate_uuid4(value, label)?;
        }
        if self.source_space_id == self.target_space_id {
            return Err("transfer cleanup acknowledgment must cross spaces".into());
        }
        for (value, label) in [
            (
                &self.source_release_proof_sha256,
                "source release proof commitment",
            ),
            (
                &self.target_activation_proof_sha256,
                "target activation proof commitment",
            ),
            (&self.archive_sha256, "transfer archive digest"),
        ] {
            validate_digest(value, label)?;
        }
        if self.source_fence_head.target.kind != "main"
            || self.source_fence_head.target.branch_id.is_some()
        {
            return Err("transfer cleanup acknowledgment must bind the fenced main head".into());
        }
        if let Some(transition_id) = &self.source_fence_head.transition_id {
            validate_digest(transition_id, "transfer fence transition identity")?;
        }
        Ok(())
    }
}

pub async fn run(
    lifecycle: &BackendState,
    connections: &TeamConnectionState,
    sessions: &TeamSessionState,
    tunnels: &TeamTunnelState,
    request_id: &str,
    on_event: &Channel<Value>,
    ssh_program: PathBuf,
) -> Result<ProjectTransferRunResult, String> {
    let (pinned, source) = load_source_request(lifecycle, request_id, false).await?;
    let target = resolve_target_connection(&source, connections, sessions)?;
    let _ = tunnels
        .connect_saved(connections, lifecycle, &target.connection_id)
        .await?;
    let target_readback = read_target_transfer(sessions, connections, &source, &target).await?;

    if matches!(source.phase.as_str(), "cleanup_acknowledged" | "completed") {
        if target_readback.phase == "completed" {
            return Ok(completed_run_result(&source, &target));
        }
        if target_readback.phase != "target_activated" {
            return Err(format!(
                "the source transfer is {} but the target transfer is {}; retry after the target reaches activation",
                source.phase, target_readback.phase
            ));
        }
        let archive_sha256 = source.archive_sha256.clone();
        let archive_size_bytes = source.archive_size_bytes;
        let finish =
            finish_loaded(lifecycle, connections, sessions, tunnels, source, target).await?;
        return Ok(ProjectTransferRunResult {
            request_id: finish.request_id,
            target_request_id: finish.target_request_id,
            target_space_id: finish.target_space_id,
            connection_id: finish.connection_id,
            archive_sha256,
            archive_size_bytes,
            exit_code: 0,
            event_count: 0,
            proof_verified: finish.proof_verified,
            cleanup_acknowledged: finish.cleanup_acknowledged,
        });
    }
    if source.phase != "archive_bound" {
        return Err("the source transfer is not at a relayable archive boundary".into());
    }
    if target_readback.phase == "completed" {
        return Err(concat!(
            "the target transfer is complete while the source remains archive-bound; ",
            "inspect both durable requests before retrying"
        )
        .into());
    }
    if target_readback.phase == "target_activated" {
        let archive_sha256 = source.archive_sha256.clone();
        let archive_size_bytes = source.archive_size_bytes;
        let finish =
            finish_loaded(lifecycle, connections, sessions, tunnels, source, target).await?;
        return Ok(ProjectTransferRunResult {
            request_id: finish.request_id,
            target_request_id: finish.target_request_id,
            target_space_id: finish.target_space_id,
            connection_id: finish.connection_id,
            archive_sha256,
            archive_size_bytes,
            exit_code: 0,
            event_count: 0,
            proof_verified: finish.proof_verified,
            cleanup_acknowledged: finish.cleanup_acknowledged,
        });
    }
    let archive = fetch_source_archive(&pinned, &source).await?;
    let (exit_code, event_count) = server_commands::run_project_transfer_import(
        &target,
        &source.target_request_id,
        archive,
        &source.archive_sha256,
        source.archive_size_bytes,
        on_event,
        ssh_program,
    )
    .await?;

    if exit_code != 0 {
        return Ok(ProjectTransferRunResult {
            request_id: source.request_id,
            target_request_id: source.target_request_id,
            target_space_id: source.target_space_id,
            connection_id: target.connection_id,
            archive_sha256: source.archive_sha256,
            archive_size_bytes: source.archive_size_bytes,
            exit_code,
            event_count,
            proof_verified: false,
            cleanup_acknowledged: false,
        });
    }

    let activated = read_target_transfer(sessions, connections, &source, &target).await?;
    if activated.phase != "target_activated" {
        return Err(format!(
            "the transfer command exited successfully but the target transfer is {}",
            activated.phase
        ));
    }
    let archive_sha256 = source.archive_sha256.clone();
    let archive_size_bytes = source.archive_size_bytes;
    let finish = finish_loaded(lifecycle, connections, sessions, tunnels, source, target).await?;
    Ok(ProjectTransferRunResult {
        request_id: finish.request_id,
        target_request_id: finish.target_request_id,
        target_space_id: finish.target_space_id,
        connection_id: finish.connection_id,
        archive_sha256,
        archive_size_bytes,
        exit_code,
        event_count,
        proof_verified: finish.proof_verified,
        cleanup_acknowledged: finish.cleanup_acknowledged,
    })
}

pub async fn finish(
    lifecycle: &BackendState,
    connections: &TeamConnectionState,
    sessions: &TeamSessionState,
    tunnels: &TeamTunnelState,
    request_id: &str,
    archive_path: PathBuf,
) -> Result<ProjectTransferFinishResult, String> {
    let (_pinned, source) = load_source_request(lifecycle, request_id, false).await?;
    let target = resolve_target_connection(&source, connections, sessions)?;
    let _ = tunnels
        .connect_saved(connections, lifecycle, &target.connection_id)
        .await?;
    let target_readback = read_target_transfer(sessions, connections, &source, &target).await?;
    if matches!(source.phase.as_str(), "cleanup_acknowledged" | "completed")
        && target_readback.phase == "completed"
    {
        let cleanup_source = source.clone();
        let result = ProjectTransferFinishResult {
            request_id: source.request_id,
            target_request_id: source.target_request_id,
            target_space_id: source.target_space_id,
            connection_id: target.connection_id,
            proof_verified: true,
            cleanup_acknowledged: true,
        };
        remove_local_export(&archive_path, &cleanup_source)?;
        return Ok(result);
    }
    if target_readback.phase != "target_activated" {
        return Err(format!(
            "the target transfer is {}; finish-proof requires durable target activation",
            target_readback.phase
        ));
    }
    let cleanup_source = source.clone();
    let result = finish_loaded(lifecycle, connections, sessions, tunnels, source, target).await?;
    remove_local_export(&archive_path, &cleanup_source)?;
    Ok(result)
}

pub async fn discard_export(
    lifecycle: &BackendState,
    request_id: &str,
    archive_path: PathBuf,
) -> Result<ProjectTransferExportCleanupResult, String> {
    let (_pinned, source) = load_source_request(lifecycle, request_id, false).await?;
    remove_local_export(&archive_path, &source)?;
    Ok(ProjectTransferExportCleanupResult {
        request_id: source.request_id,
        removed: true,
        path: archive_path.display().to_string(),
    })
}

pub async fn export(
    lifecycle: &BackendState,
    request_id: &str,
    destination: PathBuf,
) -> Result<ProjectTransferExportResult, String> {
    let (pinned, source) = load_source_request(lifecycle, request_id, true).await?;
    validate_export_destination(&destination)?;
    let archive = fetch_source_archive(&pinned, &source).await?;
    let path = write_local_archive(archive, &source, &destination).await?;
    Ok(ProjectTransferExportResult {
        saved: true,
        request_id: source.request_id,
        target_request_id: Some(source.target_request_id),
        target_space_id: Some(source.target_space_id),
        archive_sha256: Some(source.archive_sha256),
        archive_size_bytes: Some(source.archive_size_bytes),
        path: Some(path),
    })
}

pub async fn terminal(
    lifecycle: &BackendState,
    connections: &TeamConnectionState,
    sessions: &TeamSessionState,
    request_id: &str,
    archive_path: PathBuf,
) -> Result<TerminalLaunchResult, String> {
    let (_pinned, source) = load_source_request(lifecycle, request_id, true).await?;
    let target = resolve_target_connection(&source, connections, sessions)?;
    verify_local_archive(&archive_path, &source)?;
    let argv =
        server_commands::terminal_transfer_argv(&target, &source.target_request_id, &archive_path)?;
    server_commands::open_terminal(argv).await
}

async fn finish_loaded(
    lifecycle: &BackendState,
    connections: &TeamConnectionState,
    sessions: &TeamSessionState,
    tunnels: &TeamTunnelState,
    source: SourceTransferRequest,
    target: TeamConnectionMetadata,
) -> Result<ProjectTransferFinishResult, String> {
    let established = sessions.established(&target.connection_id)?;
    let ready = tunnels
        .connect_saved(connections, lifecycle, &target.connection_id)
        .await?;
    if ready.local_origin != established.connection.local_origin {
        return Err("the established team session is not pinned to its saved tunnel".into());
    }
    let proof = sessions
        .retrieve_target_activation_proof(
            connections,
            &target.connection_id,
            &source.target_request_id,
        )
        .await?;

    // Reverify immediately before the proof crosses back to the source.  The
    // original source backend identity is not allowed to drift during relay.
    let status = lifecycle.status()?;
    let pinned = pin_personal_backend(lifecycle, &status).await?;
    let acknowledgment = post_target_activation_proof(&pinned, &source, proof.as_slice()).await?;
    sessions
        .post_cleanup_acknowledgment(
            connections,
            &target.connection_id,
            &source.target_request_id,
            &acknowledgment,
        )
        .await?;
    Ok(ProjectTransferFinishResult {
        request_id: source.request_id,
        target_request_id: source.target_request_id,
        target_space_id: source.target_space_id,
        connection_id: target.connection_id,
        proof_verified: true,
        cleanup_acknowledged: true,
    })
}

async fn load_source_request(
    lifecycle: &BackendState,
    request_id: &str,
    require_archive: bool,
) -> Result<(PinnedPersonalBackend, SourceTransferRequest), String> {
    validate_uuid4(request_id, "project transfer request identity")?;
    let status = lifecycle.status()?;
    let pinned = pin_personal_backend(lifecycle, &status).await?;
    let client = personal_client(&pinned.base_url)?;
    let response = client
        .get(format!(
            "{}{}",
            pinned.base_url,
            source_request_path(request_id)
        ))
        .header(
            "X-RCP-Instance-ID",
            HeaderValue::from_str(&pinned.instance_id)
                .map_err(|_| "the personal backend instance identity is invalid".to_string())?,
        )
        .send()
        .await
        .map_err(|error| format!("could not read the personal transfer request: {error}"))?;
    if !response.status().is_success() {
        return Err(format!(
            "the personal transfer request was rejected (HTTP {})",
            response.status().as_u16()
        ));
    }
    let value = response
        .json::<Value>()
        .await
        .map_err(|_| "the personal transfer request returned an invalid response".to_string())?;
    let source = parse_source_request(&value, request_id, require_archive)?;
    Ok((pinned, source))
}

async fn pin_personal_backend(
    lifecycle: &BackendState,
    status: &DesktopStatus,
) -> Result<PinnedPersonalBackend, String> {
    let health = backend::reverify_identity(lifecycle, status).await?;
    if health.instance_id != status.instance_id {
        return Err("the personal backend identity changed before transfer relay".into());
    }
    Ok(PinnedPersonalBackend {
        base_url: status.base_url.trim_end_matches('/').to_string(),
        instance_id: health.instance_id,
    })
}

fn personal_client(base_url: &str) -> Result<Client, String> {
    Client::builder()
        .timeout(PERSONAL_REQUEST_TIMEOUT)
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|error| format!("could not create the personal transfer client: {error}"))
        .and_then(|client| {
            let url = url::Url::parse(base_url)
                .map_err(|_| "the personal backend origin is invalid".to_string())?;
            if url.scheme() != "http" && url.scheme() != "https" {
                return Err("the personal backend origin is invalid".into());
            }
            Ok(client)
        })
}

fn personal_stream_client(base_url: &str) -> Result<Client, String> {
    Client::builder()
        .connect_timeout(PERSONAL_REQUEST_TIMEOUT)
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|error| format!("could not create the personal transfer client: {error}"))
        .and_then(|client| {
            let url = url::Url::parse(base_url)
                .map_err(|_| "the personal backend origin is invalid".to_string())?;
            if url.scheme() != "http" && url.scheme() != "https" {
                return Err("the personal backend origin is invalid".into());
            }
            Ok(client)
        })
}

fn source_request_path(request_id: &str) -> String {
    format!("{SOURCE_REQUEST_PATH_PREFIX}{request_id}")
}

fn source_archive_path(request_id: &str) -> String {
    format!("{SOURCE_ARCHIVE_PATH_PREFIX}{request_id}/archive")
}

async fn fetch_source_archive(
    pinned: &PinnedPersonalBackend,
    source: &SourceTransferRequest,
) -> Result<Response, String> {
    let client = personal_stream_client(&pinned.base_url)?;
    let response = tokio::time::timeout(
        PERSONAL_REQUEST_TIMEOUT,
        client
            .get(format!(
                "{}{}",
                pinned.base_url,
                source_archive_path(&source.request_id)
            ))
            .header(
                "X-RCP-Instance-ID",
                HeaderValue::from_str(&pinned.instance_id)
                    .map_err(|_| "the personal backend instance identity is invalid".to_string())?,
            )
            .send(),
    )
    .await
    .map_err(|_| "the personal transfer archive response headers timed out".to_string())?
    .map_err(|error| format!("could not stream the personal transfer archive: {error}"))?;
    if !response.status().is_success() {
        return Err(format!(
            "the personal transfer archive was rejected (HTTP {})",
            response.status().as_u16()
        ));
    }
    validate_archive_headers(&response, source)?;
    Ok(response)
}

fn validate_archive_headers(
    response: &Response,
    source: &SourceTransferRequest,
) -> Result<(), String> {
    let content_type = response
        .headers()
        .get(CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .map(|value| value.split(';').next().unwrap_or_default().trim());
    if content_type != Some(TRANSFER_ARCHIVE_CONTENT_TYPE) {
        return Err("the personal transfer archive returned an invalid content type".into());
    }
    let content_length = response
        .headers()
        .get(CONTENT_LENGTH)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<u64>().ok())
        .ok_or_else(|| "the personal transfer archive omitted its bounded size".to_string())?;
    if content_length != source.archive_size_bytes {
        return Err("the personal transfer archive size differs from its durable receipt".into());
    }
    let digest = response
        .headers()
        .get("X-RCP-Archive-SHA256")
        .and_then(|value| value.to_str().ok());
    if digest != Some(source.archive_sha256.as_str()) {
        return Err("the personal transfer archive digest differs from its durable receipt".into());
    }
    Ok(())
}

fn parse_source_request(
    value: &Value,
    expected_request_id: &str,
    require_archive: bool,
) -> Result<SourceTransferRequest, String> {
    let object = value
        .as_object()
        .ok_or_else(|| "the personal transfer request returned an invalid response".to_string())?;
    let text = |field: &str| {
        object
            .get(field)
            .and_then(Value::as_str)
            .ok_or_else(|| format!("the personal transfer request has no valid {field}"))
    };
    let request_id = text("request_id")?.to_string();
    if request_id != expected_request_id {
        return Err(
            "the personal transfer request identity does not match the requested id".into(),
        );
    }
    validate_uuid4(&request_id, "project transfer request identity")?;
    if text("side")? != "source" {
        return Err("the requested transfer is not a personal source request".into());
    }
    let phase = text("phase")?;
    if !matches!(
        phase,
        "archive_bound" | "cleanup_acknowledged" | "completed"
    ) {
        return Err("the personal transfer is not at a relayable phase".into());
    }
    let target_request_id = text("linked_request_id")?.to_string();
    let project_id = text("project_id")?.to_string();
    let source_space_id = text("source_space_id")?.to_string();
    let target_space_id = text("target_space_id")?.to_string();
    validate_uuid4(&target_request_id, "target transfer request identity")?;
    validate_uuid4(&project_id, "transfer project identity")?;
    validate_uuid4(&source_space_id, "source transfer space identity")?;
    validate_uuid4(&target_space_id, "target transfer space identity")?;
    if source_space_id == target_space_id {
        return Err("the personal transfer request does not cross spaces".into());
    }
    let target_activation_proof_sha256 = text("target_activation_proof_sha256")?.to_string();
    validate_digest(
        &target_activation_proof_sha256,
        "target activation proof commitment",
    )?;

    let archive_sha256 = object
        .get("archive_sha256")
        .and_then(Value::as_str)
        .ok_or_else(|| "the personal transfer request has no valid archive_sha256".to_string())?
        .to_string();
    let archive_size_bytes = object
        .get("archive_size_bytes")
        .and_then(Value::as_u64)
        .ok_or_else(|| {
            "the personal transfer request has no valid archive_size_bytes".to_string()
        })?;
    if require_archive && phase != "archive_bound" {
        return Err("the personal transfer archive is no longer available for local relay".into());
    }
    validate_digest(&archive_sha256, "transfer archive digest")?;
    if archive_size_bytes == 0 {
        return Err("the personal transfer request has no bounded archive size".into());
    }
    Ok(SourceTransferRequest {
        request_id,
        phase: phase.to_string(),
        target_request_id,
        project_id,
        source_space_id,
        target_space_id,
        target_activation_proof_sha256,
        archive_sha256,
        archive_size_bytes,
    })
}

fn resolve_target_connection(
    source: &SourceTransferRequest,
    connections: &TeamConnectionState,
    sessions: &TeamSessionState,
) -> Result<TeamConnectionMetadata, String> {
    let matches = connections
        .list()?
        .into_iter()
        .filter(|connection| connection.expected_space_id == source.target_space_id)
        .collect::<Vec<_>>();
    let [target] = matches.as_slice() else {
        return Err(
            "the source transfer target space has no unique saved desktop connection".into(),
        );
    };
    sessions.established(&target.connection_id)?;
    server_commands::configured_route(target)?;
    Ok(target.clone())
}

async fn read_target_transfer(
    sessions: &TeamSessionState,
    connections: &TeamConnectionState,
    source: &SourceTransferRequest,
    target: &TeamConnectionMetadata,
) -> Result<ProjectTransferTargetReadback, String> {
    let readback = sessions
        .read_project_transfer(
            connections,
            &target.connection_id,
            &source.target_request_id,
        )
        .await?;
    if readback.linked_request_id != source.request_id
        || readback.target_space_id != source.target_space_id
    {
        return Err("the target transfer readback does not match the source request".into());
    }
    Ok(readback)
}

fn completed_run_result(
    source: &SourceTransferRequest,
    target: &TeamConnectionMetadata,
) -> ProjectTransferRunResult {
    ProjectTransferRunResult {
        request_id: source.request_id.clone(),
        target_request_id: source.target_request_id.clone(),
        target_space_id: source.target_space_id.clone(),
        connection_id: target.connection_id.clone(),
        archive_sha256: source.archive_sha256.clone(),
        archive_size_bytes: source.archive_size_bytes,
        exit_code: 0,
        event_count: 0,
        proof_verified: true,
        cleanup_acknowledged: true,
    }
}

async fn post_target_activation_proof(
    pinned: &PinnedPersonalBackend,
    source: &SourceTransferRequest,
    proof: &[u8],
) -> Result<ProjectTransferCleanupAcknowledgment, String> {
    if proof.len() != MAX_PROOF_BYTES {
        return Err("the target activation proof has an invalid size".into());
    }
    let digest = hex_digest(proof);
    if digest != source.target_activation_proof_sha256 {
        return Err("the target activation proof does not match its commitment".into());
    }
    let client = personal_client(&pinned.base_url)?;
    let content_type = HeaderValue::from_static(TRANSFER_ARCHIVE_CONTENT_TYPE);
    let proof_path = format!(
        "{SOURCE_ARCHIVE_PATH_PREFIX}{}/target-activation-proof",
        source.request_id
    );
    let response = client
        .post(format!("{}{proof_path}", pinned.base_url))
        .header(
            "X-RCP-Instance-ID",
            HeaderValue::from_str(&pinned.instance_id)
                .map_err(|_| "the personal backend instance identity is invalid".to_string())?,
        )
        .header(CONTENT_TYPE, content_type)
        .body(proof.to_vec())
        .send()
        .await
        .map_err(|error| {
            format!("could not return target activation proof to the personal backend: {error}")
        })?;
    if !response.status().is_success() {
        return Err(format!(
            "the personal backend rejected target activation proof (HTTP {})",
            response.status().as_u16()
        ));
    }
    let acknowledgment = response
        .json::<ProjectTransferCleanupAcknowledgment>()
        .await
        .map_err(|_| {
            "the personal backend returned an invalid cleanup acknowledgment".to_string()
        })?;
    acknowledgment.validate()?;
    if acknowledgment.source_request_id != source.request_id
        || acknowledgment.target_request_id != source.target_request_id
        || acknowledgment.project_id != source.project_id
        || acknowledgment.source_space_id != source.source_space_id
        || acknowledgment.target_space_id != source.target_space_id
        || acknowledgment.archive_sha256 != source.archive_sha256
        || acknowledgment.target_activation_proof_sha256 != digest
    {
        return Err("the personal backend returned a mismatched cleanup acknowledgment".into());
    }
    Ok(acknowledgment)
}

async fn write_local_archive(
    mut response: Response,
    source: &SourceTransferRequest,
    destination: &Path,
) -> Result<String, String> {
    let parent = destination
        .parent()
        .ok_or_else(|| "the local transfer archive has no parent directory".to_string())?;
    let mut temporary = tempfile::Builder::new()
        .prefix(".rcp-transfer-")
        .tempfile_in(parent)
        .map_err(|error| format!("could not create a protected local transfer export: {error}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        temporary
            .as_file()
            .set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|error| format!("could not protect the local transfer export: {error}"))?;
    }
    let mut hasher = Sha256::new();
    let mut size = 0_u64;
    while let Some(chunk) = tokio::time::timeout(TRANSFER_STREAM_IDLE_TIMEOUT, response.chunk())
        .await
        .map_err(|_| "the personal transfer archive stream made no progress".to_string())?
        .map_err(|error| format!("the personal transfer archive stream failed: {error}"))?
    {
        let chunk_size = u64::try_from(chunk.len())
            .map_err(|_| "the local transfer archive size is too large".to_string())?;
        size = size
            .checked_add(chunk_size)
            .ok_or_else(|| "the local transfer archive size overflowed".to_string())?;
        if size > source.archive_size_bytes {
            return Err("the personal transfer archive exceeded its durable size".into());
        }
        hasher.update(&chunk);
        temporary.write_all(&chunk).map_err(|error| {
            format!("could not write the protected local transfer export: {error}")
        })?;
    }
    if size != source.archive_size_bytes
        || format_digest(&hasher.finalize()) != source.archive_sha256
    {
        return Err("the local transfer export differs from its durable archive receipt".into());
    }
    temporary.as_file().sync_all().map_err(|error| {
        format!("could not finish the protected local transfer export: {error}")
    })?;
    temporary.persist_noclobber(destination).map_err(|error| {
        format!(
            "could not publish the protected local transfer export: {}",
            error.error
        )
    })?;
    Ok(destination.display().to_string())
}

fn validate_export_destination(destination: &Path) -> Result<(), String> {
    if !destination.is_absolute()
        || destination == Path::new("/")
        || destination
            .components()
            .any(|component| matches!(component, Component::ParentDir))
    {
        return Err("the local transfer export must be one specific absolute path".into());
    }
    let value = destination
        .to_str()
        .ok_or_else(|| "the local transfer export path is not valid UTF-8".to_string())?;
    if value.len() > 4096 || value.chars().any(char::is_control) {
        return Err("the local transfer export path is not bounded and safe".into());
    }
    if destination.exists() {
        return Err("the selected local transfer export already exists".into());
    }
    Ok(())
}

fn verify_local_archive(path: &Path, source: &SourceTransferRequest) -> Result<(), String> {
    validate_export_destination_for_read(path)?;
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("could not inspect the local transfer export: {error}"))?;
    if !metadata.file_type().is_file() || metadata.len() != source.archive_size_bytes {
        return Err("the local transfer export does not match the source archive size".into());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o777 != 0o600 {
            return Err("the local transfer export must be mode 0600".into());
        }
    }
    let mut file = File::open(path)
        .map_err(|error| format!("could not open the local transfer export: {error}"))?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; COPY_BUFFER_BYTES];
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|error| format!("could not read the local transfer export: {error}"))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    if format_digest(&hasher.finalize()) != source.archive_sha256 {
        return Err("the local transfer export differs from its durable archive digest".into());
    }
    Ok(())
}

fn remove_local_export(path: &Path, source: &SourceTransferRequest) -> Result<(), String> {
    verify_local_archive(path, source)?;
    fs::remove_file(path).map_err(|error| {
        format!("could not remove the completed local transfer export: {error}")
    })?;
    #[cfg(unix)]
    if let Some(parent) = path.parent() {
        let directory = File::open(parent).map_err(|error| {
            format!("could not open the local transfer export directory: {error}")
        })?;
        directory.sync_all().map_err(|error| {
            format!("could not finish removing the local transfer export: {error}")
        })?;
    }
    Ok(())
}

fn validate_export_destination_for_read(path: &Path) -> Result<(), String> {
    if !path.is_absolute()
        || path == Path::new("/")
        || path
            .components()
            .any(|component| matches!(component, Component::ParentDir))
    {
        return Err("the local transfer export must be one specific absolute path".into());
    }
    let value = path
        .to_str()
        .ok_or_else(|| "the local transfer export path is not valid UTF-8".to_string())?;
    if value.len() > 4096 || value.chars().any(char::is_control) {
        return Err("the local transfer export path is not bounded and safe".into());
    }
    Ok(())
}

fn validate_uuid4(value: &str, label: &str) -> Result<(), String> {
    let parsed = Uuid::parse_str(value).map_err(|_| format!("{label} is invalid"))?;
    if parsed.get_version() != Some(UuidVersion::Random) || parsed.to_string() != value {
        return Err(format!("{label} is invalid"));
    }
    Ok(())
}

fn validate_digest(value: &str, label: &str) -> Result<(), String> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
    {
        return Err(format!("{label} is invalid"));
    }
    Ok(())
}

fn hex_digest(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    format_digest(&digest)
}

fn format_digest(digest: &[u8]) -> String {
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    use std::os::unix::fs::PermissionsExt;

    const REQUEST_ID: &str = "11111111-1111-4111-8111-111111111111";
    const TARGET_ID: &str = "22222222-2222-4222-8222-222222222222";
    const PROJECT_ID: &str = "33333333-3333-4333-8333-333333333333";
    const SOURCE_SPACE_ID: &str = "44444444-4444-4444-8444-444444444444";
    const TARGET_SPACE_ID: &str = "55555555-5555-4555-8555-555555555555";

    fn acknowledgment() -> ProjectTransferCleanupAcknowledgment {
        ProjectTransferCleanupAcknowledgment {
            source_request_id: REQUEST_ID.into(),
            target_request_id: TARGET_ID.into(),
            project_id: PROJECT_ID.into(),
            source_space_id: SOURCE_SPACE_ID.into(),
            target_space_id: TARGET_SPACE_ID.into(),
            source_release_proof_sha256: "a".repeat(64),
            target_activation_proof_sha256: "b".repeat(64),
            archive_sha256: "c".repeat(64),
            source_fence_head: TransferGraphHead {
                target: TransferGraphTarget {
                    kind: "main".into(),
                    branch_id: None,
                },
                revision: 3,
                transition_id: Some("d".repeat(64)),
            },
        }
    }

    #[test]
    fn cleanup_acknowledgment_is_strict_and_public_only() {
        let value = serde_json::to_value(acknowledgment()).unwrap();
        let parsed: ProjectTransferCleanupAcknowledgment = serde_json::from_value(value).unwrap();
        parsed.validate().unwrap();
        let mut extra = serde_json::to_value(parsed).unwrap();
        extra["target_activation_proof"] = Value::String("secret".into());
        assert!(serde_json::from_value::<ProjectTransferCleanupAcknowledgment>(extra).is_err());
    }

    #[test]
    fn proof_commitment_is_checked_before_source_post() {
        let source = SourceTransferRequest {
            request_id: REQUEST_ID.into(),
            phase: "archive_bound".into(),
            target_request_id: TARGET_ID.into(),
            project_id: PROJECT_ID.into(),
            source_space_id: SOURCE_SPACE_ID.into(),
            target_space_id: TARGET_SPACE_ID.into(),
            target_activation_proof_sha256: hex_digest(&[7; 32]),
            archive_sha256: "c".repeat(64),
            archive_size_bytes: 1,
        };
        assert_eq!(hex_digest(&[7; 32]), source.target_activation_proof_sha256);
        assert_ne!(hex_digest(&[8; 32]), source.target_activation_proof_sha256);
    }

    #[test]
    fn source_phase_is_preserved_for_idempotent_relay_retries() {
        let archive = "c".repeat(64);
        let payload = |phase: &str| {
            serde_json::json!({
                "request_id": REQUEST_ID,
                "side": "source",
                "phase": phase,
                "linked_request_id": TARGET_ID,
                "project_id": PROJECT_ID,
                "source_space_id": SOURCE_SPACE_ID,
                "target_space_id": TARGET_SPACE_ID,
                "target_activation_proof_sha256": "b".repeat(64),
                "archive_sha256": archive.clone(),
                "archive_size_bytes": 17,
            })
        };
        let completed = parse_source_request(&payload("completed"), REQUEST_ID, false).unwrap();
        assert_eq!(completed.phase, "completed");
        assert_eq!(completed.archive_size_bytes, 17);
        assert!(parse_source_request(&payload("completed"), REQUEST_ID, true).is_err());
        let bound = parse_source_request(&payload("archive_bound"), REQUEST_ID, false).unwrap();
        assert_eq!(bound.phase, "archive_bound");
        assert_eq!(bound.archive_sha256, archive);
    }

    #[test]
    fn completed_source_and_target_retry_returns_metadata_without_relay_activity() {
        let source = SourceTransferRequest {
            request_id: REQUEST_ID.into(),
            phase: "completed".into(),
            target_request_id: TARGET_ID.into(),
            project_id: PROJECT_ID.into(),
            source_space_id: SOURCE_SPACE_ID.into(),
            target_space_id: TARGET_SPACE_ID.into(),
            target_activation_proof_sha256: "b".repeat(64),
            archive_sha256: "c".repeat(64),
            archive_size_bytes: 17,
        };
        let target = TeamConnectionMetadata {
            connection_id: "66666666-6666-4666-8666-666666666666".into(),
            display_name: "Vision lab".into(),
            ssh_target: "member@lab-server".into(),
            remote_loopback_port: 8421,
            expected_space_id: TARGET_SPACE_ID.into(),
            local_origin: "https://rcp-66666666666646668666666666666666.localhost:18421".into(),
            minimum_shell_version: "0.3.2".into(),
            last_known_cards: Vec::new(),
            operator_route: None,
        };
        let result = completed_run_result(&source, &target);
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.event_count, 0);
        assert!(result.proof_verified);
        assert!(result.cleanup_acknowledged);
        assert_eq!(result.archive_sha256, source.archive_sha256);
        assert_eq!(result.archive_size_bytes, source.archive_size_bytes);
    }

    #[cfg(unix)]
    #[test]
    fn manual_export_cleanup_removes_only_the_exact_verified_copy() {
        let directory = tempfile::tempdir().unwrap();
        let archive_path = directory.path().join("manual.rcp-transfer");
        let bytes = b"manual transfer archive";
        fs::write(&archive_path, bytes).unwrap();
        fs::set_permissions(&archive_path, fs::Permissions::from_mode(0o600)).unwrap();
        let source = SourceTransferRequest {
            request_id: REQUEST_ID.into(),
            phase: "archive_bound".into(),
            target_request_id: TARGET_ID.into(),
            project_id: PROJECT_ID.into(),
            source_space_id: SOURCE_SPACE_ID.into(),
            target_space_id: TARGET_SPACE_ID.into(),
            target_activation_proof_sha256: "b".repeat(64),
            archive_sha256: hex_digest(bytes),
            archive_size_bytes: bytes.len() as u64,
        };

        remove_local_export(&archive_path, &source).unwrap();
        assert!(!archive_path.exists());
    }

    #[cfg(unix)]
    #[test]
    fn manual_export_cleanup_leaves_a_mismatched_copy_untouched() {
        let directory = tempfile::tempdir().unwrap();
        let archive_path = directory.path().join("manual.rcp-transfer");
        fs::write(&archive_path, b"different archive").unwrap();
        fs::set_permissions(&archive_path, fs::Permissions::from_mode(0o600)).unwrap();
        let source = SourceTransferRequest {
            request_id: REQUEST_ID.into(),
            phase: "archive_bound".into(),
            target_request_id: TARGET_ID.into(),
            project_id: PROJECT_ID.into(),
            source_space_id: SOURCE_SPACE_ID.into(),
            target_space_id: TARGET_SPACE_ID.into(),
            target_activation_proof_sha256: "b".repeat(64),
            archive_sha256: "c".repeat(64),
            archive_size_bytes: 17,
        };

        assert!(remove_local_export(&archive_path, &source).is_err());
        assert!(archive_path.exists());
    }
}
