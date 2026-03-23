//! Session discovery — scan ~/.claude/projects/ and detect live sessions.

use crate::parser::{self, Session};
use anyhow::Result;
use std::collections::HashSet;
use std::fs;
use std::path::Path;

/// Discover all sessions from ~/.claude/projects/.
/// Returns sessions sorted by last_activity (most recent first).
pub fn discover_all(projects_dir: &Path) -> Result<Vec<Session>> {
    let mut sessions = Vec::new();

    if !projects_dir.is_dir() {
        return Ok(sessions);
    }

    for entry in fs::read_dir(projects_dir)? {
        let entry = entry?;
        let proj_dir = entry.path();
        if !proj_dir.is_dir() {
            continue;
        }

        for file_entry in fs::read_dir(&proj_dir)? {
            let file_entry = file_entry?;
            let path = file_entry.path();

            if path.extension().and_then(|e| e.to_str()) != Some("jsonl") {
                continue;
            }
            if path
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .ends_with(".wakatime")
            {
                continue;
            }

            match parser::parse_session(&path) {
                Ok(session) => {
                    if session.message_count >= 1 {
                        sessions.push(session);
                    }
                }
                Err(_) => continue,
            }
        }
    }

    // Sort: most recent first
    sessions.sort_by(|a, b| b.last_activity.cmp(&a.last_activity));
    Ok(sessions)
}

/// Read ~/.claude/sessions/*.json to find currently-running session IDs.
/// Verifies PIDs are still alive. Also resolves --resume arguments.
pub fn get_live_session_ids(sessions_dir: &Path) -> Result<HashSet<String>> {
    let mut live = HashSet::new();

    if !sessions_dir.is_dir() {
        return Ok(live);
    }

    for entry in fs::read_dir(sessions_dir)? {
        let entry = entry?;
        let path = entry.path();

        if path.extension().and_then(|e| e.to_str()) != Some("json") {
            continue;
        }

        let content = match fs::read_to_string(&path) {
            Ok(c) => c,
            Err(_) => continue,
        };

        let data: serde_json::Value = match serde_json::from_str(&content) {
            Ok(v) => v,
            Err(_) => continue,
        };

        let pid = match data["pid"].as_i64() {
            Some(p) => p as i32,
            None => continue,
        };

        let session_id = match data["sessionId"].as_str() {
            Some(s) if !s.is_empty() => s.to_string(),
            _ => continue,
        };

        // Check if process is still running
        if !is_process_alive(pid) {
            continue;
        }

        live.insert(session_id);

        // Also add the original session ID if this is a resumed session
        if let Some(original) = get_resumed_session_id(pid) {
            live.insert(original);
        }
    }

    Ok(live)
}

/// Check if a process is still alive via kill(pid, 0).
fn is_process_alive(pid: i32) -> bool {
    use nix::sys::signal::kill;
    use nix::sys::signal::Signal;
    use nix::unistd::Pid;

    kill(Pid::from_raw(pid), Signal::try_from(0).ok()).is_ok()
}

/// Extract the original session ID from a --resume argument in /proc/PID/cmdline.
fn get_resumed_session_id(pid: i32) -> Option<String> {
    let cmdline_path = format!("/proc/{}/cmdline", pid);
    let content = fs::read(&cmdline_path).ok()?;
    let cmdline = String::from_utf8_lossy(&content);
    let args: Vec<&str> = cmdline.split('\0').collect();

    for (i, arg) in args.iter().enumerate() {
        if (*arg == "--resume" || *arg == "--session-id") && i + 1 < args.len() {
            return Some(args[i + 1].to_string());
        }
    }
    None
}
