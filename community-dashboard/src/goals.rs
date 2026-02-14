use anyhow::{bail, Result};
use rusqlite::{params, Connection};
use serde::Deserialize;
use std::collections::HashMap;
use std::fs;
use std::path::Path;

/// Valid directions for goal thresholds
#[derive(Debug, Deserialize, Clone, Copy, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Direction {
    /// Lower values are better (e.g., merge time, failure rate)
    LowerIsBetter,
    /// Higher values are better (e.g., community %, retention)
    HigherIsBetter,
}

impl Direction {
    fn as_str(&self) -> &'static str {
        match self {
            Direction::LowerIsBetter => "lower_is_better",
            Direction::HigherIsBetter => "higher_is_better",
        }
    }

    fn default_warning_ratio(&self) -> f64 {
        match self {
            Direction::LowerIsBetter => 0.75,
            Direction::HigherIsBetter => 0.70,
        }
    }
}

/// A goal entry from the YAML configuration
#[derive(Debug, Deserialize)]
struct GoalEntry {
    value: f64,
    label: Option<String>,
    direction: Direction,
    warning_ratio: Option<f64>,
}

#[derive(Debug, Deserialize)]
struct GoalsConfig {
    goals: HashMap<String, GoalEntry>,
}

/// A goal with all its configuration
#[derive(Debug, Clone)]
pub struct Goal {
    pub metric: String,
    pub value: f64,
    pub label: Option<String>,
    pub direction: Direction,
    pub warning_ratio: Option<f64>,
}

impl Goal {
    /// Calculate the warning threshold value
    pub fn warning_value(&self) -> f64 {
        let ratio = self.warning_ratio.unwrap_or_else(|| self.direction.default_warning_ratio());
        self.value * ratio
    }
}

pub fn init_goals_table(conn: &Connection) -> Result<()> {
    conn.execute(
        "CREATE TABLE IF NOT EXISTS goals (
            metric TEXT PRIMARY KEY,
            value REAL NOT NULL,
            label TEXT,
            direction TEXT,
            warning_ratio REAL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )",
        [],
    )?;

    // Add columns if they don't exist (migration for existing DBs)
    let _ = conn.execute("ALTER TABLE goals ADD COLUMN label TEXT", []);
    let _ = conn.execute("ALTER TABLE goals ADD COLUMN direction TEXT", []);
    let _ = conn.execute("ALTER TABLE goals ADD COLUMN warning_ratio REAL", []);

    // Create or replace view for dynamic thresholds
    // Used by Grafana's "Config from Query results" transformation
    //
    // Threshold logic:
    // - lower_is_better: warning < goal (e.g., merge time: warn at 18h, critical at 24h)
    // - higher_is_better: warning < goal (e.g., community %: warn at 14%, critical at 20%)
    conn.execute("DROP VIEW IF EXISTS goal_thresholds", [])?;
    conn.execute(
        "CREATE VIEW IF NOT EXISTS goal_thresholds AS
        SELECT
            metric,
            value as goal_value,
            label,
            direction,
            warning_ratio,
            CASE
                WHEN direction = 'lower_is_better' THEN
                    value * COALESCE(warning_ratio, 0.75)
                WHEN direction = 'higher_is_better' THEN
                    value * COALESCE(warning_ratio, 0.70)
                ELSE value * 0.75
            END as warning_value
        FROM goals
        WHERE direction IS NOT NULL",
        [],
    )?;

    // Create team_members table for configurable team lists
    conn.execute(
        "CREATE TABLE IF NOT EXISTS team_members (
            username TEXT PRIMARY KEY,
            display_name TEXT,
            added_at TEXT NOT NULL DEFAULT (datetime('now'))
        )",
        [],
    )?;

    Ok(())
}

pub fn load_goals<P: AsRef<Path>>(conn: &Connection, yaml_path: P) -> Result<usize> {
    let path = yaml_path.as_ref();
    let content = fs::read_to_string(path)?;
    let config: GoalsConfig = serde_yaml::from_str(&content)?;

    let mut count = 0;
    for (metric, entry) in config.goals {
        // Validate warning_ratio is in valid range
        if let Some(ratio) = entry.warning_ratio {
            if ratio <= 0.0 || ratio >= 1.0 {
                bail!(
                    "warning_ratio must be between 0 and 1 (exclusive), got {} for metric '{}'",
                    ratio,
                    metric
                );
            }
        }

        let direction_str = entry.direction.as_str();

        conn.execute(
            "INSERT INTO goals (metric, value, label, direction, warning_ratio, updated_at)
             VALUES (?1, ?2, ?3, ?4, ?5, datetime('now'))
             ON CONFLICT(metric) DO UPDATE SET
                value = excluded.value,
                label = excluded.label,
                direction = excluded.direction,
                warning_ratio = excluded.warning_ratio,
                updated_at = datetime('now')",
            params![metric, entry.value, entry.label, direction_str, entry.warning_ratio],
        )?;
        count += 1;
    }

    Ok(count)
}

pub fn list_goals(conn: &Connection) -> Result<Vec<Goal>> {
    let mut stmt = conn.prepare(
        "SELECT metric, value, label, direction, warning_ratio FROM goals ORDER BY metric",
    )?;

    let rows = stmt.query_map([], |row| {
        let direction_str: Option<String> = row.get(3)?;
        let direction = match direction_str.as_deref() {
            Some("lower_is_better") => Direction::LowerIsBetter,
            Some("higher_is_better") => Direction::HigherIsBetter,
            _ => Direction::LowerIsBetter, // Default fallback
        };

        Ok(Goal {
            metric: row.get(0)?,
            value: row.get(1)?,
            label: row.get(2)?,
            direction,
            warning_ratio: row.get(4)?,
        })
    })?;

    let mut goals = Vec::new();
    for row in rows {
        goals.push(row?);
    }
    Ok(goals)
}

