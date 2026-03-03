use anyhow::Result;
use chrono::{DateTime, Datelike, Utc};
use http::header::ACCEPT;
use http::StatusCode;
use indicatif::ProgressBar;
use octocrab::{models, Octocrab, OctocrabBuilder};
use rusqlite::{params, Connection};
use serde::Deserialize;
use serde_json::Value;
use std::collections::HashMap;
use std::collections::HashSet;

#[derive(Deserialize, Debug)]
struct SimpleUser {
    login: String,
}

#[derive(Deserialize, Debug)]
struct StarEntry {
    starred_at: Option<DateTime<Utc>>,
    user: Option<SimpleUser>,
}

pub struct GitHubClient<'a> {
    pub gh: Octocrab,
    db: &'a mut Connection,
    pb: ProgressBar,
}

impl<'a> GitHubClient<'a> {
    pub fn new(gh: Octocrab, db: &'a mut Connection, pb: ProgressBar) -> Self {
        Self { gh, db, pb }
    }

    /// Update spinner message and also log to tracing (visible in non-TTY/container mode).
    fn log_progress(&self, msg: &str) {
        self.pb.set_message(msg.to_string());
        tracing::info!("{}", msg);
    }

    pub async fn check_limits(&self) -> Result<()> {
        let rate = self.gh.ratelimit().get().await?;

        // Check REST API rate limit
        let core = rate.resources.core;
        if core.remaining < 50 {
            let reset = core.reset;
            let now = Utc::now().timestamp() as u64;
            let wait_secs = reset.saturating_sub(now) + 10;
            self.log_progress(&format!("REST rate limit low. Sleeping {}s...", wait_secs));
            tokio::time::sleep(tokio::time::Duration::from_secs(wait_secs)).await;
        }

        // Check GraphQL rate limit
        if let Some(graphql) = rate.resources.graphql {
            if graphql.remaining < 50 {
                let reset = graphql.reset;
                let now = Utc::now().timestamp() as u64;
                let wait_secs = reset.saturating_sub(now) + 10;
                self.log_progress(&format!("GraphQL rate limit low. Sleeping {}s...", wait_secs));
                tokio::time::sleep(tokio::time::Duration::from_secs(wait_secs)).await;
            }
        }

        Ok(())
    }

    /// Execute a GraphQL query with retry + exponential backoff on errors.
    async fn graphql_with_retry(&self, query: &str, max_retries: u32) -> Result<Value> {
        let mut attempt = 0;
        loop {
            self.check_limits().await?;
            let result: std::result::Result<Value, octocrab::Error> = self
                .gh
                .graphql(&serde_json::json!({ "query": query }))
                .await;

            match result {
                Ok(response) => {
                    // Check for rate limit errors in the GraphQL response
                    if let Some(errors) = response.get("errors") {
                        let err_str = errors.to_string();
                        if (err_str.contains("rate limit") || err_str.contains("abuse"))
                            && attempt < max_retries
                        {
                            let wait = 2u64.pow(attempt) * 10;
                            self.log_progress(&format!(
                                "GraphQL rate limited, retrying in {}s...", wait
                            ));
                            tokio::time::sleep(tokio::time::Duration::from_secs(wait)).await;
                            attempt += 1;
                            continue;
                        }
                    }
                    return Ok(response);
                }
                Err(e) => {
                    if attempt < max_retries {
                        let wait = 2u64.pow(attempt) * 5;
                        self.log_progress(&format!(
                            "GraphQL error (attempt {}/{}), retrying in {}s: {}",
                            attempt + 1, max_retries, wait, e
                        ));
                        tokio::time::sleep(tokio::time::Duration::from_secs(wait)).await;
                        attempt += 1;
                    } else {
                        return Err(e.into());
                    }
                }
            }
        }
    }

    pub async fn sync_org(&mut self, org: &str) -> Result<()> {
        self.check_limits().await?;
        let repos = self.fetch_repos(org).await?;
        let mut failures: Vec<String> = Vec::new();
        for repo in repos {
            self.log_progress(&format!("Syncing {}", repo.name));
            if let Err(e) = self.sync_repo(org, &repo).await {
                tracing::error!("Failed to sync {}: {}", repo.name, e);
                self.log_progress(&format!("WARN: {} sync failed, continuing...", repo.name));
                failures.push(repo.name.clone());
            }
        }

        // Sync GitHub Project V2 items (project #4 = Strands Agents board)
        if let Err(e) = self.sync_project_items(org, 4).await {
            tracing::error!("Failed to sync project items: {}", e);
            failures.push("project_items".to_string());
        }

        if !failures.is_empty() {
            tracing::warn!("Sync completed with failures: {:?}", failures);
        }

        Ok(())
    }

