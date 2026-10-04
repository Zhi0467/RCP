//! Personal credentials stay native: only the session cookie enters WKWebView.
use std::{
    collections::HashMap,
    sync::{Mutex, OnceLock},
    time::Duration,
};

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use reqwest::{
    header::{HeaderMap, HeaderValue, COOKIE, SET_COOKIE},
    Client,
};
use ring::rand::{SecureRandom, SystemRandom};
use tauri::{AppHandle, Manager};
use zeroize::Zeroizing;

use crate::{backend, lifecycle::DesktopStatus, local_https};

const KEYCHAIN_SERVICE: &str = "app.researchcontrolpanel.rcp.owner";
const REQUEST_TIMEOUT: Duration = Duration::from_secs(15);
const COOKIE_NAME: &str = "rcp_owner_session";
static SESSIONS: OnceLock<Mutex<HashMap<String, OwnerCookie>>> = OnceLock::new();

struct OwnerCookie {
    instance_id: String,
    header: HeaderValue,
}

pub fn new_secret() -> Result<Zeroizing<String>, String> {
    let mut bytes = Zeroizing::new([0u8; 32]);
    SystemRandom::new()
        .fill(bytes.as_mut())
        .map_err(|_| "could not generate owner secret")?;
    Ok(Zeroizing::new(URL_SAFE_NO_PAD.encode(bytes.as_ref())))
}

fn sessions() -> &'static Mutex<HashMap<String, OwnerCookie>> {
    SESSIONS.get_or_init(|| Mutex::new(HashMap::new()))
}

/// Every caller pins its URLs to this verified origin; redirects never carry cookies.
pub fn client(base_url: &str, timeout: Option<Duration>) -> Result<Client, String> {
    let (cookie, instance_id) = sessions()
        .lock()
        .map_err(|_| "owner session is unavailable")?
        .get(base_url)
        .map(|session| (session.header.clone(), session.instance_id.clone()))
        .ok_or("owner sign-in required")?;
    let mut headers = HeaderMap::new();
    headers.insert(COOKIE, cookie);
    headers.insert(
        "X-RCP-Instance-ID",
        HeaderValue::from_str(&instance_id).map_err(|_| "invalid backend instance")?,
    );
    let mut builder = Client::builder()
        .default_headers(headers)
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(REQUEST_TIMEOUT);
    if let Some(timeout) = timeout {
        builder = builder.timeout(timeout);
    }
    builder.build().map_err(|error| error.to_string())
}

pub fn has_session(status: &DesktopStatus) -> bool {
    sessions().lock().ok().is_some_and(|sessions| {
        sessions
            .get(&status.base_url)
            .is_some_and(|session| session.instance_id == status.instance_id)
    })
}

