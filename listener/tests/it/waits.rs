use rusqlite::params;
use std::{path::Path, process::Command, sync::Barrier, thread};
use tj_listener::Uuid;
use tj_listener::{
    domain::{
        delivery::Receipt,
        forge::{IssueRef, Links, Oid, Pull, PullRef, State},
        owner::{Host, Owner},
        wait::{Candidate, ItemName, Wait},
    },
    sqlite::{BindError, Binding, SaveOutcome, Store},
    worktree::Worktree,
};

use super::{ISSUE, MERGE, NEXT, PR, SHA, THREAD, sql};

fn owner(host: Host, profile: &Path, thread: &str) -> Owner {
    Owner::new(
        host,
        profile.to_path_buf(),
        Uuid::parse_str(thread).unwrap(),
    )
    .unwrap()
}

fn checkout(root: &Path) -> Worktree {
    if !root.join(".git").is_dir() {
        assert!(
            Command::new("git")
                .args(["init", "--quiet"])
                .arg(root)
                .status()
                .unwrap()
                .success()
        );
    }
    Worktree::discover(root).expect("discover the temporary Git checkout")
}

fn bind(store: &mut Store, root: &Path, owner: &Owner) -> Wait {
    bind_revision(store, root, owner, SHA)
}

fn bind_revision(store: &mut Store, root: &Path, owner: &Owner, revision: &str) -> Wait {
    register(store, root, owner, ISSUE, PR, revision).unwrap()
}

fn register(
    store: &mut Store,
    root: &Path,
    owner: &Owner,
    issue: &str,
    pr: &str,
    revision: &str,
) -> Result<Wait, BindError> {
    store.bind(&Binding {
        item: &ItemName::parse("widget").unwrap(),
        issue: &IssueRef::parse(issue).unwrap(),
        candidate: &Candidate::accept(
            &pull(pr, State::Open, revision),
            &Oid::parse(revision).unwrap(),
        )
        .unwrap(),
        owner,
        worktree: &checkout(root),
    })
}

fn links(state: State, revision: &str) -> Links {
    pull(PR, state, revision)
}

fn pull(pr: &str, state: State, revision: &str) -> Links {
    Links::Single(Pull::new(
        PullRef::parse(pr).unwrap(),
        Oid::parse(revision).unwrap(),
        state,
    ))
}

fn merged() -> State {
    State::Merged(Oid::parse(MERGE).unwrap())
}

