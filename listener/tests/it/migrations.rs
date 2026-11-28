use super::{ISSUE, PR, SHA, THREAD, sql};
use rusqlite::{Connection, params};
use std::{path::Path, process::Command, thread};

// Existing previews used the original migration; the upgrade must preserve those rows.
fn version_one(root: &Path) -> Connection {
    let db = sql(root);
    db.execute_batch(include_str!("../../../src/tjentic/workflows/schema.sql"))
        .unwrap();
    db.execute_batch(include_str!("../../src/schema.sql"))
        .unwrap();
    db.pragma_update(None, "user_version", 1).unwrap();
    let directory = root.to_str().unwrap();
    db.execute("INSERT INTO items(id,name,thread,directory,worktree,git_dir,command,phase,host) VALUES('one','existing',?1,?2,?2,?3,'[]','assigned','codex')",params![THREAD,directory,root.join(".git").to_str().unwrap()]).unwrap();
    db.execute("INSERT INTO pr_waits(item,issue,pr,revision,host,profile,thread) VALUES('one',?,?,?,'codex',?,?)",params![ISSUE,PR,SHA,directory,THREAD]).unwrap();
    db.execute("INSERT INTO pr_decisions(item,pr,revision,host,profile,thread,generation,kind,reason,delivery) VALUES('one',?,?,'codex',?,?,1,'review','native result unknown','dispatching')",params![PR,SHA,directory,THREAD]).unwrap();
    db
}

fn init(root: &Path) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_tj-listener"))
        .arg("--state")
        .arg(root)
        .arg("init")
        .output()
        .unwrap()
}

fn version(db: &Connection) -> i64 {
    db.pragma_query_value(None, "user_version", |row| row.get(0))
        .unwrap()
}

#[test]
fn simultaneous_upgrade_preserves_v1_bindings_and_quarantines_an_unknown_delivery() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    let db = version_one(root);
    thread::scope(|scope| {
        let a = scope.spawn(|| init(root));
        let b = scope.spawn(|| init(root));
        assert!(a.join().unwrap().status.success());
        assert!(b.join().unwrap().status.success());
    });
    assert_eq!(version(&db), 2);
    let saved:(String,String,String,String)=db.query_row("SELECT i.name,w.thread,d.delivery,d.reason FROM items i JOIN pr_waits w ON w.item=i.id JOIN pr_decisions d ON d.item=w.item",[],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).unwrap();
    assert_eq!(
        saved,
        (
            "existing".into(),
            THREAD.into(),
            "uncertain".into(),
            "native result unknown".into()
        )
    );
    assert_eq!(
        db.query_row("SELECT count(*) FROM pragma_foreign_key_check", [], |r| r
            .get::<_, i64>(
            0
        ))
        .unwrap(),
        0
    );
}

#[test]
fn failed_upgrade_does_not_modify_v1_data_or_advance_its_version() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    let db = version_one(root);
    db.execute_batch("CREATE TRIGGER simulate_disk_failure BEFORE UPDATE ON pr_decisions BEGIN SELECT RAISE(ABORT,'fixture denied migration update'); END;").unwrap();
    let result = init(root);
    assert!(!result.status.success());
    assert_eq!(version(&db), 1);
    assert_eq!(
        db.query_row("SELECT delivery FROM pr_decisions", [], |r| r
            .get::<_, String>(0))
            .unwrap(),
        "dispatching"
    );
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM pragma_table_info('pr_decisions') WHERE name='dispatch_token'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        0
    );
    db.execute_batch("DROP TRIGGER simulate_disk_failure;")
        .unwrap();
    assert!(init(root).status.success());
    assert_eq!(version(&db), 2);
}

#[test]
fn upgrade_cannot_dispatch_or_close_an_unknown_legacy_condition() {
    use tj_listener::{domain::owner::Host, sqlite::Store};
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    let db = version_one(root);
    db.execute("UPDATE pr_waits SET condition='unrecognised'", [])
        .unwrap();
    db.execute("UPDATE pr_decisions SET delivery='pending'", [])
        .unwrap();
    let store = Store::open(root).unwrap();
    assert!(store.notices(Host::Codex, None).unwrap().is_empty());
    assert!(store.waits().unwrap().is_empty());
}
