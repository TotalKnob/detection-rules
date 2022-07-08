# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License
# 2.0; you may not use this file except in compliance with the Elastic License
# 2.0.
"""Rule object."""
import copy
import dataclasses
import json
import re
import requests
import typing
import yaml
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Literal, Union, Optional, List, Any, Dict, Tuple
from uuid import uuid4

import eql
from marshmallow import ValidationError, validates_schema

import kql
from kql.ast import FieldComparison
from . import beats
from . import ecs
from . import utils
from .misc import load_current_package_version
from .mixins import MarshmallowDataclassMixin, StackCompatMixin
from .rule_formatter import toml_write, nested_normalize
from .schemas import SCHEMA_DIR, definitions, downgrade, get_stack_schemas, get_min_supported_stack_version
from .schemas.stack_compat import get_restricted_fields
from .semver import Version
from .utils import cached
from .integrations import IntegrationPackages

BUILD_FIELD_VERSIONS = {"required_fields": (Version('8.3'), None),
                        "related_integrations": (Version('8.3'), None)}
_META_SCHEMA_REQ_DEFAULTS = {}
MIN_FLEET_PACKAGE_VERSION = '7.13.0'


@dataclass(frozen=True)
class RuleMeta(MarshmallowDataclassMixin):
    """Data stored in a rule's [metadata] section of TOML."""
    creation_date: definitions.Date
    updated_date: definitions.Date
    deprecation_date: Optional[definitions.Date]

    # Optional fields
    comments: Optional[str]
    integration: Optional[str]
    maturity: Optional[definitions.Maturity]
    min_stack_version: Optional[definitions.SemVer]
    min_stack_comments: Optional[str]
    os_type_list: Optional[List[definitions.OSType]]
    query_schema_validation: Optional[bool]
    related_endpoint_rules: Optional[List[str]]

    # Extended information as an arbitrary dictionary
    extended: Optional[Dict[str, Any]]

    def get_validation_stack_versions(self) -> Dict[str, dict]:
        """Get a dict of beats and ecs versions per stack release."""
        stack_versions = get_stack_schemas(self.min_stack_version)
        return stack_versions


@dataclass(frozen=True)
class BaseThreatEntry:
    id: str
    name: str
    reference: str


@dataclass(frozen=True)
class SubTechnique(BaseThreatEntry):
    """Mapping to threat subtechnique."""
    reference: definitions.SubTechniqueURL


@dataclass(frozen=True)
class Technique(BaseThreatEntry):
    """Mapping to threat subtechnique."""
    # subtechniques are stored at threat[].technique.subtechnique[]
    reference: definitions.TechniqueURL
    subtechnique: Optional[List[SubTechnique]]


@dataclass(frozen=True)
class Tactic(BaseThreatEntry):
    """Mapping to a threat tactic."""
    reference: definitions.TacticURL


@dataclass(frozen=True)
class ThreatMapping(MarshmallowDataclassMixin):
    """Mapping to a threat framework."""
    framework: Literal["MITRE ATT&CK"]
    tactic: Tactic
    technique: Optional[List[Technique]]

    @staticmethod
    def flatten(threat_mappings: Optional[List]) -> 'FlatThreatMapping':
        """Get flat lists of tactic and technique info."""
        tactic_names = []
        tactic_ids = []
        technique_ids = set()
        technique_names = set()
        sub_technique_ids = set()
        sub_technique_names = set()

        for entry in (threat_mappings or []):
            tactic_names.append(entry.tactic.name)
            tactic_ids.append(entry.tactic.id)

            for technique in (entry.technique or []):
                technique_names.add(technique.name)
                technique_ids.add(technique.id)

                for subtechnique in (technique.subtechnique or []):
                    sub_technique_ids.add(subtechnique.id)
                    sub_technique_names.add(subtechnique.name)

        return FlatThreatMapping(
            tactic_names=sorted(tactic_names),
            tactic_ids=sorted(tactic_ids),
            technique_names=sorted(technique_names),
            technique_ids=sorted(technique_ids),
            sub_technique_names=sorted(sub_technique_names),
            sub_technique_ids=sorted(sub_technique_ids)
        )


