//! `rcp://open` share links handed off by a team's /open page.
//!
//! A link names a team space, the host its access URL lives on, and an opaque
//! token. The shell opens `/open#l=<token>` only on the saved connection whose
//! verified space matches, and never contacts `host`; it is shown, not fetched.
//! The token is never logged.

use std::fmt;

use serde::Serialize;
use tauri::AppHandle;
use url::Url;

use crate::team_connections::{validate_uuid4, TeamConnectionMetadata};

const RELEASE_SCHEME: &str = "rcp";
const DEV_SCHEME: &str = "rcp-dev";
const DEV_IDENTIFIER: &str = "app.researchcontrolpanel.rcp.dev";
const MAX_HOST_BYTES: usize = 260;
const MAX_TOKEN_BYTES: usize = 128;

#[derive(Clone, PartialEq, Eq)]
pub struct ShareLink {
    pub space_id: String,
    pub host: String,
    token: String,
}

impl fmt::Debug for ShareLink {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ShareLink")
            .field("space_id", &self.space_id)
            .field("host", &self.host)
            .finish_non_exhaustive()
    }
}

impl ShareLink {
    /// The path and fragment the matched space's own /open page reads.
    pub fn open_path(&self) -> String {
        format!("/open#l={}", self.token)
    }
}

/// The scheme this bundle registers, so RCP Dev never answers a release link.
pub fn scheme_for(identifier: &str) -> &'static str {
    if identifier == DEV_IDENTIFIER {
        DEV_SCHEME
    } else {
        RELEASE_SCHEME
    }
}

pub fn parse(url: &Url, scheme: &str) -> Result<ShareLink, String> {
    if url.scheme() != scheme
        || url.host_str() != Some("open")
        || !matches!(url.path(), "" | "/")
        || url.port().is_some()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
    {
        return Err("not an RCP share link".into());
    }
    let (mut space_id, mut host, mut token) = (None, None, None);
    for (key, value) in url.query_pairs() {
        let slot = match key.as_ref() {
            "space" => &mut space_id,
            "host" => &mut host,
            "l" => &mut token,
            _ => return Err("the share link has an unknown field".into()),
        };
        if slot.replace(value.into_owned()).is_some() {
            return Err("the share link repeats a field".into());
        }
    }
    let (Some(space_id), Some(host), Some(token)) = (space_id, host, token) else {
        return Err("the share link is incomplete".into());
    };
    validate_uuid4(&space_id, "the share link's space")?;
    if host.is_empty()
        || host.len() > MAX_HOST_BYTES
        || !host.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b':' | b'[' | b']')
        })
    {
        return Err("the share link's host is invalid".into());
    }
    if token.is_empty()
        || token.len() > MAX_TOKEN_BYTES
        || !token
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    {
        return Err("the share link's token is invalid".into());
    }
    Ok(ShareLink {
        space_id,
        host,
        token,
    })
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum NoticeState {
    Unpaired,
    Unreachable,
}

/// What the Web banner shows: the host only.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ShareLinkNotice {
    pub host: String,
    pub state: NoticeState,
}

/// A received link waiting for its space; it lives in the notification
/// poller's single pending slot.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PendingShare {
    pub link: ShareLink,
    pub reconnect_attempted: bool,
    pub notice: Option<NoticeState>,
}

impl PendingShare {
    pub fn new(link: ShareLink) -> Self {
        Self {
            link,
            reconnect_attempted: false,
            notice: None,
        }
    }

