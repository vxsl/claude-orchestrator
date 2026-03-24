//! SQLite persistence layer — WAL mode for concurrent read/write.

use crate::parser::Session;
use anyhow::Result;
use rusqlite::{params, Connection};
use std::collections::HashSet;
use std::path::Path;

/// Open (or create) the SQLite database with WAL mode.
pub fn open_db(path: &Path) -> Result<Connection> {
    let conn = Connection::open(path)?;
    conn.execute_batch(
        "
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous = NORMAL;
        PRAGMA busy_timeout = 5000;
        PRAGMA cache_size = -8000;
        ",
    )?;
    create_tables(&conn)?;
    migrate(&conn)?;
    Ok(conn)
}

/// Add columns that didn't exist in older schema versions.
fn migrate(conn: &Connection) -> Result<()> {
    let _ = conn.execute(
        "ALTER TABLE sessions ADD COLUMN total_work_ms INTEGER NOT NULL DEFAULT 0",
        [],
    );
    Ok(())
}

fn create_tables(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        "
        CREATE TABLE IF NOT EXISTS sessions (
            session_id      TEXT PRIMARY KEY,
            project_dir     TEXT NOT NULL,
            project_path    TEXT NOT NULL,
            title           TEXT NOT NULL DEFAULT '',
            started_at      TEXT NOT NULL DEFAULT '',
            last_activity   TEXT NOT NULL DEFAULT '',
            total_input_tokens  INTEGER NOT NULL DEFAULT 0,
            total_output_tokens INTEGER NOT NULL DEFAULT 0,
            message_count       INTEGER NOT NULL DEFAULT 0,
            assistant_message_count INTEGER NOT NULL DEFAULT 0,
            model               TEXT NOT NULL DEFAULT '',
            jsonl_path          TEXT NOT NULL DEFAULT '',
            is_live             INTEGER NOT NULL DEFAULT 0,
            last_message_role   TEXT NOT NULL DEFAULT '',
            last_user_message_at TEXT NOT NULL DEFAULT '',
            last_stop_reason    TEXT NOT NULL DEFAULT '',
            turn_complete       INTEGER NOT NULL DEFAULT 0,
            all_session_ids     TEXT NOT NULL DEFAULT '[]',
            last_message_text   TEXT NOT NULL DEFAULT '',
            last_user_message_text TEXT NOT NULL DEFAULT '',
            last_tool_name      TEXT NOT NULL DEFAULT '',
            last_commit_sha     TEXT NOT NULL DEFAULT '',
            last_commit_summary TEXT NOT NULL DEFAULT '',
            tool_counts         TEXT NOT NULL DEFAULT '{}',
            files_mutated       TEXT NOT NULL DEFAULT '[]',
            git_branch          TEXT NOT NULL DEFAULT '',
            first_message       TEXT NOT NULL DEFAULT '',
            context_tokens      INTEGER NOT NULL DEFAULT 0,
            total_work_ms       INTEGER NOT NULL DEFAULT 0,
            mtime               REAL NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_dir);
        CREATE INDEX IF NOT EXISTS idx_sessions_activity ON sessions(last_activity);
        CREATE INDEX IF NOT EXISTS idx_sessions_live ON sessions(is_live);

        -- Generation counter: bumped on every write, Python polls this.
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT OR IGNORE INTO meta (key, value) VALUES ('generation', '0');
        ",
    )?;
    Ok(())
}

/// Bump the generation counter (called after writes).
fn bump_generation(conn: &Connection) -> Result<()> {
    conn.execute(
        "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'generation'",
        [],
    )?;
    Ok(())
}

/// Write a batch of sessions to the database, setting liveness flags.
pub fn write_sessions(
    conn: &Connection,
    sessions: &[Session],
    live_ids: &HashSet<String>,
) -> Result<()> {
    let tx = conn.unchecked_transaction()?;

    for session in sessions {
        let is_live = session
            .all_session_ids
            .iter()
            .any(|id| live_ids.contains(id))
            || live_ids.contains(&session.session_id);

        let mtime = std::fs::metadata(&session.jsonl_path)
            .and_then(|m| m.modified())
            .map(|t| {
                t.duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_secs_f64()
            })
            .unwrap_or(0.0);

        upsert_session(&tx, session, is_live, mtime)?;
    }

    // Remove sessions from DB that no longer exist on disk
    let stale: Vec<String> = {
        let mut stmt = tx.prepare("SELECT session_id, jsonl_path FROM sessions")?;
        stmt.query_map([], |row| {
            let id: String = row.get(0)?;
            let path: String = row.get(1)?;
            Ok((id, path))
        })?
        .filter_map(|r| r.ok())
        .filter(|(_, path)| !Path::new(path).exists())
        .map(|(id, _)| id)
        .collect()
    };

    for id in &stale {
        tx.execute("DELETE FROM sessions WHERE session_id = ?", params![id])?;
    }

    bump_generation(&tx)?;
    tx.commit()?;
    Ok(())
}

