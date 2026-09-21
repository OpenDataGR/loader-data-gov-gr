"""Field mapping: provider record -> DataStore row.

Package/resource-level metadata (title, notes, license, theme, ...) is
static, provider-declared config filled in once at registration -- it is
NOT extracted from individual records. Only row-level values (the actual
data going into DataStore) are extracted per-record via JSONPath, because
that is the part that genuinely varies record to record.

Declared field types and a primary key are REQUIRED inputs (not inferred
from live data): ``datastore_create`` needs an explicit schema, and
``datastore_upsert`` needs a stable key. Inferring either at runtime
against a moving provider API is fragile and was explicitly rejected in
the design.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

from jsonpath_ng import parse as jsonpath_parse

#: Maps our declared field types onto CKAN DataStore/Postgres column types.
DATASTORE_TYPE_MAP = {
    "text": "text",
    "numeric": "numeric",
    "integer": "int",
    "boolean": "bool",
    "timestamp": "timestamp",
}

_TRANSFORMS = {
    "strip": lambda v: v.strip() if isinstance(v, str) else v,
    "lower": lambda v: v.lower() if isinstance(v, str) else v,
    None: lambda v: v,
}


class FieldMappingError(ValueError):
    pass


@dataclasses.dataclass
class FieldMappingRule:
    ckan_field: str  # the DataStore column name
    source_path: str  # JSONPath expression against one provider record
    field_type: str = "text"  # key into DATASTORE_TYPE_MAP
    transform: Optional[str] = None  # key into _TRANSFORMS
    is_primary_key: bool = False
    required: bool = False

    def __post_init__(self):
        if self.field_type not in DATASTORE_TYPE_MAP:
            raise FieldMappingError(
                "Unknown field_type %r for field %r (must be one of %s)"
                % (self.field_type, self.ckan_field, sorted(DATASTORE_TYPE_MAP))
            )
        if self.transform not in _TRANSFORMS:
            raise FieldMappingError(
                "Unknown transform %r for field %r" % (self.transform, self.ckan_field)
            )
        # Compiled once, reused across every record in a job.
        self._compiled_path = jsonpath_parse(self.source_path)

    def extract(self, record: dict):
        matches = self._compiled_path.find(record)
        if not matches:
            if self.required:
                raise FieldMappingError(
                    "Required field %r (path %r) not found in record"
                    % (self.ckan_field, self.source_path)
                )
            return None
        value = matches[0].value
        return _TRANSFORMS[self.transform](value)


@dataclasses.dataclass
class FieldMappingProfile:
    row_rules: list[FieldMappingRule]
    dataset_defaults: dict = dataclasses.field(default_factory=dict)
    resource_defaults: dict = dataclasses.field(default_factory=dict)

    def __post_init__(self):
        # Empty row_rules is legitimate for a bulk-file source (whole
        # files are streamed as resources, there's no per-record
        # extraction) -- only a *non-empty* rule set without a primary
        # key is the actual error this guards against.
        if self.row_rules and not self.primary_key_fields:
            raise FieldMappingError(
                "At least one row_rule must have is_primary_key=True "
                "(see the 'no stable primary key' fallback policy before "
                "relaxing this)."
            )

    @property
    def primary_key_fields(self) -> list[str]:
        return [r.ckan_field for r in self.row_rules if r.is_primary_key]

    def datastore_fields(self) -> list[dict]:
        return [
            {"id": rule.ckan_field, "type": DATASTORE_TYPE_MAP[rule.field_type]}
            for rule in self.row_rules
        ]

    def extract_row(self, record: dict) -> dict:
        return {rule.ckan_field: rule.extract(record) for rule in self.row_rules}
