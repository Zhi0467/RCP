use sha2::{Digest, Sha256};
use zeroize::Zeroizing;

const IDENTITY_VERSION: u8 = 1;
const IDENTITY_HEADER: &[u8] = b"RCP-LOCAL-HTTPS\0";
const MAX_CERTIFICATE_BYTES: usize = 32 * 1024;
const MAX_PRIVATE_KEY_BYTES: usize = 16 * 1024;
const KEYCHAIN_SERVICE: &str = "app.researchcontrolpanel.rcp.local-https";
const KEYCHAIN_ACCOUNT: &str = "desktop-identity/v1";

pub struct LocalHttpsIdentity {
    certificate_der: Vec<u8>,
    private_key_der: Zeroizing<Vec<u8>>,
    fingerprint_sha256: String,
}

impl LocalHttpsIdentity {
    pub fn load_or_create() -> Result<Self, String> {
        match load_keychain_identity()? {
            Some(bytes) => decode_identity(&bytes),
            None => {
                let identity = generate_identity()?;
                let encoded = encode_identity(&identity)?;
                store_keychain_identity(&encoded)?;
                Ok(identity)
            }
        }
    }

    pub fn fingerprint_sha256(&self) -> &str {
        &self.fingerprint_sha256
    }
}

fn generate_identity() -> Result<LocalHttpsIdentity, String> {
    let rcgen::CertifiedKey { cert, signing_key } = rcgen::generate_simple_self_signed(vec![
        "localhost".to_string(),
        "*.localhost".to_string(),
    ])
    .map_err(|error| format!("could not generate the local HTTPS identity: {error}"))?;
    identity_from_parts(cert.der().to_vec(), signing_key.serialize_der())
}

fn identity_from_parts(
    certificate_der: Vec<u8>,
    private_key_der: Vec<u8>,
) -> Result<LocalHttpsIdentity, String> {
    let private_key_der = Zeroizing::new(private_key_der);
    if certificate_der.is_empty() || certificate_der.len() > MAX_CERTIFICATE_BYTES {
        return Err("the local HTTPS certificate has an invalid size".into());
    }
    if private_key_der.is_empty() || private_key_der.len() > MAX_PRIVATE_KEY_BYTES {
        return Err("the local HTTPS private key has an invalid size".into());
    }
    let private_key = rustls::pki_types::PrivateKeyDer::try_from(private_key_der.as_slice())
        .map_err(|_| "the local HTTPS private key is invalid".to_string())?;
    let signing_key = rustls::crypto::ring::sign::any_supported_type(&private_key)
        .map_err(|_| "the local HTTPS private key is invalid".to_string())?;
    rustls::sign::CertifiedKey::new(
        vec![rustls::pki_types::CertificateDer::from(
            certificate_der.clone(),
        )],
        signing_key,
    )
    .keys_match()
    .map_err(|_| "the local HTTPS certificate and private key do not match".to_string())?;
    let fingerprint_sha256 = lowercase_sha256(&certificate_der);
    Ok(LocalHttpsIdentity {
        certificate_der,
        private_key_der,
        fingerprint_sha256,
    })
}

fn encode_identity(identity: &LocalHttpsIdentity) -> Result<Zeroizing<Vec<u8>>, String> {
    let certificate_length = u32::try_from(identity.certificate_der.len())
        .map_err(|_| "the local HTTPS certificate is too large".to_string())?;
    let key_length = u32::try_from(identity.private_key_der.len())
        .map_err(|_| "the local HTTPS private key is too large".to_string())?;
    let mut encoded = Zeroizing::new(Vec::with_capacity(
        IDENTITY_HEADER.len()
            + 1
            + 8
            + identity.certificate_der.len()
            + identity.private_key_der.len(),
    ));
    encoded.extend_from_slice(IDENTITY_HEADER);
    encoded.push(IDENTITY_VERSION);
    encoded.extend_from_slice(&certificate_length.to_be_bytes());
    encoded.extend_from_slice(&key_length.to_be_bytes());
    encoded.extend_from_slice(&identity.certificate_der);
    encoded.extend_from_slice(&identity.private_key_der);
    Ok(encoded)
}

fn decode_identity(encoded: &[u8]) -> Result<LocalHttpsIdentity, String> {
    let prefix = IDENTITY_HEADER.len() + 1 + 8;
    if encoded.len() < prefix || !encoded.starts_with(IDENTITY_HEADER) {
        return Err("the local HTTPS Keychain identity has an unsupported shape".into());
    }
    if encoded[IDENTITY_HEADER.len()] != IDENTITY_VERSION {
        return Err("the local HTTPS Keychain identity has an unsupported version".into());
    }
    let lengths = &encoded[IDENTITY_HEADER.len() + 1..prefix];
    let certificate_length = u32::from_be_bytes(lengths[..4].try_into().unwrap()) as usize;
    let key_length = u32::from_be_bytes(lengths[4..].try_into().unwrap()) as usize;
    if certificate_length > MAX_CERTIFICATE_BYTES
        || key_length > MAX_PRIVATE_KEY_BYTES
        || prefix
            .checked_add(certificate_length)
            .and_then(|value| value.checked_add(key_length))
            != Some(encoded.len())
    {
        return Err("the local HTTPS Keychain identity has invalid lengths".into());
    }
    let certificate_end = prefix + certificate_length;
    identity_from_parts(
        encoded[prefix..certificate_end].to_vec(),
        encoded[certificate_end..].to_vec(),
    )
}

