use super::*;
use std::io::{BufRead, BufReader};
use std::net::TcpListener;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

#[test]
fn rss_atom_images_entities_and_dates() {
    let rss = r#"<rss xmlns:media="http://search.yahoo.com/mrss/"><channel><item>
        <title>News &amp; science</title><link>https://example.com/story</link>
        <description>&lt;p&gt;Summary&lt;/p&gt;</description>
        <pubDate>Fri, 02 Oct 2026 12:00:00 GMT</pubDate>
        <media:content url="https://example.com/image.jpg"/>
        </item></channel></rss>"#;
    let article = parse_rss(rss, "Example News", "fallback")
        .unwrap()
        .remove(0);
    assert_eq!(article["title"], "News & science");
    assert_eq!(article["description"], "Summary");
    assert_eq!(article["publishedAt"], "2026-10-02T12:00:00Z");
    assert_eq!(article["urlToImage"], "https://example.com/image.jpg");
    let atom = r#"<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Story</title>
        <link rel="self" href="https://example.com/api"/><link href="https://example.com/story"/>
        <updated>2026-10-02T12:00:00Z</updated></entry></feed>"#;
    assert_eq!(
        parse_rss(atom, "Example", "fallback").unwrap()[0]["url"],
        "https://example.com/story"
    );
    assert_eq!(date_string("not a date", "fallback"), "fallback");
}

#[test]
fn invalid_feed_is_failure_but_real_empty_feed_succeeds() {
    for xml in [
        "<html>Unavailable</html>",
        "<rss><error>Unavailable</error></rss>",
        "<rss><channel><item><title>No URL</title></item></channel></rss>",
    ] {
        assert!(parse_rss(xml, "Example", "fallback").is_err(), "{xml}");
    }
    for xml in [
        "<rss><channel/></rss>",
        r#"<feed xmlns="http://www.w3.org/2005/Atom"/>"#,
    ] {
        assert!(parse_rss(xml, "Example", "fallback").unwrap().is_empty());
    }
}

fn ctv_page(stories: Value) -> String {
    let params = r#"{\"includeSections\":\"/health\"}"#;
    let cache = json!({"story-feed-sections":{params:{"data":{"content_elements":stories}}}});
    format!("<script>Fusion.globalContent={{\"_id\":\"/health\"}};Fusion.contentCache={cache};</script>not executable")
}

fn ctv_story() -> Value {
    json!({"type":"story","owner":{"sponsored":false},"headlines":{"basic":"Health &amp; care"},
        "description":{"basic":"<p>Summary</p>"},"display_date":"2026-11-25T00:00:00Z",
        "websites":{"ctvnews":{"website_url":"https://www.ctvnews.ca/health/article/story/"}}})
}

#[test]
fn listing_is_validated_and_not_executed() {
    let valid = ctv_story();
    let mut external = valid.clone();
    external["websites"]["ctvnews"]["website_url"] = json!("https://evil.example/article/story/");
    let mut sponsored = valid.clone();
    sponsored["owner"]["sponsored"] = json!(true);
    let articles = parse_ctv(
        &ctv_page(json!([null, external, sponsored, valid, valid])),
        "https://www.ctvnews.ca/health/",
    )
    .unwrap();
    assert_eq!(articles.len(), 1);
    assert_eq!(articles[0]["title"], "Health & care");
    assert!(parse_ctv("<html>Unavailable</html>", "https://www.ctvnews.ca/health/").is_err());
    assert!(parse_ctv(&ctv_page(json!([])), "https://www.ctvnews.ca/health/").is_err());
}

#[test]
fn history_prefers_current_versions_and_expires_old_articles() {
    let now = parsed_date("2026-11-25T00:00:00Z").unwrap();
    let fresh = json!({"url":"https://example.com/fresh","publishedAt":"2026-11-25T00:00:00Z","title":"new"});
    let recent = json!({"url":"https://example.com/recent","publishedAt":"2026-11-18T00:00:00Z"});
    let history = json!({"articles":[recent,{"url":"https://example.com/old","publishedAt":"2026-11-17T23:59:59Z"},
        {"url":"https://example.com/future","publishedAt":"2026-11-26T00:00:00Z"},
        {"url":"https://example.com/fresh","publishedAt":"2026-11-25T00:00:00Z","title":"old"},null,{"url":123}]});
    assert_eq!(
        merge_recent(vec![fresh.clone()], &history, now),
        vec![fresh, recent]
    );
    assert!(merge_recent(Vec::new(), &history, now).is_empty());
    let articles = (0..600)
        .map(|n| json!({"url":format!("https://example.com/{n}")}))
        .collect();
    assert_eq!(deduplicate(articles).len(), 500);
}

