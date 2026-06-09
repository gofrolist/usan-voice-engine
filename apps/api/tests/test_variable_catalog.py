from usan_api.schemas.variable_catalog import (
    BUILTIN_DEFAULTS,
    BUILTIN_NAMES,
    BUILTIN_VARIABLES,
    PHI_BUILTIN_NAMES,
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


# --- PHI flag tests ---

PHI_NAMES = {"last_check_in", "last_check_in_line", "last_mood", "last_pain", "today_meds"}


def test_phi_true_variables_are_exactly_the_health_data_set():
    actual_phi = {v.name for v in BUILTIN_VARIABLES if v.phi}
    assert actual_phi == PHI_NAMES


def test_non_phi_builtins_have_phi_false():
    for v in BUILTIN_VARIABLES:
        if v.name not in PHI_NAMES:
            assert v.phi is False, f"{v.name} should have phi=False"


def test_every_builtin_has_phi_field():
    for v in BUILTIN_VARIABLES:
        assert isinstance(v.phi, bool), f"{v.name}.phi must be bool"


# --- PHI_BUILTIN_NAMES constant ---


def test_phi_builtin_names_is_exactly_the_health_data_frozenset():
    expected = frozenset(
        {"last_check_in", "last_check_in_line", "last_mood", "last_pain", "today_meds"}
    )
    assert expected == PHI_BUILTIN_NAMES
