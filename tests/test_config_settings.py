from codemate.config.paths import codemate_paths
from codemate.config.settings import build_permission_rules, load_codemate_settings


def test_permission_rules_compact_parent_child_paths(tmp_path):
    paths = codemate_paths(tmp_path, home_root=tmp_path / "home")
    rules = build_permission_rules(
        paths,
        user_settings={
            "permissions": {
                "read": {"allow": ["/tmp", "/tmp/awdawd"], "deny": []},
                "write": {"allow": [], "deny": []},
            }
        },
        project_settings={
            "permissions": {
                "read": {"allow": ["/tmp/awdawd/nested"], "deny": []},
                "write": {"allow": ["/tmp/write", "/tmp/write/nested"], "deny": []},
            }
        },
    )

    assert "/tmp" in {str(path) for path in rules.read_allow}
    assert "/tmp/awdawd" not in {str(path) for path in rules.read_allow}
    assert "/tmp/awdawd/nested" not in {str(path) for path in rules.read_allow}
    assert "/tmp/write" in {str(path) for path in rules.write_allow}
    assert "/tmp/write/nested" not in {str(path) for path in rules.write_allow}


def test_permission_rules_compact_each_rule_class_independently(tmp_path):
    paths = codemate_paths(tmp_path, home_root=tmp_path / "home")
    rules = build_permission_rules(
        paths,
        user_settings={
            "permissions": {
                "read": {"allow": ["/tmp"], "deny": ["/tmp/secret"]},
                "write": {"allow": ["/var"], "deny": ["/var/blocked"]},
            }
        },
        project_settings={},
    )

    assert "/tmp" in {str(path) for path in rules.read_allow}
    assert "/tmp/secret" in {str(path) for path in rules.read_deny}
    assert "/var" in {str(path) for path in rules.write_allow}
    assert "/var/blocked" in {str(path) for path in rules.write_deny}


def test_permission_rules_include_temporary_settings(tmp_path):
    paths = codemate_paths(tmp_path, home_root=tmp_path / "home")
    temporary = {
        "permissions": {
            "read": {"allow": ["/tmp/session-read"], "deny": []},
            "write": {"allow": ["/tmp/session-write"], "deny": []},
        }
    }

    rules = build_permission_rules(paths, user_settings={}, project_settings={}, temporary_settings=temporary)

    assert "/tmp/session-read" in {str(path) for path in rules.read_allow}
    assert "/tmp/session-write" in {str(path) for path in rules.write_allow}
    assert "/tmp/session-write" in {str(path) for path in rules.read_allow}


def test_sandbox_enabled_must_be_boolean(tmp_path):
    paths = codemate_paths(tmp_path, home_root=tmp_path / "home")
    paths.user_settings.parent.mkdir(parents=True, exist_ok=True)
    paths.project_settings.parent.mkdir(parents=True, exist_ok=True)
    paths.user_settings.write_text('{"sandbox": {"enabled": true}}\n', encoding="utf-8")
    paths.project_settings.write_text('{"sandbox": {"enabled": "false"}}\n', encoding="utf-8")

    try:
        load_codemate_settings(paths)
    except ValueError as exc:
        assert "sandbox.enabled must be a boolean" in str(exc)
    else:
        raise AssertionError("invalid sandbox.enabled was accepted")
