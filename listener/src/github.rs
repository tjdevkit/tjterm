//! Read the closing pull requests of a GitHub issue through the authenticated GitHub CLI.

use std::{io, process::Command};

use serde::Deserialize;
use thiserror::Error;

use crate::domain::forge::{IssueRef, Links, Oid, OidError, Pull, PullRef, ReferenceError, State};

const QUERY: &str = "query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo){issue(number:$number){closedByPullRequestsReferences(first:2,includeClosedPrs:true){totalCount nodes{url headRefOid state mergeCommit{oid}}}}}}";

/// GitHub could not provide an authoritative issue observation.
#[derive(Debug, Error)]
pub enum ReadError {
    /// The native CLI could not start or its output could not be collected.
    #[error("could not run the GitHub CLI")]
    Run(#[source] io::Error),
    /// The native CLI reported a failing exit status.
    #[error("could not read the GitHub issue: {0}")]
    Rejected(String),
    /// GitHub reported a GraphQL error, potentially alongside partial data.
    #[error("incomplete GitHub issue lookup")]
    Incomplete,
    /// The CLI returned an invalid GraphQL response envelope.
    #[error("invalid GitHub issue response")]
    Response(#[source] serde_json::Error),
    /// The returned node count could not represent the first page of up to two results.
    #[error("inconsistent GitHub pull request count")]
    Count,
    /// A closing link did not identify a pull request.
    #[error("invalid linked GitHub pull request")]
    Pull(#[source] ReferenceError),
    /// The observed head did not identify a commit.
    #[error("invalid linked pull request revision")]
    Head(#[source] OidError),
    /// A reported merge commit was not a full Git object identifier.
    #[error("invalid pull request merge commit")]
    Merge(#[source] OidError),
    /// A merged response needs its merge commit before the listener can close.
    #[error("merged PR has no merge commit")]
    MissingMerge,
}

/// The GitHub CLI adapter used to observe exact closing relationships.
pub struct Client;

impl Client {
    /// Query the tracker issue and validate the complete first page of closing links.
    pub fn read(&self, issue: &IssueRef) -> Result<Links, ReadError> {
        let output = Command::new("gh")
            .env(
                "GH_REPO",
                format!("github.com/{}/{}", issue.owner(), issue.repo()),
            )
            .args([
                "api",
                "--hostname",
                "github.com",
                "graphql",
                "-f",
                &format!("query={QUERY}"),
                "-f",
                &format!("owner={}", issue.owner()),
                "-f",
                &format!("repo={}", issue.repo()),
                "-F",
                &format!("number={}", issue.number()),
            ])
            .output()
            .map_err(ReadError::Run)?;
        if !output.status.success() {
            return Err(ReadError::Rejected(
                String::from_utf8_lossy(&output.stderr).trim().to_owned(),
            ));
        }
        Reply::decode(&output.stdout)
    }
}

#[derive(Deserialize)]
struct Reply {
    errors: Option<Vec<serde::de::IgnoredAny>>,
    data: Option<Data>,
}
#[derive(Deserialize)]
struct Data {
    repository: Option<Repository>,
}
#[derive(Deserialize)]
struct Repository {
    issue: Option<Issue>,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Issue {
    closed_by_pull_requests_references: Connection,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Connection {
    total_count: usize,
    nodes: Vec<PullNode>,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct PullNode {
    url: String,
    head_ref_oid: String,
    state: PullState,
    merge_commit: Option<Commit>,
}
#[derive(Deserialize)]
#[serde(rename_all = "UPPERCASE")]
enum PullState {
    Open,
    Closed,
    Merged,
}
#[derive(Deserialize)]
struct Commit {
    oid: String,
}

impl Reply {
    fn decode(bytes: &[u8]) -> Result<Links, ReadError> {
        let response: Self = serde_json::from_slice(bytes).map_err(ReadError::Response)?;
        if response.errors.is_some_and(|errors| !errors.is_empty()) {
            return Err(ReadError::Incomplete);
        }
        let issue = response
            .data
            .and_then(|data| data.repository)
            .and_then(|repo| repo.issue)
            .ok_or(ReadError::Incomplete)?;
        issue.closed_by_pull_requests_references.try_into()
    }
}

impl TryFrom<Connection> for Links {
    type Error = ReadError;
    fn try_from(page: Connection) -> Result<Self, Self::Error> {
        if page.nodes.len() != page.total_count.min(2) {
            return Err(ReadError::Count);
        }
        let mut pulls = page.nodes.into_iter().map(Pull::try_from);
        match (pulls.next().transpose()?, pulls.next().transpose()?) {
            (Some(pull), None) => Ok(Self::Single(pull)),
            _ => Ok(Self::NotSingle),
        }
    }
}

impl TryFrom<PullNode> for Pull {
    type Error = ReadError;
    fn try_from(node: PullNode) -> Result<Self, Self::Error> {
        let reference = PullRef::parse(&node.url).map_err(ReadError::Pull)?;
        let head = Oid::parse(&node.head_ref_oid).map_err(ReadError::Head)?;
        let merge = node
            .merge_commit
            .map(|commit| Oid::parse(&commit.oid))
            .transpose()
            .map_err(ReadError::Merge)?;
        let state = match node.state {
            PullState::Open => State::Open,
            PullState::Closed => State::Closed,
            PullState::Merged => State::Merged(merge.ok_or(ReadError::MissingMerge)?),
        };
        Ok(Self::new(reference, head, state))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};

    fn reply(nodes: &[Value], total: usize) -> Value {
        json!({"data":{"repository":{"issue":{"closedByPullRequestsReferences":{
            "nodes": nodes, "totalCount": total
        }}}}})
    }

    #[test]
    fn a_partial_graphql_response_never_becomes_an_actionable_link_change() {
        let node = json!({
            "url":"https://github.com/paper/widget/pull/2",
            "headRefOid":"b".repeat(40), "state":"OPEN", "mergeCommit":null
        });
        let mut partial = reply(std::slice::from_ref(&node), 1);
        partial["errors"] = json!([{"message":"partial data"}]);
        assert!(matches!(
            Reply::decode(partial.to_string().as_bytes()),
            Err(ReadError::Incomplete)
        ));
        assert!(matches!(
            Reply::decode(reply(&[], 1).to_string().as_bytes()),
            Err(ReadError::Count)
        ));
        assert!(matches!(
            Reply::decode(reply(&[node.clone(), node], 9).to_string().as_bytes()),
            Ok(Links::NotSingle)
        ));
    }

    #[test]
    fn a_merged_observation_requires_the_exact_merge_oid() {
        let node = json!({
            "url":"https://github.com/paper/widget/pull/2",
            "headRefOid":"a".repeat(40), "state":"MERGED", "mergeCommit":null
        });
        assert!(matches!(
            Reply::decode(reply(&[node], 1).to_string().as_bytes()),
            Err(ReadError::MissingMerge)
        ));
    }
}
