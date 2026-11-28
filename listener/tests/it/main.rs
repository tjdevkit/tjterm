use rusqlite::{Connection, params};
use serde_json::json;
use std::{path::Path, thread};
use tj_listener::{
    github::Links,
    store::{Binding, Store},
};

const THREAD: &str = "019a0000-0000-7000-8000-000000000001";
const NEXT: &str = "019a0000-0000-7000-8000-000000000002";
const ISSUE: &str = "https://github.com/paper/widget/issues/7";
const PR: &str = "https://github.com/paper/widget/pull/17";
const SHA: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const MERGE: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

fn binding(host: &str) -> Binding<'_> {
    Binding {
        item: "widget",
        issue: ISSUE,
        pr: PR,
        revision: SHA,
        host,
        profile: "/profiles/work",
        thread: THREAD,
        root: "/work",
        git_dir: "/work/.git",
        device: "1",
        inode: "2",
    }
}
fn links(state: &str, revision: &str, count: u32) -> Links {
    let merge = (state == "MERGED").then(|| json!({ "oid": MERGE }));
    serde_json::from_value(json!({
        "totalCount": count,
        "nodes": [{
            "url": PR,
            "headRefOid": revision,
            "state": state,
            "mergeCommit": merge,
        }],
    }))
    .expect("deserialize the fixture PR links")
}
fn sql(path: &Path) -> Connection {
    Connection::open(path.join("workflows.sqlite3")).expect("open the fixture database")
}