    pub async fn sweep_org(&mut self, org: &str) -> Result<()> {
        self.check_limits().await?;
        let repos = self.fetch_repos(org).await?;
        for repo in repos {
            self.log_progress(&format!("Sweeping {}", repo.name));
            self.sweep_repo(org, &repo).await?;
        }
        Ok(())
    }

    async fn fetch_repos(&self, org: &str) -> Result<Vec<models::Repository>> {
        let mut repos = Vec::new();
        let mut page = self.gh.orgs(org).list_repos().per_page(100).send().await?;
        repos.extend(page.items);
        while let Some(next) = page.next {
            self.check_limits().await?;
            page = self.gh.get_page(&Some(next)).await?.unwrap();
            repos.extend(page.items);
        }

        repos.retain(|r| {
            !r.archived.unwrap_or(false)
                && !r.private.unwrap_or(false)
                && !r.name.starts_with("private_")
        });

        Ok(repos)
    }

    async fn sweep_repo(&self, org: &str, repo: &models::Repository) -> Result<()> {
        let mut remote_open_numbers = HashSet::new();
        let route = format!("/repos/{}/{}/issues", org, repo.name);
        let mut page: octocrab::Page<Value> = self
            .gh
            .get(
                &route,
                Some(&serde_json::json!({
                    "state": "open", "per_page": 100
                })),
            )
            .await?;

        loop {
            let next_page = page.next.clone();
            for item in page.items {
                if let Some(num) = item.get("number").and_then(|n| n.as_i64()) {
                    remote_open_numbers.insert(num);
                }
            }
            if let Some(next) = next_page {
                self.check_limits().await?;
                page = self.gh.get_page(&Some(next)).await?.unwrap();
            } else {
                break;
            }
        }

        let mut stmt = self.db.prepare(
            "SELECT number FROM issues WHERE repo = ?1 AND state = 'open' AND closed_at IS NULL AND deleted_at IS NULL"
        )?;
        let local_open_nums: Vec<i64> = stmt
            .query_map(params![repo.name], |row| row.get(0))?
            .collect::<Result<Vec<_>, _>>()?;

        let now = Utc::now().to_rfc3339();

        for local_num in local_open_nums {
            if !remote_open_numbers.contains(&local_num) {
                self.check_limits().await?;
                let issue_route = format!("/repos/{}/{}/issues/{}", org, repo.name, local_num);

                let result: Result<Value, _> = self.gh.get(&issue_route, None::<&()>).await;

                match result {
                    Ok(json) => {
                        let state = json
                            .get("state")
                            .and_then(|s| s.as_str())
                            .unwrap_or("closed");
                        let closed_at = json.get("closed_at").and_then(|s| s.as_str());
                        self.db.execute(
                            "UPDATE issues SET state = ?1, closed_at = ?2 WHERE repo = ?3 AND number = ?4",
                            params![state, closed_at, repo.name, local_num]
                        )?;
                    }
                    Err(e) => {
                        if Self::is_missing_resource(&e) {
                            // Explicit 404/410 means deleted/missing
                            self.db.execute(
                                "UPDATE issues SET state = 'deleted', deleted_at = ?1 WHERE repo = ?2 AND number = ?3",
                                params![now, repo.name, local_num]
                            )?;
                        } else {
                            // Any other error (500, 502, timeout) is a crash.
                            return Err(e.into());
                        }
                    }
                }
            }
        }
        Ok(())
    }

    async fn sync_repo(&mut self, org: &str, repo: &models::Repository) -> Result<()> {
        let repo_name = &repo.name;
        let last_sync_key = format!("last_sync_{}_{}", org, repo_name);

        let since: DateTime<Utc> = self
            .db
            .query_row(
                "SELECT value FROM app_state WHERE key = ?1",
                params![last_sync_key],
                |row| {
                    let s: String = row.get(0)?;
                    Ok(DateTime::parse_from_rfc3339(&s)
                        .map(|dt| dt.with_timezone(&Utc))
                        .unwrap_or(Utc::now()))
                },
            )
            .unwrap_or_else(|_| {
                DateTime::parse_from_rfc3339("1970-01-01T00:00:00Z")
                    .unwrap()
                    .with_timezone(&Utc)
            });

        self.sync_pull_requests(org, repo_name, since).await?;
        self.sync_issues(org, repo_name, since).await?;
        self.sync_issue_comments(org, repo_name, since).await?;
        self.sync_pr_comments(org, repo_name, since).await?;
        self.sync_stars(org, repo).await?;
        self.sync_commits(org, repo_name, since).await?;
        self.sync_workflows(org, repo_name, since).await?;

        let now_str = Utc::now().to_rfc3339();
        self.db.execute(
            "INSERT OR REPLACE INTO app_state (key, value) VALUES (?1, ?2)",
            params![last_sync_key, now_str],
        )?;

        Ok(())
    }

