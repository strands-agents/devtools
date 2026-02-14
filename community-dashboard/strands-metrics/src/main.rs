mod aggregates;
mod client;
mod db;

use anyhow::Result;
use clap::{Parser, Subcommand};
use client::GitHubClient;
use db::init_db;
use indicatif::{MultiProgress, ProgressBar, ProgressStyle};
use octocrab::OctocrabBuilder;
use std::path::PathBuf;
use std::sync::Arc;
use tracing::level_filters::LevelFilter;

const ORG: &str = "strands-agents";

#[derive(Parser)]
#[clap(author, version, about)]
struct Cli {
    #[clap(long, short, default_value = "../metrics.db")]
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
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_max_level(LevelFilter::WARN)
        .init();

    let args = Cli::parse();
    let mut conn = init_db(&args.db_path)?;

    match args.command {
        Commands::Sync => {
            let gh_token = std::env::var("GITHUB_TOKEN").expect("GITHUB_TOKEN must be set");
            let octocrab = OctocrabBuilder::new().personal_token(gh_token).build()?;

            let m = Arc::new(MultiProgress::new());
            let sty = ProgressStyle::with_template("{spinner:.green} {msg}")
                .unwrap()
                .tick_chars("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ ");

            let pb = m.add(ProgressBar::new_spinner());
            pb.set_style(sty.clone());
            pb.enable_steady_tick(std::time::Duration::from_millis(120));
            pb.set_message("Initializing Sync...");

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
            let sty = ProgressStyle::with_template("{spinner:.green} {msg}")
                .unwrap()
                .tick_chars("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ ");

            let pb = m.add(ProgressBar::new_spinner());
            pb.set_style(sty);
            pb.enable_steady_tick(std::time::Duration::from_millis(120));
            pb.set_message("Starting Sweep...");

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
    }

    Ok(())
}
