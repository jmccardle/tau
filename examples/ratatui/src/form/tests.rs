//! Forms from schemas: the supported subset, the unsupported refused by name, and what is sent.

use serde_json::json;

use super::*;

fn schema(value: &Value) -> Schema {
    value.as_object().unwrap().clone()
}

fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, ratatui::crossterm::event::KeyModifiers::NONE)
}

fn type_into(form: &mut Form, text: &str) {
    for c in text.chars() {
        form.key(key(KeyCode::Char(c)));
    }
}

/// An extension with one option of each kind the renderer shows.
fn options() -> Form {
    Form::new(
        1,
        "demo",
        &schema(&json!({
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10, "description": "steps"},
                "ratio": {"type": "number", "default": 0.5},
                "name": {"type": "string", "default": "x"},
                "loud": {"type": "boolean", "default": false},
                "mode": {"type": "string", "enum": ["fast", "slow"], "default": "fast"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["a", "b"]},
                    "default": [],
                },
                "ids": {"type": "array", "items": {"type": "integer"}, "default": []},
                "maybe": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": null},
            },
            "required": ["name"],
        })),
    )
    .unwrap()
}

fn submit(form: &mut Form) -> Done {
    form.focus = form.fields.len() + 1;
    assert_eq!(form.focus(), Focus::Apply);
    form.key(key(KeyCode::Enter))
}

fn field(form: &Form, name: &str) -> usize {
    form.fields.iter().position(|f| f.name == name).unwrap()
}

#[test]
fn every_supported_kind_gets_its_field_in_the_schemas_order() {
    let form = options();
    let names: Vec<&str> = form.fields.iter().map(|f| f.name.as_str()).collect();
    assert_eq!(
        names,
        [
            "limit", "ratio", "name", "loud", "mode", "tags", "ids", "maybe"
        ]
    );
    assert!(matches!(form.fields[3].widget, Widget::Switch(false)));
    assert!(matches!(
        form.fields[4].widget,
        Widget::Choice {
            picked: Some(0),
            ..
        }
    ));
    assert!(matches!(form.fields[5].widget, Widget::Multi { .. }));
    assert!(matches!(form.fields[0].widget, Widget::Text(_)));
    assert_eq!(form.fields[0].placeholder(), "10");
    assert_eq!(form.fields[0].description.as_deref(), Some("steps"));
    assert!(form.fields[2].required && !form.fields[0].required);
    assert!(
        !form.fields[7].required,
        "a nullable field may be left empty"
    );
}

#[test]
fn a_schema_the_form_cannot_show_is_refused_naming_the_option() {
    for (bad, why) in [
        (json!({"type": "object"}), "object"),
        (json!({"$ref": "#/$defs/X"}), "no `type`"),
        (json!({"oneOf": [{"type": "string"}]}), "no `type`"),
        (json!({"const": 3}), "no `type`"),
        (json!({"type": "array"}), "no `items`"),
        (
            json!({"type": "array", "items": {"type": "object"}}),
            "object",
        ),
        (json!({"anyOf": [{"type": "null"}]}), "only null"),
    ] {
        let err = Form::new(
            1,
            "demo",
            &schema(&json!({"properties": {"fine": {"type": "string"}, "odd": bad}})),
        )
        .unwrap_err();
        assert!(err.contains("\"odd\""), "{err}");
        assert!(err.contains(why), "{err} should say {why}");
    }
}

#[test]
fn text_is_read_as_the_fields_type() {
    let int = schema(&json!({"type": "integer"}));
    assert_eq!(read_value(" 7 ", &int).unwrap(), json!(7));
    assert_eq!(
        read_value("x", &int).unwrap_err(),
        "\"x\" is not an integer"
    );
    let list = schema(&json!({"type": "array", "items": {"type": "integer"}}));
    assert_eq!(read_value("1,2", &list).unwrap(), json!([1, 2]));
    assert_eq!(read_value("[3, 4]", &list).unwrap(), json!([3, 4]));
    assert!(read_value("1,x", &list).is_err());
    assert!(read_value("[1", &list).unwrap_err().contains("JSON array"));
    let flag = schema(&json!({"type": "boolean"}));
    assert_eq!(read_value("Yes", &flag).unwrap(), json!(true));
    let one_of = schema(&json!({"type": "string", "enum": ["a", "b"]}));
    assert!(
        read_value("c", &one_of)
            .unwrap_err()
            .contains("is not one of")
    );
    let union =
        schema(&json!({"anyOf": [{"type": "integer"}, {"type": "string", "enum": ["new"]}]}));
    assert_eq!(read_value("new", &union).unwrap(), json!("new"));
    assert_eq!(read_value("3", &union).unwrap(), json!(3));
    assert!(read_value("old", &union).unwrap_err().contains(" and "));
}

