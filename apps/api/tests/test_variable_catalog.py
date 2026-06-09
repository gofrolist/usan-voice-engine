from usan_api.schemas.variable_catalog import (
    BUILTIN_DEFAULTS,
    BUILTIN_NAMES,
    BUILTIN_VARIABLES,
    VariableSpec,
)


def test_builtin_variables_are_the_ten_contract_names_in_order():
    names = [v.name for v in BUILTIN_VARIABLES]
    assert names == [
        "first_name",
        "elder_name",
        "call_direction",
        "current_time",
        "current_date",
        "last_check_in",
        "last_check_in_line",
        "last_mood",
        "last_pain",
        "today_meds",
    ]


def test_every_builtin_is_tier_builtin_and_specced():
    for v in BUILTIN_VARIABLES:
        assert isinstance(v, VariableSpec)
        assert v.tier == "builtin"
        assert v.description  # non-empty human text
        assert v.example  # non-empty example
        # default is "" or a real fallback string; never None
        assert isinstance(v.default, str)


def test_first_name_and_elder_name_default_to_there():
    assert BUILTIN_DEFAULTS["first_name"] == "there"
    assert BUILTIN_DEFAULTS["elder_name"] == "there"


def test_data_builtins_default_to_empty_string():
    for name in (
        "call_direction",
        "current_time",
        "current_date",
        "last_check_in",
        "last_check_in_line",
        "last_mood",
        "last_pain",
        "today_meds",
    ):
        assert BUILTIN_DEFAULTS[name] == ""


def test_builtin_names_is_frozenset_of_all_ten():
    assert frozenset(v.name for v in BUILTIN_VARIABLES) == BUILTIN_NAMES
    assert len(BUILTIN_NAMES) == 10


def test_builtin_defaults_cover_every_name():
    assert set(BUILTIN_DEFAULTS) == BUILTIN_NAMES
