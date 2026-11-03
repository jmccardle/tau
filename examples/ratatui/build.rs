//! Generates the wire types from `docs/protocol.schema.json` into `$OUT_DIR/wire.rs`.
//!
//! The schema is JSON Schema 2020-12; the generator (typify) reads draft-07. So the schema is
//! spelled in the older dialect first, and nothing else about it changes:
//!
//! - `$defs` becomes `definitions` (and every `#/$defs/` reference with it);
//! - `prefixItems` becomes the array form of `items` (a tuple);
//! - `{"const": x}` becomes `{"enum": [x]}`, which is how a `kind`-tagged event is told apart
//!   from its neighbours (`TextDelta` and `ThinkingDelta` differ in that and nothing else).
//!
//! Then the schema's two kinds of definition are told apart. A call is a definition with an
//! `x-result` (named like its method, `node`, `open`): it becomes `<Method>Params`, and its
//! result a definition `<Method>Result`, so a method never collides with a type (`node` and
//! `Node`). `ErrorCode`'s `x-names` become constants. Everything is read from the schema; no
//! wire shape is written by hand.

use serde_json::{Map, Value, json};
use std::{env, fmt::Write as _, fs, path::PathBuf};
use typify::{TypeSpace, TypeSpaceSettings};

fn main() {
    let manifest = PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("cargo sets it"));
    let schema_path = manifest.join("../../docs/protocol.schema.json");
    println!("cargo:rerun-if-changed={}", schema_path.display());
    println!("cargo:rerun-if-changed=build.rs");

    let text = fs::read_to_string(&schema_path)
        .unwrap_or_else(|e| panic!("cannot read {}: {e}", schema_path.display()));
    let schema: Value = serde_json::from_str(&text).expect("the schema is JSON");
    let protocol = schema["x-protocol"]
        .as_i64()
        .expect("the schema names its protocol");

    let mut defs = match &schema["$defs"] {
        Value::Object(defs) => defs.clone(),
        other => panic!("the schema has no $defs object: {other}"),
    };
    let calls = split_calls(&mut defs);
    let names = error_names(&defs["ErrorCode"]);

    let root = json!({
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "WireProtocol",
        "definitions": downgrade(Value::Object(defs)),
    });
    let root: schemars::schema::RootSchema =
        serde_json::from_value(root).expect("the downgraded schema is draft-07");
    let mut space = TypeSpace::new(
        TypeSpaceSettings::default()
            .with_derive("Clone".into())
            .with_derive("PartialEq".into()),
    );
    space
        .add_root_schema(root)
        .expect("typify reads the schema");
    let file: syn::File = syn::parse2(space.to_stream()).expect("typify writes valid Rust");
    let mut out = prettyplease::unparse(&file);

    writeln!(out, "/// The protocol number the schema describes.").unwrap();
    writeln!(out, "pub const PROTOCOL: i64 = {protocol};").unwrap();
    writeln!(
        out,
        "/// Every call of the schema: its method name and its types."
    )
    .unwrap();
    for (method, type_name) in &calls {
        writeln!(
            out,
            "impl Call for {type_name}Params {{ const METHOD: &'static str = \"{method}\"; \
             type Output = {type_name}Result; }}"
        )
        .unwrap();
    }
    writeln!(out, "/// `ErrorCode`s by the name the schema gives them.").unwrap();
    writeln!(out, "#[allow(dead_code)]\npub mod code {{").unwrap();
    for (name, code) in names {
        writeln!(out, "    pub const {}: i64 = {code};", screaming(&name)).unwrap();
    }
    writeln!(out, "}}").unwrap();

    let dest = PathBuf::from(env::var("OUT_DIR").expect("cargo sets it")).join("wire.rs");
    fs::write(dest, out).expect("OUT_DIR is writable");
}

/// Moves every call (a definition with `x-result`) to `<Type>Params`, adds `<Type>Result`, and
/// returns `(method, Type)` for each.
fn split_calls(defs: &mut Map<String, Value>) -> Vec<(String, String)> {
    let methods: Vec<String> = defs
        .iter()
        .filter(|(_, d)| d.get("x-result").is_some())
        .map(|(name, _)| name.clone())
        .collect();
    let mut calls = Vec::new();
    for method in methods {
        let mut params = defs.remove(&method).expect("listed above");
        let result = params
            .as_object_mut()
            .and_then(|p| p.remove("x-result"))
            .expect("filtered on it");
        let type_name = camel(&method);
        defs.insert(format!("{type_name}Params"), params);
        defs.insert(format!("{type_name}Result"), result);
        calls.push((method, type_name));
    }
    calls
}

fn error_names(code: &Value) -> Vec<(String, i64)> {
    let names = code["x-names"].as_array().expect("ErrorCode has x-names");
    let codes = code["enum"].as_array().expect("ErrorCode has an enum");
    assert_eq!(
        names.len(),
        codes.len(),
        "ErrorCode: x-names and enum differ in length"
    );
    names
        .iter()
        .zip(codes)
        .map(|(n, c)| {
            (
                n.as_str().expect("a name").to_owned(),
                c.as_i64().expect("an integer"),
            )
        })
        .collect()
}

/// 2020-12 as typify reads it (see the top of this file).
fn downgrade(value: Value) -> Value {
    match value {
        Value::Object(map) => {
            // Keywords are respelled wherever they are keys, so a property that is called by one
            // would be mangled: refuse it, and `items` beside `prefixItems`.
            if let Some(Value::Object(properties)) = map.get("properties") {
                for name in ["const", "$ref", "prefixItems"] {
                    assert!(
                        !properties.contains_key(name),
                        "a property called {name:?} would be taken for the keyword"
                    );
                }
            }
            assert!(
                !(map.contains_key("prefixItems") && map.contains_key("items")),
                "prefixItems beside items has no draft-07 spelling: {map:?}"
            );
            let mut out = Map::new();
            for (key, inner) in map {
                match key.as_str() {
                    "prefixItems" => {
                        out.insert("items".into(), downgrade(inner));
                    }
                    "const" => {
                        out.insert("enum".into(), json!([inner]));
                    }
                    "$ref" => {
                        let target = inner.as_str().expect("a $ref is a string");
                        out.insert(key, json!(target.replace("#/$defs/", "#/definitions/")));
                    }
                    _ => {
                        out.insert(key, downgrade(inner));
                    }
                }
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(items.into_iter().map(downgrade).collect()),
        other => other,
    }
}

fn camel(snake: &str) -> String {
    snake
        .split('_')
        .map(|w| {
            let mut c = w.chars();
            c.next()
                .map(|f| f.to_uppercase().chain(c).collect::<String>())
                .unwrap_or_default()
        })
        .collect()
}

fn screaming(camel: &str) -> String {
    let mut out = String::new();
    for (i, c) in camel.chars().enumerate() {
        if c.is_uppercase() && i > 0 {
            out.push('_');
        }
        out.push(c.to_ascii_uppercase());
    }
    out
}
