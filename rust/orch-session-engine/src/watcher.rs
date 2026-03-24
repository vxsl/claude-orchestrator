//! Daemon mode — watch files via inotify, maintain SQLite, notify via pipe.

use crate::{db, discovery, parser, threading};
use anyhow::Result;
use notify::{Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::Write;
use std::os::unix::fs::{FileTypeExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::time::{Duration, Instant, UNIX_EPOCH};

fn home_dir() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/"))
}

/// Run the daemon: initial sync, then watch for changes.
pub fn run_daemon(db_path: &str, pipe_path: &str) -> Result<()> {
    let home = home_dir();
    let projects_dir = home.join(".claude").join("projects");
    let sessions_dir = home.join(".claude").join("sessions");
    let db_path = PathBuf::from(db_path);

    if let Some(parent) = db_path.parent() {
        fs::create_dir_all(parent)?;
    }

    let conn = db::open_db(&db_path)?;

    // Create the notification pipe (FIFO) if it doesn't exist
    setup_pipe(pipe_path)?;

    // Phase 1: full initial sync
    eprintln!("orch-session-engine: initial sync...");
    let sessions = discovery::discover_all(&projects_dir)?;
    let live_ids = discovery::get_live_session_ids(&sessions_dir)?;
    db::write_sessions(&conn, &sessions, &live_ids)?;
    let threads = threading::compute_threads(&sessions);
    db::write_threads(&conn, &threads)?;
    eprintln!(
        "orch-session-engine: synced {} sessions ({} live) → {} threads",
        sessions.len(),
        live_ids.len(),
        threads.len(),
    );
    notify_pipe(pipe_path);

    // Build mtime cache from initial sync
    let mut mtime_cache: HashMap<PathBuf, f64> = HashMap::new();
    for s in &sessions {
        let path = PathBuf::from(&s.jsonl_path);
        if let Ok(mtime) = get_mtime(&path) {
            mtime_cache.insert(path, mtime);
        }
    }

    // Phase 2: watch for changes
    let (tx, rx) = mpsc::channel::<Event>();

    let mut watcher = RecommendedWatcher::new(
        move |res: Result<Event, notify::Error>| {
            if let Ok(event) = res {
                let _ = tx.send(event);
            }
        },
        notify::Config::default().with_poll_interval(Duration::from_secs(2)),
    )?;

    // Watch both directories
    if projects_dir.is_dir() {
        watcher.watch(&projects_dir, RecursiveMode::Recursive)?;
        eprintln!(
            "orch-session-engine: watching {}",
            projects_dir.display()
        );
    }
    if sessions_dir.is_dir() {
        watcher.watch(&sessions_dir, RecursiveMode::NonRecursive)?;
        eprintln!(
            "orch-session-engine: watching {}",
            sessions_dir.display()
        );
    }

    // Also do periodic liveness checks (every 10s) and full re-sync (every 60s)
    let mut last_liveness = Instant::now();
    let mut last_full_sync = Instant::now();

    eprintln!("orch-session-engine: daemon ready");

    loop {
        // Drain all pending events (batch them)
        let mut jsonl_changed: HashSet<PathBuf> = HashSet::new();
        let mut session_json_changed = false;

        // Wait for events with a timeout for periodic tasks
        match rx.recv_timeout(Duration::from_secs(5)) {
            Ok(event) => {
                process_event(&event, &mut jsonl_changed, &mut session_json_changed);
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
        }

        // Drain any additional queued events
        while let Ok(event) = rx.try_recv() {
            process_event(&event, &mut jsonl_changed, &mut session_json_changed);
        }

        let mut changed = false;
        let mut sessions_changed = false;

        // Handle JSONL changes: re-parse only the changed files
        for path in &jsonl_changed {
            if let Ok(new_mtime) = get_mtime(path) {
                let old_mtime = mtime_cache.get(path).copied().unwrap_or(0.0);
                if (new_mtime - old_mtime).abs() < 0.001 {
                    continue; // mtime unchanged, skip
                }
                mtime_cache.insert(path.clone(), new_mtime);

                match parser::parse_session(path) {
                    Ok(session) => {
                        if session.message_count >= 1 {
                            let live_ids = discovery::get_live_session_ids(&sessions_dir)
                                .unwrap_or_default();
                            let is_live = session
                                .all_session_ids
                                .iter()
                                .any(|id| live_ids.contains(id))
                                || live_ids.contains(&session.session_id);
                            db::upsert_session(&conn, &session, is_live, new_mtime)?;
                            changed = true;
                            sessions_changed = true;
                        }
                    }
                    Err(_) => continue,
                }
            }
        }

        // Handle session.json changes (start/stop): refresh liveness
        if session_json_changed || last_liveness.elapsed() > Duration::from_secs(10) {
            let live_ids =
                discovery::get_live_session_ids(&sessions_dir).unwrap_or_default();
            if update_all_liveness(&conn, &live_ids)? {
                changed = true;
            }
            last_liveness = Instant::now();
        }

        // Periodic full re-sync to catch missed events
        if last_full_sync.elapsed() > Duration::from_secs(60) {
            let sessions = discovery::discover_all(&projects_dir)?;
            let live_ids =
                discovery::get_live_session_ids(&sessions_dir).unwrap_or_default();
            db::write_sessions(&conn, &sessions, &live_ids)?;
            let threads = threading::compute_threads(&sessions);
            db::write_threads(&conn, &threads)?;
            for s in &sessions {
                let path = PathBuf::from(&s.jsonl_path);
                if let Ok(mtime) = get_mtime(&path) {
                    mtime_cache.insert(path, mtime);
                }
            }
            last_full_sync = Instant::now();
            changed = true;
            sessions_changed = false; // full sync already rebuilt threads
        }

        // After incremental JSONL changes, recompute threads from full sessions table
        if sessions_changed {
            if let Ok(all_sessions) = load_all_sessions(&conn) {
                let threads = threading::compute_threads(&all_sessions);
                let _ = db::write_threads(&conn, &threads);
            }
            // Bump generation so Python knows something changed
            let _ = db::bump_generation_pub(&conn);
        }

        if changed {
            notify_pipe(pipe_path);
        }
    }

    Ok(())
}

fn process_event(event: &Event, jsonl_changed: &mut HashSet<PathBuf>, session_json_changed: &mut bool) {
    match event.kind {
        EventKind::Create(_) | EventKind::Modify(_) | EventKind::Remove(_) => {
            for path in &event.paths {
                if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
                    if ext == "jsonl"
                        && !path
                            .file_name()
                            .unwrap_or_default()
                            .to_string_lossy()
                            .ends_with(".wakatime")
                    {
                        jsonl_changed.insert(path.clone());
                    } else if ext == "json" {
                        // Could be a session start/stop marker
                        *session_json_changed = true;
                    }
                }
            }
        }
        _ => {}
    }
}

/// Returns true if any liveness state actually changed.
fn update_all_liveness(conn: &rusqlite::Connection, live_ids: &HashSet<String>) -> Result<bool> {
    // Get all session IDs and their all_session_ids from DB
    let mut stmt = conn.prepare("SELECT session_id, all_session_ids, is_live FROM sessions")?;
    let rows: Vec<(String, String, bool)> = stmt
        .query_map([], |row| {
            let id: String = row.get(0)?;
            let all_ids: String = row.get(1)?;
            let is_live: bool = row.get(2)?;
            Ok((id, all_ids, is_live))
        })?
        .filter_map(|r| r.ok())
        .collect();

    let mut any_changed = false;
    for (session_id, all_ids_json, was_live) in &rows {
        let all_ids: Vec<String> = serde_json::from_str(all_ids_json).unwrap_or_default();
        let is_live =
            live_ids.contains(session_id) || all_ids.iter().any(|id| live_ids.contains(id));

        if is_live != *was_live {
            conn.execute(
                "UPDATE sessions SET is_live = ? WHERE session_id = ?",
                rusqlite::params![is_live as i32, session_id],
            )?;
            any_changed = true;
        }
    }
    Ok(any_changed)
}

/// Load all sessions from SQLite for thread recomputation after incremental updates.
fn load_all_sessions(conn: &rusqlite::Connection) -> Result<Vec<parser::Session>> {
    use std::collections::HashMap;
    let mut stmt = conn.prepare(
        "SELECT session_id, project_path, title, started_at, last_activity,
                message_count, git_branch, first_message, all_session_ids,
                last_message_role, last_stop_reason, turn_complete, last_tool_name,
                is_live
         FROM sessions WHERE message_count >= 1",
    )?;
    let sessions: Vec<parser::Session> = stmt
        .query_map([], |row| {
            let all_ids_json: String = row.get(8).unwrap_or_default();
            Ok(parser::Session {
                session_id: row.get(0)?,
                project_path: row.get(1)?,
                title: row.get(2).unwrap_or_default(),
                started_at: row.get(3).unwrap_or_default(),
                last_activity: row.get(4).unwrap_or_default(),
                message_count: row.get(5).unwrap_or(0),
                git_branch: row.get(6).unwrap_or_default(),
                first_message: row.get(7).unwrap_or_default(),
                all_session_ids: serde_json::from_str(&all_ids_json).unwrap_or_default(),
                last_message_role: row.get(9).unwrap_or_default(),
                last_stop_reason: row.get(10).unwrap_or_default(),
                turn_complete: row.get::<_, i32>(11).unwrap_or(0) != 0,
                last_tool_name: row.get(12).unwrap_or_default(),
                is_live: row.get::<_, i32>(13).unwrap_or(0) != 0,
                tool_counts: HashMap::new(),
                ..Default::default()
            })
        })?
        .filter_map(|r| r.ok())
        .collect();
    Ok(sessions)
}

fn setup_pipe(pipe_path: &str) -> Result<()> {
    let path = Path::new(pipe_path);
    if path.exists() {
        // Check if it's actually a FIFO
        let meta = fs::metadata(path)?;
        if !meta.file_type().is_fifo() {
            fs::remove_file(path)?;
            nix::unistd::mkfifo(path, nix::sys::stat::Mode::from_bits_truncate(0o600))?;
        }
    } else {
        nix::unistd::mkfifo(path, nix::sys::stat::Mode::from_bits_truncate(0o600))?;
    }
    Ok(())
}

fn notify_pipe(pipe_path: &str) {
    // Non-blocking write to FIFO — if nobody's reading, just skip
    let path = Path::new(pipe_path);
    if !path.exists() {
        return;
    }
    // Open FIFO in non-blocking mode
    match fs::OpenOptions::new()
        .write(true)
        .custom_flags(nix::libc::O_NONBLOCK)
        .open(path)
    {
        Ok(mut f) => {
            let _ = f.write_all(b"1");
        }
        Err(_) => {} // No reader connected, that's fine
    }
}

fn get_mtime(path: &Path) -> Result<f64> {
    let meta = fs::metadata(path)?;
    let mtime = meta
        .modified()?
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    Ok(mtime)
}
