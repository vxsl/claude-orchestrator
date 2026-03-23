//! JSONL session parser — mirrors sessions.py:parse_session() field-for-field.

use anyhow::Result;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;
use std::fs::File;
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom};
use std::path::Path;

/// Tool name → category mapping (mirrors _TOOL_CATEGORIES in sessions.py)
fn tool_category(name: &str) -> &'static str {
    match name {
        "Edit" | "Write" | "MultiEdit" | "NotebookEdit" => "mutate",
        "Read" => "read",
        "Grep" | "Glob" | "ToolSearch" => "search",
        "Bash" => "bash",
        "Agent" | "TaskCreate" | "TaskUpdate" => "agent",
        _ => "other",
    }
}

/// Regex-free check for commit pattern: [branch sha] message
/// Returns (sha, summary) or None.
fn extract_commit_from_text(text: &str) -> Option<(String, String)> {
    // Pattern: [branch sha] message — sha is 7+ hex chars
    let start = text.find('[')?;
    let end = text[start..].find(']')?;
    let bracket = &text[start + 1..start + end];
    let mut parts = bracket.splitn(2, ' ');
    let _branch = parts.next()?;
    let sha = parts.next()?;
    if sha.len() < 7 || !sha.chars().all(|c| c.is_ascii_hexdigit()) {
        return None;
    }
    let summary = text[start + end + 1..].trim().to_string();
    Some((sha.to_string(), summary))
}

/// Plans directory prefix for filtering mutated files.
fn plans_dir_prefix() -> String {
    if let Some(home) = std::env::var_os("HOME") {
        format!("{}/.claude/plans/", home.to_string_lossy())
    } else {
        "/.claude/plans/".to_string()
    }
}

/// A parsed Claude Code session — field-for-field match with Python's ClaudeSession.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct Session {
    pub session_id: String,
    pub project_dir: String,
    pub project_path: String,
    pub title: String,
    pub started_at: String,
    pub last_activity: String,
    pub total_input_tokens: i64,
    pub total_output_tokens: i64,
    pub message_count: i32,
    pub assistant_message_count: i32,
    pub model: String,
    pub jsonl_path: String,
    pub is_live: bool,
    pub last_message_role: String,
    pub last_user_message_at: String,
    pub last_stop_reason: String,
    pub turn_complete: bool,
    pub all_session_ids: Vec<String>,
    pub last_message_text: String,
    pub last_user_message_text: String,
    pub last_tool_name: String,
    pub last_commit_sha: String,
    pub last_commit_summary: String,
    pub tool_counts: HashMap<String, i32>,
    pub files_mutated: Vec<String>,
    pub git_branch: String,
    pub first_message: String,
    pub context_tokens: i64,
}

/// Decode Claude's project dir name back to a real path.
/// e.g. "-home-kyle-dev-claude-orchestrator" → "/home/kyle/dev/claude-orchestrator"
///
/// Uses filesystem probing to handle ambiguous dashes (path separators vs literal dashes).
pub fn decode_project_dir(dirname: &str) -> String {
    let stripped = dirname.strip_prefix('-').unwrap_or(dirname);
    let raw_parts: Vec<&str> = stripped.split('-').collect();

    // Merge empty parts with following segment (double-dash = dotfile: "--xmonad" → ".xmonad")
    let mut parts: Vec<String> = Vec::new();
    let mut i = 0;
    while i < raw_parts.len() {
        if raw_parts[i].is_empty() && i + 1 < raw_parts.len() {
            parts.push(format!(".{}", raw_parts[i + 1]));
            i += 2;
        } else {
            parts.push(raw_parts[i].to_string());
            i += 1;
        }
    }

    // Greedy filesystem probing: try to reconstruct the path
    let mut reconstructed = String::from("/");
    let mut remaining = parts.as_slice();

    while !remaining.is_empty() {
        // Try single segment
        let candidate = format!(
            "{}{}{}",
            reconstructed,
            if reconstructed.ends_with('/') { "" } else { "/" },
            remaining[0]
        );
        if Path::new(&candidate).exists() {
            reconstructed = candidate;
            remaining = &remaining[1..];
            continue;
        }

        // Try joining with next segments (dash in filename)
        if remaining.len() > 1 {
            let mut found = false;
            for n in 2..std::cmp::min(remaining.len() + 1, 8) {
                let joined: String = remaining[..n].join("-");
                let candidate = format!(
                    "{}{}{}",
                    reconstructed,
                    if reconstructed.ends_with('/') { "" } else { "/" },
                    joined
                );
                if Path::new(&candidate).exists() {
                    reconstructed = candidate;
                    remaining = &remaining[n..];
                    found = true;
                    break;
                }
            }
            if found {
                continue;
            }
        }

        // Give up on smart reconstruction — join the rest
        let rest: String = remaining.join("-");
        reconstructed = format!(
            "{}{}{}",
            reconstructed,
            if reconstructed.ends_with('/') { "" } else { "/" },
            rest
        );
        break;
    }

    reconstructed
}

