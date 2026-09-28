use std::{
    fs::{self, OpenOptions},
    io::{self, Write},
    path::Path,
    time::{Duration, SystemTime},
};

use tauri::{AppHandle, Manager};

pub const PDF_PREVIEW_MAX_BYTES: usize = 16 * 1024 * 1024;
pub const PDF_PREVIEW_RETENTION: Duration = Duration::from_secs(24 * 60 * 60);

pub fn validate_headers(
    status: reqwest::StatusCode,
    content_type: Option<&str>,
    content_length: Option<u64>,
) -> Result<(), String> {
    if !status.is_success() {
        return Err(format!("PDF download returned HTTP {status}"));
    }
    if !content_type.is_some_and(|value| {
        value
            .split(';')
            .next()
            .unwrap_or_default()
            .trim()
            .eq_ignore_ascii_case("application/pdf")
    }) {
        return Err("artifact download is not a PDF".into());
    }
    if content_length.is_some_and(|size| size > PDF_PREVIEW_MAX_BYTES as u64) {
        return Err("PDF exceeds the preview size limit".into());
    }
    Ok(())
}

pub fn append_chunk(bytes: &mut Vec<u8>, chunk: &[u8]) -> Result<(), String> {
    if chunk.len() > PDF_PREVIEW_MAX_BYTES.saturating_sub(bytes.len()) {
        return Err("PDF exceeds the preview size limit".into());
    }
    bytes.extend_from_slice(chunk);
    Ok(())
}

pub fn validate_bytes(bytes: &[u8]) -> Result<(), String> {
    if bytes.len() > PDF_PREVIEW_MAX_BYTES || !bytes.starts_with(b"%PDF-") {
        return Err("artifact download is not a valid bounded PDF".into());
    }
    Ok(())
}

pub fn prepare_cache(app: &AppHandle) -> Result<std::path::PathBuf, String> {
    let root = app
        .path()
        .app_cache_dir()
        .map_err(|error| error.to_string())?
        .join("pdf-previews");
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(&root).map_err(|error| error.to_string())?;
    if !fs::symlink_metadata(&root)
        .map_err(|error| error.to_string())?
        .is_dir()
    {
        return Err("PDF preview cache is not a directory".into());
    }
    prune(&root, SystemTime::now())
        .map_err(|error| format!("cannot prune PDF previews: {error}"))?;
    Ok(root)
}

/// Remove expired preview directories. Cleanup is best-effort per entry: a copy
/// the system viewer still holds open must not block the next PDF from opening.
pub fn prune(root: &Path, now: SystemTime) -> io::Result<()> {
    for entry in fs::read_dir(root)? {
        let path = match entry {
            Ok(entry) => entry.path(),
            Err(error) => {
                eprintln!("[rcp] skipped unreadable PDF preview entry: {error}");
                continue;
            }
        };
        let expired = fs::symlink_metadata(&path).and_then(|metadata| {
            Ok(metadata.is_dir()
                && now
                    .duration_since(metadata.modified()?)
                    .is_ok_and(|age| age > PDF_PREVIEW_RETENTION))
        });
        match expired {
            Ok(true) => {
                if let Err(error) = fs::remove_dir_all(&path) {
                    eprintln!("[rcp] kept expired PDF preview {}: {error}", path.display());
                }
            }
            Ok(false) => {}
            Err(error) => eprintln!("[rcp] skipped PDF preview {}: {error}", path.display()),
        }
    }
    Ok(())
}