#[test]
fn simultaneous_recovery_after_offline_merge_records_one_closeout_for_each_host() {
    for host in ["codex", "claude"] {
        let tmp = tempfile::tempdir().unwrap();
        // Start with a bound item in the previous preview schema.
        let old = sql(tmp.path());
        old.execute_batch("CREATE TABLE items(name TEXT PRIMARY KEY,thread TEXT NOT NULL,directory TEXT NOT NULL,worktree TEXT NOT NULL,git_dir TEXT NOT NULL,command TEXT NOT NULL,phase TEXT NOT NULL,hold TEXT,last_error TEXT);")
            .unwrap();
        old.execute("INSERT INTO items VALUES('widget',?,'/work','/work','/work/.git','[]','assigned',NULL,NULL)",[THREAD]).unwrap();
        {
            let mut store = Store::open(tmp.path()).unwrap();
            if host == "claude" {
                old.execute("UPDATE items SET host='claude'", []).unwrap();
            }
            store.bind(&binding(host)).unwrap();
            for _minute in 1..=10 {
                let wait = store.waits().unwrap().remove(0);
                store.observe(&wait, &links("OPEN", SHA, 1)).unwrap();
            }
            let count: i64 = old
                .query_row("SELECT count(*) FROM pr_decisions", [], |r| r.get(0))
                .unwrap();
            assert_eq!(count, 0);
        }
        // All listener connections are gone before the forge changes.
        let barrier = std::sync::Barrier::new(2);
        thread::scope(|scope| {
            for _ in 0..2 {
                scope.spawn(|| {
                    let mut store = Store::open(tmp.path()).unwrap();
                    let wait = store.waits().unwrap().remove(0);
                    barrier.wait();
                    for _ in 0..2 {
                        store.observe(&wait, &links("MERGED", SHA, 1)).unwrap();
                    }
                    assert!(store.waits().unwrap().is_empty());
                });
            }
        });
        let receipt: (String, String, String, String, String) = old
            .query_row(
                "SELECT host,thread,revision,merge_oid,delivery FROM pr_decisions",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
            )
            .unwrap();
        assert_eq!(
            receipt,
            (
                host.into(),
                THREAD.into(),
                SHA.into(),
                MERGE.into(),
                "recorded".into()
            )
        );
        assert_eq!(
            old.query_row("SELECT COUNT(*) FROM pr_decisions", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            1
        );
        assert_eq!(
            old.query_row("SELECT phase FROM items", [], |r| r.get::<_, String>(0))
                .unwrap(),
            "closed"
        );
    }
}

#[test]
fn held_transferred_or_changed_candidates_never_consume_the_original_wait() {
    let tmp = tempfile::tempdir().unwrap();
    let mut store = Store::open(tmp.path()).unwrap();
    store.bind(&binding("codex")).unwrap();
    let old = store.waits().unwrap().remove(0);
    let db = sql(tmp.path());
    db.execute("UPDATE items SET hold='waiting'", []).unwrap();
    store.observe(&old, &links("MERGED", SHA, 1)).unwrap();
    db.execute("UPDATE items SET hold=NULL,host='claude',thread=?", [NEXT])
        .unwrap();
    store.observe(&old, &links("MERGED", SHA, 1)).unwrap();
    assert_eq!(
        db.query_row("SELECT COUNT(*) FROM pr_decisions", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        0
    );
    let mut new = binding("claude");
    new.thread = NEXT;
    store.bind(&new).unwrap();
    let current = store.waits().unwrap().remove(0);
    assert_eq!(current.generation, 2);
    for observation in [
        links("MERGED", MERGE, 1),
        links("MERGED", SHA, 2),
        links("CLOSED", SHA, 1),
    ] {
        store.observe(&current, &observation).unwrap();
    }
    assert!(store.claim_notice("codex", None, None).unwrap().is_none());
    assert!(
        store
            .claim_notice("claude", Some("/different"), Some(NEXT))
            .unwrap()
            .is_none()
    );
    assert!(
        store
            .claim_notice("claude", Some(new.profile), Some(THREAD))
            .unwrap()
            .is_none()
    );
    let event = store
        .claim_notice("claude", Some(new.profile), Some(NEXT))
        .unwrap()
        .unwrap();
    assert_eq!(event.thread, NEXT);
    db.execute("UPDATE items SET hold='paused'", []).unwrap();
    store
        .dispatch(&event, |_| panic!("a held item must not be sent"))
        .unwrap();
    db.execute("UPDATE items SET hold=NULL", []).unwrap();
    let event = store
        .claim_notice("claude", Some(new.profile), Some(NEXT))
        .unwrap()
        .unwrap();
    store.dispatch(&event, |_| Ok("emitted")).unwrap();
    assert!(
        store
            .claim_notice("claude", Some(new.profile), Some(NEXT))
            .unwrap()
            .is_none()
    );
    assert_eq!(store.waits().unwrap().len(), 1);
}

#[test]
fn already_merged_on_first_observation_is_idempotent() {
    let tmp = tempfile::tempdir().unwrap();
    let mut store = Store::open(tmp.path()).unwrap();
    let b = binding("codex");
    store.bind(&b).unwrap();
    let wait = store.waits().unwrap().remove(0);
    store.observe(&wait, &links("MERGED", SHA, 1)).unwrap();
    store.bind(&b).unwrap();
    assert!(store.waits().unwrap().is_empty());
    let db = sql(tmp.path());
    assert_eq!(
        db.query_row(
            "SELECT COUNT(*) FROM pr_decisions WHERE kind='closeout' AND generation=?",
            params![1],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
}

#[test]
fn a_forge_outage_preserves_the_wait_and_a_successful_read_clears_the_error() {
    let tmp = tempfile::tempdir().unwrap();
    let mut store = Store::open(tmp.path()).unwrap();
    let wait = store.bind(&binding("codex")).unwrap();
    store.error(&wait, "GitHub unavailable").unwrap();
    assert_eq!(store.waits().unwrap().len(), 1);
    assert!(!store.has_closeout(&wait).unwrap());
    store.observe(&wait, &links("MERGED", SHA, 1)).unwrap();
    assert!(store.has_closeout(&wait).unwrap());
    let db = sql(tmp.path());
    let error: Option<String> = db
        .query_row("SELECT last_error FROM pr_waits", [], |r| r.get(0))
        .unwrap();
    assert_eq!(error, None);
}
