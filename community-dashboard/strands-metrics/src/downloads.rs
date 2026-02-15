use anyhow::{Context, Result};
use chrono::{Duration, Utc};
use rusqlite::{params, Connection};
use serde::Deserialize;

// ============================================================================
// PyPI API Types
// ============================================================================

#[derive(Debug, Deserialize)]
struct PyPIStatsResponse {
    data: Vec<PyPIDataPoint>,
}

#[derive(Debug, Deserialize)]
struct PyPIDataPoint {
    date: String,
    downloads: i64,
}

// ============================================================================
// npm API Types
// ============================================================================

#[derive(Debug, Deserialize)]
struct NpmRangeResponse {
    downloads: Vec<NpmDownloadPoint>,
}

#[derive(Debug, Deserialize)]
struct NpmDownloadPoint {
    day: String,
    downloads: i64,
}

// ============================================================================
// Sync Functions
// ============================================================================

pub async fn sync_pypi_downloads(
    conn: &Connection,
    package: &str,
    days: i64,
) -> Result<usize> {
    let client = reqwest::Client::new();
    let mut total_inserted = 0;

    // First, get overall daily downloads
    let url = format!(
        "https://pypistats.org/api/packages/{}/overall?mirrors=false",
        package
    );

    let response: PyPIStatsResponse = client
        .get(&url)
        .header("User-Agent", "strands-metrics/1.0")
        .send()
        .await?
        .json()
        .await
        .context(format!("Failed to fetch PyPI stats for {}", package))?;

    let cutoff = (Utc::now() - Duration::days(days))
        .format("%Y-%m-%d")
        .to_string();

    // PyPI stats are updated daily around 01:00 UTC, data is for previous day
    // Skip today and yesterday to avoid incomplete data
    let max_date = (Utc::now() - Duration::days(1))
        .format("%Y-%m-%d")
        .to_string();

    for point in response.data {
        // Only include data within our date range, excluding incomplete recent data
        if point.date >= cutoff && point.date <= max_date {
            conn.execute(
                "INSERT INTO package_downloads (date, package, registry, version, downloads)
                 VALUES (?1, ?2, 'pypi', 'total', ?3)
                 ON CONFLICT(date, package, registry, version) DO UPDATE SET downloads = excluded.downloads",
                params![point.date, package, point.downloads],
            )?;
            total_inserted += 1;
        }
    }

    // Now get per-version downloads if available
    let _version_url = format!(
        "https://pypistats.org/api/packages/{}/python_minor?mirrors=false",
        package
    );

    // Note: pypistats doesn't have per-package-version data easily available
    // The python_minor endpoint shows by Python version, not package version
    // For true per-version data, we'd need BigQuery access
    // For now, we'll track totals which is most useful for adoption metrics

    Ok(total_inserted)
}

pub async fn sync_npm_downloads(
    conn: &Connection,
    package: &str,
    days: i64,
) -> Result<usize> {
    let client = reqwest::Client::new();
    let mut total_inserted = 0;

    // npm stats are updated daily, data for a given day is available the next day
    // Use yesterday as end date to avoid incomplete data
    let end_date = (Utc::now() - Duration::days(1))
        .format("%Y-%m-%d")
        .to_string();
    let start_date = (Utc::now() - Duration::days(days))
        .format("%Y-%m-%d")
        .to_string();

    // Get daily downloads for the date range
    let url = format!(
        "https://api.npmjs.org/downloads/range/{}:{}/{}",
        start_date, end_date, package
    );

    let response: NpmRangeResponse = client
        .get(&url)
        .header("User-Agent", "strands-metrics/1.0")
        .send()
        .await?
        .json()
        .await
        .context(format!("Failed to fetch npm stats for {}", package))?;

    for point in response.downloads {
        conn.execute(
            "INSERT INTO package_downloads (date, package, registry, version, downloads)
             VALUES (?1, ?2, 'npm', 'total', ?3)
             ON CONFLICT(date, package, registry, version) DO UPDATE SET downloads = excluded.downloads",
            params![point.day, package, point.downloads],
        )?;
        total_inserted += 1;
    }

    // Get per-version data from npm registry
    // npm doesn't provide per-version download counts via public API
    // The downloads API only gives totals
    // For version breakdown, we'd need to use npm's BigQuery dataset

    Ok(total_inserted)
}

pub async fn backfill_pypi_downloads(
    conn: &Connection,
    package: &str,
) -> Result<usize> {
    // PyPI stats API provides ~180 days of history
    sync_pypi_downloads(conn, package, 180).await
}

pub async fn backfill_npm_downloads(
    conn: &Connection,
    package: &str,
) -> Result<usize> {
    // npm API allows fetching up to 18 months of history
    // Let's fetch 365 days to get a good history
    sync_npm_downloads(conn, package, 365).await
}

// ============================================================================
// Config Loading
// ============================================================================

#[derive(Debug, Deserialize)]
pub struct PackagesConfig {
    #[serde(default)]
    pub repo_mappings: std::collections::HashMap<String, Vec<PackageMapping>>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct PackageMapping {
    pub package: String,
    pub registry: String,
}

impl PackagesConfig {
    /// Get all unique packages for a given registry
    pub fn packages_for_registry(&self, registry: &str) -> Vec<String> {
        let mut packages: Vec<String> = self
            .repo_mappings
            .values()
            .flatten()
            .filter(|m| m.registry == registry)
            .map(|m| m.package.clone())
            .collect();
        packages.sort();
        packages.dedup();
        packages
    }
}

pub fn load_packages_config(path: &str) -> Result<PackagesConfig> {
    let content = std::fs::read_to_string(path)?;
    let config: PackagesConfig = serde_yaml::from_str(&content)?;
    Ok(config)
}

pub fn load_repo_mappings(conn: &Connection, config: &PackagesConfig) -> Result<usize> {
    // Create table if not exists
    conn.execute(
        "CREATE TABLE IF NOT EXISTS repo_package_mapping (
            repo TEXT NOT NULL,
            package TEXT NOT NULL,
            registry TEXT NOT NULL,
            PRIMARY KEY (repo, package)
        )",
        [],
    )?;

    // Clear existing mappings
    conn.execute("DELETE FROM repo_package_mapping", [])?;

    let mut count = 0;
    for (repo, mappings) in &config.repo_mappings {
        for mapping in mappings {
            conn.execute(
                "INSERT INTO repo_package_mapping (repo, package, registry) VALUES (?1, ?2, ?3)",
                params![repo, mapping.package, mapping.registry],
            )?;
            count += 1;
        }
    }

    Ok(count)
}

