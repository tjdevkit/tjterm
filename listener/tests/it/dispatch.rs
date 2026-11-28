use rusqlite::{Connection, params};
use serde_json::json;
use std::{
    env, fs,
    path::Path,
    process::{Child, Command, Output, Stdio},
    thread,
    time::{Duration, Instant},
};
use tempfile::TempDir;

use super::{ISSUE, MERGE, PR, SHA, THREAD};

const ITEM: &str = "39b094709f9b4fc697299dc049d8a390";

struct Trial {
    root: TempDir,
    db: Connection,
}

impl Trial {
    fn new(host: &str) -> Self {
        let root = tempfile::tempdir().unwrap();
        let binary = root.path().join(executable("fixture"));
        let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/it/fixtures/codex.rs");
        assert!(
            Command::new("rustc")
                .arg(source)
                .arg("-o")
                .arg(&binary)
                .status()
                .unwrap()
                .success()
        );
        for name in ["codex", "gh", "python"] {
            fs::copy(&binary, root.path().join(executable(name))).unwrap();
        }
        let state = root.path().join("state");
        let init = Command::new(env!("CARGO_BIN_EXE_tj-listener"))
            .arg("--state")
            .arg(&state)
            .arg("init")
            .output()
            .unwrap();
        assert!(
            init.status.success(),
            "{}",
            String::from_utf8_lossy(&init.stderr)
        );
        let db = Connection::open(state.join("workflows.sqlite3")).unwrap();
        db.busy_timeout(Duration::from_millis(300)).unwrap();
        let directory = root.path().to_str().unwrap();
        db.execute(
            "INSERT INTO items(id,name,thread,directory,worktree,git_dir,command,phase,host)
             VALUES(?1,'widget',?2,?3,?3,?4,'[]','assigned',?5)",
            params![
                ITEM,
                THREAD,
                directory,
                root.path().join(".git").to_str().unwrap(),
                host
            ],
        )
        .unwrap();
        db.execute(
            "INSERT INTO pr_waits(item,issue,pr,revision,host,profile,thread)
             VALUES(?,?,?,?,?,?,?)",
            params![ITEM, ISSUE, PR, SHA, host, directory, THREAD],
        )
        .unwrap();
        db.execute(
            "INSERT INTO pr_decisions(item,pr,revision,host,profile,thread,generation,kind,reason,delivery)
             VALUES(?,?,?,?,?,?,1,'review','closed without merging','pending')",
            params![ITEM, PR, SHA, host, directory, THREAD],
        )
        .unwrap();
        let links = json!({
            "totalCount": 1,
            "nodes": [{"url": PR, "headRefOid": SHA, "state": "OPEN", "mergeCommit": null}]
        });
        let forge =
            json!({"data":{"repository":{"issue":{"closedByPullRequestsReferences":links}}}});
        fs::write(root.path().join("forge.json"), forge.to_string()).unwrap();
        Self { root, db }
    }

    fn command(&self, action: &str, mode: &str) -> Command {
        let root = self.root.path();
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_tj-listener"));
        cmd.env("PATH", root)
            .env("TJ_FIXTURE_DIR", root)
            .env("TJ_FIXTURE_MODE", mode)
            .arg("--state")
            .arg(root.join("state"))
            .arg("--python")
            .arg(root.join(executable("python")))
            .arg(action)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        cmd
    }

    fn run(&self, mode: &str) -> Output {
        self.command("reconcile", mode).output().unwrap()
    }

    fn pause(&self, action: &str, mode: &str, signal: &str) -> Child {
        let mut child = self.command(action, mode).spawn().unwrap();
        let deadline = Instant::now() + Duration::from_secs(10);
        while !self.root.path().join(signal).exists() {
            assert!(
                child.try_wait().unwrap().is_none(),
                "listener exited before {signal}"
            );
            assert!(Instant::now() < deadline, "timed out waiting for {signal}");
            thread::sleep(Duration::from_millis(5));
        }
        child
    }

    fn release(&self) {
        fs::write(self.root.path().join("release"), "").unwrap();
    }

    fn calls(&self) -> String {
        fs::read_to_string(self.root.path().join("calls")).unwrap()
    }

    fn queue_calls(&self) -> usize {
        self.calls()
            .lines()
            .filter(|call| call.starts_with("queue "))
            .count()
    }

    fn receipt(&self) -> String {
        self.db
            .query_row("SELECT delivery FROM pr_decisions", [], |row| row.get(0))
            .unwrap()
    }
}