@dataclass(frozen=True)
class RiskScoreMapping(MarshmallowDataclassMixin):
    field: str
    operator: Optional[definitions.Operator]
    value: Optional[str]


@dataclass(frozen=True)
class SeverityMapping(MarshmallowDataclassMixin):
    field: str
    operator: Optional[definitions.Operator]
    value: Optional[str]
    severity: Optional[str]


@dataclass(frozen=True)
class FlatThreatMapping(MarshmallowDataclassMixin):
    tactic_names: List[str]
    tactic_ids: List[str]
    technique_names: List[str]
    technique_ids: List[str]
    sub_technique_names: List[str]
    sub_technique_ids: List[str]


@dataclass(frozen=True)
class BaseRuleData(MarshmallowDataclassMixin, StackCompatMixin):
    @dataclass
    class RequiredFields:
        name: definitions.NonEmptyStr
        type: definitions.NonEmptyStr
        ecs: bool

    @dataclass
    class RelatedIntegrations:
        package: definitions.NonEmptyStr
        version: definitions.NonEmptyStr
        integration: Optional[definitions.NonEmptyStr]

    actions: Optional[list]
    author: List[str]
    building_block_type: Optional[str]
    description: str
    enabled: Optional[bool]
    exceptions_list: Optional[list]
    license: Optional[str]
    false_positives: Optional[List[str]]
    filters: Optional[List[dict]]
    # trailing `_` required since `from` is a reserved word in python
    from_: Optional[str] = field(metadata=dict(data_key="from"))

    interval: Optional[definitions.Interval]
    max_signals: Optional[definitions.MaxSignals]
    meta: Optional[Dict[str, Any]]
    name: definitions.RuleName
    note: Optional[definitions.Markdown]
    # can we remove this comment?
    # explicitly NOT allowed!
    # output_index: Optional[str]
    references: Optional[List[str]]
    related_integrations: Optional[List[RelatedIntegrations]] = field(metadata=dict(metadata=dict(min_compat="8.3")))
    required_fields: Optional[List[RequiredFields]] = field(metadata=dict(metadata=dict(min_compat="8.3")))
    risk_score: definitions.RiskScore
    risk_score_mapping: Optional[List[RiskScoreMapping]]
    rule_id: definitions.UUIDString
    rule_name_override: Optional[str]
    setup: Optional[str] = field(metadata=dict(metadata=dict(min_compat="8.3")))
    severity_mapping: Optional[List[SeverityMapping]]
    severity: definitions.Severity
    tags: Optional[List[str]]
    throttle: Optional[str]
    timeline_id: Optional[definitions.TimelineTemplateId]
    timeline_title: Optional[definitions.TimelineTemplateTitle]
    timestamp_override: Optional[str]
    to: Optional[str]
    type: definitions.RuleType
    threat: Optional[List[ThreatMapping]]

    @classmethod
    def save_schema(cls):
        """Save the schema as a jsonschema."""
        fields: Tuple[dataclasses.Field, ...] = dataclasses.fields(cls)
        type_field = next(f for f in fields if f.name == "type")
        rule_type = typing.get_args(type_field.type)[0] if cls != BaseRuleData else "base"
        schema = cls.jsonschema()
        version_dir = SCHEMA_DIR / "master"
        version_dir.mkdir(exist_ok=True, parents=True)

        # expand out the jsonschema definitions
        with (version_dir / f"master.{rule_type}.json").open("w") as f:
            json.dump(schema, f, indent=2, sort_keys=True)

    def validate_query(self, meta: RuleMeta) -> None:
        pass

    @cached_property
    def get_restricted_fields(self) -> Optional[Dict[str, tuple]]:
        """Get stack version restricted fields."""
        fields: List[dataclasses.Field, ...] = list(dataclasses.fields(self))
        return get_restricted_fields(fields)