#[test]
fn simultaneous_recovery_after_offline_merge_records_one_closeout_for_each_host() {
    for host in [Host::Codex, Host::Claude] {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        let tree = checkout(root);
        let db = sql(root);
        db.execute_batch("CREATE TABLE items(name TEXT PRIMARY KEY,thread TEXT NOT NULL,directory TEXT NOT NULL,worktree TEXT NOT NULL,git_dir TEXT NOT NULL,command TEXT NOT NULL,phase TEXT NOT NULL,hold TEXT,last_error TEXT);").unwrap();
        db.execute(
            "INSERT INTO items VALUES('widget',?1,?2,?2,?3,'[]','assigned',NULL,NULL)",
            params![
                THREAD,
                tree.root().to_str().unwrap(),
                tree.git_dir().to_str().unwrap()
            ],
        )
        .unwrap();
        {
            let mut store = Store::open(root).unwrap();
            db.execute("UPDATE items SET host=?", [host.as_str()])
                .unwrap();
            let mut wait = bind(&mut store, root, &owner(host, root, THREAD));
            for _minute in 1..=10 {
                wait.observe(&links(State::Open, SHA)).unwrap();
                store.save(&mut wait).unwrap();
            }
            assert_eq!(
                db.query_row("SELECT count(*) FROM pr_decisions", [], |row| row
                    .get::<_, i64>(0))
                    .unwrap(),
                0
            );
        }
        let barrier = Barrier::new(2);
        thread::scope(|scope| {
            for _ in 0..2 {
                scope.spawn(|| {
                    let mut store = Store::open(root).unwrap();
                    let mut wait = store.waits().unwrap().remove(0);
                    barrier.wait();
                    for _ in 0..2 {
                        wait.observe(&links(merged(), SHA)).unwrap();
                        store.save(&mut wait).unwrap();
                    }
                    assert!(store.waits().unwrap().is_empty());
                });
            }
        });
        let receipt: (String, String, String, String, String) = db
            .query_row(
                "SELECT host,thread,revision,merge_oid,delivery FROM pr_decisions",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
            )
            .unwrap();
        assert_eq!(
            receipt,
            (
                host.as_str().into(),
                THREAD.into(),
                SHA.into(),
                MERGE.into(),
                "recorded".into()
            )
        );
        assert_eq!(
            db.query_row("SELECT COUNT(*) FROM pr_decisions", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            1
        );
        assert_eq!(
            db.query_row("SELECT phase FROM items", [], |r| r.get::<_, String>(0))
                .unwrap(),
            "closed"
        );
    }
}

#[test]
fn held_transferred_or_changed_candidates_never_consume_the_original_wait() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    let mut store = Store::open(root).unwrap();
    let original = owner(Host::Codex, root, THREAD);
    bind(&mut store, root, &original);
    let mut while_held = store.waits().unwrap().remove(0);
    let mut before_transfer = store.waits().unwrap().remove(0);
    let db = sql(root);
    db.execute("UPDATE items SET hold='waiting'", []).unwrap();
    while_held.observe(&links(merged(), SHA)).unwrap();
    store.save(&mut while_held).unwrap();
    db.execute("UPDATE items SET hold=NULL,host='claude',thread=?", [NEXT])
        .unwrap();
    before_transfer.observe(&links(merged(), SHA)).unwrap();
    store.save(&mut before_transfer).unwrap();
    assert_eq!(
        db.query_row("SELECT count(*) FROM pr_decisions", [], |row| row
            .get::<_, i64>(0))
            .unwrap(),
        0
    );

    let recipient = owner(Host::Claude, root, NEXT);
    let mut current = bind(&mut store, root, &recipient);
    assert_eq!(current.generation(), 2);
    for observation in [
        links(merged(), MERGE),
        Links::NotSingle,
        links(State::Closed, SHA),
    ] {
        current.observe(&observation).unwrap();
        store.save(&mut current).unwrap();
    }
    assert!(store.notices(Host::Codex, None).unwrap().is_empty());
    for other in [
        owner(Host::Claude, &root.join("other"), NEXT),
        owner(Host::Claude, root, THREAD),
    ] {
        assert!(
            store
                .notices(Host::Claude, Some(&other))
                .unwrap()
                .is_empty()
        );
    }
    let event = store
        .notices(Host::Claude, Some(&recipient))
        .unwrap()
        .remove(0);
    assert_eq!(event.owner(), &recipient);
    db.execute("UPDATE items SET hold='paused'", []).unwrap();
    assert!(store.begin_submission(&event).unwrap().is_none());
    db.execute("UPDATE items SET hold=NULL", []).unwrap();
    let claim = store.begin_submission(&event).unwrap().unwrap();
    store
        .finish(
            claim,
            &Ok::<_, tj_listener::domain::delivery::Failure<()>>(Receipt::Emitted),
        )
        .unwrap();
    assert!(
        store
            .notices(Host::Claude, Some(&recipient))
            .unwrap()
            .is_empty()
    );
    drop(store);
    let reopened = Store::open(root).unwrap();
    let mut pending = reopened.waits().unwrap().remove(0);
    pending.observe(&links(State::Closed, SHA)).unwrap();
    assert_eq!(pending.events(), []);
}

#[test]
fn already_merged_on_first_observation_is_idempotent() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    let mut store = Store::open(root).unwrap();
    let session = owner(Host::Codex, root, THREAD);
    let mut wait = bind(&mut store, root, &session);
    wait.observe(&links(merged(), SHA)).unwrap();
    store.save(&mut wait).unwrap();
    drop(store);
    let mut reopened = Store::open(root).unwrap();
    let mut completed = bind(&mut reopened, root, &session);
    assert!(completed.observe(&links(State::Open, SHA)).is_err());
    assert!(reopened.waits().unwrap().is_empty());
    assert_eq!(
        sql(root)
            .query_row(
                "SELECT count(*) FROM pr_decisions WHERE kind='closeout' AND generation=1",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
        1
    );
}

