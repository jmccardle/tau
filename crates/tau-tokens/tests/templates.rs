//! The template engine against Jinja2: every `tests/fixtures/tokens/templates/*.jinja` rendered
//! on every request of conversations.json must be, byte for byte, what `transformers` rendered
//! (templates.json). Where `transformers` refused a request, so must this. Runs offline.

mod common;

use std::fmt::Write as _;

use tau_tokens::{ChatTemplate, Error};

/// Where two renderings part, with some text around the place.
fn parting(ours: &str, theirs: &str) -> String {
    let at = ours.bytes().zip(theirs.bytes()).position(|(a, b)| a != b);
    let at = at.unwrap_or(ours.len().min(theirs.len()));
    let window = |text: &str| {
        let start = (0..=at.saturating_sub(50))
            .rev()
            .find(|i| text.is_char_boundary(*i))
            .unwrap_or(0);
        let end = (at + 50..text.len())
            .find(|i| text.is_char_boundary(*i))
            .unwrap_or(text.len());
        text.get(start..end).unwrap_or_default().to_owned()
    };
    format!(
        "at byte {at}:\n     ours: {:?}\n   theirs: {:?}",
        window(ours),
        window(theirs)
    )
}

/// Every line that differs when the two have the same lines, else the first place they part.
fn first_difference(ours: &str, theirs: &str) -> String {
    let (our_lines, their_lines): (Vec<&str>, Vec<&str>) =
        (ours.split('\n').collect(), theirs.split('\n').collect());
    if our_lines.len() != their_lines.len() {
        return parting(ours, theirs);
    }
    let mut out = String::new();
    for (n, (a, b)) in our_lines.iter().zip(&their_lines).enumerate() {
        if a != b {
            let _ = write!(out, "\n  line {}: {}", n + 1, parting(a, b));
        }
    }
    out
}

#[test]
fn every_template_renders_as_jinja2_renders_it() {
    let golden = common::fixture("templates.json");
    let now = common::now(&golden);
    let tokens: Vec<(String, String)> = golden["tokens"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(name, text)| (name.clone(), text.as_str().unwrap().to_owned()))
        .collect();
    let mut problems = Vec::new();
    let mut compared = 0;
    for (name, cases) in golden["templates"].as_object().unwrap() {
        let source = common::read(&common::fixtures().join(format!("templates/{name}.jinja")));
        let template = ChatTemplate::new(std::str::from_utf8(&source).unwrap(), &tokens).unwrap();
        for request in common::requests() {
            let expected = &cases[&request.name];
            let ours = template.render(
                &request.messages,
                request.tools.as_deref(),
                request.generate,
                request.extra.as_deref(),
                Some(now),
            );
            compared += 1;
            match (ours, expected["rendered"].as_str()) {
                (Ok(ours), Some(theirs)) if ours == theirs => {}
                (Ok(ours), Some(theirs)) => problems.push(format!(
                    "{name} on {}: differs {}",
                    request.name,
                    first_difference(&ours, theirs)
                )),
                (Err(Error::Template(_)), None) => {}
                (Err(err), _) => {
                    problems.push(format!("{name} on {}: we fail: {err}", request.name));
                }
                (Ok(_), None) => problems.push(format!(
                    "{name} on {}: we render what transformers refuses ({})",
                    request.name, expected["error"]
                )),
            }
        }
    }
    assert!(compared >= 100, "only {compared} renderings compared");
    assert!(
        problems.is_empty(),
        "{} of {compared} differ:\n{}",
        problems.len(),
        problems.join("\n")
    );
}

#[test]
fn a_template_that_reads_the_clock_needs_the_time() {
    let template = ChatTemplate::new("{{ strftime_now('%Y') }}", &[]).unwrap();
    let messages = br#"[{"role": "user", "content": "hi"}]"#;
    let Err(Error::Template(message)) = template.render(messages, None, true, None, None) else {
        panic!("rendered without a clock");
    };
    assert!(message.contains("pass `now`"), "{message}");
}

#[test]
fn bad_input_is_refused_by_name() {
    assert!(matches!(
        ChatTemplate::new("{% if %}", &[]),
        Err(Error::Template(_))
    ));
    assert!(matches!(
        ChatTemplate::new("{% generation %}x{% endgeneration %}", &[]),
        Err(Error::Template(_))
    ));
    let template = ChatTemplate::new("{{ messages | length }}", &[]).unwrap();
    let render = |messages: &[u8], tools: Option<&[u8]>, extra: Option<&[u8]>| match template
        .render(messages, tools, true, extra, None)
    {
        Err(Error::Template(message)) => message,
        other => panic!("expected a template error, got {other:?}"),
    };
    assert!(render(b"{", None, None).starts_with("messages:"));
    assert!(render(br#"{"role": "user"}"#, None, None).contains("must be a JSON array"));
    assert!(render(br#"["hi"]"#, None, None).contains("each message must be an object"));
    assert!(render(b"[]", Some(b"nope"), None).starts_with("tools:"));
    assert!(render(b"[]", None, Some(b"[1]")).contains("must be a JSON object"));
    assert!(render(b"[]", None, Some(br#"{"messages": []}"#)).contains("argument of its own"));
    assert!(render(b"[1e400]", None, None).starts_with("messages:"));
}

#[test]
fn a_config_gives_the_template_and_the_named_tokens() {
    let config = br#"{
        "bos_token": {"content": "<s>", "lstrip": false},
        "eos_token": "</s>",
        "pad_token": null,
        "chat_template": [
            {"name": "default", "template": "{{ bos_token }}plain{{ eos_token }}[{{ pad_token }}]"},
            {"name": "tool_use", "template": "{{ bos_token }}{{ tools | length }} tools"}
        ]
    }"#;
    let template = ChatTemplate::from_config(config, None).unwrap();
    let messages = br#"[{"role": "user", "content": "hi"}]"#;
    let render = |t: &ChatTemplate, tools| t.render(messages, tools, true, None, None).unwrap();
    assert_eq!(render(&template, None), "<s>plain</s>[]");
    assert_eq!(render(&template, Some(b"[{}, {}]")), "<s>2 tools");
    let replaced = ChatTemplate::from_config(config, Some("{{ eos_token }}!")).unwrap();
    assert_eq!(render(&replaced, Some(b"[]")), "</s>!");
    assert!(
        matches!(ChatTemplate::from_config(b"{}", None), Err(Error::Template(m)) if m.contains("no chat_template"))
    );
    assert!(matches!(
        ChatTemplate::from_config(b"[]", None),
        Err(Error::Template(_))
    ));
}