#[test]
fn api_keys_budget_and_invalid_fields() {
    let environment = BTreeMap::from([
        ("NEWSAPI_KEYS".into(), "one,two one".into()),
        ("FIRSTAPI".into(), "three".into()),
        ("SECONDAPI".into(), "two".into()),
    ]);
    assert_eq!(load_keys(&environment), vec!["one", "two", "three"]);
    assert_eq!(run_budget(4), 31);
    assert_eq!(run_budget(0), 0);
    let response = json!({"articles":[null,"bad",{"url":123},{"url":" "},{"url":"https://example.com/bad","publishedAt":123},{"url":"https://example.com/valid"}]});
    assert_eq!(
        valid_articles(&response),
        vec![json!({"url":"https://example.com/valid"})]
    );
}

#[test]
fn api_rotates_exhausted_keys_and_counts_actual_attempts() {
    let (address, handle, counter) = server(vec![
        (
            429,
            json!({"status":"error","code":"rateLimited","message":"first is exhausted"})
                .to_string(),
        ),
        (200, json!({"status":"ok","articles":[]}).to_string()),
    ]);
    let client = Client::builder().no_proxy().build().unwrap();
    let mut api = Api::new(vec!["first".into(), "second".into()]);
    assert!(api
        .fetch_from(
            &client,
            &Sources::bundled().unwrap(),
            "us",
            "general",
            false,
            &address
        )
        .is_some());
    handle.join().unwrap();
    assert_eq!(counter.load(Ordering::SeqCst), 2);
    assert_eq!(api.used, 2);
    assert!(api.disabled.contains(&0));
    assert!(api.errors.is_empty());
    api.limit = api.used;
    assert!(api
        .fetch_from(
            &client,
            &Sources::bundled().unwrap(),
            "us",
            "general",
            false,
            &address
        )
        .is_none());
    assert!(api.errors.is_empty());
}

#[test]
fn malformed_api_success_is_failure() {
    let (address, handle, _) = server(vec![(
        200,
        json!({"status":"ok","articles":{}}).to_string(),
    )]);
    let mut api = Api::new(vec!["test-key".into()]);
    assert!(api
        .fetch_from(
            &Client::new(),
            &Sources::bundled().unwrap(),
            "us",
            "general",
            false,
            &address
        )
        .is_none());
    handle.join().unwrap();
    assert_eq!(api.used, 1);
    assert_eq!(api.errors.len(), 1);
}

#[test]
fn atomic_files_replace_and_dry_run_does_not_write() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("data/feed.json");
    save_json(&json!({"old":true}), &path, false).unwrap();
    save_json(&json!({"new":true}), &path, true).unwrap();
    assert_eq!(load_json(&path), json!({"old":true}));
    save_json(&json!({"new":true}), &path, false).unwrap();
    assert_eq!(load_json(&path), json!({"new":true}));
    assert_eq!(fs::read_dir(path.parent().unwrap()).unwrap().count(), 1);
    let blocked = directory.path().join("blocked");
    fs::create_dir(&blocked).unwrap();
    assert!(save_json(&json!({}), &blocked, false).is_err());
    assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 2);
}

// Local HTTP fixtures exercise the production fetch, merge, status and write paths.
fn server(bodies: Vec<(u16, String)>) -> (String, std::thread::JoinHandle<()>, Arc<AtomicUsize>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = format!("http://{}", listener.local_addr().unwrap());
    listener.set_nonblocking(true).unwrap();
    let counter = Arc::new(AtomicUsize::new(0));
    let count = counter.clone();
    let handle = std::thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(15);
        for (status, body) in bodies {
            loop {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        stream.set_nonblocking(false).unwrap();
                        stream
                            .set_read_timeout(Some(Duration::from_secs(5)))
                            .unwrap();
                        let mut reader = BufReader::new(stream.try_clone().unwrap());
                        loop {
                            let mut line = String::new();
                            reader.read_line(&mut line).unwrap();
                            if line == "\r\n" || line.is_empty() {
                                break;
                            }
                        }
                        count.fetch_add(1, Ordering::SeqCst);
                        write!(stream,"HTTP/1.1 {status} Test\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len()).unwrap();
                        break;
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        assert!(Instant::now() < deadline, "HTTP fixture timed out");
                        std::thread::sleep(Duration::from_millis(5));
                    }
                    Err(error) => panic!("{error}"),
                }
            }
        }
    });
    (address, handle, counter)
}