@dataclass
class QueryValidator:
    query: str

    @property
    def ast(self) -> Any:
        raise NotImplementedError

    @property
    def unique_fields(self) -> Any:
        raise NotImplementedError

    def validate(self, data: 'QueryRuleData', meta: RuleMeta) -> None:
        raise NotImplementedError()

    @cached
    def get_required_fields(self, index: str) -> List[dict]:
        """Retrieves fields needed for the query along with type information from the schema."""
        current_version = Version(Version(load_current_package_version()) + (0,))
        ecs_version = get_stack_schemas()[str(current_version)]['ecs']
        beats_version = get_stack_schemas()[str(current_version)]['beats']
        ecs_schema = ecs.get_schema(ecs_version)

        beat_types, beat_schema, schema = self.get_beats_schema(index or [], beats_version, ecs_version)

        required = []
        unique_fields = self.unique_fields or []

        for fld in unique_fields:
            field_type = ecs_schema.get(fld, {}).get('type')
            is_ecs = field_type is not None

            if beat_schema and not is_ecs:
                field_type = beat_schema.get(fld, {}).get('type')

            required.append(dict(name=fld, type=field_type or 'unknown', ecs=is_ecs))

        return sorted(required, key=lambda f: f['name'])

    @cached
    def get_beats_schema(self, index: list, beats_version: str, ecs_version: str) -> (list, dict, dict):
        """Get an assembled beats schema."""
        beat_types = beats.parse_beats_from_index(index)
        beat_schema = beats.get_schema_from_kql(self.ast, beat_types, version=beats_version) if beat_types else None
        schema = ecs.get_kql_schema(version=ecs_version, indexes=index, beat_schema=beat_schema)
        return beat_types, beat_schema, schema


@dataclass(frozen=True)
class QueryRuleData(BaseRuleData):
    """Specific fields for query event types."""
    type: Literal["query"]

    index: Optional[List[str]]
    query: str
    language: definitions.FilterLanguages

    @cached_property
    def validator(self) -> Optional[QueryValidator]:
        if self.language == "kuery":
            return KQLValidator(self.query)
        elif self.language == "eql":
            return EQLValidator(self.query)

    def validate_query(self, meta: RuleMeta) -> None:
        validator = self.validator
        if validator is not None:
            return validator.validate(self, meta)

    @cached_property
    def ast(self):
        validator = self.validator
        if validator is not None:
            return validator.ast

    @cached_property
    def unique_fields(self):
        validator = self.validator
        if validator is not None:
            return validator.unique_fields

    @cached
    def get_required_fields(self, index: str) -> List[dict]:
        validator = self.validator
        if validator is not None:
            return validator.get_required_fields(index or [])


@dataclass(frozen=True)
class MachineLearningRuleData(BaseRuleData):
    type: Literal["machine_learning"]

    anomaly_threshold: int
    machine_learning_job_id: Union[str, List[str]]


@dataclass(frozen=True)
class ThresholdQueryRuleData(QueryRuleData):
    """Specific fields for query event types."""

    @dataclass(frozen=True)
    class ThresholdMapping(MarshmallowDataclassMixin):
        @dataclass(frozen=True)
        class ThresholdCardinality:
            field: str
            value: definitions.ThresholdValue

        field: definitions.CardinalityFields
        value: definitions.ThresholdValue
        cardinality: Optional[List[ThresholdCardinality]]

    type: Literal["threshold"]
    threshold: ThresholdMapping


