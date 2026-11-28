use crate::hosts::{Transport, claude::watch::Monitor, codex::watch::Queue};
use anyhow::{Context, Result, ensure};
use clap::{Args, Parser, Subcommand};
use std::{
    fs::{File, OpenOptions, TryLockError},
    hash::{DefaultHasher, Hash, Hasher},
    io,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::Duration,
};
use tj_listener::{
    Uuid,
    domain::{
        forge::IssueRef,
        owner::{Host, Owner},
        wait::{Candidate, ItemName},
    },
    github::Client,
    sqlite::{Binding, Store},
    worktree::Worktree,
};

#[derive(Parser)]
struct Cli {
    #[arg(long)]
    state: Option<PathBuf>,
    /// Interpreter from the TJ installation that started this listener.
    #[arg(long)]
    python: Option<PathBuf>,
    #[command(subcommand)]
    command: Action,
}

#[derive(Args)]
struct Address {
    #[arg(long)]
    host: Host,
    #[arg(long)]
    profile: PathBuf,
    #[arg(long)]
    thread: Uuid,
}

impl Address {
    fn into_owner(self) -> Result<Owner, tj_listener::domain::owner::ProfileError> {
        Owner::new(self.host, self.profile, self.thread)
    }
}

#[derive(Subcommand)]
enum Action {
    /// Inspect a Git worktree without opening the workflow database.
    Worktree {
        #[arg(long)]
        directory: PathBuf,
    },
    #[command(flatten)]
    Workflow(Workflow),
}

#[derive(Subcommand)]
enum Workflow {
    Init,
    Ensure,
    Serve,
    Reconcile,
    Register {
        #[command(flatten)]
        owner: Address,
        #[arg(long)]
        item: String,
        #[arg(long)]
        issue: String,
        #[arg(long)]
        directory: PathBuf,
        // Older launchers pass a fingerprint. Verify it against the same local inspection.
        #[arg(long, requires = "inode")]
        device: Option<u64>,
        #[arg(long, requires = "device")]
        inode: Option<u128>,
    },
    Watch {
        #[command(flatten)]
        owner: Address,
        #[arg(long)]
        once: bool,
    },
}

fn deliver<T: Transport>(
    db: &mut Store,
    adapter: &T,
    owner: Option<&Owner>,
    once: bool,
) -> Result<bool> {
    let mut delivered = false;
    let mut last_failure = None;
    for notice in db.notices(T::HOST, owner)? {
        // A notice remains pending during the read-only preflight, including across crashes.
        let failure = if let Err(error) = adapter.prepare(&notice) {
            anyhow::Error::new(error)
        } else {
            let Some(claim) = db.begin_submission(&notice)? else {
                continue;
            };
            let submission = adapter.send(&notice);
            db.finish(claim, &submission)?;
            match submission {
                Ok(_) => {
                    delivered = true;
                    if once {
                        break;
                    }
                    continue;
                }
                Err(error) => anyhow::Error::new(error),
            }
        }
        .context(format!(
            "review notice {} for {}",
            notice.id().get(),
            notice.item().as_str()
        ));
        if let Some(previous) = last_failure.replace(failure) {
            eprintln!("{previous:#}");
        }
    }
    if let Some(error) = last_failure {
        if once && delivered {
            eprintln!("{error:#}");
        } else {
            return Err(error);
        }
    }
    Ok(delivered)
}

struct Service {
    state: PathBuf,
    python: PathBuf,
    github: Client,
}

