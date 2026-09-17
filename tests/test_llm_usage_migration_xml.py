"""Tests verifying Liquibase 0007-llm-usage-controls changeset structure and XML validity."""

import xml.etree.ElementTree as ET
from pathlib import Path


def test_liquibase_0007_changeset_parses_and_is_included() -> None:
    liquibase_dir = Path(__file__).resolve().parents[1] / "liquibase" / "changelog" / "changes" / "users"
    changeset_file = liquibase_dir / "0007-llm-usage-controls.xml"
    changelog_users_file = liquibase_dir / "changelog-users.xml"

    assert changeset_file.exists(), f"{changeset_file} must exist"
    assert changelog_users_file.exists(), f"{changelog_users_file} must exist"

    # Verify changelog-users.xml includes 0007
    changelog_tree = ET.parse(changelog_users_file)
    changelog_root = changelog_tree.getroot()
    includes = [
        elem.attrib.get("file")
        for elem in changelog_root.iter()
        if elem.tag.endswith("include")
    ]
    assert "0007-llm-usage-controls.xml" in includes, (
        "changelog-users.xml must include 0007-llm-usage-controls.xml"
    )

    # Verify 0007-llm-usage-controls.xml parses and has required elements
    tree = ET.parse(changeset_file)
    root = tree.getroot()

    changesets = [
        elem for elem in root.iter() if elem.tag.endswith("changeSet")
    ]
    assert len(changesets) == 1
    cs = changesets[0]
    assert cs.attrib.get("id") == "users-0007-llm-usage-controls"

    # Verify SQL creates the 4 tables and rollback drops them
    sql_text = "".join(cs.itertext())
    assert "users.llm_usage_event" in sql_text
    assert "users.llm_usage_reservation" in sql_text
    assert "users.llm_usage_counter" in sql_text
    assert "users.llm_entitlement" in sql_text
    assert "DROP TABLE IF EXISTS users.llm_usage_event" in sql_text
