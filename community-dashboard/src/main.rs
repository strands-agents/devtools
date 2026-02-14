mod aggregates;
mod client;
mod db;
mod downloads;
mod goals;

use anyhow::Result;
use clap::{Parser, Subcommand};
use client::GitHubClient;
use db::init_db;
use goals::Direction;
use indicatif::{MultiProgress, ProgressBar, ProgressStyle};
use octocrab::OctocrabBuilder;
use std::path::PathBuf;
use std::sync::Arc;
use tracing::level_filters::LevelFilter;

const ORG: &str = "strands-agents";

#[derive(Parser)]
#[clap(author, version, about)]
struct Cli {
    #[clap(long, short, default_value = "metrics.db")]
    db_path: PathBuf,
    #[clap(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Smart sync. Grabs only what is new.
    Sync,
    /// Garbage collection. Checks open items against reality and marks missing ones as deleted.
    Sweep,
    /// Run raw SQL.
    Query { sql: String },
    /// Load goals from YAML config file into the database.
    LoadGoals {
        /// Path to goals.yaml file
        #[clap(default_value = "goals.yaml")]
        config_path: PathBuf,
    },
    /// List all configured goals.
    ListGoals,
    /// Load team members into the database for dashboard queries.
    LoadTeam {
        /// Path to team.yaml config file
        #[clap(default_value = "team.yaml")]
        config_path: PathBuf,
        /// Comma-separated list of GitHub usernames (overrides config file)
        #[clap(long, value_delimiter = ',')]
        members: Option<Vec<String>>,
    },
    /// Sync package download stats from PyPI and npm.
    SyncDownloads {
        /// Path to packages.yaml config file
        #[clap(long, default_value = "packages.yaml")]
        config_path: PathBuf,
        /// Number of days to fetch (default: 30)
        #[clap(long, default_value = "30")]
        days: i64,
    },
    /// Backfill historical download data (PyPI: ~180 days, npm: ~365 days).
    BackfillDownloads {
        /// Path to packages.yaml config file
        #[clap(long, default_value = "packages.yaml")]
        config_path: PathBuf,
    },
}

