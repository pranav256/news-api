use anyhow::{bail, Context, Result};
use chrono::{DateTime, Duration as Days, NaiveDateTime, SecondsFormat, Utc};
use regex::Regex;
use reqwest::blocking::Client;
use roxmltree::Node;
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashSet};
use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

const MAX_BYTES: u64 = 4 * 1024 * 1024;
const MAX_FEED_ARTICLES: usize = 250;
const MAX_OUTPUT_ARTICLES: usize = 500;
const USER_AGENT: &str = "news-api-static-feed/3.0 (+https://github.com/pranav256/news-api)";

#[derive(Clone, Deserialize)]
pub struct Sources {
    pub countries: Vec<String>,
    pub categories: Vec<String>,
    pub queries: BTreeMap<String, String>,
    pub country_sources: BTreeMap<String, String>,
    pub rss_feeds: BTreeMap<String, BTreeMap<String, Vec<(String, String)>>>,
    pub ctv_health_url: String,
}

impl Sources {
    pub fn bundled() -> Result<Self> {
        Ok(serde_json::from_str(include_str!("../sources.json"))?)
    }
}

pub struct Options {
    pub root: PathBuf,
    pub dry_run: bool,
    pub rss_only: bool,
    pub workers: usize,
}

pub fn timestamp() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Micros, true)
}

fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value.get(key).and_then(Value::as_str).unwrap_or("")
}

fn cleaned(value: &str) -> Option<String> {
    static TAGS: OnceLock<Regex> = OnceLock::new();
    let decoded = html_escape::decode_html_entities(value);
    let stripped = TAGS
        .get_or_init(|| Regex::new(r"<[^>]+>").unwrap())
        .replace_all(&decoded, " ");
    let result = stripped.split_whitespace().collect::<Vec<_>>().join(" ");
    (!result.is_empty()).then_some(result)
}

fn parsed_date(value: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value.trim())
        .or_else(|_| DateTime::parse_from_rfc2822(value.trim()))
        .map(|date| date.with_timezone(&Utc))
        .ok()
        .or_else(|| {
            NaiveDateTime::parse_from_str(value.trim(), "%Y-%m-%dT%H:%M:%S")
                .ok()
                .map(|date| date.and_utc())
        })
}

fn date_string(value: &str, fallback: &str) -> String {
    parsed_date(value)
        .map(|date| date.to_rfc3339_opts(SecondsFormat::AutoSi, true))
        .unwrap_or_else(|| fallback.to_owned())
}

fn child_text<'a, 'input>(node: Node<'a, 'input>, names: &[&str]) -> &'a str {
    node.children()
        .find(|child| child.is_element() && names.contains(&child.tag_name().name()))
        .and_then(|child| child.text())
        .unwrap_or("")
}