    async fn sync_commits(&self, org: &str, repo: &str, since: DateTime<Utc>) -> Result<()> {
        self.check_limits().await?;

        let route = format!("/repos/{}/{}/commits", org, repo);
        let mut page: octocrab::Page<Value> = self
            .gh
            .get(
                &route,
                Some(&serde_json::json!({
                    "since": since.to_rfc3339(), "per_page": 100
                })),
            )
            .await?;

        loop {
            let next_page = page.next.clone();

            // Optimization: Collect SHAs and check in batch locally to avoid DB thrashing
            let mut shas = HashSet::new();
            for item in &page.items {
                if let Some(sha) = item.get("sha").and_then(|s| s.as_str()) {
                    shas.insert(sha.to_string());
                }
            }

            for sha in shas {
                // Check if exists
                let exists: bool = self
                    .db
                    .query_row("SELECT 1 FROM commits WHERE sha = ?1", params![sha], |_| {
                        Ok(true)
                    })
                    .unwrap_or(false);

                if !exists {
                    // We must fetch details to get stats (additions/deletions)
                    // Check limits BEFORE the heavy call
                    self.check_limits().await?;

                    let detail_route = format!("/repos/{}/{}/commits/{}", org, repo, sha);
                    let detail: Value = self.gh.get(&detail_route, None::<&()>).await?;

                    let author = detail
                        .get("commit")
                        .and_then(|c| c.get("author"))
                        .and_then(|a| a.get("name"))
                        .and_then(|n| n.as_str())
                        .unwrap_or("unknown");

                    let date_str = detail
                        .get("commit")
                        .and_then(|c| c.get("author"))
                        .and_then(|a| a.get("date"))
                        .and_then(|d| d.as_str())
                        .unwrap_or("");

                    let stats = detail.get("stats");
                    let adds = stats
                        .and_then(|s| s.get("additions"))
                        .and_then(|v| v.as_i64())
                        .unwrap_or(0);
                    let dels = stats
                        .and_then(|s| s.get("deletions"))
                        .and_then(|v| v.as_i64())
                        .unwrap_or(0);
                    let msg = detail
                        .get("commit")
                        .and_then(|c| c.get("message"))
                        .and_then(|m| m.as_str())
                        .unwrap_or("");

                    self.db.execute(
                        "INSERT OR REPLACE INTO commits (sha, repo, author, date, additions, deletions, message) 
                         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                        params![sha, repo, author, date_str, adds, dels, msg]
                    )?;
                }
            }

            if let Some(next) = next_page {
                self.check_limits().await?;
                page = self.gh.get_page(&Some(next)).await?.unwrap();
            } else {
                break;
            }
        }
        Ok(())
    }

    async fn sync_workflows(&self, org: &str, repo: &str, since: DateTime<Utc>) -> Result<()> {
        self.check_limits().await?;
        let route = format!("/repos/{}/{}/actions/runs", org, repo);
        let created_filter = format!(">{}", since.format("%Y-%m-%d"));

        let mut page: octocrab::Page<Value> = self
            .gh
            .get(
                &route,
                Some(&serde_json::json!({
                    "created": created_filter, "per_page": 100
                })),
            )
            .await?;

        loop {
            let next_page = page.next.clone();
            for run in page.items {
                let id = run.get("id").and_then(|v| v.as_i64()).unwrap_or(0);
                let name = run.get("name").and_then(|v| v.as_str()).unwrap_or("");
                let head = run
                    .get("head_branch")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let conclusion = run
                    .get("conclusion")
                    .and_then(|v| v.as_str())
                    .unwrap_or("in_progress");
                let created_at = run.get("created_at").and_then(|v| v.as_str()).unwrap_or("");
                let updated_at = run.get("updated_at").and_then(|v| v.as_str()).unwrap_or("");

                let duration = if let (Some(start), Some(end)) = (
                    run.get("created_at").and_then(|v| v.as_str()),
                    run.get("updated_at").and_then(|v| v.as_str()),
                ) {
                    let s = DateTime::parse_from_rfc3339(start).unwrap_or(Utc::now().into());
                    let e = DateTime::parse_from_rfc3339(end).unwrap_or(Utc::now().into());
                    (e - s).num_milliseconds()
                } else {
                    0
                };

                self.db.execute(
                    "INSERT OR REPLACE INTO workflow_runs (id, repo, name, head_branch, conclusion, created_at, updated_at, duration_ms)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                    params![id, repo, name, head, conclusion, created_at, updated_at, duration]
                )?;
            }

            if let Some(next) = next_page {
                self.check_limits().await?;
                page = self.gh.get_page(&Some(next)).await?.unwrap();
            } else {
                break;
            }
        }
        Ok(())
    }

