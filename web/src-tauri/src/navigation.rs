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

pub fn is_main_window_url(url: &Url, current_base_url: Option<&str>, allow_dev: bool) -> bool {
    is_loopback_rcp_url(
        url,
        current_base_url.unwrap_or("http://127.0.0.1:8421"),
        allow_dev,
    )
}

pub fn is_external_reference(url: &Url) -> bool {
    matches!(url.scheme(), "http" | "https")
}

fn same_origin(left: &Url, right: &Url) -> bool {
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
        assert!(!is_main_window_url(&candidate, None, false));
        assert!(is_main_window_url(&candidate, Some(reused), false));
        assert!(!is_main_window_url(
            &Url::parse("http://127.0.0.1:8421").unwrap(),
            Some(reused),
            false,
        ));
        assert!(!is_main_window_url(
            &Url::parse("http://127.0.0.1:19421").unwrap(),
            Some(reused),
            false,
        ));
        assert!(is_main_window_url(
            &Url::parse("http://127.0.0.1:5173").unwrap(),
            Some(reused),
            true,
        ));
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
}