/// Extract first text block from message content (truncated to 200 chars).
fn extract_message_text(msg: &Value) -> String {
    let content = &msg["content"];
    if let Some(s) = content.as_str() {
        let collapsed: String = s.split_whitespace().collect::<Vec<_>>().join(" ");
        truncate(&collapsed, 200)
    } else if let Some(arr) = content.as_array() {
        for block in arr {
            if block["type"].as_str() == Some("text") {
                let t = block["text"].as_str().unwrap_or("");
                if !t.is_empty() && !t.contains("[Request interrupted") {
                    let collapsed: String = t.split_whitespace().collect::<Vec<_>>().join(" ");
                    return truncate(&collapsed, 200);
                }
            }
        }
        String::new()
    } else {
        String::new()
    }
}

/// True if this user message contains a real human prompt (text block), not just tool_results.
fn is_human_turn(msg: &Value) -> bool {
    let content = &msg["content"];
    if let Some(s) = content.as_str() {
        return !s.trim().is_empty();
    }
    if let Some(arr) = content.as_array() {
        for block in arr {
            if block["type"].as_str() == Some("text") {
                let t = block["text"].as_str().unwrap_or("");
                if !t.is_empty() && !t.contains("[Request interrupted") {
                    return true;
                }
            }
        }
    }
    false
}

/// True if this user message is a "[Request interrupted…]" marker.
fn is_interrupt_marker(msg: &Value) -> bool {
    let content = &msg["content"];
    if let Some(s) = content.as_str() {
        return s.contains("[Request interrupted");
    }
    if let Some(arr) = content.as_array() {
        for block in arr {
            if block["type"].as_str() == Some("text") {
                if block["text"]
                    .as_str()
                    .unwrap_or("")
                    .contains("[Request interrupted")
                {
                    return true;
                }
            }
        }
    }
    false
}

/// Return the name of the last tool_use block in an assistant message.
fn last_tool_name(msg: &Value) -> String {
    let mut name = String::new();
    if let Some(arr) = msg["content"].as_array() {
        for block in arr {
            if block["type"].as_str() == Some("tool_use") {
                if let Some(n) = block["name"].as_str() {
                    name = n.to_string();
                }
            }
        }
    }
    name
}

/// True if the last Bash tool_use contains "git commit".
fn last_bash_has_commit(msg: &Value) -> bool {
    let mut has = false;
    if let Some(arr) = msg["content"].as_array() {
        for block in arr {
            if block["type"].as_str() == Some("tool_use") && block["name"].as_str() == Some("Bash")
            {
                let cmd = block["input"]["command"].as_str().unwrap_or("");
                has = cmd.contains("git commit");
            }
        }
    }
    has
}

/// Extract (sha, summary) from a tool_result following a git commit.
fn extract_commit_from_result(msg: &Value) -> Option<(String, String)> {
    if let Some(arr) = msg["content"].as_array() {
        for block in arr {
            if block["type"].as_str() != Some("tool_result") {
                continue;
            }
            let content = &block["content"];
            let texts: Vec<&str> = if let Some(s) = content.as_str() {
                vec![s]
            } else if let Some(arr) = content.as_array() {
                arr.iter()
                    .filter(|item| item["type"].as_str() == Some("text"))
                    .filter_map(|item| item["text"].as_str())
                    .collect()
            } else {
                continue;
            };
            for text in texts {
                if let Some(result) = extract_commit_from_text(text) {
                    return Some(result);
                }
            }
        }
    }
    None
}