/// A team member entry from YAML configuration
#[derive(Debug, Deserialize)]
pub struct TeamMemberEntry {
    pub username: String,
    pub display_name: Option<String>,
}

#[derive(Debug, Deserialize)]
struct TeamConfig {
    members: Vec<TeamMemberEntry>,
}

/// Load team members from a YAML config file
pub fn load_team_from_yaml<P: AsRef<Path>>(conn: &Connection, yaml_path: P) -> Result<usize> {
    let content = fs::read_to_string(yaml_path)?;
    let config: TeamConfig = serde_yaml::from_str(&content)?;

    let mut count = 0;
    for member in config.members {
        conn.execute(
            "INSERT INTO team_members (username, display_name, added_at)
             VALUES (?1, ?2, datetime('now'))
             ON CONFLICT(username) DO UPDATE SET
                display_name = excluded.display_name,
                added_at = datetime('now')",
            params![member.username, member.display_name],
        )?;
        count += 1;
    }
    Ok(count)
}

/// Load team members from a list (for CLI --members flag)
pub fn load_team_members(conn: &Connection, members: &[(&str, Option<&str>)]) -> Result<usize> {
    let mut count = 0;
    for (username, display_name) in members {
        conn.execute(
            "INSERT INTO team_members (username, display_name, added_at)
             VALUES (?1, ?2, datetime('now'))
             ON CONFLICT(username) DO UPDATE SET
                display_name = excluded.display_name,
                added_at = datetime('now')",
            params![username, display_name],
        )?;
        count += 1;
    }
    Ok(count)
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;

    fn setup_test_db() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        init_goals_table(&conn).unwrap();
        conn
    }

    #[test]
    fn test_init_creates_tables_and_view() {
        let conn = setup_test_db();

        // Check goals table exists
        let table_exists: i32 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='goals'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(table_exists, 1);

        // Check view exists
        let view_exists: i32 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='view' AND name='goal_thresholds'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(view_exists, 1);

        // Check team_members table exists
        let team_table_exists: i32 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='team_members'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(team_table_exists, 1);
    }

    #[test]
    fn test_warning_value_lower_is_better_default() {
        let conn = setup_test_db();
        conn.execute(
            "INSERT INTO goals (metric, value, direction) VALUES ('test_metric', 100.0, 'lower_is_better')",
            [],
        )
        .unwrap();

        let warning: f64 = conn
            .query_row(
                "SELECT warning_value FROM goal_thresholds WHERE metric = 'test_metric'",
                [],
                |row| row.get(0),
            )
            .unwrap();

        assert_eq!(warning, 75.0); // 100 * 0.75 default
    }

    #[test]
    fn test_warning_value_higher_is_better_default() {
        let conn = setup_test_db();
        conn.execute(
            "INSERT INTO goals (metric, value, direction) VALUES ('test_metric', 100.0, 'higher_is_better')",
            [],
        )
        .unwrap();

        let warning: f64 = conn
            .query_row(
                "SELECT warning_value FROM goal_thresholds WHERE metric = 'test_metric'",
                [],
                |row| row.get(0),
            )
            .unwrap();

        assert_eq!(warning, 70.0); // 100 * 0.70 default
    }

    #[test]
    fn test_warning_value_custom_ratio() {
        let conn = setup_test_db();
        conn.execute(
            "INSERT INTO goals (metric, value, direction, warning_ratio) VALUES ('test_metric', 100.0, 'lower_is_better', 0.5)",
            [],
        )
        .unwrap();

        let warning: f64 = conn
            .query_row(
                "SELECT warning_value FROM goal_thresholds WHERE metric = 'test_metric'",
                [],
                |row| row.get(0),
            )
            .unwrap();

        assert_eq!(warning, 50.0); // 100 * 0.5 custom
    }

    #[test]
    fn test_list_goals_returns_struct() {
        let conn = setup_test_db();
        conn.execute(
            "INSERT INTO goals (metric, value, label, direction, warning_ratio)
             VALUES ('merge_time', 24.0, 'Goal (24h)', 'lower_is_better', 0.75)",
            [],
        )
        .unwrap();

        let goals = list_goals(&conn).unwrap();
        assert_eq!(goals.len(), 1);

        let goal = &goals[0];
        assert_eq!(goal.metric, "merge_time");
        assert_eq!(goal.value, 24.0);
        assert_eq!(goal.label, Some("Goal (24h)".to_string()));
        assert_eq!(goal.direction, Direction::LowerIsBetter);
        assert_eq!(goal.warning_ratio, Some(0.75));
        assert_eq!(goal.warning_value(), 18.0); // 24 * 0.75
    }

    #[test]
    fn test_direction_default_ratios() {
        assert_eq!(Direction::LowerIsBetter.default_warning_ratio(), 0.75);
        assert_eq!(Direction::HigherIsBetter.default_warning_ratio(), 0.70);
    }

    #[test]
    fn test_team_members_table() {
        let conn = setup_test_db();

        let members = vec![
            ("alice", Some("Alice Smith")),
            ("bob", None),
        ];
        let count = load_team_members(&conn, &members).unwrap();
        assert_eq!(count, 2);

        let stored_count: i32 = conn
            .query_row("SELECT COUNT(*) FROM team_members", [], |row| row.get(0))
            .unwrap();
        assert_eq!(stored_count, 2);
    }
}
