use anyhow::{Context, Result, bail, ensure};
use serde::Deserialize;
use std::{path::Path, process::Command};

const QUERY: &str = "query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo){issue(number:$number){closedByPullRequestsReferences(first:2,includeClosedPrs:true){totalCount nodes{url headRefOid state mergeCommit{oid}}}}}}";

#[derive(Debug)]
pub struct Issue<'a> {
    owner: &'a str,
    repo: &'a str,
    number: u32,
}

impl<'a> Issue<'a> {
    pub fn parse(url: &'a str) -> Result<Self> {
        let path = url
            .strip_prefix("https://github.com/")
            .context("expected a GitHub issue URL")?;
        let parts: Vec<_> = path.split('/').collect();
        let [owner, repo, "issues", number] = parts.as_slice() else {
            bail!("expected https://github.com/OWNER/REPO/issues/NUMBER")
        };
        for part in [owner, repo] {
            ensure!(
                !part.is_empty()
                    && part
                        .bytes()
                        .all(|c| c.is_ascii_alphanumeric() || b"-_.".contains(&c))
                    && *part != "."
                    && *part != "..",
                "invalid repository name"
            );
        }
        let number: u32 = number.parse().context("invalid issue number")?;
        ensure!(number > 0, "invalid issue number");
        Ok(Self {
            owner,
            repo,
            number,
        })
    }

    pub fn read(&self) -> Result<Links> {
        let output = Command::new("gh")
            .env(
                "GH_REPO",
                format!("github.com/{}/{}", self.owner, self.repo),
            )
            .args([
                "api",
                "--hostname",
                "github.com",
                "graphql",
                "-f",
                &format!("query={QUERY}"),
                "-f",
                &format!("owner={}", self.owner),
                "-f",
                &format!("repo={}", self.repo),
                "-F",
                &format!("number={}", self.number),
            ])
            .output()
            .context("start GitHub CLI")?;
        ensure!(
            output.status.success(),
            "GitHub issue lookup failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
        let reply: serde_json::Value = serde_json::from_slice(&output.stdout)?;
        ensure!(
            reply.get("errors").is_none(),
            "GitHub returned an incomplete issue lookup"
        );
        let links: Links = serde_json::from_value(
            reply
                .pointer("/data/repository/issue/closedByPullRequestsReferences")
                .context("missing GitHub issue")?
                .clone(),
        )?;
        for pull in &links.nodes {
            ensure!(
                pull.url.starts_with("https://github.com/")
                    && valid_oid(&pull.head_ref_oid)
                    && pull.merge_commit.as_ref().is_none_or(|m| valid_oid(&m.oid)),
                "invalid GitHub pull request"
            );
        }
        Ok(links)
    }
}

fn valid_oid(value: &str) -> bool {
    matches!(value.len(), 40 | 64) && value.bytes().all(|c| c.is_ascii_hexdigit())
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Links {
    total_count: usize,
    nodes: Vec<Pull>,
}
impl Links {
    pub fn single(&self) -> Option<&Pull> {
        match self.nodes.as_slice() {
            [pr] if self.total_count == 1 => Some(pr),
            _ => None,
        }
    }
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Pull {
    pub url: String,
    pub head_ref_oid: String,
    pub state: State,
    pub merge_commit: Option<Commit>,
}
#[derive(Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum State {
    Open,
    Closed,
    Merged,
}
#[derive(Deserialize)]
pub struct Commit {
    pub oid: String,
}

pub fn git(directory: &Path, args: &[&str]) -> Result<String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(directory)
        .args(args)
        .output()?;
    ensure!(
        output.status.success(),
        "could not identify the checked-out Git revision or worktree"
    );
    Ok(String::from_utf8(output.stdout)?.trim_end().to_owned())
}