fn executable(name: &str) -> String {
    format!("{name}{}", env::consts::EXE_SUFFIX)
}

#[test]
fn rejected_codex_wake_leaves_the_notice_pending_without_queueing() {
    let trial = Trial::new("codex");
    let output = trial.run("refuse");
    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr)
            .contains("fixture rejected missing project standards")
    );
    let calls = trial.calls();
    assert!(calls.lines().any(|call| call == "app-server daemon start"));
    assert!(calls.lines().any(|call| call
        == format!(
            "-I -m tjentic.watch.preflight {} {ITEM} {THREAD}",
            trial.root.path().join("state").display()
        )));
    assert_eq!(trial.queue_calls(), 0);
    assert_eq!(trial.receipt(), "pending");
}

#[test]
fn a_native_submission_leaves_sqlite_writable_and_only_one_listener_can_send() {
    let trial = Trial::new("codex");
    let sender = trial.pause("reconcile", "pause-queue", "submitted");
    let write = trial
        .db
        .execute("UPDATE items SET last_error='independent writer'", []);
    let mut duplicate = trial.command("reconcile", "ok").spawn().unwrap();
    let deadline = Instant::now() + Duration::from_secs(3);
    while duplicate.try_wait().unwrap().is_none() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(5));
    }
    let reconciled_before_acknowledgement = duplicate.try_wait().unwrap().is_some();
    trial.release();
    assert!(sender.wait_with_output().unwrap().status.success());
    assert!(duplicate.wait_with_output().unwrap().status.success());
    assert!(reconciled_before_acknowledgement);
    assert_eq!(write.unwrap(), 1);
    assert_eq!(trial.queue_calls(), 1);
    assert_eq!(trial.receipt(), "queued");
}

#[test]
fn the_background_service_closes_another_item_while_a_queue_is_blocked() {
    let trial = Trial::new("codex");
    trial.db.busy_timeout(Duration::from_secs(10)).unwrap();
    trial.db.execute_batch("
        INSERT INTO items SELECT 'second','other',thread,directory,worktree,git_dir,command,phase,NULL,NULL,'claude' FROM items;
        INSERT INTO pr_waits(item,issue,pr,revision,host,profile,thread)
            SELECT 'second',issue,pr,revision,'claude',profile,thread FROM pr_waits;").unwrap();
    let mut service = trial.pause("serve", "pause-queue", "submitted");
    let forge = json!({"data":{"repository":{"issue":{"closedByPullRequestsReferences":{
        "totalCount":1,"nodes":[{"url":PR,"headRefOid":SHA,"state":"MERGED","mergeCommit":{"oid":MERGE}}]
    }}}}});
    fs::write(trial.root.path().join("forge.json"), forge.to_string()).unwrap();

    let deadline = Instant::now() + Duration::from_secs(35);
    let closed =
        loop {
            let closed: bool = trial.db.query_row(
            "SELECT EXISTS(SELECT 1 FROM pr_decisions WHERE item='second' AND kind='closeout')",
            [],
            |row| row.get(0),
        ).unwrap();
            if closed || Instant::now() >= deadline {
                break closed;
            }
            thread::sleep(Duration::from_millis(50));
        };
    let in_flight: String = trial
        .db
        .query_row(
            "SELECT delivery FROM pr_decisions WHERE item=? AND kind='review'",
            [ITEM],
            |row| row.get(0),
        )
        .unwrap();
    trial.release();
    if !closed {
        service.kill().unwrap();
    }
    let exit = service.wait_with_output().unwrap();
    assert!(closed, "the sole poller was waiting for the Codex queue");
    assert_eq!(in_flight, "dispatching");
    assert!(
        exit.status.success(),
        "{}",
        String::from_utf8_lossy(&exit.stderr)
    );
    assert_eq!(trial.queue_calls(), 1);
}

#[test]
fn a_definite_spawn_failure_can_be_retried_after_the_executable_is_restored() {
    let trial = Trial::new("codex");
    let result = trial.run("remove-codex");
    assert!(!result.status.success());
    assert_eq!(trial.queue_calls(), 0);
    assert_eq!(trial.receipt(), "pending");
    fs::copy(
        trial.root.path().join(executable("fixture")),
        trial.root.path().join(executable("codex")),
    )
    .unwrap();
    assert!(trial.run("ok").status.success());
    assert_eq!(trial.queue_calls(), 1);
    assert_eq!(trial.receipt(), "queued");
}

#[test]
fn a_newer_check_during_preflight_revokes_the_original_revision() {
    let trial = Trial::new("codex");
    trial
        .db
        .execute("UPDATE items SET phase='ready'", [])
        .unwrap();
    trial.db.execute("INSERT INTO deliveries(id,thread,directory,prompt) VALUES('fixture',?,'/fixture','fixture')", [THREAD]).unwrap();
    let insert = "INSERT INTO checks(item,attempt,revision,status,exit_code,output,delivery) VALUES(?,?,?,'',0,'','fixture')";
    trial.db.execute(insert, params![ITEM, 1, SHA]).unwrap();
    let sender = trial.pause("reconcile", "pause-preflight", "preflight");
    trial.db.execute(insert, params![ITEM, 2, MERGE]).unwrap();
    trial.release();
    assert!(sender.wait_with_output().unwrap().status.success());
    assert_eq!(trial.queue_calls(), 0);
    assert_eq!(trial.receipt(), "pending");
}

#[test]
fn an_ambiguous_exit_or_crash_after_queue_submission_never_replays_the_message() {
    for crash in [false, true] {
        let trial = Trial::new("codex");
        let receipt = if crash {
            let mut sender = trial.pause("reconcile", "pause-queue", "submitted");
            sender.kill().unwrap();
            trial.release();
            assert!(!sender.wait_with_output().unwrap().status.success());
            "dispatching"
        } else {
            assert!(!trial.run("unknown").status.success());
            "uncertain"
        };
        assert_eq!(trial.receipt(), receipt);
        assert!(trial.run("ok").status.success());
        assert_eq!(trial.queue_calls(), 1);
    }
}

#[test]
fn claude_watch_emits_the_existing_json_contract_and_records_the_flushed_notice() {
    let trial = Trial::new("claude");
    let root = trial.root.path();
    let output = Command::new(env!("CARGO_BIN_EXE_tj-listener"))
        .arg("--state")
        .arg(root.join("state"))
        .args([
            "watch",
            "--host",
            "claude",
            "--thread",
            THREAD,
            "--once",
            "--profile",
        ])
        .arg(root)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&output.stdout).unwrap(),
        json!({"id":1, "item":"widget","pr":PR,"revision":SHA,"reason":"closed without merging","profile":root,"thread":THREAD})
    );
    assert_eq!(trial.receipt(), "emitted");
}