    async fn sync_stars(&mut self, org: &str, repo: &models::Repository) -> Result<()> {
        self.check_limits().await?;
        let token = std::env::var("GITHUB_TOKEN").unwrap_or_default();
        let star_gh = OctocrabBuilder::new()
            .personal_token(token)
            .add_header(ACCEPT, "application/vnd.github.star+json".to_string())
            .build()?;

        let mut remote_users = HashSet::new();

        let route = format!("/repos/{}/{}/stargazers", org, repo.name);
        let mut page: octocrab::Page<StarEntry> = star_gh
            .get(&route, Some(&serde_json::json!({ "per_page": 100 })))
            .await?;

        loop {
            let next_page = page.next.clone();
            for entry in page.items {
                if let (Some(starred_at), Some(user)) = (entry.starred_at, entry.user) {
                    remote_users.insert(user.login.clone());
                    self.db.execute(
                        "INSERT OR REPLACE INTO stargazers (repo, user, starred_at) VALUES (?1, ?2, ?3)",
                        params![repo.name, user.login, starred_at.to_rfc3339()],
                    )?;
                }
            }
            if let Some(next) = next_page {
                self.check_limits().await?;
                page = star_gh.get_page(&Some(next)).await?.unwrap();
            } else {
                break;
            }
        }

        let mut stmt = self
            .db
            .prepare("SELECT user FROM stargazers WHERE repo = ?1")?;
        let rows = stmt.query_map(params![repo.name], |row| row.get::<_, String>(0))?;

        let mut to_delete = Vec::new();
        for local_user in rows {
            let u = local_user?;
            if !remote_users.contains(&u) {
                to_delete.push(u);
            }
        }

        for u in to_delete {
            self.db.execute(
                "DELETE FROM stargazers WHERE repo = ?1 AND user = ?2",
                params![repo.name, u],
            )?;
        }

        Ok(())
    }

    async fn sync_pull_requests(&self, org: &str, repo: &str, since: DateTime<Utc>) -> Result<()> {
        self.check_limits().await?;
        let mut page = self
            .gh
            .pulls(org, repo)
            .list()
            .state(octocrab::params::State::All)
            .sort(octocrab::params::pulls::Sort::Updated)
            .direction(octocrab::params::Direction::Descending)
            .per_page(100)
            .send()
            .await?;

        let mut keep_fetching = true;
        loop {
            let next_page = page.next;
            for pr in page.items {
                if let Some(updated) = pr.updated_at {
                    if updated < since {
                        keep_fetching = false;
                        break;
                    }
                }

                let json = serde_json::to_string(&pr)?;
                let pr_id = pr.id.0 as i64;
                let pr_number = pr.number as i64;
                let state_str = match pr.state {
                    Some(models::IssueState::Open) => "open",
                    Some(models::IssueState::Closed) => "closed",
                    _ => "unknown",
                };

                self.db.execute(
                    "INSERT OR REPLACE INTO pull_requests 
                    (id, repo, number, state, author, title, created_at, updated_at, merged_at, closed_at, data) 
                    VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
                    params![
                        pr_id, repo, pr_number, state_str,
                        pr.user.as_ref().map(|u| u.login.clone()).unwrap_or_default(),
                        pr.title.unwrap_or_default(),
                        pr.created_at.map(|d| d.to_rfc3339()).unwrap_or_default(),
                        pr.updated_at.map(|d| d.to_rfc3339()).unwrap_or_default(),
                        pr.merged_at.map(|t| t.to_rfc3339()),
                        pr.closed_at.map(|t| t.to_rfc3339()),
                        json
                    ],
                )?;

                if pr.updated_at.map(|t| t >= since).unwrap_or(false) {
                    self.sync_reviews(org, repo, pr.number).await?;
                }
            }

            if !keep_fetching {
                break;
            }
            if let Some(next) = next_page {
                self.check_limits().await?;
                page = self.gh.get_page(&Some(next)).await?.unwrap();
            } else {
                break;
            }
        }
        Ok(())
    }