/// Upsert a single session into the database.
pub fn upsert_session(
    conn: &Connection,
    session: &Session,
    is_live: bool,
    mtime: f64,
) -> Result<()> {
    let all_ids = serde_json::to_string(&session.all_session_ids)?;
    let tool_counts = serde_json::to_string(&session.tool_counts)?;
    let files_mutated = serde_json::to_string(&session.files_mutated)?;

    conn.execute(
        "INSERT INTO sessions (
            session_id, project_dir, project_path, title, started_at, last_activity,
            total_input_tokens, total_output_tokens, message_count, assistant_message_count,
            model, jsonl_path, is_live, last_message_role, last_user_message_at,
            last_stop_reason, turn_complete, all_session_ids, last_message_text,
            last_user_message_text, last_tool_name, last_commit_sha, last_commit_summary,
            tool_counts, files_mutated, git_branch, first_message, context_tokens,
            total_work_ms, mtime
        ) VALUES (
            ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10,
            ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19,
            ?20, ?21, ?22, ?23, ?24, ?25, ?26, ?27, ?28, ?29, ?30
        )
        ON CONFLICT(session_id) DO UPDATE SET
            project_dir = excluded.project_dir,
            project_path = excluded.project_path,
            title = excluded.title,
            started_at = excluded.started_at,
            last_activity = excluded.last_activity,
            total_input_tokens = excluded.total_input_tokens,
            total_output_tokens = excluded.total_output_tokens,
            message_count = excluded.message_count,
            assistant_message_count = excluded.assistant_message_count,
            model = excluded.model,
            jsonl_path = excluded.jsonl_path,
            is_live = excluded.is_live,
            last_message_role = excluded.last_message_role,
            last_user_message_at = excluded.last_user_message_at,
            last_stop_reason = excluded.last_stop_reason,
            turn_complete = excluded.turn_complete,
            all_session_ids = excluded.all_session_ids,
            last_message_text = excluded.last_message_text,
            last_user_message_text = excluded.last_user_message_text,
            last_tool_name = excluded.last_tool_name,
            last_commit_sha = excluded.last_commit_sha,
            last_commit_summary = excluded.last_commit_summary,
            tool_counts = excluded.tool_counts,
            files_mutated = excluded.files_mutated,
            git_branch = excluded.git_branch,
            first_message = excluded.first_message,
            context_tokens = excluded.context_tokens,
            total_work_ms = excluded.total_work_ms,
            mtime = excluded.mtime",
        params![
            session.session_id,
            session.project_dir,
            session.project_path,
            session.title,
            session.started_at,
            session.last_activity,
            session.total_input_tokens,
            session.total_output_tokens,
            session.message_count,
            session.assistant_message_count,
            session.model,
            session.jsonl_path,
            is_live as i32,
            session.last_message_role,
            session.last_user_message_at,
            session.last_stop_reason,
            session.turn_complete as i32,
            all_ids,
            session.last_message_text,
            session.last_user_message_text,
            session.last_tool_name,
            session.last_commit_sha,
            session.last_commit_summary,
            tool_counts,
            files_mutated,
            session.git_branch,
            session.first_message,
            session.context_tokens,
            session.total_work_ms,
            mtime,
        ],
    )?;
    Ok(())
}

/// Update only liveness-related fields for sessions (used during tail refresh).
pub fn update_liveness(
    conn: &Connection,
    session_id: &str,
    is_live: bool,
    last_activity: &str,
    last_message_role: &str,
    last_stop_reason: &str,
    turn_complete: bool,
    last_tool_name: &str,
    last_message_text: &str,
    last_user_message_text: &str,
    last_user_message_at: &str,
    last_commit_sha: &str,
    last_commit_summary: &str,
    context_tokens: i64,
) -> Result<()> {
    conn.execute(
        "UPDATE sessions SET
            is_live = ?2,
            last_activity = ?3,
            last_message_role = ?4,
            last_stop_reason = ?5,
            turn_complete = ?6,
            last_tool_name = ?7,
            last_message_text = ?8,
            last_user_message_text = ?9,
            last_user_message_at = ?10,
            last_commit_sha = ?11,
            last_commit_summary = ?12,
            context_tokens = ?13
        WHERE session_id = ?1",
        params![
            session_id,
            is_live as i32,
            last_activity,
            last_message_role,
            last_stop_reason,
            turn_complete as i32,
            last_tool_name,
            last_message_text,
            last_user_message_text,
            last_user_message_at,
            last_commit_sha,
            last_commit_summary,
            context_tokens,
        ],
    )?;
    bump_generation(conn)?;
    Ok(())
}
