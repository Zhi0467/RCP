//! The owned backend's stdout and stderr, kept for the whole run.
//!
//! Startup reads the launcher's output to find its launch result; after that
//! nothing else reads it, so this file is the only record of Uvicorn's access
//! lines and the `rcp.*` warnings, `rcp.client` among them. It sits in the
//! app's log directory beside the launch-failure log. Each line gets a UTC
//! time and its stream, owner sign-in codes are redacted, and the file rotates
//! so the set never exceeds `(BACKEND_LOG_ROTATIONS + 1) * BACKEND_LOG_MAX_BYTES`.
//! Writing is best-effort: a log that cannot be written never affects the run.

use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};

use crate::backend::redact_owner_code;

pub const BACKEND_LOG_FILENAME: &str = "backend.log";
const BACKEND_LOG_MAX_BYTES: u64 = 5 * 1024 * 1024;
const BACKEND_LOG_ROTATIONS: usize = 3;
// A line with no newline yet is written once it reaches this, so one runaway
// line cannot grow memory without bound.
const MAX_LINE_BYTES: usize = 16 * 1024;

#[derive(Clone, Copy)]
pub enum Stream {
    Stdout,
    Stderr,
}

impl Stream {
    fn tag(self) -> &'static str {
        match self {
            Self::Stdout => "out",
            Self::Stderr => "err",
        }
    }
}

pub struct BackendLog {
    path: PathBuf,
    max_bytes: u64,
    rotations: usize,
    file: Option<File>,
    size: u64,
    pending: [Vec<u8>; 2],
}

impl BackendLog {
    pub fn new(directory: &Path) -> Self {
        Self::with_limits(
            directory.join(BACKEND_LOG_FILENAME),
            BACKEND_LOG_MAX_BYTES,
            BACKEND_LOG_ROTATIONS,
        )
    }

    fn with_limits(path: PathBuf, max_bytes: u64, rotations: usize) -> Self {
        Self {
            path,
            max_bytes,
            rotations,
            file: None,
            size: 0,
            pending: [Vec::new(), Vec::new()],
        }
    }

    /// A marker line, so one file can hold several runs and each is findable.
    pub fn note(&mut self, text: &str) {
        self.write_entry(&format!("{} --- {text}\n", utc_now()));
    }

    pub fn record(&mut self, stream: Stream, bytes: &[u8]) {
        let index = stream as usize;
        self.pending[index].extend_from_slice(bytes);
        // The shell plugin ends an event at `\n` or `\r`; a bare `\r` is a
        // progress redraw and ends a line here too.
        while let Some(newline) = self.pending[index]
            .iter()
            .position(|byte| matches!(byte, b'\n' | b'\r'))
        {
            let line = self.pending[index].drain(..=newline).collect::<Vec<_>>();
            self.write_line(stream, &line);
        }
        while self.pending[index].len() >= MAX_LINE_BYTES {
            let line = self.pending[index]
                .drain(..MAX_LINE_BYTES)
                .collect::<Vec<_>>();
            self.write_line(stream, &line);
        }
    }

    /// Writes any unterminated final fragments, then the closing marker.
    pub fn finish(&mut self, text: &str) {
        for stream in [Stream::Stdout, Stream::Stderr] {
            let line = std::mem::take(&mut self.pending[stream as usize]);
            if !line.is_empty() {
                self.write_line(stream, &line);
            }
        }
        self.note(text);
    }

    fn write_line(&mut self, stream: Stream, line: &[u8]) {
        let text = redact_owner_code(line);
        let text = text.trim_end();
        if text.is_empty() {
            return;
        }
        self.write_entry(&format!("{} {} {text}\n", utc_now(), stream.tag()));
    }

    fn write_entry(&mut self, entry: &str) {
        if self.file.is_none() {
            self.open();
        }
        if self.size > 0 && self.size + entry.len() as u64 > self.max_bytes {
            self.rotate();
            self.open();
        }
        let Some(file) = self.file.as_mut() else {
            return;
        };
        if file.write_all(entry.as_bytes()).is_ok() {
            self.size += entry.len() as u64;
        } else {
            self.file = None;
        }
    }

