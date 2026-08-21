"""Tests for tau_llm.tools.define_tool — the validated ToolDefinition builder.

define_tool was an exported stub that raised NotImplementedError for the whole
life of the package, so every rule it enforces is new surface. The rules exist
to move failures from tool-CALL time (deep in an agent loop, several turns
after the mistake) to tool-DEFINITION time, so each test below names the
run-time failure it is buying out.

Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.
"""

import pytest

from tau_llm import define_tool as define_tool_from_package
from tau_llm.tools import ToolDefinition, define_tool, validate_tool_arguments


def _fields(**overrides):
    """A definition that passes every rule, as keyword fields."""
    base = {
        "name": "word_count",
        "label": "Word count",
        "description": "Count the words in a string.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "execute": lambda text: {"words": len(text.split())},
    }
    base.update(overrides)
    return base


class TestKeywordForm:
    """The primary, documented call shape."""

    def test_keyword_form_sets_every_field_as_given(self):
        execute = _fields()["execute"]
        params = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }
        tool = define_tool(
            name="word_count",
            label="Word count",
            description="Count the words in a string.",
            parameters=params,
            execute=execute,
            prompt_snippet="word_count: counts words",
            prompt_guidelines=["Use it on prose, not on code."],
            execution_mode="sequential",
        )
        assert isinstance(tool, ToolDefinition)
        assert tool.name == "word_count"
        assert tool.label == "Word count"
        assert tool.description == "Count the words in a string."
        assert tool.parameters == params
        assert tool.execute is execute
        assert tool.prompt_snippet == "word_count: counts words"
        assert tool.prompt_guidelines == ["Use it on prose, not on code."]
        assert tool.execution_mode == "sequential"

    def test_optional_fields_default(self):
        tool = define_tool(**_fields())
        assert tool.prompt_snippet is None
        assert tool.prompt_guidelines is None
        # SUBPHASE-0.0.md pins the default; a silent flip to "sequential" would
        # serialise every tool batch without any error to notice it by.
        assert tool.execution_mode == "parallel"

    def test_exported_from_package_root(self):
        assert define_tool_from_package is define_tool


class TestPositionalMappingForm:
    """The signature define_tool has always advertised: a single dict."""

    def test_positional_mapping_matches_keyword_form(self):
        fields = _fields()
        from_mapping = define_tool(dict(fields))
        from_keywords = define_tool(**fields)
        # `execute` is a callable, so compare it identically and the rest by value.
        assert from_mapping.execute is from_keywords.execute is fields["execute"]
        assert from_mapping.model_dump(exclude={"execute"}) == from_keywords.model_dump(
            exclude={"execute"}
        )

    def test_accepts_any_mapping_not_only_dict(self):
        from collections import OrderedDict

        tool = define_tool(OrderedDict(_fields()))
        assert tool.name == "word_count"

    def test_positional_non_mapping_raises_type_error(self):
        with pytest.raises(TypeError, match="must be a mapping"):
            define_tool([("name", "word_count")])

    def test_non_string_keys_raise_type_error(self):
        fields = _fields()
        fields[7] = "nonsense"
        with pytest.raises(TypeError, match="field names must be strings"):
            define_tool(fields)


class TestCallFormIsExclusive:
    """Both forms at once, or neither, is a caller error — not a merge."""

    def test_both_forms_raises(self):
        with pytest.raises(TypeError, match="not both"):
            define_tool(_fields(), label="Overridden")

    def test_neither_form_raises(self):
        with pytest.raises(TypeError, match="nothing to define"):
            define_tool()

    def test_definition_passed_by_keyword_is_reported_with_a_hint(self):
        # `definition` is positional-only now, so this lands as an unknown field.
        # The message has to say what to do instead, because the old signature
        # made `definition=` look right.
        with pytest.raises(ValueError, match="pass a mapping positionally"):
            define_tool(definition=_fields())


class TestRequiredFields:
    @pytest.mark.parametrize("field", ["name", "label", "description", "parameters", "execute"])
    def test_missing_required_field_names_it(self, field):
        fields = _fields()
        del fields[field]
        with pytest.raises(ValueError, match=field):
            define_tool(**fields)

    def test_label_is_not_derived_from_name(self):
        """label must be supplied, never invented from `name`.

        Deriving it would make the TUI show a developer identifier as a human
        label and no one would ever see a problem, because it would "work".
        """
        fields = _fields()
        del fields["label"]
        with pytest.raises(ValueError, match="label"):
            define_tool(**fields)

    @pytest.mark.parametrize("field", ["label", "description"])
    def test_blank_text_field_raises(self, field):
        # Passes pydantic's `str` but is useless at the destination: a blank
        # chip in the TUI, or a tool the model is told nothing about.
        with pytest.raises(ValueError, match=field):
            define_tool(**_fields(**{field: "   "}))

    @pytest.mark.parametrize("field", ["name", "label", "description"])
    def test_non_string_text_field_raises(self, field):
        with pytest.raises(TypeError, match=field):
            define_tool(**_fields(**{field: 42}))


