//! Parse the benchmark's captured public responses; never fetches the network.
use anyhow::{Context, Result};
use news_api::{parse_ctv, parse_rss};
use serde_json::{json, Value};
use std::{fs, path::PathBuf, time::Instant};

fn main() -> Result<()> {
    let directory = PathBuf::from(
        std::env::args()
            .nth(1)
            .context("Expected snapshot directory")?,
    );
    let manifest: Vec<Value> = serde_json::from_slice(&fs::read(directory.join("manifest.json"))?)?;
    let documents: Vec<_> = manifest
        .iter()
        .map(|item| fs::read_to_string(directory.join(item["file"].as_str().unwrap())))
        .collect::<std::io::Result<_>>()?;
    let started = Instant::now();
    let mut parsed = Vec::new();
    for _ in 0..5 {
        parsed.clear();
        for (item, document) in manifest.iter().zip(&documents) {
            let articles = if item["listing"] == true {
                parse_ctv(document, item["url"].as_str().unwrap())?
            } else {
                parse_rss(
                    document,
                    item["source"].as_str().unwrap(),
                    "2026-11-25T00:00:00Z",
                )?
            };
            parsed.push(articles);
        }
    }
    let seconds = started.elapsed().as_secs_f64() / 5.0;
    println!("{}", json!({"parse_seconds":seconds,"articles":parsed}));
    Ok(())
}