#[test]
fn one_refused_preflight_does_not_starve_another_eligible_notice() {
    let trial = Trial::new("codex");
    trial.db.execute_batch("
        INSERT INTO items SELECT 'second','other',thread,directory,worktree,git_dir,command,phase,hold,last_error,host FROM items;
        INSERT INTO pr_waits(item,issue,pr,revision,host,profile,thread)
            SELECT 'second',issue,pr,revision,host,profile,thread FROM pr_waits;
        INSERT INTO pr_decisions(item,pr,revision,host,profile,thread,generation,kind,reason,delivery)
            SELECT 'second',pr,revision,host,profile,thread,generation,kind,reason,'pending' FROM pr_decisions;").unwrap();
    trial.run("refuse-one");
    let mut query = trial
        .db
        .prepare("SELECT item,delivery FROM pr_decisions ORDER BY id")
        .unwrap();
    let states: Vec<(String, String)> = query
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();
    assert_eq!(
        states,
        [
            (ITEM.into(), "pending".into()),
            ("second".into(), "queued".into())
        ]
    );
    assert_eq!(trial.queue_calls(), 1);
}

#[test]
fn finishing_a_superseded_claim_cannot_acknowledge_a_different_submission() {
    use tj_listener::{
        domain::{delivery::Receipt, owner::Host},
        sqlite::{FinishError, Store},
    };
    let trial = Trial::new("codex");
    let mut store = Store::open(&trial.root.path().join("state")).unwrap();
    let notice = store.notices(Host::Codex, None).unwrap().remove(0);
    let claim = store.begin_submission(&notice).unwrap().unwrap();
    trial
        .db
        .execute("UPDATE pr_decisions SET dispatch_token='other-claim'", [])
        .unwrap();
    assert!(matches!(
        store.finish(
            claim,
            &Ok::<_, tj_listener::domain::delivery::Failure<()>>(Receipt::Queued)
        ),
        Err(FinishError::LostClaim)
    ));
    assert_eq!(trial.receipt(), "dispatching");
}
