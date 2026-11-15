use url::Url;

pub fn is_loopback_rcp_url(url: &Url, base_url: &str, allow_dev: bool) -> bool {
    if url.scheme() == "about" {
        return true;
    }
    let Ok(base) = Url::parse(base_url) else {
        return false;
    };
    (same_origin(url, &base) && url.host_str().is_some_and(is_loopback_host))
        || (allow_dev
            && url.scheme() == "http"
            && url.host_str() == Some("127.0.0.1")
            && url.port_or_known_default() == Some(5173))
}

pub fn is_main_window_url(
    url: &Url,
    current_base_url: &str,
    saved_team_origins: &[String],
    allow_dev: bool,
) -> bool {
    is_loopback_rcp_url(url, current_base_url, allow_dev)
        || saved_team_origins.iter().any(|origin| {
            Url::parse(origin)
                .is_ok_and(|saved| same_origin(url, &saved) && saved.scheme() == "https")
        })
}

/// Whether `url` is an RCP application root document. Hash routes remain on
/// that document; same-origin API and arbitrary paths do not.
pub fn is_rcp_app_document_url(url: &Url, base_url: &str, allow_dev: bool) -> bool {
    let Ok(base) = Url::parse(base_url) else {
        return false;
    };
    (same_origin(url, &base) && url.path() == "/")
        || (allow_dev
            && url.scheme() == "http"
            && url.host_str() == Some("127.0.0.1")
            && url.port_or_known_default() == Some(5173)
            && url.path() == "/")
}

/// A team's share-link page, which the shell itself opens on that origin.
pub fn is_team_share_open_url(url: &Url, team_base_url: &str) -> bool {
    Url::parse(team_base_url).is_ok_and(|base| same_origin(url, &base) && url.path() == "/open")
}

/// Origin and path only: a fragment or query may carry a share-link token.
pub fn loggable(url: &Url) -> String {
    match url.origin() {
        url::Origin::Tuple(..) => format!("{}{}", url.origin().ascii_serialization(), url.path()),
        url::Origin::Opaque(_) => url.scheme().to_string(),
    }
}

pub fn is_external_reference(url: &Url) -> bool {
    matches!(url.scheme(), "http" | "https")
}

pub fn same_origin(left: &Url, right: &Url) -> bool {
    left.scheme() == right.scheme()
        && left.host_str() == right.host_str()
        && left.port_or_known_default() == right.port_or_known_default()
}

fn is_loopback_host(host: &str) -> bool {
    matches!(host, "127.0.0.1" | "localhost" | "::1" | "[::1]")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn logged_urls_keep_no_fragment_or_query() {
        let logged = loggable(&Url::parse("https://lab.example:8443/open?x=1#l=Token_1").unwrap());
        assert_eq!(logged, "https://lab.example:8443/open");
    }

    #[test]
    fn main_window_accepts_only_its_loopback_origins() {
        let base = "http://127.0.0.1:8421";
        assert!(is_loopback_rcp_url(
            &Url::parse(&format!("{base}/#/projects/a")).unwrap(),
            base,
            false
        ));
        assert!(!is_loopback_rcp_url(
            &Url::parse("https://example.com").unwrap(),
            base,
            true
        ));
        assert!(!is_loopback_rcp_url(
            &Url::parse("http://127.0.0.1:9999").unwrap(),
            base,
            false
        ));
        assert!(is_loopback_rcp_url(
            &Url::parse("http://127.0.0.1:5173").unwrap(),
            base,
            true
        ));
    }

    #[test]
    fn main_window_follows_only_the_verified_reused_port() {
        let reused = "http://127.0.0.1:18421";
        let candidate = Url::parse("http://127.0.0.1:18421/#/projects/a").unwrap();
        assert!(!is_main_window_url(
            &candidate,
            "http://127.0.0.1:8421",
            &[],
            false
        ));
        assert!(is_main_window_url(&candidate, reused, &[], false));
        assert!(!is_main_window_url(
            &Url::parse("http://127.0.0.1:8421").unwrap(),
            reused,
            &[],
            false,
        ));
        assert!(!is_main_window_url(
            &Url::parse("http://127.0.0.1:19421").unwrap(),
            reused,
            &[],
            false,
        ));
        assert!(is_main_window_url(
            &Url::parse("http://127.0.0.1:5173").unwrap(),
            reused,
            &[],
            true,
        ));
    }

    #[test]
    fn main_window_accepts_only_exact_saved_team_https_origins() {
        let saved =
            vec!["https://rcp-11111111111141118111111111111111.rcp.localhost:18421".to_string()];
        assert!(is_main_window_url(
            &Url::parse(&format!("{}/#/projects/a", saved[0])).unwrap(),
            "http://127.0.0.1:8421",
            &saved,
            false,
        ));
        for rejected in [
            "https://rcp-11111111111141118111111111111111.rcp.localhost:19421/",
            "https://rcp-22222222222242228222222222222222.rcp.localhost:18421/",
            "http://rcp-11111111111141118111111111111111.rcp.localhost:18421/",
        ] {
            assert!(!is_main_window_url(
                &Url::parse(rejected).unwrap(),
                "http://127.0.0.1:8421",
                &saved,
                false,
            ));
        }
    }

    #[test]
    fn every_http_reference_can_leave_for_the_system_browser() {
        assert!(is_external_reference(
            &Url::parse("https://example.com/paper").unwrap()
        ));
        assert!(is_external_reference(
            &Url::parse("http://127.0.0.1:8421/api").unwrap()
        ));
        assert!(!is_external_reference(
            &Url::parse("file:///tmp/a").unwrap()
        ));
    }

    #[test]
    fn app_document_must_be_the_root_but_may_use_a_hash_route() {
        let base = "http://127.0.0.1:8421";
        assert!(is_rcp_app_document_url(
            &Url::parse("http://127.0.0.1:8421/").unwrap(),
            base,
            false,
        ));
        assert!(is_rcp_app_document_url(
            &Url::parse("http://127.0.0.1:8421/#/projects/a").unwrap(),
            base,
            false,
        ));
        assert!(!is_rcp_app_document_url(
            &Url::parse("http://127.0.0.1:8421/api/projects").unwrap(),
            base,
            false,
        ));
        assert!(!is_rcp_app_document_url(
            &Url::parse("http://127.0.0.1:8421/some/path").unwrap(),
            base,
            false,
        ));
        assert!(!is_rcp_app_document_url(
            &Url::parse("http://127.0.0.1:19421/#/projects/a").unwrap(),
            base,
            false,
        ));
    }

    #[test]
    fn vite_is_an_app_document_only_at_its_root_in_dev() {
        let base = "http://127.0.0.1:8421";
        let root = Url::parse("http://127.0.0.1:5173/#/projects/a").unwrap();
        let error = Url::parse("http://127.0.0.1:5173/api/projects").unwrap();
        assert!(is_rcp_app_document_url(&root, base, true));
        assert!(!is_rcp_app_document_url(&root, base, false));
        assert!(!is_rcp_app_document_url(&error, base, true));
    }
}