@dataclass(frozen=True)
class EQLRuleData(QueryRuleData):
    """EQL rules are a special case of query rules."""
    type: Literal["eql"]
    language: Literal["eql"]

    @staticmethod
    def convert_time_span(span: str) -> int:
        """Convert time span in datemath to value in milliseconds."""
        amount = int("".join(char for char in span if char.isdigit()))
        unit = eql.ast.TimeUnit("".join(char for char in span if char.isalpha()))
        return eql.ast.TimeRange(amount, unit).as_milliseconds()

    def convert_relative_delta(self, lookback: str) -> int:
        now = len("now")
        min_length = now + len('+5m')

        if lookback.startswith("now") and len(lookback) >= min_length:
            lookback = lookback[len("now"):]
            sign = lookback[0]  # + or -
            span = lookback[1:]
            amount = self.convert_time_span(span)
            return amount * (-1 if sign == "-" else 1)
        else:
            return self.convert_time_span(lookback)

    @cached_property
    def is_sequence(self) -> bool:
        """Checks if the current rule is a sequence-based rule."""
        return eql.utils.get_query_type(self.ast) == 'sequence'

    @cached_property
    def max_span(self) -> Optional[int]:
        """Maxspan value for sequence rules if defined."""
        if self.is_sequence and hasattr(self.ast.first, 'max_span'):
            return self.ast.first.max_span.as_milliseconds() if self.ast.first.max_span else None

    @cached_property
    def look_back(self) -> Optional[Union[int, Literal['unknown']]]:
        """Lookback value of a rule."""
        # https://www.elastic.co/guide/en/elasticsearch/reference/current/common-options.html#date-math
        to = self.convert_relative_delta(self.to) if self.to else 0
        from_ = self.convert_relative_delta(self.from_ or "now-6m")

        if not (to or from_):
            return 'unknown'
        else:
            return to - from_

    @cached_property
    def interval_ratio(self) -> Optional[float]:
        """Ratio of interval time window / max_span time window."""
        if self.max_span:
            interval = self.convert_time_span(self.interval or '5m')
            return interval / self.max_span


@dataclass(frozen=True)
class ThreatMatchRuleData(QueryRuleData):
    """Specific fields for indicator (threat) match rule."""

    @dataclass(frozen=True)
    class Entries:

        @dataclass(frozen=True)
        class ThreatMapEntry:
            field: definitions.NonEmptyStr
            type: Literal["mapping"]
            value: definitions.NonEmptyStr

        entries: List[ThreatMapEntry]

    type: Literal["threat_match"]

    concurrent_searches: Optional[definitions.PositiveInteger]
    items_per_search: Optional[definitions.PositiveInteger]

    threat_mapping: List[Entries]
    threat_filters: Optional[List[dict]]
    threat_query: Optional[str]
    threat_language: Optional[definitions.FilterLanguages]
    threat_index: List[str]
    threat_indicator_path: Optional[str]

    def validate_query(self, meta: RuleMeta) -> None:
        super(ThreatMatchRuleData, self).validate_query(meta)

        if self.threat_query:
            if not self.threat_language:
                raise ValidationError('`threat_language` required when a `threat_query` is defined')

            if self.threat_language == "kuery":
                threat_query_validator = KQLValidator(self.threat_query)
            elif self.threat_language == "eql":
                threat_query_validator = EQLValidator(self.threat_query)
            else:
                return

            threat_query_validator.validate(self, meta)


# All of the possible rule types
# Sort inverse of any inheritance - see comment in TOMLRuleContents.to_dict
AnyRuleData = Union[EQLRuleData, ThresholdQueryRuleData, ThreatMatchRuleData, MachineLearningRuleData, QueryRuleData]