#[test]
fn only_what_changed_is_sent_unless_save_is_on() {
    let mut form = options();
    let i = field(&form, "name");
    form.focus = i;
    type_into(&mut form, "y"); // required, differs from "x"
    let Done::Submit(sent, save) = submit(&mut form) else {
        panic!("should submit")
    };
    assert!(!save);
    assert_eq!(Value::Object(sent), json!({"name": "y"}));

    // Typing the value the field already had is no change, with save on or off.
    let mut form = options();
    form.focus = field(&form, "name");
    type_into(&mut form, "x");
    form.focus = field(&form, "limit");
    type_into(&mut form, "10");
    form.focus = form.fields.len();
    form.key(key(KeyCode::Char(' ')));
    assert!(form.save);
    let Done::Submit(sent, save) = submit(&mut form) else {
        panic!("should submit")
    };
    assert!(save, "save is asked for even though nothing changed");
    assert!(sent.is_empty(), "{sent:?}");
}

#[test]
fn a_switch_toggled_twice_is_not_a_change() {
    let mut form = options();
    form.focus = field(&form, "name");
    type_into(&mut form, "x");
    form.focus = field(&form, "loud");
    form.key(key(KeyCode::Char(' ')));
    form.key(key(KeyCode::Char(' ')));
    let Done::Submit(sent, _) = submit(&mut form) else {
        panic!("should submit")
    };
    assert!(sent.is_empty(), "{sent:?}");
}

#[test]
fn an_enum_with_no_values_is_refused_not_a_panic() {
    for bad in [
        json!({"type": "string", "enum": []}),
        json!({"type": "array", "items": {"enum": []}}),
    ] {
        let err = Form::new(1, "demo", &schema(&json!({"properties": {"odd": bad}}))).unwrap_err();
        assert!(
            err.contains("\"odd\"") && err.contains("no values"),
            "{err}"
        );
    }
}

#[test]
fn a_late_answer_lands_in_its_own_form_only() {
    assert_eq!(landing(Some(3), 3), Landing::Form);
    assert_eq!(
        landing(Some(4), 3),
        Landing::Notice,
        "another form opened since"
    );
    assert_eq!(landing(None, 3), Landing::Notice, "the form was cancelled");
}

#[test]
fn a_required_field_left_empty_and_an_unreadable_one_keep_the_form_open() {
    let mut form = options();
    form.focus = field(&form, "limit");
    type_into(&mut form, "lots");
    assert_eq!(submit(&mut form), Done::Open);
    let i = field(&form, "limit");
    assert_eq!(
        form.fields[i].error.as_deref(),
        Some("\"lots\" is not an integer")
    );
    let n = field(&form, "name");
    assert_eq!(form.fields[n].error.as_deref(), Some("a value is needed"));
}

#[test]
fn switches_choices_and_multi_selects_are_set_with_the_keyboard() {
    let mut form = options();
    form.focus = field(&form, "name");
    type_into(&mut form, "x");
    form.focus = field(&form, "loud");
    form.key(key(KeyCode::Char(' ')));
    form.focus = field(&form, "mode");
    form.key(key(KeyCode::Right));
    form.focus = field(&form, "tags");
    form.key(key(KeyCode::Right));
    form.key(key(KeyCode::Char(' ')));
    let Done::Submit(sent, _) = submit(&mut form) else {
        panic!("should submit")
    };
    assert_eq!(
        Value::Object(sent),
        json!({"loud": true, "mode": "slow", "tags": ["b"]})
    );
}

#[test]
fn escape_and_the_cancel_button_cancel() {
    let mut form = options();
    assert_eq!(form.key(key(KeyCode::Esc)), Done::Cancel);
    form.focus = form.fields.len() + 2;
    assert_eq!(form.focus(), Focus::Cancel);
    assert_eq!(form.key(key(KeyCode::Enter)), Done::Cancel);
}

#[test]
fn a_form_for_an_extension_without_properties_is_refused() {
    assert!(Form::new(1, "demo", &schema(&json!({"properties": {}}))).is_err());
}