#[test]
fn a_forge_outage_preserves_the_wait_and_a_successful_read_clears_the_error() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    let mut store = Store::open(root).unwrap();
    let mut wait = bind(&mut store, root, &owner(Host::Codex, root, THREAD));
    store.record_error(&wait, "GitHub unavailable").unwrap();
    assert_eq!(store.waits().unwrap().len(), 1);
    assert!(!store.has_closeout(&wait).unwrap());
    wait.observe(&links(merged(), SHA)).unwrap();
    store.save(&mut wait).unwrap();
    assert!(store.has_closeout(&wait).unwrap());
    let error: Option<String> = sql(root)
        .query_row("SELECT last_error FROM pr_waits", [], |r| r.get(0))
        .unwrap();
    assert_eq!(error, None);
}

#[test]
fn a_failed_closeout_rolls_back_its_receipt_and_retains_the_event_for_retry() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    let mut store = Store::open(root).unwrap();
    let mut wait = bind(&mut store, root, &owner(Host::Codex, root, THREAD));
    wait.observe(&links(merged(), SHA)).unwrap();
    let db = sql(root);
    db.execute("UPDATE items SET hold='waiting for approval'", [])
        .unwrap();
    assert!(matches!(
        store.save(&mut wait).unwrap(),
        SaveOutcome::Ineligible
    ));
    assert_eq!(wait.events().len(), 1);
    db.execute("UPDATE items SET hold=NULL", []).unwrap();
    db.execute_batch("CREATE TRIGGER refuse_closeout BEFORE UPDATE OF phase ON items BEGIN SELECT RAISE(ABORT,'fixture refused closeout'); END;").unwrap();
    assert!(store.save(&mut wait).is_err());
    assert!(!store.has_closeout(&wait).unwrap());
    assert_eq!(store.waits().unwrap().len(), 1);
    assert_eq!(wait.events().len(), 1);
    db.execute_batch("DROP TRIGGER refuse_closeout").unwrap();
    store.save(&mut wait).unwrap();
    assert!(store.has_closeout(&wait).unwrap());
    assert_eq!(wait.events(), []);
}

#[test]
fn a_return_to_an_earlier_revision_does_not_revive_its_superseded_notice() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    let mut store = Store::open(root).unwrap();
    let session = owner(Host::Codex, root, THREAD);
    let mut first = bind(&mut store, root, &session);
    first.observe(&links(State::Closed, SHA)).unwrap();
    store.save(&mut first).unwrap();
    assert_eq!(store.notices(Host::Codex, None).unwrap().len(), 1);
    let second = bind_revision(&mut store, root, &session, MERGE);
    let mut third = bind_revision(&mut store, root, &session, SHA);
    assert_eq!(
        [first.generation(), second.generation(), third.generation()],
        [1, 2, 3]
    );
    assert!(store.notices(Host::Codex, None).unwrap().is_empty());
    third.observe(&links(State::Closed, SHA)).unwrap();
    store.save(&mut third).unwrap();
    assert_eq!(store.notices(Host::Codex, None).unwrap().len(), 1);
}

#[test]
fn accepted_transfers_allow_closeout_after_all_recipient_work_is_finished() {
    for delegated_action in [false, true] {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        let mut store = Store::open(root).unwrap();
        let mut wait = bind(&mut store, root, &owner(Host::Codex, root, NEXT));
        let db = sql(root);
        db.execute("INSERT INTO deliveries(id,thread,directory,prompt) VALUES('work',?1,?2,'recipient assignment')",params![NEXT,root.to_str().unwrap()]).unwrap();
        db.execute("INSERT INTO transfers(id,item,sender,receiver,next_action,state,action_delivery) VALUES('transfer',?1,?2,?3,'work','accepted',?4)",params![wait.item().as_str(),THREAD,NEXT,delegated_action.then_some("work")]).unwrap();
        wait.observe(&links(merged(), SHA)).unwrap();
        store.save(&mut wait).unwrap();
        if delegated_action {
            assert!(!store.has_closeout(&wait).unwrap());
            db.execute("UPDATE deliveries SET state='done' WHERE id='work'", [])
                .unwrap();
            let mut eligible = store.waits().unwrap().remove(0);
            eligible.observe(&links(merged(), SHA)).unwrap();
            store.save(&mut eligible).unwrap();
        }
        assert!(store.has_closeout(&wait).unwrap());
    }
}