    async fn sync_reviews(&self, org: &str, repo: &str, pr_number: u64) -> Result<()> {
        let mut page = self
            .gh
            .pulls(org, repo)
            .list_reviews(pr_number)
            .per_page(100)
            .send()
            .await?;
        loop {
            let next_page = page.next;
            for review in page.items {
                let json = serde_json::to_string(&review)?;
                let review_id = review.id.0 as i64;
                let pr_num = pr_number as i64;
                let state_str = review
                    .state
                    .map(|s| format!("{:?}", s).to_uppercase())
                    .unwrap_or_else(|| "UNKNOWN".to_string());

                self.db.execute(
                    "INSERT OR REPLACE INTO pr_reviews (id, repo, pr_number, state, author, submitted_at, data)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                    params![
                        review_id, repo, pr_num, state_str,
                        review.user.as_ref().map(|u| u.login.clone()).unwrap_or_default(),
                        review.submitted_at.map(|t| t.to_rfc3339()).unwrap_or_default(),
                        json
                    ],
                )?;
            }
            if let Some(next) = next_page {
                self.check_limits().await?;
                page = self.gh.get_page(&Some(next)).await?.unwrap();
            } else {
                break;
            }
        }
        Ok(())
    }

    async fn sync_issues(&self, org: &str, repo: &str, since: DateTime<Utc>) -> Result<()> {
        self.check_limits().await?;
        let route = format!("/repos/{}/{}/issues", org, repo);

        // GitHub's /issues endpoint rejects very old "since" dates (returns 0 items).
        // This appears to work for our use case.
        let use_since_filter = since.year() >= 2010;

        let mut page: octocrab::Page<Value> = if use_since_filter {
            self.gh.get(&route, Some(&serde_json::json!({
                "state": "all", "sort": "updated", "direction": "desc", "since": since.to_rfc3339(), "per_page": 100
            }))).await?
        } else {
            // First sync: don't pass since parameter to avoid GitHub API bug
            self.gh.get(&route, Some(&serde_json::json!({
                "state": "all", "sort": "updated", "direction": "desc", "per_page": 100
            }))).await?
        };

        let mut keep_fetching = true;
        loop {
            let next_page = page.next.clone();
            for issue in page.items {
                let updated_at_str = issue
                    .get("updated_at")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let updated_at = DateTime::parse_from_rfc3339(updated_at_str)
                    .map(|dt| dt.with_timezone(&Utc))
                    .unwrap_or_else(|_| Utc::now());

                if updated_at < since {
                    keep_fetching = false;
                    break;
                }
                if issue.get("pull_request").is_some() {
                    continue;
                }

                let json = serde_json::to_string(&issue)?;
                let id = issue.get("id").and_then(|v| v.as_i64()).unwrap_or(0);
                let number = issue.get("number").and_then(|v| v.as_i64()).unwrap_or(0);
                let state = issue
                    .get("state")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown");
                let author = issue
                    .get("user")
                    .and_then(|u| u.get("login"))
                    .and_then(|l| l.as_str())
                    .unwrap_or("unknown");
                let title = issue.get("title").and_then(|v| v.as_str()).unwrap_or("");
                let created = issue
                    .get("created_at")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let closed = issue.get("closed_at").and_then(|v| v.as_str());

                self.db.execute(
                    "INSERT OR REPLACE INTO issues 
                    (id, repo, number, state, author, title, created_at, updated_at, closed_at, data) 
                    VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
                    params![id, repo, number, state, author, title, created, updated_at_str, closed, json],
                )?;
            }
            if !keep_fetching {
                break;
            }
            if let Some(next) = next_page {
                self.check_limits().await?;
                page = self.gh.get_page(&Some(next)).await?.unwrap();
            } else {
                break;
            }
        }
        Ok(())
    }

    async fn sync_issue_comments(&self, org: &str, repo: &str, since: DateTime<Utc>) -> Result<()> {
        self.check_limits().await?;
        let route = format!("/repos/{}/{}/issues/comments", org, repo);
        let mut page: octocrab::Page<Value> = self.gh.get(&route, Some(&serde_json::json!({
                "sort": "updated", "direction": "desc", "since": since.to_rfc3339(), "per_page": 100
            }))).await?;

        let mut keep_fetching = true;
        loop {
            let next_page = page.next.clone();
            for comment in page.items {
                let updated_at_str = comment
                    .get("updated_at")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let updated_at = DateTime::parse_from_rfc3339(updated_at_str)
                    .map(|dt| dt.with_timezone(&Utc))
                    .unwrap_or_else(|_| Utc::now());

                if updated_at < since {
                    keep_fetching = false;
                    break;
                }
                let issue_url = comment
                    .get("issue_url")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let issue_number: i64 = issue_url
                    .split('/')
                    .next_back()
                    .unwrap_or("0")
                    .parse()
                    .unwrap_or(0);
                let id = comment.get("id").and_then(|v| v.as_i64()).unwrap_or(0);
                let author = comment
                    .get("user")
                    .and_then(|u| u.get("login"))
                    .and_then(|l| l.as_str())
                    .unwrap_or("unknown");
                let created = comment
                    .get("created_at")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let json = serde_json::to_string(&comment)?;

                self.db.execute(
                    "INSERT OR REPLACE INTO issue_comments (id, repo, issue_number, author, created_at, updated_at, data)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                    params![id, repo, issue_number, author, created, updated_at_str, json],
                )?;
            }
            if !keep_fetching {
                break;
            }
            if let Some(next) = next_page {
                self.check_limits().await?;
                page = self.gh.get_page(&Some(next)).await?.unwrap();
            } else {
                break;
            }
        }
        Ok(())
    }

    async fn sync_pr_comments(&self, org: &str, repo: &str, since: DateTime<Utc>) -> Result<()> {
        self.check_limits().await?;
        let route = format!("/repos/{}/{}/pulls/comments", org, repo);
        let mut page: octocrab::Page<Value> = self.gh.get(&route, Some(&serde_json::json!({
                "sort": "updated", "direction": "desc", "since": since.to_rfc3339(), "per_page": 100
            }))).await?;

        let mut keep_fetching = true;
        loop {
            let next_page = page.next.clone();
            for comment in page.items {
                let updated_at_str = comment
                    .get("updated_at")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let updated_at = DateTime::parse_from_rfc3339(updated_at_str)
                    .map(|dt| dt.with_timezone(&Utc))
                    .unwrap_or_else(|_| Utc::now());

                if updated_at < since {
                    keep_fetching = false;
                    break;
                }
                let pull_url = comment
                    .get("pull_request_url")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let pr_number: i64 = pull_url
                    .split('/')
                    .next_back()
                    .unwrap_or("0")
                    .parse()
                    .unwrap_or(0);
                let id = comment.get("id").and_then(|v| v.as_i64()).unwrap_or(0);
                let author = comment
                    .get("user")
                    .and_then(|u| u.get("login"))
                    .and_then(|l| l.as_str())
                    .unwrap_or("unknown");
                let created = comment
                    .get("created_at")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let json = serde_json::to_string(&comment)?;

                self.db.execute(
                    "INSERT OR REPLACE INTO pr_review_comments (id, repo, pr_number, author, created_at, updated_at, data)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                    params![id, repo, pr_number, author, created, updated_at_str, json],
                )?;
            }
            if !keep_fetching {
                break;
            }
            if let Some(next) = next_page {
                self.check_limits().await?;
                page = self.gh.get_page(&Some(next)).await?.unwrap();
            } else {
                break;
            }
        }
        Ok(())
    }

    /// Sync GitHub Project V2 items (org-level, not per-repo).
    /// Fetches priority and status fields from the specified project number.
    pub async fn sync_project_items(&self, org: &str, project_number: i32) -> Result<()> {
        self.log_progress(&format!("Syncing project #{} items...", project_number));

        // Snapshot existing priorities so we can detect NULL → non-NULL transitions
        let mut old_priorities: HashMap<String, Option<String>> = HashMap::new();
        {
            let mut stmt = self.db.prepare(
                "SELECT node_id, priority FROM project_items"
            )?;
            let rows = stmt.query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, Option<String>>(1)?))
            })?;
            for row in rows {
                let (nid, pri) = row?;
                old_priorities.insert(nid, pri);
            }
        }

        // Snapshot existing triaged_at values to preserve them
        let mut old_triaged: HashMap<String, Option<String>> = HashMap::new();
        {
            let mut stmt = self.db.prepare(
                "SELECT node_id, triaged_at FROM project_items"
            )?;
            let rows = stmt.query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, Option<String>>(1)?))
            })?;
            for row in rows {
                let (nid, ta) = row?;
                old_triaged.insert(nid, ta);
            }
        }

        // Clear and re-sync fully — wrapped in a transaction so a mid-sync
        // failure (network error, rate limit) doesn't lose all project items.
        self.db.execute_batch("BEGIN")?;

        let result = self
            .sync_project_items_inner(org, project_number, &old_priorities, &old_triaged)
            .await;

        match result {
            Ok(total) => {
                self.db.execute_batch("COMMIT")?;
                self.log_progress(&format!("Synced {} project items", total));
                Ok(())
            }
            Err(e) => {
                let _ = self.db.execute_batch("ROLLBACK");
                Err(e)
            }
        }
    }

    async fn sync_project_items_inner(
        &self,
        org: &str,
        project_number: i32,
        old_priorities: &HashMap<String, Option<String>>,
        old_triaged: &HashMap<String, Option<String>>,
    ) -> Result<u64> {
        self.db.execute("DELETE FROM project_items", [])?;

        let now = Utc::now().to_rfc3339();
        let mut cursor: Option<String> = None;
        let mut total = 0u64;

        loop {
            self.check_limits().await?;

            let after_clause = cursor
                .as_ref()
                .map(|c| format!(r#", after: "{}""#, c))
                .unwrap_or_default();

            let query = format!(
                r#"query {{
  organization(login: "{}") {{
    projectV2(number: {}) {{
      items(first: 100{}) {{
        nodes {{
          id
          updatedAt
          content {{
            ... on Issue {{
              number
              repository {{ name }}
            }}
          }}
          priority: fieldValueByName(name: "Priority") {{
            ... on ProjectV2ItemFieldSingleSelectValue {{
              name
            }}
          }}
          status: fieldValueByName(name: "Status") {{
            ... on ProjectV2ItemFieldSingleSelectValue {{
              name
            }}
          }}
        }}
        pageInfo {{
          hasNextPage
          endCursor
        }}
      }}
    }}
  }}
}}"#,
                org, project_number, after_clause
            );

            let response: Value = self
                .graphql_with_retry(&query, 3)
                .await?;

            // Check for GraphQL errors
            if let Some(errors) = response.get("errors") {
                let err_msg = errors.to_string();
                if err_msg.contains("Could not resolve") || err_msg.contains("insufficient") {
                    tracing::error!(
                        "Cannot access project #{} — ensure GITHUB_TOKEN has project read scope. Error: {}",
                        project_number, err_msg
                    );
                    return Ok(());
                }
                tracing::warn!("GraphQL errors syncing project items: {}", err_msg);
            }

            let items = match response.pointer("/data/organization/projectV2/items") {
                Some(i) => i,
                None => {
                    tracing::error!(
                        "No project data returned for project #{}. Ensure GITHUB_TOKEN has 'read:project' scope. Response: {}",
                        project_number,
                        serde_json::to_string_pretty(&response).unwrap_or_default()
                    );
                    return Ok(());
                }
            };

            let nodes = items
                .get("nodes")
                .and_then(|n| n.as_array())
                .cloned()
                .unwrap_or_default();

            for node in &nodes {
                let node_id = node.get("id").and_then(|v| v.as_str()).unwrap_or("");
                let updated_at = node
                    .get("updatedAt")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");

                // content can be null for draft items
                let content = match node.get("content") {
                    Some(c) if !c.is_null() => c,
                    _ => continue,
                };

                let issue_number = match content.get("number").and_then(|n| n.as_i64()) {
                    Some(n) => n,
                    None => continue, // Not an issue (could be a PR or draft)
                };

                let repo = content
                    .pointer("/repository/name")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");

                if repo.is_empty() {
                    continue;
                }

                let priority = node.pointer("/priority/name").and_then(|v| v.as_str());
                let status = node.pointer("/status/name").and_then(|v| v.as_str());

                // Determine triaged_at:
                // 1. If we already had a triaged_at, preserve it
                // 2. If item existed before with NULL priority and now has priority, set triaged_at = now
                // 3. Brand new items (not in old_priorities) → leave NULL for backfill to handle
                // 4. Otherwise leave NULL
                let triaged_at = if let Some(existing) = old_triaged.get(node_id).and_then(|t| t.as_deref()) {
                    Some(existing.to_string())
                } else if priority.is_some() && old_priorities.contains_key(node_id) {
                    // Item existed before — check if priority transitioned from NULL
                    let was_null = old_priorities.get(node_id).map(|p| p.is_none()).unwrap_or(false);
                    if was_null { Some(now.clone()) } else { None }
                } else {
                    None
                };

                self.db.execute(
                    "INSERT OR REPLACE INTO project_items (node_id, repo, issue_number, priority, status, updated_at, triaged_at)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                    params![node_id, repo, issue_number, priority, status, updated_at, triaged_at],
                )?;
                total += 1;
            }

            let page_info = items.get("pageInfo");
            let has_next = page_info
                .and_then(|p| p.get("hasNextPage"))
                .and_then(|v| v.as_bool())
                .unwrap_or(false);

            if has_next {
                cursor = page_info
                    .and_then(|p| p.get("endCursor"))
                    .and_then(|v| v.as_str())
                    .map(String::from);
            } else {
                break;
            }
        }

        Ok(total)
    }

    /// Backfill triaged_at timestamps by checking issue timeline events.
    /// For each project item that has a priority but no triaged_at, fetches the
    /// issue timeline to find when the priority field was first set.
    pub async fn backfill_triage_timestamps(&self, org: &str) -> Result<()> {
        // Find items that have priority set but no triaged_at
        let items: Vec<(String, String, i64)> = {
            let mut stmt = self.db.prepare(
                "SELECT node_id, repo, issue_number FROM project_items
                 WHERE priority IS NOT NULL AND triaged_at IS NULL"
            )?;
            let result = stmt.query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, i64>(2)?,
                ))
            })?
            .collect::<Result<Vec<_>, _>>()?;
            result
        };

        let total = items.len();
        self.log_progress(&format!("Backfilling triage timestamps for {} items...", total));

        for (i, (node_id, repo, issue_number)) in items.iter().enumerate() {
            if i % 10 == 0 {
                self.log_progress(&format!(
                    "Backfilling triage timestamps... {}/{}", i, total
                ));
            }

            self.check_limits().await?;

            // Fetch issue timeline events looking for project field changes
            let triaged_at = self
                .find_triage_timestamp(org, repo, *issue_number as u64)
                .await?;

            if let Some(ts) = triaged_at {
                self.db.execute(
                    "UPDATE project_items SET triaged_at = ?1 WHERE node_id = ?2",
                    params![ts, node_id],
                )?;
            } else {
                // Fallback: use the issue's created_at as a rough approximation
                // (better than nothing for historical data)
                let created_at: Option<String> = self.db.query_row(
                    "SELECT created_at FROM issues WHERE repo = ?1 AND number = ?2",
                    params![repo, issue_number],
                    |row| row.get(0),
                ).ok();
                if let Some(ca) = created_at {
                    self.db.execute(
                        "UPDATE project_items SET triaged_at = ?1 WHERE node_id = ?2",
                        params![ca, node_id],
                    )?;
                }
            }
        }

        self.log_progress(&format!("Backfilled triage timestamps for {} items", total));
        Ok(())
    }

    /// Look through issue timeline events to find when the issue was triaged.
    /// Checks for: 1) priority field change, 2) added to project (as proxy for triage).
    async fn find_triage_timestamp(
        &self,
        org: &str,
        repo: &str,
        issue_number: u64,
    ) -> Result<Option<String>> {
        let mut page = 1u32;
        let mut added_to_project_at: Option<String> = None;

        loop {
            self.check_limits().await?;

            let route = format!(
                "/repos/{}/{}/issues/{}/timeline",
                org, repo, issue_number
            );

            let result: Result<Vec<Value>, _> = self
                .gh
                .get(
                    &route,
                    Some(&serde_json::json!({
                        "per_page": 100,
                        "page": page
                    })),
                )
                .await;

            let events = match result {
                Ok(e) => e,
                Err(e) => {
                    if Self::is_missing_resource(&e) {
                        return Ok(None);
                    }
                    tracing::warn!(
                        "Error fetching timeline for {}/{}#{}: {}",
                        org, repo, issue_number, e
                    );
                    return Ok(None);
                }
            };

            if events.is_empty() {
                break;
            }

            for event in &events {
                let event_type = event.get("event").and_then(|v| v.as_str()).unwrap_or("");

                // Best signal: explicit priority field change
                if event_type == "project_v2_item_field_value_changed" {
                    let field_name = event
                        .pointer("/project_v2_item_field_value_change/field_name")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");

                    if field_name == "Priority" {
                        let created_at = event
                            .get("created_at")
                            .and_then(|v| v.as_str())
                            .unwrap_or("");
                        if !created_at.is_empty() {
                            return Ok(Some(created_at.to_string()));
                        }
                    }
                }

                // Fallback signal: when the issue was added to the project board
                if event_type == "added_to_project_v2" && added_to_project_at.is_none() {
                    if let Some(ts) = event.get("created_at").and_then(|v| v.as_str()) {
                        added_to_project_at = Some(ts.to_string());
                    }
                }
            }

            if events.len() < 100 {
                break;
            }
            page += 1;
        }

        // Return added_to_project timestamp as proxy for triage time
        Ok(added_to_project_at)
    }

    fn is_missing_resource(err: &octocrab::Error) -> bool {
        match err {
            octocrab::Error::GitHub { source, .. } => {
                source.status_code == StatusCode::NOT_FOUND
                    || source.status_code == StatusCode::GONE
                    || source.message.eq_ignore_ascii_case("Not Found")
                    || source.message.eq_ignore_ascii_case("Not Found.")
            }
            _ => false,
        }
    }
}
