//! Session discovery — scan ~/.claude/projects/ and detect live sessions.

use crate::parser::{self, Session};
use anyhow::Result;
use std::collections::HashSet;
use std::fs;
use std::path::Path;

/// Project directories whose sessions are not anyone's work.
///
/// Tools that shell out to `claude -p` for a utility purpose — an intent classifier, a
/// summarizer, the work-arcs arc namer — start a real Claude Code session every time,
/// and orch listed each one. A single batch put 67 of them in a workstream, each holding
/// nothing but a meta-prompt, which is also why Claude's own session titler could not
/// name them.
///
/// Those callers run with cwd set to `$XDG_STATE_HOME/claude-headless`, so the whole
/// class is identifiable by the one project directory it lands in. Matching the
/// directory rather than the prompt text means a reworded prompt cannot slip through.
/// Additional directories may be named in `ORCH_IGNORED_PROJECT_DIRS`, comma-separated.
pub fn ignored_project_dirs() -> Vec<String> {
    let state = std::env::var("XDG_STATE_HOME").unwrap_or_else(|_| {
        let home = std::env::var("HOME").unwrap_or_default();
        format!("{home}/.local/state")
    });
    // Claude Code names a project directory after its cwd with every non-alphanumeric
    // character replaced by a dash.
    let headless: String = format!("{state}/claude-headless")
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect();

    let mut dirs = vec![headless];
    if let Ok(extra) = std::env::var("ORCH_IGNORED_PROJECT_DIRS") {
        dirs.extend(
            extra
                .split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(str::to_string),
        );
    }
    dirs
}

/// Whether a JSONL path sits in a project directory orch should not surface.
pub fn is_ignored_path(path: &Path, ignored: &[String]) -> bool {
    path.parent()
        .and_then(|p| p.file_name())
        .map(|n| {
            let n = n.to_string_lossy();
            // The suffix, not only the derived name: a test run under a scratch
            // XDG_STATE_HOME writes headless transcripts to a project dir the
            // current-env derivation cannot name, and five such dirs were found
            // live being offered to the titler.
            ignored.iter().any(|d| d.as_str() == n) || n.ends_with("-claude-headless")
        })
        .unwrap_or(false)
}

/// Discover all sessions from ~/.claude/projects/.
/// Returns sessions sorted by last_activity (most recent first).
pub fn discover_all(projects_dir: &Path) -> Result<Vec<Session>> {
    let mut sessions = Vec::new();

    if !projects_dir.is_dir() {
        return Ok(sessions);
    }

    let ignored = ignored_project_dirs();

    for entry in fs::read_dir(projects_dir)? {
        let entry = entry?;
        let proj_dir = entry.path();
        if !proj_dir.is_dir() {
            continue;
        }
        if proj_dir
            .file_name()
            .map(|n| {
            let n = n.to_string_lossy();
            // The suffix, not only the derived name: a test run under a scratch
            // XDG_STATE_HOME writes headless transcripts to a project dir the
            // current-env derivation cannot name, and five such dirs were found
            // live being offered to the titler.
            ignored.iter().any(|d| d.as_str() == n) || n.ends_with("-claude-headless")
        })
            .unwrap_or(false)
        {
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
