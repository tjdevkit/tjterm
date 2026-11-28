use anyhow::{Result, ensure};
use std::{path::Path, process::Command};
use tj_listener::store::Notice;

pub(crate) fn prepare(notice: &Notice, state: &Path, python: &Path) -> Result<()> {
    // An unloaded thread needs a running server for queue --resume. The native
    // daemon command is idempotent and does not submit a turn.
    let server = Command::new("codex")
        .args(["app-server", "daemon", "start"])
        .env("CODEX_HOME", &notice.profile)
        .output()?;
    if !server.status.success() {
        eprintln!(
            "Codex daemon startup: {}",
            String::from_utf8_lossy(&server.stderr).trim()
        );
    }
    let check = Command::new(python)
        .args(["-I", "-m", "tjentic.watch.preflight"])
        .arg(state)
        .arg(notice.item_key())
        .arg(&notice.thread)
        .env("CODEX_HOME", &notice.profile)
        .output()?;
    ensure!(
        check.status.success(),
        "Codex wake preflight failed: {}",
        String::from_utf8_lossy(&check.stderr).trim()
    );
    Ok(())
}

pub(crate) fn send(notice: &Notice) -> Result<&'static str> {
    let text = format!(
        "TJ workflow observation {} for item {:?}. {} PR {} at {}. Review this under the item's existing authorization.",
        notice.id, notice.item, notice.reason, notice.pr, notice.revision
    );
    let output = Command::new("codex")
        .args([
            "queue",
            "--thread",
            &notice.thread,
            "--resume",
            "--message",
            &text,
        ])
        .env("CODEX_HOME", &notice.profile)
        .output()?;
    ensure!(
        output.status.success(),
        "Codex queue failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    // A resume warning still means the message was queued. Resending could start another turn.
    if !output.stderr.is_empty() {
        eprint!("{}", String::from_utf8_lossy(&output.stderr));
    }
    Ok("queued")
}
