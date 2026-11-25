use anyhow::{bail, Context, Result};
use news_api::{Options, Sources};
use std::path::PathBuf;

fn start() -> Result<i32> {
    let mut options = Options {
        root: std::env::current_dir()?,
        dry_run: false,
        rss_only: false,
        workers: 4,
    };
    let mut arguments = std::env::args().skip(1);
    while let Some(argument) = arguments.next() {
        match argument.as_str() {
            "--rss-only" => options.rss_only = true,
            "--dry-run" => options.dry_run = true,
            "--output-dir" => {
                options.root =
                    PathBuf::from(arguments.next().context("--output-dir needs a directory")?)
            }
            "--workers" => {
                options.workers = arguments
                    .next()
                    .context("--workers needs a number")?
                    .parse()?
            }
            "--help" | "-h" => {
                println!("news-api [--rss-only] [--dry-run] [--output-dir DIR] [--workers 1..16]\nPublic feeds and targeted listings, with optional NewsAPI. Default: four network workers.");
                return Ok(0);
            }
            _ => bail!("Unknown argument: {argument}"),
        }
    }
    news_api::run(&options, &Sources::bundled()?)
}

fn main() {
    match start() {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            eprintln!("ERROR: {error:#}");
            std::process::exit(1);
        }
    }
}
