//! Thread clustering — mirrors Python's threads.py `discover_threads()` logic.
//!
//! Groups sessions into logical threads using project path + time + branch
//! heuristics. Runs inside the daemon after every session write so Python
//! just reads pre-computed clusters from SQLite instead of clustering itself.

use crate::parser::Session;
use std::collections::HashMap;

/// Gap threshold for merging two sessions on the same default branch.
const DEFAULT_BRANCH_GAP_SECS: i64 = 30 * 60; // 30 minutes

const DEFAULT_BRANCHES: &[&str] = &["master", "main", "HEAD", ""];

const GENERIC_BRANCHES: &[&str] = &[
    "master", "main", "HEAD", "develop", "dev", "wip", "temp", "tmp", "test",
    "testing", "feature", "fix", "hotfix", "bugfix", "prod", "production",
    "staging",
];

/// A computed thread cluster ready to be written to SQLite.
#[derive(Debug, Clone)]
pub struct ThreadCluster {
    pub thread_id: String,
    pub name: String,
    pub project_path: String,
    pub session_ids: Vec<String>,
    pub last_activity: String,
}

// ─── Timestamp helpers ────────────────────────────────────────────────────────

/// Parse ISO 8601 timestamp to Unix seconds (UTC).
/// Handles "2024-01-15T10:30:45.123+05:30", "2024-01-15T10:30:45Z", etc.
fn ts_to_unix_secs(ts: &str) -> Option<i64> {
    if ts.len() < 19 {
        return None;
    }
    let year: i64 = ts[0..4].parse().ok()?;
    let month: i64 = ts[5..7].parse().ok()?;
    let day: i64 = ts[8..10].parse().ok()?;
    let hour: i64 = ts[11..13].parse().ok()?;
    let min: i64 = ts[14..16].parse().ok()?;
    let sec: i64 = ts[17..19].parse().ok()?;

    let days = date_to_unix_days(year, month, day);
    let utc = days * 86400 + hour * 3600 + min * 60 + sec;
    let tz = parse_tz_offset_secs(&ts[19..]);
    Some(utc - tz)
}

/// Days since Unix epoch using Howard Hinnant's civil calendar algorithm.
fn date_to_unix_days(year: i64, month: i64, day: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400; // year of era [0, 399]
    let m3 = if month > 2 { month - 3 } else { month + 9 };
    let doy = (153 * m3 + 2) / 5 + day - 1; // day of year [0, 365]
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy; // day of era [0, 146096]
    era * 146097 + doe - 719468
}

/// Parse timezone offset in seconds from the tail of an ISO timestamp.
/// Input: everything after "HH:MM:SS", e.g. ".123+05:30", "Z", "+00:00", "".
fn parse_tz_offset_secs(s: &str) -> i64 {
    // Skip optional fractional seconds
    let s = if s.starts_with('.') {
        let end = s[1..]
            .find(|c: char| !c.is_ascii_digit())
            .map(|i| i + 1)
            .unwrap_or(s.len());
        &s[end..]
    } else {
        s
    };
    let s = s.trim();
    if s.is_empty() || s == "Z" {
        return 0;
    }
    let sign: i64 = if s.starts_with('-') { -1 } else { 1 };
    let rest = &s[1..];
    if rest.len() < 5 {
        return 0;
    }
    let h: i64 = rest[0..2].parse().unwrap_or(0);
    let m: i64 = rest[3..5].parse().unwrap_or(0);
    sign * (h * 3600 + m * 60)
}

// ─── Clustering helpers ───────────────────────────────────────────────────────

fn is_default_branch(b: &str) -> bool {
    DEFAULT_BRANCHES.contains(&b)
}

/// Should session `a` (earlier) and `b` (later) be in the same thread?
/// Mirrors Python's `_should_merge()`.
fn should_merge(a: &Session, b: &Session) -> bool {
    let ba = a.git_branch.as_str();
    let bb = b.git_branch.as_str();
    let a_default = is_default_branch(ba);
    let b_default = is_default_branch(bb);

    // Same non-default branch → always merge (feature branch = intentional)
    if !a_default && !b_default && ba == bb {
        return true;
    }
    // Different non-default branches → never merge
    if !a_default && !b_default {
        return false;
    }
    // One feature, one default → don't merge
    if a_default != b_default {
        return false;
    }
    // Both on default branch → merge only if gap ≤ 30 min
    let ts_a = if !a.last_activity.is_empty() {
        &a.last_activity
    } else {
        &a.started_at
    };
    let ts_b = if !b.started_at.is_empty() {
        &b.started_at
    } else {
        &b.last_activity
    };
    match (ts_to_unix_secs(ts_a), ts_to_unix_secs(ts_b)) {
        (Some(ta), Some(tb)) => (tb - ta).abs() <= DEFAULT_BRANCH_GAP_SECS,
        _ => false,
    }
}

