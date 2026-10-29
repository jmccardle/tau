//! `diagram-types FILE:Name ...` prints, as JSON in the order asked, each
//! named struct or enum of the Rust source FILE: its fields or variants as
//! written. Nothing else is read from the source (no docs, no derives).

use quote::ToTokens;
use serde_json::{Value, json};
use syn::{Fields, Item, Visibility};

/// A type as written, with the token printer's spaces taken out.
fn ty(t: &impl ToTokens) -> String {
    t.to_token_stream()
        .to_string()
        .replace(" < ", "<")
        .replace(" >", ">")
        .replace("< ", "<")
        .replace(" ,", ",")
        .replace(" :: ", "::")
        .replace("& ", "&")
        .replace("( ", "(")
        .replace(" )", ")")
}

fn vis(v: &Visibility) -> &'static str {
    match v {
        Visibility::Public(_) => "pub",
        Visibility::Inherited => "private",
        Visibility::Restricted(_) => "restricted",
    }
}

fn fields(f: &Fields) -> Value {
    let list: Vec<Value> = f
        .iter()
        .enumerate()
        .map(|(i, f)| {
            json!({
                "name": f.ident.as_ref().map_or_else(|| i.to_string(), ToString::to_string),
                "ty": ty(&f.ty),
                "vis": vis(&f.vis),
            })
        })
        .collect();
    Value::Array(list)
}

fn shape(f: &Fields) -> &'static str {
    match f {
        Fields::Named(_) => "named",
        Fields::Unnamed(_) => "tuple",
        Fields::Unit => "unit",
    }
}

fn describe(item: &Item, name: &str) -> Option<Value> {
    match item {
        Item::Struct(s) if s.ident == name => Some(json!({
            "name": name, "kind": "struct", "fields": fields(&s.fields),
            "shape": shape(&s.fields), "vis": vis(&s.vis),
        })),
        Item::Enum(e) if e.ident == name => {
            let variants: Vec<Value> = e
                .variants
                .iter()
                .map(|v| {
                    json!({"name": v.ident.to_string(), "shape": shape(&v.fields),
                           "fields": fields(&v.fields)})
                })
                .collect();
            Some(json!({"name": name, "kind": "enum", "variants": variants, "vis": vis(&e.vis)}))
        }
        _ => None,
    }
}

fn main() {
    let mut out = vec![];
    for arg in std::env::args().skip(1) {
        let (file, name) = arg
            .split_once(':')
            .unwrap_or_else(|| panic!("expected FILE:Name, got {arg}"));
        let src = std::fs::read_to_string(file).unwrap_or_else(|e| panic!("{file}: {e}"));
        let ast = syn::parse_file(&src).unwrap_or_else(|e| panic!("{file}: {e}"));
        let Some(mut d) = ast.items.iter().find_map(|i| describe(i, name)) else {
            panic!("{name} is not a struct or enum in {file}");
        };
        d["file"] = json!(file);
        out.push(d);
    }
    println!("{}", serde_json::to_string_pretty(&out).unwrap());
}