pub fn write(root: &Path, bytes: &[u8]) -> Result<tempfile::TempDir, String> {
    validate_bytes(bytes)?;
    let mut builder = tempfile::Builder::new();
    builder.prefix("pdf-");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        builder.permissions(fs::Permissions::from_mode(0o700));
    }
    let directory = builder
        .tempdir_in(root)
        .map_err(|error| format!("cannot create PDF preview directory: {error}"))?;
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options
        .open(directory.path().join("artifact.pdf"))
        .and_then(|mut file| file.write_all(bytes))
        .map_err(|error| format!("cannot write PDF preview: {error}"))?;
    Ok(directory)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pdf_validation_checks_status_type_size_and_prefix() {
        use reqwest::StatusCode;
        assert!(validate_headers(
            StatusCode::OK,
            Some("application/pdf; charset=binary"),
            None
        )
        .is_ok());
        assert!(validate_headers(StatusCode::NOT_FOUND, Some("application/pdf"), None).is_err());
        for content_type in [None, Some("text/html"), Some("application/pdf-extra")] {
            assert!(validate_headers(StatusCode::OK, content_type, None).is_err());
        }
        assert!(validate_headers(
            StatusCode::OK,
            Some("application/pdf"),
            Some(PDF_PREVIEW_MAX_BYTES as u64 + 1)
        )
        .is_err());
        assert!(validate_bytes(b"%PDF-1.7\n").is_ok());
        assert!(validate_bytes(b"<html>").is_err());
        assert!(validate_bytes(b"").is_err());
        let mut oversized = vec![0; PDF_PREVIEW_MAX_BYTES + 1];
        oversized[..5].copy_from_slice(b"%PDF-");
        assert!(validate_bytes(&oversized).is_err());
        let mut bytes = vec![0; PDF_PREVIEW_MAX_BYTES - 1];
        append_chunk(&mut bytes, &[0]).unwrap();
        assert!(append_chunk(&mut bytes, &[0]).is_err());
        assert_eq!(bytes.len(), PDF_PREVIEW_MAX_BYTES);
    }

    #[test]
    fn private_previews_are_unique_and_pruning_only_removes_expired_directories() {
        let root = tempfile::tempdir().unwrap();
        let first = write(root.path(), b"%PDF-1.7").unwrap();
        let second = write(root.path(), b"%PDF-1.7").unwrap();
        assert_ne!(first.path(), second.path());
        let failed = write(root.path(), b"%PDF-1.7").unwrap();
        let failed_path = failed.path().to_path_buf();
        drop(failed);
        assert!(!failed_path.exists());
        assert_eq!(
            fs::read(first.path().join("artifact.pdf")).unwrap(),
            b"%PDF-1.7"
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::{symlink, PermissionsExt};
            assert_eq!(
                fs::metadata(first.path()).unwrap().permissions().mode() & 0o777,
                0o700
            );
            assert_eq!(
                fs::metadata(first.path().join("artifact.pdf"))
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
            symlink(first.path(), root.path().join("link")).unwrap();
        }
        let now = SystemTime::now();
        fs::File::open(first.path())
            .unwrap()
            .set_times(
                fs::FileTimes::new()
                    .set_modified(now - PDF_PREVIEW_RETENTION - Duration::from_secs(1)),
            )
            .unwrap();
        fs::write(root.path().join("unrelated"), b"keep").unwrap();
        prune(root.path(), now).unwrap();
        assert!(!first.path().exists());
        assert!(second.path().exists());
        assert!(root.path().join("unrelated").exists());
        #[cfg(unix)]
        assert!(fs::symlink_metadata(root.path().join("link"))
            .unwrap()
            .file_type()
            .is_symlink());
    }

    #[cfg(unix)]
    #[test]
    fn a_stale_preview_that_cannot_be_removed_does_not_block_pruning() {
        use std::os::unix::fs::PermissionsExt;
        let root = tempfile::tempdir().unwrap();
        let expired = SystemTime::now() - PDF_PREVIEW_RETENTION - Duration::from_secs(1);
        let stuck = root.path().join("pdf-stuck");
        let locked = stuck.join("locked");
        fs::create_dir_all(&locked).unwrap();
        fs::write(locked.join("artifact.pdf"), b"%PDF-").unwrap();
        // A read-only child makes remove_dir_all fail, like a copy a viewer holds open.
        fs::set_permissions(&locked, fs::Permissions::from_mode(0o500)).unwrap();
        let stale = root.path().join("pdf-stale");
        fs::create_dir(&stale).unwrap();
        for path in [&stuck, &stale] {
            fs::File::open(path)
                .unwrap()
                .set_times(fs::FileTimes::new().set_modified(expired))
                .unwrap();
        }
        let result = prune(root.path(), SystemTime::now());
        fs::set_permissions(&locked, fs::Permissions::from_mode(0o700)).unwrap();
        result.unwrap();
        assert!(stuck.exists());
        assert!(!stale.exists());
    }
}