class BaseRuleContents(ABC):
    """Base contents object for shared methods between active and deprecated rules."""

    @property
    @abstractmethod
    def id(self):
        pass

    @property
    @abstractmethod
    def name(self):
        pass

    @property
    @abstractmethod
    def version_lock(self):
        pass

    @property
    @abstractmethod
    def type(self):
        pass

    def lock_info(self, bump=True) -> dict:
        version = self.autobumped_version if bump else (self.latest_version or 1)
        contents = {"rule_name": self.name, "sha256": self.sha256(), "version": version, "type": self.type}

        return contents

    @property
    def is_dirty(self) -> Optional[bool]:
        """Determine if the rule has changed since its version was locked."""
        min_stack = self.metadata.get('min_stack_version') or str(get_min_supported_stack_version(drop_patch=True))
        existing_sha256 = self.version_lock.get_locked_hash(self.id, min_stack)

        if existing_sha256 is not None:
            return existing_sha256 != self.sha256()

    @property
    def latest_version(self) -> Optional[int]:
        """Retrieve the latest known version of the rule."""
        min_stack = self.metadata.get('min_stack_version') or str(get_min_supported_stack_version(drop_patch=True))
        return self.version_lock.get_locked_version(self.id, min_stack)

    @property
    def autobumped_version(self) -> Optional[int]:
        """Retrieve the current version of the rule, accounting for automatic increments."""
        version = self.latest_version
        if version is None:
            return 1

        return version + 1 if self.is_dirty else version

    def _post_dict_transform(self, obj: dict) -> dict:
        """Transform the converted API in place before sending to Kibana."""

        # cleanup the whitespace in the rule
        obj = nested_normalize(obj)

        # fill in threat.technique so it's never missing
        for threat_entry in obj.get("threat", []):
            threat_entry.setdefault("technique", [])

        return obj

    @abstractmethod
    def to_api_format(self, include_version=True) -> dict:
        """Convert the rule to the API format."""

    @cached
    def sha256(self, include_version=False) -> str:
        # get the hash of the API dict without the version by default, otherwise it'll always be dirty.
        hashable_contents = self.to_api_format(include_version=include_version)
        return utils.dict_hash(hashable_contents)