    fn open(&mut self) {
        if let Some(directory) = self.path.parent() {
            let _ = fs::create_dir_all(directory);
        }
        self.file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .ok();
        self.size = self
            .file
            .as_ref()
            .and_then(|file| file.metadata().ok())
            .map_or(0, |metadata| metadata.len());
    }

    /// `backend.log` becomes `.1`, `.1` becomes `.2`, and the oldest is removed.
    fn rotate(&mut self) {
        self.file = None;
        self.size = 0;
        let rotated = |index: usize| PathBuf::from(format!("{}.{index}", self.path.display()));
        let _ = fs::remove_file(rotated(self.rotations));
        for index in (1..self.rotations).rev() {
            let _ = fs::rename(rotated(index), rotated(index + 1));
        }
        if self.rotations > 0 {
            let _ = fs::rename(&self.path, rotated(1));
        } else {
            let _ = fs::remove_file(&self.path);
        }
    }
}

fn utc_now() -> String {
    let now = time::OffsetDateTime::now_utc();
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        now.year(),
        u8::from(now.month()),
        now.day(),
        now.hour(),
        now.minute(),
        now.second()
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn read(path: &Path) -> String {
        fs::read_to_string(path).unwrap_or_default()
    }

    #[test]
    fn rotation_keeps_a_bounded_set_with_the_newest_lines_in_the_live_file() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("logs").join(BACKEND_LOG_FILENAME);
        let max_bytes = 200;
        let mut log = BackendLog::with_limits(path.clone(), max_bytes, 2);
        for index in 0..100 {
            log.record(Stream::Stderr, format!("line {index:03}\n").as_bytes());
        }

        let rotated = |index: usize| PathBuf::from(format!("{}.{index}", path.display()));
        let files = [path.clone(), rotated(1), rotated(2)];
        for file in &files {
            let size = fs::metadata(file).unwrap().len();
            assert!(
                size > 0 && size <= max_bytes,
                "{} is {size}",
                file.display()
            );
        }
        assert!(!rotated(3).exists());
        assert!(read(&path).trim_end().ends_with("err line 099"));
        // Each file holds an older, contiguous run than the one it replaced.
        let first_index = |file: &Path| {
            read(file)
                .lines()
                .next()
                .unwrap()
                .rsplit(' ')
                .next()
                .unwrap()
                .to_string()
        };
        assert!(first_index(&rotated(2)) < first_index(&rotated(1)));
        assert!(first_index(&rotated(1)) < first_index(&path));

        // A new run appends to the live file and still respects the cap.
        let mut next = BackendLog::with_limits(path.clone(), max_bytes, 2);
        next.note("backend started");
        assert!(fs::metadata(&path).unwrap().len() <= max_bytes);
        assert!(read(&path).contains("--- backend started"));
    }

    #[test]
    fn chunks_become_whole_redacted_lines_and_runaway_lines_are_cut() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join(BACKEND_LOG_FILENAME);
        let mut log = BackendLog::with_limits(path.clone(), 1 << 20, 1);
        log.record(Stream::Stdout, b"INFO:     127.0.0.1 - \"GET /api/hea");
        log.record(Stream::Stderr, b"WARNING rcp.client: notice\n");
        log.record(Stream::Stdout, b"lth HTTP/1.1\" 200 OK\r\n");
        log.record(
            Stream::Stdout,
            b"RCP sign-in: http://127.0.0.1:8421/#owner-code=ABCD-EFGH\n",
        );
        log.record(Stream::Stdout, b"{\"owner_sign_in_code\":\"ABCD-EFGH\"}\n");
        log.record(Stream::Stderr, &vec![b'x'; MAX_LINE_BYTES + 10]);
        log.finish("backend exited");

        let text = read(&path);
        let lines = text.lines().collect::<Vec<_>>();
        assert!(lines[0].ends_with(" err WARNING rcp.client: notice"));
        assert!(
            lines[1].ends_with(" out INFO:     127.0.0.1 - \"GET /api/health HTTP/1.1\" 200 OK")
        );
        assert!(!text.contains("ABCD-EFGH"));
        // The runaway line is written at the cap and its remainder at finish.
        assert_eq!(
            lines[4].len() - lines[4].rfind(' ').unwrap() - 1,
            MAX_LINE_BYTES
        );
        assert_eq!(lines[5].rsplit(' ').next().unwrap().len(), 10);
        assert!(lines[6].ends_with("--- backend exited"));
    }
}