class TestUnknownFields:
    def test_unknown_field_raises_rather_than_being_dropped(self):
        # ToolDefinition is `extra="ignore"`, so without this check a typo'd
        # optional field vanishes and the feature silently never happens.
        with pytest.raises(ValueError, match="prompt_snipet"):
            define_tool(**_fields(prompt_snipet="typo"))


class TestExecutable:
    def test_non_callable_execute_raises(self):
        with pytest.raises(TypeError, match="execute"):
            define_tool(**_fields(execute="not a function"))


class TestParametersSchema:
    def test_non_dict_parameters_raises(self):
        with pytest.raises(TypeError, match="parameters"):
            define_tool(**_fields(parameters="{}"))

    def test_non_object_schema_raises(self):
        # `_validate_json_schema` reads `required`/`properties` regardless of
        # `type`, so a non-object schema validates every call vacuously.
        with pytest.raises(ValueError, match="type"):
            define_tool(**_fields(parameters={"type": "string"}))

    def test_missing_type_raises(self):
        with pytest.raises(ValueError, match="object"):
            define_tool(**_fields(parameters={"properties": {}}))

    def test_missing_properties_raises(self):
        with pytest.raises(ValueError, match="properties"):
            define_tool(**_fields(parameters={"type": "object"}))

    def test_empty_properties_is_a_valid_no_argument_tool(self):
        tool = define_tool(**_fields(parameters={"type": "object", "properties": {}}))
        assert tool.parameters["properties"] == {}

    def test_non_dict_properties_raises(self):
        with pytest.raises(TypeError, match="properties"):
            define_tool(**_fields(parameters={"type": "object", "properties": ["text"]}))

    def test_non_dict_property_schema_raises(self):
        # `_validate_json_schema` calls .get("type") on each property, so this
        # would be an AttributeError mid-tool-call otherwise.
        with pytest.raises(TypeError, match="text"):
            define_tool(**_fields(parameters={"type": "object", "properties": {"text": "string"}}))

    def test_non_list_required_raises(self):
        with pytest.raises(TypeError, match="required"):
            define_tool(
                **_fields(
                    parameters={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": "text",
                    }
                )
            )

    def test_non_string_required_entry_raises(self):
        with pytest.raises(TypeError, match="required"):
            define_tool(
                **_fields(
                    parameters={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": [1],
                    }
                )
            )

    def test_required_name_absent_from_properties_raises(self):
        # The model is never told about `missing`, so it cannot send it, but
        # validate_tool_arguments demands it — an unbreakable error loop.
        with pytest.raises(ValueError, match="missing"):
            define_tool(
                **_fields(
                    parameters={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text", "missing"],
                    }
                )
            )

    def test_pydantic_model_json_schema_is_accepted_verbatim(self):
        """The construction path SUBPHASE-0.0.md documents must pass unchanged."""
        from pydantic import BaseModel

        class GreetParams(BaseModel):
            name: str
            excited: bool = False

        tool = define_tool(**_fields(parameters=GreetParams.model_json_schema()))
        assert tool.parameters["properties"]["name"]["type"] == "string"

    def test_nested_schema_keywords_are_left_alone(self):
        """Extra keywords are neither validated nor stripped.

        validate_tool_arguments ignores everything but top-level type/required,
        and define_tool must not imply an enforcement that does not exist.
        """
        params = {
            "type": "object",
            "title": "Params",
            "properties": {"text": {"type": "string", "minLength": 5}},
            "required": ["text"],
            "additionalProperties": False,
        }
        tool = define_tool(**_fields(parameters=params))
        assert tool.parameters == params


class TestWireSafeName:
    @pytest.mark.parametrize("name", ["word count", "word.count", "", "a" * 65, "naïve"])
    def test_unusable_name_raises(self, name):
        # The OpenAI API rejects the whole request for one bad tool name, with a
        # 400 that never mentions which tool caused it.
        with pytest.raises(ValueError, match="name"):
            define_tool(**_fields(name=name))

    @pytest.mark.parametrize("name", ["word_count", "wordCount", "word-count", "tool9"])
    def test_usable_names_accepted(self, name):
        # camelCase is accepted on purpose: snake_case is a convention in
        # SUBPHASE-0.0.md, not something the runtime or the wire enforces.
        assert define_tool(**_fields(name=name)).name == name


class TestEndToEndWithValidateToolArguments:
    """The pair a real caller uses: build with define_tool, validate the call."""

    def test_valid_arguments_pass(self):
        tool = define_tool(**_fields())

        class Call:
            arguments = {"text": "one two three"}

        assert validate_tool_arguments(tool, Call()) == {"text": "one two three"}
        assert tool.execute(**validate_tool_arguments(tool, Call())) == {"words": 3}

    def test_missing_required_argument_is_rejected(self):
        tool = define_tool(**_fields())

        class Call:
            arguments = {}

        with pytest.raises(ValueError, match="text"):
            validate_tool_arguments(tool, Call())

    def test_wrong_argument_type_is_rejected(self):
        tool = define_tool(**_fields())

        class Call:
            arguments = {"text": 5}

        with pytest.raises(ValueError, match="expected string"):
            validate_tool_arguments(tool, Call())