    pub fn shown_notice(&self) -> Option<ShareLinkNotice> {
        self.notice.map(|state| ShareLinkNotice {
            host: self.link.host.clone(),
            state,
        })
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum Step {
    Open(String),
    Reconnect(String),
    Unpaired,
    Wait,
}

/// Re-matched on every tick, so a space added after the link arrived opens it.
pub fn next_step(
    pending: &PendingShare,
    connections: Result<&[TeamConnectionMetadata], &String>,
    established: impl Fn(&str) -> bool,
    backend_ready: bool,
) -> Step {
    let Ok(connections) = connections else {
        return Step::Wait;
    };
    let Some(connection) = connections
        .iter()
        .find(|connection| connection.expected_space_id == pending.link.space_id)
    else {
        return Step::Unpaired;
    };
    let id = connection.connection_id.clone();
    if established(&id) {
        Step::Open(id)
    } else if backend_ready && !pending.reconnect_attempted {
        Step::Reconnect(id)
    } else {
        Step::Wait
    }
}

pub fn receive(app: &AppHandle, urls: Vec<Url>) {
    let scheme = scheme_for(&app.config().identifier);
    for url in urls {
        match parse(&url, scheme) {
            Ok(link) => crate::notifications::receive_share_link(app, link),
            Err(error) => eprintln!("[rcp] an opened URL was ignored: {error}"),
        }
    }
}

#[tauri::command]
pub fn desktop_share_link_notice(app: AppHandle) -> Option<ShareLinkNotice> {
    crate::notifications::share_link_notice(&app)
}

#[tauri::command]
pub fn desktop_retry_share_link(app: AppHandle) {
    crate::notifications::retry_share_link(&app);
}

#[tauri::command]
pub fn desktop_dismiss_share_link(app: AppHandle) {
    crate::notifications::dismiss_share_link(&app);
}

#[cfg(test)]
mod tests {
    use super::*;

    const SPACE: &str = "5f0c6b5e-4c1a-4d2e-9f3b-1a2b3c4d5e6f";
    const TOKEN: &str = "Ab3_-xYz0123456789abcd";

    fn link(query: &str) -> Result<ShareLink, String> {
        parse(&Url::parse(&format!("rcp://open?{query}")).unwrap(), "rcp")
    }

    fn connection(id: &str, space: &str) -> TeamConnectionMetadata {
        TeamConnectionMetadata {
            connection_id: id.into(),
            display_name: "Lab".into(),
            ssh_target: "lab".into(),
            remote_loopback_port: 8421,
            expected_space_id: space.into(),
            local_origin: "https://c.rcp.localhost:9000".into(),
            last_known_cards: Vec::new(),
            operator_route: None,
        }
    }

    #[test]
    fn parses_a_share_link_into_its_open_path() {
        let parsed = link(&format!(
            "space={SPACE}&host=rcp.lab.example:8443&l={TOKEN}"
        ))
        .unwrap();
        assert_eq!(parsed.space_id, SPACE);
        assert_eq!(parsed.host, "rcp.lab.example:8443");
        assert_eq!(parsed.open_path(), format!("/open#l={TOKEN}"));
        assert!(!format!("{parsed:?}").contains(TOKEN));
    }

    #[test]
    fn rejects_anything_but_a_complete_open_link() {
        let good = format!("space={SPACE}&host=lab&l={TOKEN}");
        for rejected in [
            format!("rcp-dev://open?{good}"),
            format!("rcp://other?{good}"),
            format!("rcp://open/x?{good}"),
            format!("rcp://open?{good}#frag"),
            format!("rcp://user@open?{good}"),
            format!("rcp://open?space={SPACE}&host=lab"),
            format!("rcp://open?{good}&extra=1"),
            format!("rcp://open?{good}&l={TOKEN}"),
            format!("rcp://open?space=not-a-uuid&host=lab&l={TOKEN}"),
            format!("rcp://open?space={SPACE}&host=lab/evil&l={TOKEN}"),
            format!("rcp://open?space={SPACE}&host=&l={TOKEN}"),
            format!("rcp://open?space={SPACE}&host=lab&l=a%23b"),
            format!("rcp://open?space={SPACE}&host=lab&l="),
        ] {
            assert!(
                parse(&Url::parse(&rejected).unwrap(), "rcp").is_err(),
                "{rejected}"
            );
        }
    }

    #[test]
    fn each_bundle_answers_only_its_own_scheme() {
        let release: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let dev: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.dev-bundle.conf.json")).unwrap();
        assert_eq!(scheme_for(release["identifier"].as_str().unwrap()), "rcp");
        assert_eq!(scheme_for(dev["identifier"].as_str().unwrap()), "rcp-dev");
        for (plist, scheme) in [
            (include_str!("../Info.plist"), "rcp"),
            (include_str!("../Info.dev.template.plist"), "rcp-dev"),
        ] {
            assert!(plist.contains("<key>CFBundleURLSchemes</key>"));
            assert!(plist.contains(&format!("<string>{scheme}</string>")));
        }
        assert!(!include_str!("../Info.plist").contains("<string>rcp-dev</string>"));
    }

    #[test]
    fn steps_follow_the_matched_space() {
        let pending =
            PendingShare::new(link(&format!("space={SPACE}&host=lab&l={TOKEN}")).unwrap());
        let other = connection("c-other", "11111111-1111-4111-8111-111111111111");
        let matched = connection("c-lab", SPACE);

        assert_eq!(
            next_step(&pending, Ok(std::slice::from_ref(&other)), |_| true, true),
            Step::Unpaired
        );
        let saved = [other, matched];
        assert_eq!(
            next_step(&pending, Ok(&saved), |id| id == "c-lab", false),
            Step::Open("c-lab".into())
        );
        assert_eq!(
            next_step(&pending, Ok(&saved), |_| false, false),
            Step::Wait
        );
        assert_eq!(
            next_step(&pending, Ok(&saved), |_| false, true),
            Step::Reconnect("c-lab".into())
        );
        let attempted = PendingShare {
            reconnect_attempted: true,
            ..pending
        };
        assert_eq!(
            next_step(&attempted, Ok(&saved), |_| false, true),
            Step::Wait
        );
        let unreadable = "unreadable".to_string();
        assert_eq!(
            next_step(&attempted, Err(&unreadable), |_| true, true),
            Step::Wait
        );
    }
}