@dataclass(frozen=True)
class TOMLRuleContents(BaseRuleContents, MarshmallowDataclassMixin):
    """Rule object which maps directly to the TOML layout."""
    metadata: RuleMeta
    data: AnyRuleData = field(metadata=dict(data_key="rule"))

    @cached_property
    def version_lock(self):
        # VersionLock
        from .version_lock import default_version_lock

        return getattr(self, '_version_lock', None) or default_version_lock

    def set_version_lock(self, value):
        from .version_lock import VersionLock

        if value and not isinstance(value, VersionLock):
            raise TypeError(f'version lock property must be set with VersionLock objects only. Got {type(value)}')

        # circumvent frozen class
        self.__dict__['_version_lock'] = value

    @classmethod
    def all_rule_types(cls) -> set:
        types = set()
        for subclass in typing.get_args(AnyRuleData):
            field = next(field for field in dataclasses.fields(subclass) if field.name == "type")
            types.update(typing.get_args(field.type))

        return types

    @classmethod
    def get_data_subclass(cls, rule_type: str) -> typing.Type[BaseRuleData]:
        """Get the proper subclass depending on the rule type"""
        for subclass in typing.get_args(AnyRuleData):
            field = next(field for field in dataclasses.fields(subclass) if field.name == "type")
            if (rule_type, ) == typing.get_args(field.type):
                return subclass

        raise ValueError(f"Unknown rule type {rule_type}")

    @property
    def id(self) -> definitions.UUIDString:
        return self.data.rule_id

    @property
    def name(self) -> str:
        return self.data.name

    @property
    def type(self) -> str:
        return self.data.type

    def _post_dict_transform(self, obj: dict) -> dict:
        """Transform the converted API in place before sending to Kibana."""
        super()._post_dict_transform(obj)

        self.add_related_integrations(obj)
        self.add_required_fields(obj)
        self.add_setup(obj)

        # validate new fields against the schema
        rule_type = obj['type']
        subclass = self.get_data_subclass(rule_type)
        subclass.from_dict(obj)

        return obj

    def add_related_integrations(self, obj: dict) -> None:
        """Add restricted field add_related_integrations to the obj"""

        field_name = "related_integrations"
        field_value = obj.get(field_name, [])
        related_integrations = dict()
        if self.check_restricted_field_version(field_name):

            invalid = (MachineLearningRuleData, ThreatMatchRuleData, ThresholdQueryRuleData)
            if not isinstance(self.data, invalid) and self.data.get('ast'):
                package_integrations = self.get_packaged_integrations(self.data.ast)
                for package in package_integrations:
                    package["version"] = IntegrationPackages.find_least_compatible_version(
                        package=package["package"],
                        integration=package["integration"])
        obj.setdefault("related_integrations", package_integrations)

    def add_required_fields(self, obj: dict) -> None:
        """Add restricted field required_fields to the obj, derived from the query AST."""
        if isinstance(self.data, QueryRuleData) and self.data.language != 'lucene':
            index = obj.get('index') or []
            required_fields = self.data.get_required_fields(index)
        else:
            required_fields = []

        field_name = "required_fields"
        if self.check_restricted_field_version(field_name=field_name):
            obj.setdefault(field_name, required_fields)

    def add_setup(self, obj: dict) -> None:
        """Add restricted field setup to the obj."""
        # field_name = "setup"
        ...

    def check_explicit_restricted_field_version(self, field_name: str) -> bool:
        """Explicitly check restricted fields against global min and max versions."""
        min_stack, max_stack = BUILD_FIELD_VERSIONS[field_name]
        return self.compare_field_versions(min_stack, max_stack)

    def check_restricted_field_version(self, field_name: str) -> bool:
        """Check restricted fields against schema min and max versions."""
        min_stack, max_stack = self.data.get_restricted_fields.get(field_name)
        return self.compare_field_versions(min_stack, max_stack)

    def compare_field_versions(self, min_stack: Version, max_stack: Version) -> bool:
        """Check current rule version is witihin min and max stack versions."""
        current_version = Version(load_current_package_version())
        max_stack = max_stack or current_version
        return Version(min_stack) <= current_version >= Version(max_stack)

    def get_packaged_integrations(self, obj: dict) -> List[dict]:

        packaged_integrations = list()
        for item in obj.items:
            if isinstance(item, FieldComparison) and str(item.field) == 'event.dataset':
                event_dataset_values = str(item.value).split(' or ')
                for ints in event_dataset_values:
                    package, integration = ints.split(".")
                    packaged_integrations.append({"package": package, "integration": integration})
        return(packaged_integrations)

    @validates_schema
    def validate_query(self, value: dict, **kwargs):
        """Validate queries by calling into the validator for the relevant method."""
        data: AnyRuleData = value["data"]
        metadata: RuleMeta = value["metadata"]

        return data.validate_query(metadata)

    def to_dict(self, strip_none_values=True) -> dict:
        # Load schemas directly from the data and metadata classes to avoid schema ambiguity which can
        # result from union fields which contain classes and related subclasses (AnyRuleData). See issue #1141
        metadata = self.metadata.to_dict(strip_none_values=strip_none_values)
        data = self.data.to_dict(strip_none_values=strip_none_values)
        dict_obj = dict(metadata=metadata, rule=data)
        return nested_normalize(dict_obj)

    def flattened_dict(self) -> dict:
        flattened = dict()
        flattened.update(self.data.to_dict())
        flattened.update(self.metadata.to_dict())
        return flattened

    def to_api_format(self, include_version=True) -> dict:
        """Convert the TOML rule to the API format."""
        converted = self.data.to_dict()
        converted = self._post_dict_transform(converted)

        if include_version:
            converted["version"] = self.autobumped_version

        return converted

    def check_restricted_fields_compatibility(self) -> Dict[str, dict]:
        """Check for compatibility between restricted fields and the min_stack_version of the rule."""
        default_min_stack = get_min_supported_stack_version(drop_patch=True)
        if self.metadata.min_stack_version is not None:
            min_stack = Version(self.metadata.min_stack_version)
        else:
            min_stack = default_min_stack
        restricted = self.data.get_restricted_fields

        invalid = {}
        for _field, values in restricted.items():
            if self.data.get(_field) is not None:
                min_allowed, _ = values
                if min_stack < min_allowed:
                    invalid[_field] = {'min_stack_version': min_stack, 'min_allowed_version': min_allowed}

        return invalid