fn lowercase_sha256(value: &[u8]) -> String {
    Sha256::digest(value)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[cfg(target_os = "macos")]
fn load_keychain_identity() -> Result<Option<Zeroizing<Vec<u8>>>, String> {
    match security_framework::passwords::get_generic_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT) {
        Ok(bytes) => Ok(Some(Zeroizing::new(bytes))),
        Err(error) if error.code() == security_framework_sys::base::errSecItemNotFound => Ok(None),
        Err(error) => Err(format!(
            "could not read the local HTTPS identity from Keychain: {error}"
        )),
    }
}

#[cfg(not(target_os = "macos"))]
fn load_keychain_identity() -> Result<Option<Zeroizing<Vec<u8>>>, String> {
    Err("local HTTPS identity storage is supported only by the macOS desktop app".into())
}

#[cfg(target_os = "macos")]
fn store_keychain_identity(identity: &[u8]) -> Result<(), String> {
    security_framework::passwords::set_generic_password(
        KEYCHAIN_SERVICE,
        KEYCHAIN_ACCOUNT,
        identity,
    )
    .map_err(|error| format!("could not store the local HTTPS identity in Keychain: {error}"))
}

#[cfg(not(target_os = "macos"))]
fn store_keychain_identity(_identity: &[u8]) -> Result<(), String> {
    Err("local HTTPS identity storage is supported only by the macOS desktop app".into())
}

#[cfg(target_os = "macos")]
pub fn install_webview_trust(
    window: &tauri::WebviewWindow,
    identity: &LocalHttpsIdentity,
) -> Result<(), String> {
    use std::{
        ffi::CString,
        os::raw::c_char,
        sync::{
            atomic::{AtomicI32, Ordering},
            Arc,
        },
    };

    #[link(name = "rcp_https_trust", kind = "static")]
    extern "C" {
        fn rcp_https_trust_install_pin(
            fingerprint_hex: *const c_char,
            webview: *mut libc::c_void,
        ) -> libc::c_int;
    }

    let fingerprint = CString::new(identity.fingerprint_sha256())
        .map_err(|_| "the local HTTPS fingerprint contains an invalid byte".to_string())?;
    let result = Arc::new(AtomicI32::new(-1));
    let callback_result = result.clone();
    window
        .with_webview(move |platform| {
            callback_result.store(
                unsafe { rcp_https_trust_install_pin(fingerprint.as_ptr(), platform.inner()) },
                Ordering::SeqCst,
            );
        })
        .map_err(|error| format!("could not access the RCP WebView for local HTTPS: {error}"))?;
    match result.load(Ordering::SeqCst) {
        0 => Ok(()),
        code => Err(format!(
            "could not install app-scoped local HTTPS trust (native code {code})"
        )),
    }
}

#[cfg(not(target_os = "macos"))]
pub fn install_webview_trust(
    _window: &tauri::WebviewWindow,
    _identity: &LocalHttpsIdentity,
) -> Result<(), String> {
    Err("app-scoped local HTTPS trust is supported only by the macOS desktop app".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generated_identity_round_trips_as_one_bounded_secret_record() {
        let identity = generate_identity().unwrap();
        assert_eq!(identity.fingerprint_sha256.len(), 64);
        assert!(identity
            .fingerprint_sha256
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase()));
        let encoded = encode_identity(&identity).unwrap();
        let decoded = decode_identity(&encoded).unwrap();
        assert_eq!(decoded.certificate_der, identity.certificate_der);
        assert_eq!(decoded.private_key_der, identity.private_key_der);
        assert_eq!(decoded.fingerprint_sha256(), identity.fingerprint_sha256());
    }

    #[test]
    fn identity_decoder_rejects_truncation_versions_and_length_drift() {
        let identity = generate_identity().unwrap();
        let encoded = encode_identity(&identity).unwrap();
        let truncated = encoded[..encoded.len() - 1].to_vec();
        let mut unsupported_version = encoded.to_vec();
        unsupported_version[IDENTITY_HEADER.len()] = IDENTITY_VERSION + 1;
        let mut length_drift = encoded.to_vec();
        length_drift[IDENTITY_HEADER.len() + 1] ^= 1;
        for rejected in [truncated, unsupported_version, length_drift] {
            assert!(decode_identity(&rejected).is_err());
        }
    }

    #[test]
    fn identity_rejects_a_certificate_and_private_key_from_different_pairs() {
        let first = generate_identity().unwrap();
        let second = generate_identity().unwrap();
        assert!(
            identity_from_parts(first.certificate_der, second.private_key_der.to_vec()).is_err()
        );
    }
}