/// Create a spinner progress bar with consistent styling
fn create_spinner(m: &Arc<MultiProgress>, message: &str) -> ProgressBar {
    let sty = ProgressStyle::with_template("{spinner:.green} {msg}")
        .unwrap()
        .tick_chars("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ ");
    let pb = m.add(ProgressBar::new_spinner());
    pb.set_style(sty);
    pb.enable_steady_tick(std::time::Duration::from_millis(120));
    pb.set_message(message.to_string());
    pb
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_max_level(LevelFilter::WARN)
        .init();

    let args = Cli::parse();
    let mut conn = init_db(&args.db_path)?;
    goals::init_goals_table(&conn)?;

    match args.command {
        Commands::Sync => {
            let gh_token = std::env::var("GITHUB_TOKEN").expect("GITHUB_TOKEN must be set");
            let octocrab = OctocrabBuilder::new().personal_token(gh_token).build()?;

            let m = Arc::new(MultiProgress::new());
            let pb = create_spinner(&m, "Initializing Sync...");

            let mut client = GitHubClient::new(octocrab, &mut conn, pb.clone());

            client.sync_org(ORG).await?;

            pb.set_message("Calculating metrics...");
            aggregates::compute_metrics(&conn)?;

            pb.finish_with_message("Done!");
        }
        Commands::Sweep => {
            let gh_token = std::env::var("GITHUB_TOKEN").expect("GITHUB_TOKEN must be set");
            let octocrab = OctocrabBuilder::new().personal_token(gh_token).build()?;

            let m = Arc::new(MultiProgress::new());
            let pb = create_spinner(&m, "Starting Sweep...");

            let mut client = GitHubClient::new(octocrab, &mut conn, pb.clone());
            client.sweep_org(ORG).await?;

            pb.finish_with_message("Sweep complete.");
        }
        Commands::Query { sql } => {
            let mut stmt = conn.prepare(&sql)?;
            let column_count = stmt.column_count();
            let names: Vec<String> = stmt.column_names().into_iter().map(String::from).collect();

            println!("{}", names.join(" | "));
            println!("{}", "-".repeat(names.len() * 15));

            let mut rows = stmt.query([])?;
            while let Some(row) = rows.next()? {
                let mut row_values = Vec::new();
                for i in 0..column_count {
                    let val = row.get_ref(i)?;
                    let text = match val {
                        rusqlite::types::ValueRef::Null => "NULL".to_string(),
                        rusqlite::types::ValueRef::Integer(i) => i.to_string(),
                        rusqlite::types::ValueRef::Real(f) => f.to_string(),
                        rusqlite::types::ValueRef::Text(t) => {
                            String::from_utf8_lossy(t).to_string()
                        }
                        rusqlite::types::ValueRef::Blob(_) => "<BLOB>".to_string(),
                    };
                    row_values.push(text);
                }
                println!("{}", row_values.join(" | "));
            }
        }
        Commands::LoadGoals { config_path } => {
            let count = goals::load_goals(&conn, &config_path)?;
            println!("Loaded {} goals from {:?}", count, config_path);
        }
        Commands::ListGoals => {
            let all_goals = goals::list_goals(&conn)?;
            println!(
                "{:<40} | {:>10} | {:<20} | {:<15} | {}",
                "Metric", "Value", "Label", "Direction", "Warning Ratio"
            );
            println!("{}", "-".repeat(110));
            for goal in all_goals {
                let label_str = goal.label.as_deref().unwrap_or("-");
                let dir_str = match goal.direction {
                    Direction::LowerIsBetter => "lower_is_better",
                    Direction::HigherIsBetter => "higher_is_better",
                };
                let ratio_str = goal
                    .warning_ratio
                    .map(|r| format!("{:.2}", r))
                    .unwrap_or_else(|| "-".to_string());
                println!(
                    "{:<40} | {:>10} | {:<20} | {:<15} | {}",
                    goal.metric, goal.value, label_str, dir_str, ratio_str
                );
            }
        }
        Commands::LoadTeam { config_path, members } => {
            let count = if let Some(member_list) = members {
                // Use --members flag if provided
                let member_tuples: Vec<(&str, Option<&str>)> =
                    member_list.iter().map(|m| (m.as_str(), None)).collect();
                goals::load_team_members(&conn, &member_tuples)?
            } else {
                // Load from config file
                goals::load_team_from_yaml(&conn, &config_path)?
            };
            println!("Loaded {} team members", count);
        }
        Commands::SyncDownloads { config_path, days } => {
            let config = downloads::load_packages_config(config_path.to_str().unwrap())?;

            // Load repo-to-package mappings
            let mapping_count = downloads::load_repo_mappings(&conn, &config)?;
            println!("Loaded {} repo-to-package mappings\n", mapping_count);

            println!("Syncing PyPI packages...");
            for package in config.packages_for_registry("pypi") {
                match downloads::sync_pypi_downloads(&conn, &package, days).await {
                    Ok(count) => println!("  {} - {} data points", package, count),
                    Err(e) => eprintln!("  {} - Error: {}", package, e),
                }
            }

            println!("\nSyncing npm packages...");
            for package in config.packages_for_registry("npm") {
                match downloads::sync_npm_downloads(&conn, &package, days).await {
                    Ok(count) => println!("  {} - {} data points", package, count),
                    Err(e) => eprintln!("  {} - Error: {}", package, e),
                }
            }

            println!("\nDownload sync complete!");
        }
        Commands::BackfillDownloads { config_path } => {
            let config = downloads::load_packages_config(config_path.to_str().unwrap())?;

            // Load repo-to-package mappings
            let mapping_count = downloads::load_repo_mappings(&conn, &config)?;
            println!("Loaded {} repo-to-package mappings\n", mapping_count);

            println!("Backfilling PyPI packages (up to 180 days)...");
            for package in config.packages_for_registry("pypi") {
                match downloads::backfill_pypi_downloads(&conn, &package).await {
                    Ok(count) => println!("  {} - {} data points", package, count),
                    Err(e) => eprintln!("  {} - Error: {}", package, e),
                }
            }

            println!("\nBackfilling npm packages (up to 365 days)...");
            for package in config.packages_for_registry("npm") {
                match downloads::backfill_npm_downloads(&conn, &package).await {
                    Ok(count) => println!("  {} - {} data points", package, count),
                    Err(e) => eprintln!("  {} - Error: {}", package, e),
                }
            }

            println!("\nBackfill complete!");
        }
    }

    Ok(())
}