@dataclass
class TOMLRule:
    contents: TOMLRuleContents = field(hash=True)
    path: Optional[Path] = None
    gh_pr: Any = field(hash=False, compare=False, default=None, repr=False)

    @property
    def id(self):
        return self.contents.id

    @property
    def name(self):
        return self.contents.data.name

    def get_asset(self) -> dict:
        """Generate the relevant fleet compatible asset."""
        return {"id": self.id, "attributes": self.contents.to_api_format(), "type": definitions.SAVED_OBJECT_TYPE}

    def save_toml(self):
        assert self.path is not None, f"Can't save rule {self.name} (self.id) without a path"
        converted = self.contents.to_dict()
        toml_write(converted, str(self.path.absolute()))

    def save_json(self, path: Path, include_version: bool = True):
        path = path.with_suffix('.json')
        with open(str(path.absolute()), 'w', newline='\n') as f:
            json.dump(self.contents.to_api_format(include_version=include_version), f, sort_keys=True, indent=2)
            f.write('\n')


@dataclass(frozen=True)
class DeprecatedRuleContents(BaseRuleContents):
    metadata: dict
    data: dict

    @cached_property
    def version_lock(self):
        # VersionLock
        from .version_lock import default_version_lock

        return getattr(self, '_version_lock', None) or default_version_lock

    def set_version_lock(self, value):
        from .version_lock import VersionLock

        if value and not isinstance(value, VersionLock):
            raise TypeError(f'version lock property must be set with VersionLock objects only. Got {type(value)}')

        # circumvent frozen class
        self.__dict__['_version_lock'] = value

    @property
    def id(self) -> str:
        return self.data.get('rule_id')

    @property
    def name(self) -> str:
        return self.data.get('name')

    @property
    def type(self) -> str:
        return self.data.get('type')

    @classmethod
    def from_dict(cls, obj: dict):
        return cls(metadata=obj['metadata'], data=obj['rule'])

    def to_api_format(self, include_version=True) -> dict:
        """Convert the TOML rule to the API format."""
        converted = copy.deepcopy(self.data)
        if include_version:
            converted["version"] = self.autobumped_version

        converted = self._post_dict_transform(converted)
        return converted


class DeprecatedRule(dict):
    """Minimal dict object for deprecated rule."""

    def __init__(self, path: Path, contents: DeprecatedRuleContents, *args, **kwargs):
        super(DeprecatedRule, self).__init__(*args, **kwargs)
        self.path = path
        self.contents = contents

    def __repr__(self):
        return f'{type(self).__name__}(contents={self.contents}, path={self.path})'

    @property
    def id(self) -> str:
        return self.contents.id

    @property
    def name(self) -> str:
        return self.contents.name


def downgrade_contents_from_rule(rule: TOMLRule, target_version: str) -> dict:
    """Generate the downgraded contents from a rule."""
    payload = rule.contents.to_api_format()
    meta = payload.setdefault("meta", {})
    meta["original"] = dict(id=rule.id, **rule.contents.metadata.to_dict())
    payload["rule_id"] = str(uuid4())
    payload = downgrade(payload, target_version)
    return payload


def get_unique_query_fields(rule: TOMLRule) -> List[str]:
    """Get a list of unique fields used in a rule query from rule contents."""
    contents = rule.contents.to_api_format()
    language = contents.get('language')
    query = contents.get('query')
    if language in ('kuery', 'eql'):
        # TODO: remove once py-eql supports ipv6 for cidrmatch
        with eql.parser.elasticsearch_syntax, eql.parser.ignore_missing_functions:
            parsed = kql.parse(query) if language == 'kuery' else eql.parse_query(query)

        return sorted(set(str(f) for f in parsed if isinstance(f, (eql.ast.Field, kql.ast.Field))))


# avoid a circular import
from .rule_validators import KQLValidator, EQLValidator  # noqa: E402