fn truncate(s: &str, max: usize) -> String {
    if s.len() <= max {
        s.to_string()
    } else {
        // Truncate at char boundary
        let mut end = max;
        while end > 0 && !s.is_char_boundary(end) {
            end -= 1;
        }
        s[..end].to_string()
    }
}

/// Parse a JSONL session file into a Session struct.
/// Mirrors sessions.py:parse_session() line-for-line.
pub fn parse_session(jsonl_path: &Path) -> Result<Session> {
    let stem = jsonl_path
        .file_stem()
        .unwrap_or_default()
        .to_string_lossy();
    if stem.ends_with(".wakatime") {
        anyhow::bail!("wakatime file, skipping");
    }

    let project_dir = jsonl_path
        .parent()
        .and_then(|p| p.file_name())
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_default();

    let project_path = decode_project_dir(&project_dir);
    let plans_prefix = plans_dir_prefix();

    let mut session = Session {
        session_id: stem.to_string(),
        project_dir,
        project_path,
        jsonl_path: jsonl_path.to_string_lossy().to_string(),
        ..Default::default()
    };

    let file = File::open(jsonl_path)?;
    let reader = BufReader::new(file);

    let mut first_ts: Option<String> = None;
    let mut last_ts: Option<String> = None;
    let mut pending_commit = false;
    let mut line_num: usize = 0;

    for line_result in reader.lines() {
        let line = match line_result {
            Ok(l) => l,
            Err(_) => continue,
        };
        let line = line.trim().to_string();
        if line.is_empty() {
            continue;
        }
        line_num += 1;

        let data: Value = match serde_json::from_str(&line) {
            Ok(v) => v,
            Err(_) => continue,
        };

        let msg_type = data["type"].as_str().unwrap_or("");

        // Extract title
        if msg_type == "custom-title" {
            if let Some(t) = data["customTitle"].as_str() {
                session.title = t.to_string();
            }
            if session.session_id.is_empty() {
                if let Some(sid) = data["sessionId"].as_str() {
                    session.session_id = sid.to_string();
                }
            }
        }

        // Extract session ID (first one wins as primary, track all)
        if let Some(sid) = data["sessionId"].as_str() {
            if !sid.is_empty() {
                if session.session_id.is_empty() {
                    session.session_id = sid.to_string();
                }
                if !session.all_session_ids.contains(&sid.to_string()) {
                    session.all_session_ids.push(sid.to_string());
                }
            }
        }

        // Extract git branch from early lines
        if line_num <= 10 && session.git_branch.is_empty() {
            if let Some(branch) = data["gitBranch"].as_str() {
                if !branch.is_empty() && branch != "HEAD" {
                    session.git_branch = branch.to_string();
                }
            }
        }

        // Extract first user message from early lines
        if line_num <= 20 && session.first_message.is_empty() && msg_type == "user" {
            if let Some(msg) = data.get("message") {
                let content = &msg["content"];
                if let Some(s) = content.as_str() {
                    session.first_message = truncate(s, 200);
                } else if let Some(arr) = content.as_array() {
                    for block in arr {
                        if block["type"].as_str() == Some("text") {
                            let t = block["text"].as_str().unwrap_or("");
                            if !t.is_empty() && !t.contains("[Request interrupted") {
                                session.first_message = truncate(t, 200);
                                break;
                            }
                        }
                    }
                }
            }
        }

        // Track timestamps
        if let Some(ts) = data["timestamp"].as_str() {
            if !ts.is_empty() {
                if first_ts.is_none() {
                    first_ts = Some(ts.to_string());
                }
                last_ts = Some(ts.to_string());
            }
        }

        // Track last message role
        if msg_type == "user" || msg_type == "assistant" {
            session.turn_complete = false;

            if msg_type == "user" {
                session.last_stop_reason.clear();
                session.last_tool_name.clear();
                if let Some(ts) = data["timestamp"].as_str() {
                    if !ts.is_empty() {
                        session.last_user_message_at = ts.to_string();
                    }
                }
            }

            // User replied → commit is no longer the last word
            if msg_type == "user" {
                if let Some(msg) = data.get("message") {
                    if is_human_turn(msg) {
                        session.last_commit_sha.clear();
                        session.last_commit_summary.clear();
                    }
                }
            }

            if let Some(msg) = data.get("message") {
                let snippet = extract_message_text(msg);
                if !snippet.is_empty() {
                    session.last_message_role = msg_type.to_string();
                    session.last_message_text = snippet.clone();
                    if msg_type == "user" && is_human_turn(msg) {
                        session.last_user_message_text = snippet;
                    }
                }
            }

            // Interrupt marker
            if msg_type == "user" {
                if let Some(msg) = data.get("message") {
                    if is_interrupt_marker(msg) {
                        session.turn_complete = true;
                    }
                }
            }
        }

        // Turn completion signals
        if (msg_type == "system"
            && matches!(
                data["subtype"].as_str(),
                Some("turn_duration") | Some("stop_hook_summary")
            ))
            || matches!(
                msg_type,
                "last-prompt" | "custom-title" | "file-history-snapshot"
            )
        {
            session.turn_complete = true;
        }

        // Count messages
        if msg_type == "user" {
            session.message_count += 1;
        }
        if msg_type == "assistant" {
            session.assistant_message_count += 1;
        }

        // Extract commit SHA from tool_result after a git commit
        if pending_commit && msg_type == "user" {
            if let Some(msg) = data.get("message") {
                if let Some((sha, summary)) = extract_commit_from_result(msg) {
                    session.last_commit_sha = sha;
                    session.last_commit_summary = summary;
                }
            }
            pending_commit = false;
        }

        // Extract usage and stop_reason from assistant messages
        if msg_type == "assistant" {
            if let Some(msg) = data.get("message") {
                let usage = &msg["usage"];
                let inp = usage["input_tokens"].as_i64().unwrap_or(0);
                let cache_create = usage["cache_creation_input_tokens"].as_i64().unwrap_or(0);
                let cache_read = usage["cache_read_input_tokens"].as_i64().unwrap_or(0);
                session.total_input_tokens += inp + cache_create + cache_read;
                session.total_output_tokens += usage["output_tokens"].as_i64().unwrap_or(0);
                session.context_tokens = inp + cache_create + cache_read;

                session.last_stop_reason =
                    msg["stop_reason"].as_str().unwrap_or("").to_string();
                session.last_tool_name = last_tool_name(msg);
                pending_commit = last_bash_has_commit(msg);

                if session.model.is_empty() {
                    if let Some(m) = msg["model"].as_str() {
                        if !m.is_empty() {
                            session.model = m.to_string();
                        }
                    }
                }

                // Scan content blocks for tool usage
                if let Some(arr) = msg["content"].as_array() {
                    for block in arr {
                        if block["type"].as_str() == Some("tool_use") {
                            let name = block["name"].as_str().unwrap_or("");
                            let cat = tool_category(name);
                            *session.tool_counts.entry(cat.to_string()).or_insert(0) += 1;

                            if cat == "mutate" {
                                let fp = block["input"]["file_path"].as_str().unwrap_or("");
                                if !fp.is_empty() && !fp.starts_with(&plans_prefix) {
                                    if let Some(basename) = Path::new(fp).file_name() {
                                        let bn = basename.to_string_lossy().to_string();
                                        if !session.files_mutated.contains(&bn) {
                                            session.files_mutated.push(bn);
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    session.started_at = first_ts.clone().unwrap_or_default();
    session.last_activity = last_ts.or(first_ts).unwrap_or_default();

    Ok(session)
}

/// Tail-read the last `tail_bytes` of a session file and update mutable fields.
/// Returns true if any tracked field changed.
pub fn refresh_session_tail(session: &mut Session, tail_bytes: u64) -> Result<bool> {
    let path = Path::new(&session.jsonl_path);
    if !path.exists() {
        return Ok(false);
    }

    let old_role = session.last_message_role.clone();
    let old_activity = session.last_activity.clone();
    let old_stop = session.last_stop_reason.clone();

    let mut file = File::open(path)?;
    let size = file.metadata()?.len();
    let offset = size.saturating_sub(tail_bytes);

    let mut content = String::new();
    if offset > 0 {
        file.seek(SeekFrom::Start(offset))?;
        // Skip partial first line
        let mut reader = BufReader::new(&mut file);
        let mut discard = String::new();
        reader.read_line(&mut discard)?;
        reader.read_to_string(&mut content)?;
    } else {
        file.read_to_string(&mut content)?;
    }

    let mut pending_commit = false;

    for line in content.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let data: Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(_) => continue,
        };

        if let Some(ts) = data["timestamp"].as_str() {
            if !ts.is_empty() {
                session.last_activity = ts.to_string();
            }
        }

        let msg_type = data["type"].as_str().unwrap_or("");

        // Extract commit SHA from tool_result after a git commit
        if pending_commit && msg_type == "user" {
            if let Some(msg) = data.get("message") {
                if let Some((sha, summary)) = extract_commit_from_result(msg) {
                    session.last_commit_sha = sha;
                    session.last_commit_summary = summary;
                }
            }
            pending_commit = false;
        }

        if msg_type == "user" || msg_type == "assistant" {
            session.turn_complete = false;
            if msg_type == "user" {
                session.last_stop_reason.clear();
                session.last_tool_name.clear();
                if let Some(ts) = data["timestamp"].as_str() {
                    if !ts.is_empty() {
                        session.last_user_message_at = ts.to_string();
                    }
                }
            }
            if msg_type == "user" {
                if let Some(msg) = data.get("message") {
                    if is_human_turn(msg) {
                        session.last_commit_sha.clear();
                        session.last_commit_summary.clear();
                    }
                }
            }
            if let Some(msg) = data.get("message") {
                let snippet = extract_message_text(msg);
                if !snippet.is_empty() {
                    session.last_message_role = msg_type.to_string();
                    session.last_message_text = snippet.clone();
                    if msg_type == "user" && is_human_turn(msg) {
                        session.last_user_message_text = snippet;
                    }
                }
            }
            if msg_type == "user" {
                if let Some(msg) = data.get("message") {
                    if is_interrupt_marker(msg) {
                        session.turn_complete = true;
                    }
                }
            }
        }

        if msg_type == "assistant" {
            if let Some(msg) = data.get("message") {
                session.last_stop_reason =
                    msg["stop_reason"].as_str().unwrap_or("").to_string();
                session.last_tool_name = last_tool_name(msg);
                pending_commit = last_bash_has_commit(msg);
                let usage = &msg["usage"];
                let inp = usage["input_tokens"].as_i64().unwrap_or(0);
                let cache_create = usage["cache_creation_input_tokens"].as_i64().unwrap_or(0);
                let cache_read = usage["cache_read_input_tokens"].as_i64().unwrap_or(0);
                let ctx = inp + cache_create + cache_read;
                if ctx > 0 {
                    session.context_tokens = ctx;
                }
            }
        }

        if (msg_type == "system"
            && matches!(
                data["subtype"].as_str(),
                Some("turn_duration") | Some("stop_hook_summary")
            ))
            || matches!(
                msg_type,
                "last-prompt" | "custom-title" | "file-history-snapshot"
            )
        {
            session.turn_complete = true;
        }

        // Track new session IDs from resumed sessions
        if let Some(sid) = data["sessionId"].as_str() {
            if !sid.is_empty() && !session.all_session_ids.contains(&sid.to_string()) {
                session.all_session_ids.push(sid.to_string());
            }
        }
    }

    Ok(session.last_message_role != old_role
        || session.last_activity != old_activity
        || session.last_stop_reason != old_stop)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_decode_project_dir_simple() {
        // This test is filesystem-dependent; it falls through to naive join
        let result = decode_project_dir("-tmp-test-project");
        // Should at least start with /
        assert!(result.starts_with('/'));
    }

    #[test]
    fn test_decode_project_dir_dotfile() {
        let result = decode_project_dir("-home-user--config");
        assert!(result.contains(".config"));
    }

    #[test]
    fn test_extract_commit_from_text() {
        let text = "[main abc1234] Fix the bug\nSome other output";
        let result = extract_commit_from_text(text);
        assert_eq!(
            result,
            Some(("abc1234".to_string(), "Fix the bug".to_string()))
        );
    }

    #[test]
    fn test_extract_commit_no_match() {
        assert_eq!(extract_commit_from_text("no commit here"), None);
    }

    #[test]
    fn test_truncate() {
        assert_eq!(truncate("hello", 10), "hello");
        assert_eq!(truncate("hello world", 5), "hello");
    }

    #[test]
    fn test_tool_category() {
        assert_eq!(tool_category("Edit"), "mutate");
        assert_eq!(tool_category("Bash"), "bash");
        assert_eq!(tool_category("Unknown"), "other");
    }
}