/// Derive a display name for a cluster. Mirrors Python's `_derive_thread_name()`.
fn derive_thread_name(cluster: &[&Session]) -> String {
    // 1. Custom title
    for s in cluster {
        if !s.title.is_empty() {
            return s.title.clone();
        }
    }
    // 2. Non-generic branch name
    let branches: std::collections::HashSet<&str> = cluster
        .iter()
        .map(|s| s.git_branch.as_str())
        .filter(|b| !b.is_empty() && !GENERIC_BRANCHES.contains(b))
        .collect();
    if branches.len() == 1 {
        let branch = *branches.iter().next().unwrap();
        if branch.len() > 30 {
            let truncated = &branch[..30];
            // Trim at last '-'
            return truncated
                .rsplitn(2, '-')
                .last()
                .unwrap_or(truncated)
                .to_string();
        }
        return branch.to_string();
    }
    // 3. First user message from oldest session
    for s in cluster {
        let msg = s.first_message.trim();
        if msg.len() > 5 {
            let first_line = msg.lines().next().unwrap_or("").trim();
            let first_line = first_line.trim_start_matches('#').trim();
            if first_line.len() > 50 {
                return format!("{}...", &first_line[..47]);
            }
            return first_line.to_string();
        }
    }
    String::new()
}

/// Normalize project path: collapse /.claude/worktrees/agent-XXX/ → parent.
fn normalize_path(path: &str) -> &str {
    if let Some(idx) = path.find("/.claude/worktrees/") {
        &path[..idx]
    } else {
        path
    }
}

// ─── Main entry point ─────────────────────────────────────────────────────────

/// Compute thread clusters from a slice of sessions.
/// Mirrors Python's `discover_threads()` (minus file I/O fallbacks, since
/// the Rust parser always populates `git_branch` and `first_message`).
pub fn compute_threads(sessions: &[Session]) -> Vec<ThreadCluster> {
    // Group by normalized project path; skip /subagents and empty sessions
    let mut by_project: HashMap<&str, Vec<&Session>> = HashMap::new();
    for s in sessions {
        if s.project_path == "/subagents" {
            continue;
        }
        let path = normalize_path(&s.project_path);
        by_project.entry(path).or_default().push(s);
    }

    let mut threads: Vec<ThreadCluster> = Vec::new();

    for (project_path, mut proj_sessions) in by_project {
        // Sort chronologically (oldest first for greedy merge)
        proj_sessions.sort_by(|a, b| {
            let ta = if !a.started_at.is_empty() {
                &a.started_at
            } else {
                &a.last_activity
            };
            let tb = if !b.started_at.is_empty() {
                &b.started_at
            } else {
                &b.last_activity
            };
            ta.cmp(tb)
        });

        // Greedy linear merge
        let mut clusters: Vec<Vec<&Session>> = Vec::new();
        let mut current: Vec<&Session> = Vec::new();

        for s in &proj_sessions {
            if current.is_empty() {
                current.push(s);
                continue;
            }
            if should_merge(current.last().unwrap(), s) {
                current.push(s);
            } else {
                clusters.push(std::mem::take(&mut current));
                current = vec![s];
            }
        }
        if !current.is_empty() {
            clusters.push(current);
        }

        for cluster in clusters {
            let thread_id = cluster[0].session_id.clone();
            let name = derive_thread_name(&cluster);
            let last_activity = cluster
                .iter()
                .map(|s| s.last_activity.as_str())
                .max()
                .unwrap_or("")
                .to_string();
            let session_ids: Vec<String> =
                cluster.iter().map(|s| s.session_id.clone()).collect();

            threads.push(ThreadCluster {
                thread_id,
                name,
                project_path: project_path.to_string(),
                session_ids,
                last_activity,
            });
        }
    }

    // Sort: most recent first
    threads.sort_by(|a, b| b.last_activity.cmp(&a.last_activity));
    threads
}