pub fn forget(status: &DesktopStatus) -> Result<(), String> {
    let mut sessions = sessions()
        .lock()
        .map_err(|_| "owner session is unavailable")?;
    if sessions
        .get(&status.base_url)
        .is_some_and(|session| session.instance_id == status.instance_id)
    {
        sessions.remove(&status.base_url);
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn saved_secret(status: &DesktopStatus) -> Result<Option<Zeroizing<String>>, String> {
    crate::keychain::get(KEYCHAIN_SERVICE, &status.data_dir_id)?
        .map(|bytes| {
            std::str::from_utf8(&bytes)
                .map(|value| Zeroizing::new(value.to_string()))
                .map_err(|_| "invalid owner secret in Keychain".to_string())
        })
        .transpose()
}

#[cfg(not(target_os = "macos"))]
fn saved_secret(_status: &DesktopStatus) -> Result<Option<Zeroizing<String>>, String> {
    Err("owner Keychain storage requires macOS".into())
}

#[cfg(target_os = "macos")]
fn save_secret(status: &DesktopStatus, secret: &str) -> Result<(), String> {
    crate::keychain::set(KEYCHAIN_SERVICE, &status.data_dir_id, secret.as_bytes())
}

#[cfg(not(target_os = "macos"))]
fn save_secret(_status: &DesktopStatus, _secret: &str) -> Result<(), String> {
    Err("owner Keychain storage requires macOS".into())
}

pub async fn establish(
    app: &AppHandle,
    status: &DesktopStatus,
    spawned: Option<(&str, Option<&str>)>,
) -> Result<bool, String> {
    sessions()
        .lock()
        .map_err(|_| "owner session is unavailable")?
        .remove(&status.base_url);
    let saved = saved_secret(status)?;
    let secret = saved
        .as_deref()
        .map(String::as_str)
        .or(spawned.map(|(secret, _)| secret));
    if let Some(secret) = secret {
        if authenticate(
            app,
            status,
            "exchange",
            &serde_json::json!({"secret": secret}),
        )
        .await?
        {
            // Consume the private stdout code even when the existing secret matched.
            if let Some((_, Some(code))) = spawned {
                if !authenticate(
                    app,
                    status,
                    "redeem",
                    &serde_json::json!({"code": code, "secret": secret}),
                )
                .await?
                {
                    return Err("the backend startup sign-in code was refused".into());
                }
            }
            save_secret(status, secret)?;
            return Ok(true);
        }
    }
    if let Some((secret, Some(code))) = spawned {
        if authenticate(
            app,
            status,
            "redeem",
            &serde_json::json!({"code": code, "secret": secret}),
        )
        .await?
        {
            save_secret(status, secret)?;
            return Ok(true);
        }
    }
    Ok(false)
}

pub async fn redeem(app: &AppHandle, status: &DesktopStatus, code: &str) -> Result<(), String> {
    if code.is_empty() || code.len() > 256 {
        return Err("invalid owner sign-in code".into());
    }
    let secret = new_secret()?;
    if !authenticate(
        app,
        status,
        "redeem",
        &serde_json::json!({"code": code, "secret": secret.as_str()}),
    )
    .await?
    {
        return Err("owner sign-in code was refused".into());
    }
    save_secret(status, &secret)
}

async fn authenticate(
    app: &AppHandle,
    status: &DesktopStatus,
    route: &str,
    body: &serde_json::Value,
) -> Result<bool, String> {
    let current = backend::health(status).await?;
    if !status.matches_health(&current) {
        return Err("personal backend identity changed before sign-in".into());
    }
    let response = Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(REQUEST_TIMEOUT)
        .build()
        .map_err(|error| error.to_string())?
        .post(format!("{}/api/owner/{route}", status.base_url))
        .header("X-RCP-Instance-ID", &status.instance_id)
        .json(body)
        .send()
        .await
        .map_err(|error| format!("owner sign-in unavailable: {error}"))?;
    if response.status() == reqwest::StatusCode::UNAUTHORIZED {
        return Ok(false);
    }
    if !response.status().is_success() {
        return Err(format!("owner sign-in returned HTTP {}", response.status()));
    }
    let header = response
        .headers()
        .get(SET_COOKIE)
        .and_then(|value| value.to_str().ok())
        .ok_or("owner sign-in omitted its session cookie")?;
    let cookie = validate_cookie(header, &status.base_url)?;
    if !status.matches_health(&backend::health(status).await?) {
        return Err("personal backend identity changed during sign-in".into());
    }
    let window = app
        .get_webview_window("main")
        .ok_or("personal window is unavailable")?;
    local_https::install_owner_session_cookie(
        &window,
        &status.base_url,
        Zeroizing::new(header.to_string()),
    )
    .await?;
    sessions()
        .lock()
        .map_err(|_| "owner session is unavailable")?
        .insert(
            status.base_url.clone(),
            OwnerCookie {
                instance_id: status.instance_id.clone(),
                header: cookie,
            },
        );
    Ok(true)
}

fn validate_cookie(header: &str, origin: &str) -> Result<HeaderValue, String> {
    let url = url::Url::parse(origin).map_err(|_| "invalid personal origin")?;
    let mut parts = header.split(';');
    let pair = parts.next().ok_or("invalid owner cookie")?.trim();
    let (name, value) = pair.split_once('=').ok_or("invalid owner cookie")?;
    let attrs: Vec<_> = parts.map(|part| part.trim().to_ascii_lowercase()).collect();
    if !matches!(url.scheme(), "http" | "https")
        || url.host_str().is_none()
        || name != COOKIE_NAME
        || value.is_empty()
        || attrs.iter().any(|a| a.starts_with("domain="))
        || !attrs.iter().any(|a| a == "httponly")
        || !attrs.iter().any(|a| a == "samesite=strict")
        || !attrs.iter().any(|a| a == "path=/")
        || attrs.iter().any(|a| a == "secure") != (url.scheme() == "https")
    {
        return Err("owner cookie lacks required protections".into());
    }
    let mut value = HeaderValue::from_str(pair).map_err(|_| "invalid owner cookie")?;
    value.set_sensitive(true);
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn native_clients_send_owner_cookie_and_refuse_redirects() {
        use tokio::{
            io::{AsyncReadExt, AsyncWriteExt},
            net::TcpListener,
        };
        let listener = TcpListener::bind("127.0.0.1:8619").await.unwrap();
        let origin = "http://127.0.0.1:8619";
        let cookie = validate_cookie(
            "rcp_owner_session=test-session; HttpOnly; SameSite=Strict; Path=/",
            origin,
        )
        .unwrap();
        sessions().lock().unwrap().insert(
            origin.into(),
            OwnerCookie {
                instance_id: "instance".into(),
                header: cookie,
            },
        );
        let server = tokio::spawn(async move {
            for _ in 0..2 {
                let (mut stream, _) = listener.accept().await.unwrap();
                let mut request = Vec::new();
                loop {
                    let mut chunk = [0; 1024];
                    let count = stream.read(&mut chunk).await.unwrap();
                    assert!(count > 0);
                    request.extend_from_slice(&chunk[..count]);
                    if request.windows(4).any(|part| part == b"\r\n\r\n") {
                        break;
                    }
                }
                let request = String::from_utf8(request).unwrap().to_ascii_lowercase();
                assert!(request.contains("cookie: rcp_owner_session=test-session\r\n"));
                assert!(request.contains("x-rcp-instance-id: instance\r\n"));
                stream.write_all(b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:8618/private\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").await.unwrap();
            }
        });
        for timeout in [Some(REQUEST_TIMEOUT), None] {
            let response = client(origin, timeout)
                .unwrap()
                .get(format!("{origin}/api/resource"))
                .send()
                .await
                .unwrap();
            assert_eq!(response.status(), reqwest::StatusCode::FOUND);
        }
        tokio::time::timeout(REQUEST_TIMEOUT, server)
            .await
            .unwrap()
            .unwrap();
        sessions().lock().unwrap().remove(origin);
        assert!(client(origin, None).is_err());
    }

    #[tokio::test]
    async fn expired_native_session_preserves_verified_status_and_blocks_active_work() {
        use tokio::{
            io::{AsyncReadExt, AsyncWriteExt},
            net::TcpListener,
        };
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let origin = origin.as_str();
        let status = DesktopStatus {
            desktop: true,
            owner_authenticated: true,
            version: "test".into(),
            base_url: origin.into(),
            instance_id: "same-instance".into(),
            data_dir_id: "same-data".into(),
            owner_kind: "desktop".into(),
            active_agent_tasks: 1,
            owned: true,
        };
        sessions().lock().unwrap().insert(
            origin.into(),
            OwnerCookie {
                instance_id: status.instance_id.clone(),
                header: validate_cookie(
                    "rcp_owner_session=expired; HttpOnly; SameSite=Strict; Path=/",
                    origin,
                )
                .unwrap(),
            },
        );
        let server = tokio::spawn(async move {
            for response in [
                "HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    .to_string(),
                {
                    let body = serde_json::json!({
                        "status": "ok", "version": "test", "pid": 1, "owner_kind": "desktop",
                        "instance_id": "same-instance", "data_dir_id": "same-data"
                    })
                    .to_string();
                    format!(
                        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                        body.len(),
                        body
                    )
                },
            ] {
                let (mut stream, _) = listener.accept().await.unwrap();
                let mut request = Vec::new();
                while !request.windows(4).any(|part| part == b"\r\n\r\n") {
                    let mut chunk = [0; 1024];
                    let count = stream.read(&mut chunk).await.unwrap();
                    assert!(count > 0);
                    request.extend_from_slice(&chunk[..count]);
                }
                stream.write_all(response.as_bytes()).await.unwrap();
            }
        });
        let refreshed = crate::commands::refresh_personal_status(
            &backend::BackendState::default(),
            status.clone(),
        )
        .await
        .unwrap();
        assert!(!refreshed.owner_authenticated);
        assert_eq!(refreshed.instance_id, status.instance_id);
        assert_eq!(refreshed.data_dir_id, status.data_dir_id);
        assert!(!has_session(&status));
        assert!(backend::health_details(&status).await.is_err());
        tokio::time::timeout(REQUEST_TIMEOUT, server)
            .await
            .unwrap()
            .unwrap();
    }

    #[test]
    fn owner_secret_fits_keychain_and_cookie_policy_tracks_transport() {
        let secret = new_secret().unwrap();
        assert_eq!(URL_SAFE_NO_PAD.decode(secret.as_bytes()).unwrap().len(), 32);
        assert!(secret.len() <= 64);
        let cookie = "rcp_owner_session=value; HttpOnly; SameSite=Strict; Path=/";
        assert!(validate_cookie(cookie, "http://127.0.0.1:8611").is_ok());
        assert!(validate_cookie(cookie, "https://localhost").is_err());
        assert!(validate_cookie(&format!("{cookie}; Secure"), "https://localhost").is_ok());
        assert!(
            validate_cookie(&format!("{cookie}; Domain=localhost"), "http://localhost").is_err()
        );
    }
}