pub fn parse_rss(document: &str, source: &str, fetched_at: &str) -> Result<Vec<Value>> {
    let tree = roxmltree::Document::parse(document)?;
    let root = tree.root_element();
    let name = root.tag_name().name().to_ascii_lowercase();
    if !["rss", "feed", "rdf"].contains(&name.as_str()) {
        bail!("Response is not an RSS or Atom feed");
    }
    if ["rss", "rdf"].contains(&name.as_str())
        && !root
            .children()
            .any(|child| child.has_tag_name("channel") || child.tag_name().name() == "channel")
    {
        bail!("RSS feed is missing its channel");
    }
    let entries: Vec<_> = root
        .descendants()
        .filter(|node| node.is_element() && ["item", "entry"].contains(&node.tag_name().name()))
        .take(MAX_FEED_ARTICLES)
        .collect();
    let source_id = source
        .to_ascii_lowercase()
        .split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|word| !word.is_empty())
        .collect::<Vec<_>>()
        .join("-");
    let mut articles = Vec::new();
    let image_regex = || {
        static IMAGE: OnceLock<Regex> = OnceLock::new();
        IMAGE.get_or_init(|| Regex::new(r#"(?i)<img[^>]+src=["'](https?://[^"']+)["']"#).unwrap())
    };
    for entry in &entries {
        let Some(title) = cleaned(child_text(*entry, &["title"])) else {
            continue;
        };
        let mut link = child_text(*entry, &["link"]).trim();
        if link.is_empty() {
            link = entry
                .children()
                .find(|node| {
                    node.tag_name().name() == "link"
                        && node.attribute("rel").unwrap_or("alternate") == "alternate"
                        && node.attribute("href").is_some()
                })
                .and_then(|node| node.attribute("href"))
                .unwrap_or("")
                .trim();
        }
        if link.is_empty() {
            continue;
        }
        let raw_description = child_text(*entry, &["description", "summary", "encoded", "content"]);
        let description = cleaned(raw_description);
        let image = entry
            .children()
            .filter(|node| ["content", "thumbnail", "enclosure"].contains(&node.tag_name().name()))
            .find_map(|node| node.attribute("url").or_else(|| node.attribute("href")))
            .map(str::to_owned)
            .or_else(|| {
                image_regex()
                    .captures(raw_description)
                    .map(|captures| captures[1].to_owned())
            });
        articles.push(json!({
            "source": {"id": source_id, "name": source},
            "author": cleaned(child_text(*entry, &["author", "creator"])),
            "title": title, "description": description, "url": link,
            "urlToImage": image,
            "publishedAt": date_string(child_text(*entry, &["pubDate", "pubdate", "published", "updated", "date"]), fetched_at),
            "content": description,
        }));
    }
    if !entries.is_empty() && articles.is_empty() {
        bail!("RSS feed contains no usable articles");
    }
    Ok(articles)
}

pub fn parse_ctv(document: &str, listing_url: &str) -> Result<Vec<Value>> {
    let mut embedded = Vec::new();
    for marker in ["Fusion.globalContent=", "Fusion.contentCache="] {
        let (_, value) = document
            .split_once(marker)
            .context("CTV listing data is missing")?;
        let value = serde_json::Deserializer::from_str(value.trim_start())
            .into_iter::<Value>()
            .next()
            .context("CTV JSON is missing")??;
        embedded.push(value);
    }
    if embedded[0]["_id"] != "/health" {
        bail!("Response is not the CTV health listing");
    }
    let groups = embedded[1]["story-feed-sections"]
        .as_object()
        .context("CTV story collection is missing")?;
    let base = reqwest::Url::parse(listing_url)?;
    let mut articles = Vec::new();
    for (parameters, group) in groups {
        if !parameters
            .replace("\\\"", "\"")
            .contains("\"includeSections\":\"/health\"")
        {
            continue;
        }
        let Some(stories) = group["data"]["content_elements"].as_array() else {
            continue;
        };
        for story in stories.iter().take(MAX_FEED_ARTICLES) {
            if story["type"] != "story" || story["owner"]["sponsored"].as_bool() == Some(true) {
                continue;
            }
            let Some(title) = cleaned(text(&story["headlines"], "basic")) else {
                continue;
            };
            let Some(path) = story["websites"]["ctvnews"]["website_url"].as_str() else {
                continue;
            };
            let Ok(url) = base.join(path) else { continue };
            let Some(published) = parsed_date(text(story, "display_date")) else {
                continue;
            };
            if url.scheme() != "https"
                || url.host_str() != Some("www.ctvnews.ca")
                || url.port().is_some()
                || !url.username().is_empty()
                || url.password().is_some()
                || !url.path().contains("/article/")
            {
                continue;
            }
            let description = cleaned(text(&story["description"], "basic"));
            articles.push(json!({
                "source": {"id": "ctv-news", "name": "CTV News"}, "author": null,
                "title": title, "description": description, "url": url.as_str(),
                "urlToImage": story["promo_items"]["basic"]["url"].as_str(),
                "publishedAt": published.to_rfc3339_opts(SecondsFormat::AutoSi, true), "content": description,
            }));
        }
    }
    if articles.is_empty() {
        bail!("CTV health listing contains no usable articles");
    }
    Ok(deduplicate(articles))
}

pub fn valid_articles(payload: &Value) -> Vec<Value> {
    payload["articles"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|article| {
            article.is_object()
                && !text(article, "url").trim().is_empty()
                && (article["publishedAt"].is_null() || article["publishedAt"].is_string())
        })
        .cloned()
        .collect()
}

pub fn deduplicate(articles: Vec<Value>) -> Vec<Value> {
    let mut seen = HashSet::new();
    let mut unique: Vec<_> = articles
        .into_iter()
        .filter(|article| {
            let url = text(article, "url").trim();
            let key = if url.is_empty() {
                format!(
                    "{}|{}",
                    text(article, "title"),
                    text(article, "publishedAt")
                )
            } else {
                url.to_owned()
            };
            seen.insert(key)
        })
        .collect();
    unique
        .sort_by_cached_key(|article| std::cmp::Reverse(parsed_date(text(article, "publishedAt"))));
    unique.truncate(MAX_OUTPUT_ARTICLES);
    unique
}

fn load_json(path: &Path) -> Value {
    fs::read(path)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or(Value::Null)
}

pub fn merge_recent(mut articles: Vec<Value>, previous: &Value, now: DateTime<Utc>) -> Vec<Value> {
    if articles.is_empty() {
        return articles;
    }
    let cutoff = now - Days::days(7);
    articles.extend(valid_articles(previous).into_iter().filter(|article| {
        parsed_date(text(article, "publishedAt")).is_some_and(|date| cutoff <= date && date <= now)
    }));
    deduplicate(articles)
}

pub fn save_json(payload: &Value, path: &Path, dry_run: bool) -> Result<()> {
    if dry_run {
        return Ok(());
    }
    let parent = path.parent().context("Output path has no parent")?;
    fs::create_dir_all(parent)?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
    serde_json::to_writer_pretty(&mut temporary, payload)?;
    temporary.write_all(b"\n")?;
    temporary
        .persist(path)
        .with_context(|| format!("Cannot replace {}", path.display()))?;
    Ok(())
}

fn request_bytes(request: reqwest::blocking::RequestBuilder) -> Result<Vec<u8>> {
    let response = request.send()?.error_for_status()?;
    let mut bytes = Vec::new();
    response.take(MAX_BYTES + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_BYTES {
        bail!("Publisher response exceeds the size limit");
    }
    Ok(bytes)
}

#[derive(Clone)]
struct Job {
    country: String,
    category: String,
    source: String,
    url: String,
    listing: bool,
}

#[derive(Default)]
struct PublicFeed {
    articles: Vec<Value>,
    rss_attempted: usize,
    rss_succeeded: usize,
    listing_succeeded: bool,
}

#[derive(Default)]
struct State {
    files: Vec<String>,
    outcomes: Vec<Value>,
    rss_errors: Vec<String>,
    listing_errors: Vec<String>,
    listing_attempted: usize,
    listing_succeeded: usize,
    refreshed: usize,
}

fn public_feeds(
    client: &Client,
    sources: &Sources,
    workers: usize,
    fetched_at: &str,
    state: &mut State,
) -> Result<BTreeMap<(String, String), PublicFeed>> {
    let mut jobs = Vec::new();
    for country in &sources.countries {
        for category in &sources.categories {
            for (source, url) in &sources.rss_feeds[country][category] {
                jobs.push(Job {
                    country: country.clone(),
                    category: category.clone(),
                    source: source.clone(),
                    url: url.clone(),
                    listing: false,
                });
            }
        }
    }
    if sources.countries.iter().any(|country| country == "ca")
        && sources
            .categories
            .iter()
            .any(|category| category == "health")
    {
        jobs.push(Job {
            country: "ca".into(),
            category: "health".into(),
            source: "CTV News".into(),
            url: sources.ctv_health_url.clone(),
            listing: true,
        });
    }
    let chunk_size = jobs.len().div_ceil(workers).max(1);
    let results = std::thread::scope(|scope| -> Result<Vec<_>> {
        let handles: Vec<_> = jobs
            .chunks(chunk_size)
            .map(|chunk| {
                scope.spawn(move || {
                    chunk
                        .iter()
                        .map(|job| {
                            let result = (|| -> Result<Vec<Value>> {
                                let bytes = request_bytes(client.get(&job.url))?;
                                let document = std::str::from_utf8(&bytes)
                                    .context("Publisher returned non-UTF-8 content")?;
                                if job.listing {
                                    parse_ctv(document, &job.url)
                                } else {
                                    parse_rss(document, &job.source, fetched_at)
                                }
                            })();
                            (job, result)
                        })
                        .collect::<Vec<_>>()
                })
            })
            .collect();
        let mut results = Vec::new();
        for handle in handles {
            results.extend(
                handle
                    .join()
                    .map_err(|_| anyhow::anyhow!("Publisher worker panicked"))?,
            );
        }
        Ok(results)
    })?;
    let mut feeds: BTreeMap<_, PublicFeed> = BTreeMap::new();
    for (job, result) in results {
        let feed = feeds
            .entry((job.country.clone(), job.category.clone()))
            .or_default();
        if job.listing {
            state.listing_attempted += 1;
        } else {
            feed.rss_attempted += 1;
        }
        match result {
            Ok(articles) => {
                if job.listing {
                    feed.listing_succeeded = true;
                    state.listing_succeeded += 1;
                } else {
                    feed.rss_succeeded += 1;
                }
                feed.articles.extend(articles);
            }
            Err(error) => {
                let message = format!("{}/{} {}: {}", job.country, job.category, job.source, error);
                eprintln!("Provider failed: {message}");
                if job.listing {
                    state.listing_errors.push(message);
                } else {
                    state.rss_errors.push(message);
                }
            }
        }
    }
    for feed in feeds.values_mut() {
        feed.articles = deduplicate(std::mem::take(&mut feed.articles));
    }
    Ok(feeds)
}

pub fn load_keys(environment: &BTreeMap<String, String>) -> Vec<String> {
    let mut candidates: Vec<String> = environment
        .get("NEWSAPI_KEYS")
        .into_iter()
        .flat_map(|keys| keys.split(|c: char| c == ',' || c.is_whitespace()))
        .map(str::to_owned)
        .collect();
    for name in [
        "FIRSTAPI",
        "SECONDAPI",
        "THIRDAPI",
        "FOURTHAPI",
        "FIFTHAPI",
        "SIXTHAPI",
        "SEVENTHAPI",
    ] {
        if let Some(key) = environment.get(name) {
            candidates.push(key.trim().to_owned());
        }
    }
    let mut seen = HashSet::new();
    candidates
        .into_iter()
        .filter(|key| !key.is_empty() && seen.insert(key.clone()))
        .collect()
}

pub fn run_budget(keys: usize) -> usize {
    keys * 95 / 12
}

struct Api {
    keys: Vec<String>,
    disabled: HashSet<usize>,
    next: usize,
    used: usize,
    limit: usize,
    errors: Vec<String>,
}

impl Api {
    fn new(keys: Vec<String>) -> Self {
        Self {
            limit: run_budget(keys.len()),
            keys,
            disabled: HashSet::new(),
            next: 0,
            used: 0,
            errors: Vec::new(),
        }
    }

    fn fetch(
        &mut self,
        client: &Client,
        sources: &Sources,
        country: &str,
        category: &str,
        deep: bool,
    ) -> Option<Value> {
        self.fetch_from(
            client,
            sources,
            country,
            category,
            deep,
            "https://newsapi.org/v2",
        )
    }

    fn fetch_from(
        &mut self,
        client: &Client,
        sources: &Sources,
        country: &str,
        category: &str,
        deep: bool,
        base: &str,
    ) -> Option<Value> {
        if self.keys.is_empty() || self.used >= self.limit {
            return None;
        }
        let endpoint = if deep { "everything" } else { "top-headlines" };
        let label = format!("{country}/{category}/{endpoint}");
        let start = self.next;
        self.next = (start + 1) % self.keys.len();
        let mut attempts = 0;
        for offset in 0..self.keys.len() {
            let index = (start + offset) % self.keys.len();
            if self.disabled.contains(&index) {
                continue;
            }
            if attempts >= 3 {
                break;
            }
            if self.used >= self.limit {
                return None;
            }
            self.used += 1;
            attempts += 1;
            let mut query = vec![("pageSize", "100"), ("page", "1"), ("language", "en")];
            if deep {
                query.extend([
                    ("q", sources.queries[category].as_str()),
                    ("sources", sources.country_sources[country].as_str()),
                    ("sortBy", "publishedAt"),
                ]);
            } else {
                query.extend([("country", country), ("category", category)]);
            }
            let response: Result<Value> = (|| {
                let response = client
                    .get(format!("{base}/{endpoint}"))
                    .header("X-Api-Key", &self.keys[index])
                    .query(&query)
                    .timeout(Duration::from_secs(30))
                    .send()?;
                let status = response.status();
                let mut bytes = Vec::new();
                response.take(MAX_BYTES + 1).read_to_end(&mut bytes)?;
                if bytes.len() as u64 > MAX_BYTES {
                    bail!("NewsAPI response exceeds size limit");
                }
                let value: Value = serde_json::from_slice(&bytes)?;
                if !status.is_success() && value["status"] == "ok" {
                    bail!("NewsAPI returned HTTP {status}");
                }
                Ok(value)
            })();
            let (code, mut message, permanent) = match response {
                Ok(value) if value["status"] == "ok" => {
                    if let Some(articles) = value["articles"].as_array() {
                        if articles.is_empty() || !valid_articles(&value).is_empty() {
                            return Some(value);
                        }
                    }
                    (
                        String::new(),
                        "Malformed NewsAPI articles collection".into(),
                        true,
                    )
                }
                Ok(value) => {
                    let code = text(&value, "code").to_owned();
                    let permanent = [
                        "parameterInvalid",
                        "parametersMissing",
                        "sourceDoesNotExist",
                        "sourcesTooMany",
                    ]
                    .contains(&code.as_str());
                    (code, text(&value, "message").to_owned(), permanent)
                }
                Err(error) => (String::new(), error.to_string(), false),
            };
            if [
                "apiKeyDisabled",
                "apiKeyExhausted",
                "apiKeyInvalid",
                "apiKeyMissing",
                "rateLimited",
            ]
            .contains(&code.as_str())
            {
                self.disabled.insert(index);
            }
            for key in &self.keys {
                message = message.replace(key, "[REDACTED]");
            }
            eprintln!("{label} key#{} attempt {attempts}: {message}", index + 1);
            if permanent {
                break;
            }
            if attempts < 3 {
                std::thread::sleep(Duration::from_millis(250));
            }
        }
        self.errors.push(format!("{label}: all attempts failed"));
        None
    }
}

fn payload(
    source: &str,
    country: &str,
    category: &str,
    fetched_at: &str,
    articles: &[Value],
    providers: &[&str],
) -> Value {
    json!({"source":source, "country":country, "category":category, "providers":providers,
        "fetched_at":fetched_at, "articles_count":articles.len(), "articles":articles})
}

fn relative(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/")
}

fn write_pair(options: &Options, data: Value, succeeded: bool, state: &mut State) -> Result<()> {
    let country = text(&data, "country");
    let category = text(&data, "category");
    let public_path = options.root.join(country).join(format!("{category}.json"));
    let rich_path = options
        .root
        .join("data")
        .join(country)
        .join(format!("{category}_headlines.json"));
    let previous = if succeeded {
        Value::Null
    } else {
        load_json(&rich_path)
    };
    state.outcomes.push(json!({"country":country,"category":category,
        "status":if succeeded {"updated"} else {"retained"},"attempted_at":data["fetched_at"],
        "last_successful_refresh":if succeeded {&data["fetched_at"]} else {&previous["fetched_at"]},
        "providers":if succeeded {data["providers"].clone()} else {previous.get("providers").cloned().unwrap_or(json!([]))},
        "articles_count":if succeeded {&data["articles_count"]} else {&previous["articles_count"]}}));
    if !succeeded {
        println!("Keeping previous {country}/{category} files");
        return Ok(());
    }
    save_json(
        &json!({"status":"ok","totalResults":data["articles_count"],"articles":data["articles"]}),
        &public_path,
        options.dry_run,
    )?;
    save_json(&data, &rich_path, options.dry_run)?;
    if !options.dry_run {
        state.files.extend([
            relative(&options.root, &public_path),
            relative(&options.root, &rich_path),
        ]);
    }
    Ok(())
}

pub fn run(options: &Options, sources: &Sources) -> Result<i32> {
    let started = Instant::now();
    let now = Utc::now();
    let fetched_at = now.to_rfc3339_opts(SecondsFormat::Micros, true);
    let timeout: u64 = std::env::var("RSS_TIMEOUT_SECONDS")
        .unwrap_or_else(|_| "20".into())
        .parse()
        .context("RSS_TIMEOUT_SECONDS must be a positive integer")?;
    if timeout == 0 || options.workers == 0 || options.workers > 16 {
        bail!("Timeout must be positive and workers must be between 1 and 16");
    }
    let client = Client::builder()
        .user_agent(USER_AGENT)
        .timeout(Duration::from_secs(timeout))
        .connect_timeout(Duration::from_secs(timeout.min(10)))
        .build()?;
    let keys = if options.rss_only {
        Vec::new()
    } else {
        load_keys(&std::env::vars().collect())
    };
    let mut api = Api::new(keys);
    println!(
        "Rust news scraper: {} workers, {} API keys, {}/run API budget",
        options.workers,
        api.keys.len(),
        api.limit
    );
    let mut state = State::default();
    let feeds = public_feeds(&client, sources, options.workers, &fetched_at, &mut state)?;
    let data_dir = options.root.join("data");
    for country in &sources.countries {
        let mut combined = Vec::new();
        let mut country_refreshed = false;
        for category in &sources.categories {
            let feed = &feeds[&(country.clone(), category.clone())];
            let response = api.fetch(&client, sources, country, category, false);
            let mut providers = Vec::new();
            if response.is_some() {
                providers.push("newsapi");
            }
            if feed.rss_succeeded > 0 {
                providers.push("rss");
            }
            if feed.listing_succeeded {
                providers.push("publisher_listing");
            }
            let succeeded = !providers.is_empty();
            let mut articles = response.as_ref().map(valid_articles).unwrap_or_default();
            articles.extend(feed.articles.clone());
            articles = deduplicate(articles);
            if succeeded {
                articles = merge_recent(
                    articles,
                    &load_json(
                        &data_dir
                            .join(country)
                            .join(format!("{category}_headlines.json")),
                    ),
                    now,
                );
                country_refreshed = true;
                state.refreshed += 1;
            }
            println!("[{country}/{category}] {} articles", articles.len());
            write_pair(
                options,
                payload(
                    "top_headlines",
                    country,
                    category,
                    &fetched_at,
                    &articles,
                    &providers,
                ),
                succeeded,
                &mut state,
            )?;
            if !succeeded {
                articles = valid_articles(&load_json(
                    &options.root.join(country).join(format!("{category}.json")),
                ));
            }
            combined.extend(articles);
        }
        let combined = deduplicate(combined);
        write_pair(
            options,
            payload(
                "top_headlines_all",
                country,
                "all",
                &fetched_at,
                &combined,
                &["combined"],
            ),
            country_refreshed,
            &mut state,
        )?;
    }
    for country in &sources.countries {
        for category in &sources.categories {
            let feed = &feeds[&(country.clone(), category.clone())];
            let response = api.fetch(&client, sources, country, category, true);
            let mut providers = Vec::new();
            if response.is_some() {
                providers.push("newsapi");
            }
            if feed.rss_succeeded > 0 {
                providers.push("rss");
            }
            if feed.listing_succeeded {
                providers.push("publisher_listing");
            }
            if providers.is_empty() {
                continue;
            }
            state.refreshed += 1;
            let path = data_dir
                .join(country)
                .join(format!("{category}_everything.json"));
            let mut articles = response.as_ref().map(valid_articles).unwrap_or_default();
            articles.extend(feed.articles.clone());
            let articles = merge_recent(deduplicate(articles), &load_json(&path), now);
            let source = if response.is_some() {
                "everything"
            } else {
                "rss_fallback"
            };
            let mut data = payload(
                source,
                country,
                category,
                &fetched_at,
                &articles,
                &providers,
            );
            data["query"] = json!(sources.queries[category]);
            save_json(&data, &path, options.dry_run)?;
            if !options.dry_run {
                state.files.push(relative(&options.root, &path));
            }
        }
    }
    let updated = state
        .outcomes
        .iter()
        .filter(|outcome| outcome["status"] == "updated")
        .count();
    let partial = state.refreshed == 0
        || !api.errors.is_empty()
        || !state.rss_errors.is_empty()
        || !state.listing_errors.is_empty();
    let errors: Vec<_> = api
        .errors
        .iter()
        .chain(&state.rss_errors)
        .chain(&state.listing_errors)
        .collect();
    let status = json!({"schema_version":1,"generated_at":timestamp(),"run_started_at":fetched_at,
        "status":if partial {"partial"} else {"ok"},"api_keys_detected":api.keys.len(),
        "newsapi_requests_used":api.used,"newsapi_requests_allowed":api.limit,
        "rss_feeds_failed":state.rss_errors.len(),"api_requests_failed":api.errors.len(),
        "rss_feeds_attempted":feeds.values().map(|feed|feed.rss_attempted).sum::<usize>(),
        "rss_feeds_succeeded":feeds.values().map(|feed|feed.rss_succeeded).sum::<usize>(),
        "listing_pages_attempted":state.listing_attempted,"listing_pages_succeeded":state.listing_succeeded,
        "listing_pages_failed":state.listing_errors.len(),"feeds_updated":updated,
        "feeds_retained":state.outcomes.len()-updated,"feeds":state.outcomes,"files_updated":state.files,"errors":errors});
    save_json(&status, &data_dir.join("status.json"), options.dry_run)?;
    println!(
        "DONE: {updated} headline feeds refreshed; {:.2}s; {} provider errors",
        started.elapsed().as_secs_f64(),
        errors.len()
    );
    if state.refreshed == 0 {
        eprintln!("No feeds refreshed; keeping previous published data.");
        return Ok(1);
    }
    Ok(0)
}