// ─── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_ts_to_unix_secs_utc() {
        // 1970-01-01T00:00:00Z → 0
        assert_eq!(ts_to_unix_secs("1970-01-01T00:00:00Z"), Some(0));
    }

    #[test]
    fn test_ts_to_unix_secs_offset() {
        // 1970-01-01T05:30:00+05:30 → 0 (same instant as epoch)
        assert_eq!(ts_to_unix_secs("1970-01-01T05:30:00+05:30"), Some(0));
    }

    #[test]
    fn test_ts_to_unix_secs_negative_offset() {
        // 1969-12-31T23:00:00-01:00 → 0
        assert_eq!(ts_to_unix_secs("1969-12-31T23:00:00-01:00"), Some(0));
    }

    #[test]
    fn test_ts_fractional() {
        assert_eq!(ts_to_unix_secs("1970-01-01T00:00:01.500Z"), Some(1));
    }

    #[test]
    fn test_should_merge_same_feature_branch() {
        let a = Session {
            session_id: "a".into(),
            git_branch: "feat/foo".into(),
            last_activity: "2024-01-01T00:00:00Z".into(),
            started_at: "2024-01-01T00:00:00Z".into(),
            ..Default::default()
        };
        let b = Session {
            session_id: "b".into(),
            git_branch: "feat/foo".into(),
            started_at: "2024-01-01T12:00:00Z".into(), // 12 hours later
            ..Default::default()
        };
        // Same non-default branch → always merge regardless of gap
        assert!(should_merge(&a, &b));
    }

    #[test]
    fn test_should_merge_default_branch_small_gap() {
        let a = Session {
            session_id: "a".into(),
            git_branch: "main".into(),
            last_activity: "2024-01-01T00:00:00Z".into(),
            started_at: "2024-01-01T00:00:00Z".into(),
            ..Default::default()
        };
        let b = Session {
            session_id: "b".into(),
            git_branch: "main".into(),
            started_at: "2024-01-01T00:20:00Z".into(), // 20 minutes later
            ..Default::default()
        };
        assert!(should_merge(&a, &b));
    }

    #[test]
    fn test_should_not_merge_default_branch_large_gap() {
        let a = Session {
            session_id: "a".into(),
            git_branch: "main".into(),
            last_activity: "2024-01-01T00:00:00Z".into(),
            started_at: "2024-01-01T00:00:00Z".into(),
            ..Default::default()
        };
        let b = Session {
            session_id: "b".into(),
            git_branch: "main".into(),
            started_at: "2024-01-01T01:00:00Z".into(), // 60 minutes later
            ..Default::default()
        };
        assert!(!should_merge(&a, &b));
    }

    #[test]
    fn test_compute_threads_basic() {
        let sessions = vec![
            Session {
                session_id: "s1".into(),
                project_path: "/home/user/proj".into(),
                git_branch: "main".into(),
                started_at: "2024-01-01T00:00:00Z".into(),
                last_activity: "2024-01-01T00:10:00Z".into(),
                message_count: 2,
                ..Default::default()
            },
            Session {
                session_id: "s2".into(),
                project_path: "/home/user/proj".into(),
                git_branch: "main".into(),
                started_at: "2024-01-01T00:20:00Z".into(),
                last_activity: "2024-01-01T00:30:00Z".into(),
                message_count: 2,
                ..Default::default()
            },
            Session {
                session_id: "s3".into(),
                project_path: "/home/user/proj".into(),
                git_branch: "main".into(),
                started_at: "2024-01-01T10:00:00Z".into(), // big gap
                last_activity: "2024-01-01T10:30:00Z".into(),
                message_count: 2,
                ..Default::default()
            },
        ];
        let threads = compute_threads(&sessions);
        // s1+s2 should merge (20min gap), s3 should be separate (9.5h gap)
        assert_eq!(threads.len(), 2);
        let t0 = threads.iter().find(|t| t.session_ids.contains(&"s3".to_string())).unwrap();
        assert_eq!(t0.session_ids.len(), 1);
        let t1 = threads.iter().find(|t| t.session_ids.contains(&"s1".to_string())).unwrap();
        assert_eq!(t1.session_ids.len(), 2);
    }
}
