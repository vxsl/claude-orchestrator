mod db;
mod discovery;
mod parser;
mod threading;
mod watcher;

use anyhow::Result;
use clap::{Parser, Subcommand};
use std::path::PathBuf;

#[derive(Parser)]
#[command(name = "orch-session-engine", about = "Fast session discovery daemon for claude-orchestrator")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Run the daemon: watch files, parse sessions, serve via SQLite + pipe
    Daemon {
        /// Path to the SQLite database
        #[arg(long, default_value_t = default_db_path())]
        db: String,

        /// Path to the notification pipe (fifo)
        #[arg(long, default_value_t = default_pipe_path())]
        pipe: String,
    },

    /// One-shot: parse all sessions and write to SQLite, then exit
    Sync {
        /// Path to the SQLite database
        #[arg(long, default_value_t = default_db_path())]
        db: String,
    },

    /// One-shot: parse a single JSONL file and print JSON to stdout
    Parse {
        /// Path to the JSONL file
        path: PathBuf,
    },
}

fn default_db_path() -> String {
    let cache = cache_dir().join("orch-sessions.db");
    cache.to_string_lossy().into_owned()
}

fn default_pipe_path() -> String {
    let uid = nix::unistd::getuid();
    format!("/tmp/orch-session-engine.{}.pipe", uid)
}

fn cache_dir() -> PathBuf {
    if let Ok(dir) = std::env::var("XDG_CACHE_HOME") {
        PathBuf::from(dir).join("claude-orchestrator")
    } else if let Some(home) = home_dir() {
        home.join(".cache").join("claude-orchestrator")
    } else {
        PathBuf::from("/tmp")
    }
}

fn home_dir() -> Option<PathBuf> {
    std::env::var_os("HOME").map(PathBuf::from)
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Parse { path } => {
            let session = parser::parse_session(&path)?;
            let json = serde_json::to_string_pretty(&session)?;
            println!("{json}");
        }
        Command::Sync { db } => {
            let db_path = PathBuf::from(&db);
            if let Some(parent) = db_path.parent() {
                std::fs::create_dir_all(parent)?;
            }
            let conn = db::open_db(&db_path)?;
            let home = home_dir().unwrap_or_default();
            let projects_dir = home.join(".claude").join("projects");
            let sessions_dir = home.join(".claude").join("sessions");
            let sessions = discovery::discover_all(&projects_dir)?;
            let live_ids = discovery::get_live_session_ids(&sessions_dir)?;
            db::write_sessions(&conn, &sessions, &live_ids)?;
            eprintln!("Synced {} sessions ({} live) to {}", sessions.len(), live_ids.len(), db);
        }
        Command::Daemon { db, pipe } => {
            watcher::run_daemon(&db, &pipe)?;
        }
    }
    Ok(())
}