#[test]
fn production_pipeline_retains_outages_and_merges_listing() {
    for (rss_ok, listing_ok) in [(true, true), (true, false), (false, true), (false, false)] {
        let rss = if rss_ok {
            (200,"<rss><channel><item><title>RSS story</title><link>https://example.com/rss</link></item></channel></rss>".into())
        } else {
            (503, "Unavailable".into())
        };
        let listing = if listing_ok {
            (200, ctv_page(json!([ctv_story()])))
        } else {
            (503, "Unavailable".into())
        };
        let (address, handle, counter) = server(vec![rss, listing]);
        let mut sources = Sources::bundled().unwrap();
        sources.countries = vec!["ca".into()];
        sources.categories = vec!["health".into()];
        sources.rss_feeds = BTreeMap::from([(
            "ca".into(),
            BTreeMap::from([(
                "health".into(),
                vec![("Example".into(), format!("{address}/rss"))],
            )]),
        )]);
        sources.ctv_health_url = format!("{address}/listing");
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path();
        let old = json!({"articles":[{"url":"https://example.com/old"}],"articles_count":1,"fetched_at":"2026-01-01T00:00:00Z","providers":["rss"]});
        for path in [
            "ca/health.json",
            "data/ca/health_headlines.json",
            "data/ca/health_everything.json",
            "ca/all.json",
            "data/ca/all_headlines.json",
        ] {
            save_json(&old, &root.join(path), false).unwrap();
        }
        let options = Options {
            root: root.to_owned(),
            dry_run: false,
            rss_only: true,
            workers: 1,
        };
        assert_eq!(
            run(&options, &sources).unwrap(),
            if rss_ok || listing_ok { 0 } else { 1 }
        );
        handle.join().unwrap();
        assert_eq!(counter.load(Ordering::SeqCst), 2);
        let status = load_json(&root.join("data/status.json"));
        assert_eq!(status["listing_pages_succeeded"], usize::from(listing_ok));
        assert_eq!(status["newsapi_requests_used"], 0);
        if rss_ok || listing_ok {
            let public = load_json(&root.join("ca/health.json"));
            assert_eq!(
                public["articles"].as_array().unwrap().len(),
                usize::from(rss_ok) + usize::from(listing_ok)
            );
            assert_eq!(
                public["articles"],
                load_json(&root.join("data/ca/health_everything.json"))["articles"]
            );
        } else {
            assert_eq!(status["feeds_updated"], 0);
            assert_eq!(status["feeds_retained"], 2);
            assert_eq!(load_json(&root.join("ca/health.json")), old);
            assert_eq!(load_json(&root.join("ca/all.json")), old);
        }
    }
}

#[test]
fn successful_empty_pipeline_clears_history_but_dry_run_preserves_files() {
    for dry_run in [true, false] {
        let (address, handle, _) = server(vec![(200, "<rss><channel/></rss>".into())]);
        let mut sources = Sources::bundled().unwrap();
        sources.countries = vec!["us".into()];
        sources.categories = vec!["general".into()];
        sources.rss_feeds = BTreeMap::from([(
            "us".into(),
            BTreeMap::from([("general".into(), vec![("Example".into(), address)])]),
        )]);
        let directory = tempfile::tempdir().unwrap();
        let old = json!({"articles":[{"url":"https://example.com/old","publishedAt":timestamp()}]});
        let paths = [
            "us/general.json",
            "us/all.json",
            "data/us/general_headlines.json",
            "data/us/all_headlines.json",
            "data/us/general_everything.json",
        ];
        for path in paths {
            save_json(&old, &directory.path().join(path), false).unwrap();
        }
        let options = Options {
            root: directory.path().to_owned(),
            dry_run,
            rss_only: true,
            workers: 4,
        };
        assert_eq!(run(&options, &sources).unwrap(), 0);
        handle.join().unwrap();
        for path in paths {
            let actual = load_json(&directory.path().join(path));
            assert_eq!(
                actual["articles"],
                if dry_run {
                    old["articles"].clone()
                } else {
                    json!([])
                },
                "{path}"
            );
        }
        let status_path = directory.path().join("data/status.json");
        if dry_run {
            assert!(!status_path.exists());
        } else {
            let status = load_json(&status_path);
            assert_eq!(status["status"], "ok");
            assert_eq!(status["feeds_updated"], 2);
            assert_eq!(status["feeds_retained"], 0);
        }
    }
}