impl Service {
    fn queue(&self) -> Queue<'_> {
        Queue {
            state: &self.state,
            python: &self.python,
        }
    }

    fn reconcile(&self, db: &mut Store) -> Result<()> {
        for mut wait in db.waits()? {
            match self.github.read(wait.issue()) {
                Ok(links) => {
                    wait.observe(&links)?;
                    db.save(&mut wait)?;
                }
                Err(error) => {
                    db.record_error(&wait, &format!("{:#}", anyhow::Error::new(error)))?;
                }
            }
        }
        deliver(db, &self.queue(), None, false)?;
        Ok(())
    }

    fn lock_file(&self, name: &str) -> io::Result<File> {
        OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(self.state.join(name))
    }

    fn start(&self) -> Result<()> {
        let lock = self.lock_file("pr-listener.lock")?;
        match lock.try_lock() {
            Ok(()) => (),
            Err(TryLockError::WouldBlock) => return Ok(()),
            Err(TryLockError::Error(error)) => return Err(error.into()),
        }
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.state.join("pr-listener.log"))?;
        let mut child = Command::new(std::env::current_exe()?);
        child
            .arg("--state")
            .arg(&self.state)
            .arg("--python")
            .arg(&self.python)
            .arg("serve")
            .current_dir(&self.state)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(log);
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            child.process_group(0);
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            child.creation_flags(0x0800_0200);
        }
        drop(lock);
        child.spawn()?;
        Ok(())
    }

    fn serve(&self, db: &mut Store) -> Result<()> {
        let lock = self.lock_file("pr-listener.lock")?;
        match lock.try_lock() {
            Ok(()) => (),
            Err(TryLockError::WouldBlock) => return Ok(()),
            Err(TryLockError::Error(error)) => return Err(error.into()),
        }
        let mut interval = Duration::from_secs(15);
        loop {
            if let Err(error) = self.reconcile(db) {
                eprintln!("{error:#}");
            }
            if db.waits()?.is_empty() {
                // Registrations committed as we relinquished ownership must still start a service.
                drop(lock);
                if !db.waits()?.is_empty() {
                    self.start()?;
                }
                return Ok(());
            }
            thread::sleep(interval);
            interval = (interval * 2).min(Duration::from_mins(2));
        }
    }

    fn subscription(&self, owner: &Owner) -> io::Result<File> {
        let mut hash = DefaultHasher::new();
        owner.profile().hash(&mut hash);
        self.lock_file(&format!(
            "watch-{}-{}-{:x}.lock",
            owner.host().as_str(),
            owner.thread(),
            hash.finish()
        ))
    }

    fn subscribed(&self, owner: &Owner) -> io::Result<bool> {
        match self.subscription(owner)?.try_lock() {
            Ok(()) => Ok(false),
            Err(TryLockError::WouldBlock) => Ok(true),
            Err(TryLockError::Error(error)) => Err(error),
        }
    }

    fn watch<T: Transport>(
        &self,
        db: &mut Store,
        adapter: &T,
        owner: &Owner,
        once: bool,
    ) -> Result<()> {
        let subscription = self.subscription(owner)?;
        subscription.lock_shared()?;
        loop {
            if deliver(db, adapter, Some(owner), once)? && once {
                return Ok(());
            }
            thread::sleep(Duration::from_secs(1));
        }
    }

    fn run(&self, command: Workflow) -> Result<()> {
        let mut db = Store::open(&self.state)?;
        match command {
            Workflow::Init => Ok(()),
            Workflow::Ensure => {
                if !db.waits()?.is_empty() {
                    self.start()?;
                }
                Ok(())
            }
            Workflow::Serve => self.serve(&mut db),
            Workflow::Reconcile => self.reconcile(&mut db),
            Workflow::Register {
                owner,
                item,
                issue,
                directory,
                device,
                inode,
            } => {
                let owner = owner.into_owner()?;
                let issue = IssueRef::parse(&issue)?;
                let name = ItemName::parse(&item)?;
                let links = self.github.read(&issue)?;
                let tree = Worktree::discover(&directory)?;
                if let Some(stamp) = device.zip(inode) {
                    ensure!(
                        stamp == (tree.device(), tree.inode()),
                        "the registered worktree has been replaced"
                    );
                }
                let candidate = Candidate::accept(&links, &tree.head()?)?;
                let mut wait = db.bind(&Binding {
                    item: &name,
                    issue: &issue,
                    candidate: &candidate,
                    owner: &owner,
                    worktree: &tree,
                })?;
                wait.observe(&links)?;
                db.save(&mut wait)?;
                if db.has_closeout(&wait)? {
                    println!(
                        "Local closeout recorded for {item} at {}.",
                        candidate.revision().as_str()
                    );
                    return Ok(());
                }
                self.start()?;
                if owner.host() == Host::Claude && !self.subscribed(&owner)? {
                    println!(
                        "For this session, start native Bash with run_in_background=true and command `tj watch --once` to receive an event if the PR needs attention."
                    );
                }
                println!(
                    "Registered {item} at {} for {}.",
                    candidate.revision().as_str(),
                    candidate.pr().as_str()
                );
                Ok(())
            }
            Workflow::Watch { owner, once } => {
                let owner = owner.into_owner()?;
                match owner.host() {
                    Host::Claude => self.watch(&mut db, &Monitor, &owner, once),
                    Host::Codex => self.watch(&mut db, &self.queue(), &owner, once),
                }
            }
        }
    }
}

fn describe(directory: &Path) -> Result<()> {
    let tree = Worktree::discover(directory)?;
    let record = serde_json::json!({
        "root":tree.root(), "git_dir":tree.git_dir(),
        "device":tree.device().to_string(), "inode":tree.inode().to_string(),
    });
    println!("{record}");
    Ok(())
}

pub(crate) fn run() -> Result<()> {
    let args = Cli::parse();
    match args.command {
        Action::Worktree { directory } => describe(&directory),
        Action::Workflow(command) => {
            let state = args
                .state
                .context("--state is required for workflow commands")?;
            let python = match args.python {
                Some(python) => python,
                None => std::env::current_exe()?
                    .with_file_name(format!("python{}", std::env::consts::EXE_SUFFIX)),
            };
            ensure!(
                python.is_absolute(),
                "the TJ interpreter path must be absolute"
            );
            Service {
                state,
                python,
                github: Client,
            }
            .run(command)
        }
    }
}
