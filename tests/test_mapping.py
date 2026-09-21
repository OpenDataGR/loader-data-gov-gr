import pytest

from providerharvest_service.engine.mapping import (
    DATASTORE_TYPE_MAP,
    FieldMappingError,
    FieldMappingProfile,
    FieldMappingRule,
)


def test_extract_row_basic():
    profile = FieldMappingProfile(row_rules=[
        FieldMappingRule(ckan_field="id", source_path="$.attributes.id",
                          field_type="integer", is_primary_key=True),
        FieldMappingRule(ckan_field="name", source_path="$.attributes.name",
                          transform="strip"),
    ])
    record = {"attributes": {"id": 42, "name": "  Athens  "}}
    row = profile.extract_row(record)
    assert row == {"id": 42, "name": "Athens"}


def test_missing_optional_field_is_none():
    profile = FieldMappingProfile(row_rules=[
        FieldMappingRule(ckan_field="id", source_path="$.id", is_primary_key=True),
        FieldMappingRule(ckan_field="missing", source_path="$.nope"),
    ])
    assert profile.extract_row({"id": 1}) == {"id": 1, "missing": None}


def test_missing_required_field_raises():
    profile = FieldMappingProfile(row_rules=[
        FieldMappingRule(ckan_field="id", source_path="$.id", is_primary_key=True, required=True),
    ])
    with pytest.raises(FieldMappingError):
        profile.extract_row({})


def test_requires_at_least_one_primary_key():
    with pytest.raises(FieldMappingError):
        FieldMappingProfile(row_rules=[
            FieldMappingRule(ckan_field="name", source_path="$.name"),
        ])


def test_empty_row_rules_is_allowed_for_bulk_file_sources():
    # A bulk-file (e.g. SFTP) source has no per-record fields to map --
    # it streams whole files as resources -- so an empty row_rules list
    # must NOT trip the "needs a primary key" check above, unlike a
    # non-empty list with no primary key.
    profile = FieldMappingProfile(row_rules=[])
    assert profile.primary_key_fields == []


def test_unknown_field_type_raises():
    with pytest.raises(FieldMappingError):
        FieldMappingRule(ckan_field="x", source_path="$.x", field_type="bogus")


def test_unknown_transform_raises():
    with pytest.raises(FieldMappingError):
        FieldMappingRule(ckan_field="x", source_path="$.x", transform="bogus")


def test_datastore_fields_maps_declared_types():
    profile = FieldMappingProfile(row_rules=[
        FieldMappingRule(ckan_field="id", source_path="$.id", field_type="integer",
                          is_primary_key=True),
        FieldMappingRule(ckan_field="active", source_path="$.active", field_type="boolean"),
    ])
    assert profile.datastore_fields() == [
        {"id": "id", "type": DATASTORE_TYPE_MAP["integer"]},
        {"id": "active", "type": DATASTORE_TYPE_MAP["boolean"]},
    ]


def test_primary_key_fields_property():
    profile = FieldMappingProfile(row_rules=[
        FieldMappingRule(ckan_field="a", source_path="$.a", is_primary_key=True),
        FieldMappingRule(ckan_field="b", source_path="$.b", is_primary_key=True),
        FieldMappingRule(ckan_field="c", source_path="$.c"),
    ])
    assert profile.primary_key_fields == ["a", "b"]
