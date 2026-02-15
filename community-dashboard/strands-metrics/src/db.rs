use anyhow::Result;
use rusqlite::Connection;
use std::path::Path;

pub fn init_db<P: AsRef<Path>>(path: P) -> Result<Connection> {
    let conn = Connection::open(path)?;

    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )",
        [],
    )?;

    conn.execute(
        "CREATE TABLE IF NOT EXISTS pull_requests (
            id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL,
            number INTEGER NOT NULL,
            state TEXT NOT NULL,
            author TEXT NOT NULL,
            title TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            merged_at TEXT,
            closed_at TEXT,
            deleted_at TEXT, 
            data TEXT NOT NULL
        )",
        [],
    )?;

    conn.execute(
        "CREATE TABLE IF NOT EXISTS issues (
            id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL,
            number INTEGER NOT NULL,
            state TEXT NOT NULL,
            author TEXT NOT NULL,
            title TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            closed_at TEXT,
            deleted_at TEXT,
            data TEXT NOT NULL
        )",
        [],
    )?;

    conn.execute(
        "CREATE TABLE IF NOT EXISTS issue_comments (
            id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            author TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            data TEXT NOT NULL
        )",
        [],
    )?;

    conn.execute(
        "CREATE TABLE IF NOT EXISTS pr_reviews (
            id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            state TEXT NOT NULL,
            author TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            data TEXT NOT NULL
        )",
        [],
    )?;

    conn.execute(
        "CREATE TABLE IF NOT EXISTS pr_review_comments (
            id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            author TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            data TEXT NOT NULL
        )",
        [],
    )?;

    conn.execute(
        "CREATE TABLE IF NOT EXISTS stargazers (
            repo TEXT NOT NULL,
            user TEXT NOT NULL,
            starred_at TEXT NOT NULL,
            PRIMARY KEY (repo, user)
        )",
        [],
    )?;

    conn.execute(
        "CREATE TABLE IF NOT EXISTS commits (
            sha TEXT PRIMARY KEY,
            repo TEXT NOT NULL,
            author TEXT NOT NULL,
            date TEXT NOT NULL,
            additions INTEGER DEFAULT 0,
            deletions INTEGER DEFAULT 0,
            message TEXT
        )",
        [],
    )?;

    conn.execute(
        "CREATE TABLE IF NOT EXISTS workflow_runs (
            id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL,
            name TEXT,
            head_branch TEXT,
            conclusion TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            duration_ms INTEGER DEFAULT 0
        )",
        [],
    )?;

    conn.execute(
        "CREATE TABLE IF NOT EXISTS daily_metrics (
            date TEXT NOT NULL,
            repo TEXT NOT NULL,

            prs_opened INTEGER DEFAULT 0,
            prs_merged INTEGER DEFAULT 0,
            issues_opened INTEGER DEFAULT 0,
            issues_closed INTEGER DEFAULT 0,

            churn_additions INTEGER DEFAULT 0,
            churn_deletions INTEGER DEFAULT 0,

            ci_failures INTEGER DEFAULT 0,
            ci_runs INTEGER DEFAULT 0,

            stars INTEGER DEFAULT 0,

            open_items_count INTEGER DEFAULT 0,
            open_issues_count INTEGER DEFAULT 0,
            open_prs_count INTEGER DEFAULT 0,

            time_to_first_response REAL DEFAULT 0,
            avg_issue_resolution_time REAL DEFAULT 0,
            avg_pr_resolution_time REAL DEFAULT 0,

            time_to_merge_internal REAL DEFAULT 0,
            time_to_merge_external REAL DEFAULT 0,

            PRIMARY KEY (date, repo)
        )",
        [],
    )?;

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pr_repo_updated ON pull_requests(repo, updated_at)",
        [],
    )?;
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_issues_repo_updated ON issues(repo, updated_at)",
        [],
    )?;
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_comments_repo_issue ON issue_comments(repo, issue_number)",
        [],
    )?;
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reviews_repo_pr ON pr_reviews(repo, pr_number)",
        [],
    )?;
    conn.execute("CREATE INDEX IF NOT EXISTS idx_review_comments_repo_pr ON pr_review_comments(repo, pr_number)", [])?;
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stars_repo_date ON stargazers(repo, starred_at)",
        [],
    )?;
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_commits_repo_date ON commits(repo, date)",
        [],
    )?;
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_workflows_repo_date ON workflow_runs(repo, created_at)",
        [],
    )?;

    // Package download tracking
    conn.execute(
        "CREATE TABLE IF NOT EXISTS package_downloads (
            date TEXT NOT NULL,
            package TEXT NOT NULL,
            registry TEXT NOT NULL,
            version TEXT,
            downloads INTEGER DEFAULT 0,
            PRIMARY KEY (date, package, registry, version)
        )",
        [],
    )?;

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_downloads_package_date ON package_downloads(package, date)",
        [],
    )?;

    Ok(conn)
}