#[test]
fn only_explicit_registration_accepts_a_replacement_pr_and_fences_stale_replays() {
    const REPLACEMENT: &str = "https://github.com/paper/widget/pull/18";
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    let mut store = Store::open(root).unwrap();
    let session = owner(Host::Claude, root, THREAD);
    let mut first = bind(&mut store, root, &session);
    first.observe(&pull(REPLACEMENT, State::Open, SHA)).unwrap();
    store.save(&mut first).unwrap();
    assert_eq!(store.waits().unwrap()[0].pr().as_str(), PR);
    let notice = store
        .notices(Host::Claude, Some(&session))
        .unwrap()
        .remove(0);

    let mut second = register(&mut store, root, &session, ISSUE, REPLACEMENT, SHA).unwrap();
    assert!(store.begin_submission(&notice).unwrap().is_none());
    let mut third = bind(&mut store, root, &session);
    assert_eq!(
        [first.generation(), second.generation(), third.generation()],
        [1, 2, 3]
    );
    assert!(store.begin_submission(&notice).unwrap().is_none());
    first.observe(&links(merged(), SHA)).unwrap();
    assert!(matches!(
        store.save(&mut first).unwrap(),
        SaveOutcome::Ineligible
    ));

    third.observe(&pull(REPLACEMENT, State::Open, SHA)).unwrap();
    store.save(&mut third).unwrap();
    let mut current = register(&mut store, root, &session, ISSUE, REPLACEMENT, SHA).unwrap();
    assert_eq!(current.generation(), 4);
    assert_eq!(
        register(&mut store, root, &session, ISSUE, REPLACEMENT, SHA)
            .unwrap()
            .generation(),
        4
    );
    second.observe(&pull(REPLACEMENT, merged(), SHA)).unwrap();
    assert!(matches!(
        store.save(&mut second).unwrap(),
        SaveOutcome::Ineligible
    ));
    current.observe(&pull(REPLACEMENT, merged(), SHA)).unwrap();
    store.save(&mut current).unwrap();
    assert!(store.has_closeout(&current).unwrap());
    assert!(register(&mut store, root, &session, ISSUE, PR, SHA).is_err());
    let db = sql(root);
    assert_eq!(db.query_row("SELECT count(*) FROM pr_decisions WHERE kind='review' AND pr=? AND generation IN (1,3)", [PR], |r| r.get::<_,i64>(0)).unwrap(), 2);
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM pr_decisions WHERE kind='closeout' AND pr=? AND generation=4",
            [REPLACEMENT],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
}

#[test]
fn a_replacement_requires_the_existing_issue_and_authorised_owner() {
    const REPLACEMENT: &str = "https://github.com/paper/widget/pull/18";
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    let mut store = Store::open(root).unwrap();
    let session = owner(Host::Claude, root, THREAD);
    bind(&mut store, root, &session);
    for other in [
        owner(Host::Claude, root, NEXT),
        owner(Host::Codex, root, THREAD),
        owner(Host::Claude, &root.join("other"), THREAD),
    ] {
        assert!(register(&mut store, root, &other, ISSUE, REPLACEMENT, SHA).is_err());
    }
    assert!(
        register(
            &mut store,
            root,
            &session,
            "https://github.com/paper/widget/issues/8",
            REPLACEMENT,
            SHA
        )
        .is_err()
    );
    let db = sql(root);
    db.execute("UPDATE items SET phase='closed'", []).unwrap();
    assert!(register(&mut store, root, &session, ISSUE, REPLACEMENT, SHA).is_err());
    let unchanged = store.waits().unwrap().remove(0);
    assert_eq!(unchanged.generation(), 1);
    assert_eq!(unchanged.pr().as_str(), PR);

    db.execute(
        "UPDATE items SET phase='assigned', host='codex', thread=?",
        [NEXT],
    )
    .unwrap();
    assert!(register(&mut store, root, &session, ISSUE, REPLACEMENT, SHA).is_err());
    let recipient = owner(Host::Codex, root, NEXT);
    assert_eq!(bind(&mut store, root, &recipient).generation(), 2);
    let replaced = register(&mut store, root, &recipient, ISSUE, REPLACEMENT, SHA).unwrap();
    assert_eq!(replaced.generation(), 3);
    assert_eq!(replaced.pr().as_str(), REPLACEMENT);
}
