"""Parse ``use_github`` GraphQL documents into the facts the write guardrail checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from graphql import GraphQLError, parse
from graphql.language import (
    DirectiveNode,
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    ObjectValueNode,
    OperationDefinitionNode,
    OperationType,
    SelectionSetNode,
    StringValueNode,
    Visitor,
    visit,
)
from graphql.pyutils import Undefined
from graphql.utilities import value_from_ast_untyped

# Bounds parser work on model-written documents; real use_github documents stay far below it.
_MAX_TOKENS = 50_000


class GraphQLDocumentError(ValueError):
    """The document does not parse as GraphQL, or repeats a name the spec requires to be unique."""


@dataclass(frozen=True)
class MutationField:
    """One root field of a mutation operation, with its arguments as plain Python values."""

    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class GraphQLDocument:
    """What the guardrail needs from a parsed document.

    Attributes:
        has_mutation: Any operation in the document is a ``mutation``.
        mutation_fields: Root fields of every mutation operation, including fields reached through
            root-level fragment spreads and inline fragments.
        string_literals: Every inline string value (arguments, list and object members, variable
            defaults, directive arguments), in document order.
        argument_values: Every field and directive argument set in the document, as
            ``{name: value}`` with variables and variable defaults substituted.
    """

    has_mutation: bool
    mutation_fields: tuple[MutationField, ...]
    string_literals: tuple[str, ...]
    argument_values: tuple[dict[str, Any], ...]


class _Collector(Visitor):
    def __init__(self, variables: dict[str, Any]) -> None:
        super().__init__()
        self.variables = variables
        self.strings: list[str] = []
        self.arguments: list[dict[str, Any]] = []

    def enter_string_value(self, node: StringValueNode, *_args: Any) -> None:
        self.strings.append(node.value)

    def enter_field(self, node: FieldNode, *_args: Any) -> None:
        if node.arguments:
            _require_unique(a.name.value for a in node.arguments)
            self.arguments.append(_arguments(node.arguments, self.variables))

    def enter_directive(self, node: DirectiveNode, *_args: Any) -> None:
        if node.arguments:
            _require_unique(a.name.value for a in node.arguments)
            self.arguments.append(_arguments(node.arguments, self.variables))

    def enter_object_value(self, node: ObjectValueNode, *_args: Any) -> None:
        _require_unique(f.name.value for f in node.fields)

    def enter_operation_definition(self, node: OperationDefinitionNode, *_args: Any) -> None:
        _require_unique(v.variable.name.value for v in node.variable_definitions or ())


def _require_unique(names: Any) -> None:
    """Refuse a repeated argument, input field or variable name.

    Converting to a dict keeps only one of the repeats, so the value checked could differ from the
    one GitHub uses. The GraphQL spec makes repeats invalid, so refusing them blocks nothing real.
    """
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise GraphQLDocumentError(f"'{name}' is given more than once.")
        seen.add(name)


def _arguments(nodes: Any, variables: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for arg in nodes:
        value = value_from_ast_untyped(arg.value, variables)
        out[arg.name.value] = None if value is Undefined else value
    return out


def _effective_variables(document: DocumentNode, variables: dict[str, Any]) -> dict[str, Any]:
    """Variable defaults from every operation, overridden by the supplied values."""
    merged: dict[str, Any] = {}
    for definition in document.definitions:
        if isinstance(definition, OperationDefinitionNode):
            for var in definition.variable_definitions or ():
                if var.default_value is not None:
                    merged[var.variable.name.value] = value_from_ast_untyped(var.default_value)
    merged.update(variables)
    return merged


def _root_fields(
    selection_set: SelectionSetNode,
    fragments: dict[str, FragmentDefinitionNode],
    variables: dict[str, Any],
    seen: set[str],
) -> list[MutationField]:
    """Root fields of an operation, expanding fragments so a spread cannot hide a mutation."""
    fields: list[MutationField] = []
    for selection in selection_set.selections:
        if isinstance(selection, FieldNode):
            fields.append(
                MutationField(selection.name.value, _arguments(selection.arguments, variables))
            )
        elif isinstance(selection, InlineFragmentNode):
            fields.extend(_root_fields(selection.selection_set, fragments, variables, seen))
        elif isinstance(selection, FragmentSpreadNode):
            name = selection.name.value
            fragment = fragments.get(name)
            if fragment is None:
                raise GraphQLDocumentError(f"Unknown fragment '{name}'.")
            if name in seen:
                continue
            seen.add(name)
            fields.extend(_root_fields(fragment.selection_set, fragments, variables, seen))
    return fields


def parse_document(query: str, variables: dict[str, Any] | None = None) -> GraphQLDocument:
    """Parse ``query`` and collect its operations, root mutation fields, and literal values.

    Raises:
        GraphQLDocumentError: The document does not parse, repeats an argument, input field or
            variable name, or a root spread names no fragment.
    """
    try:
        document = parse(query, no_location=True, max_tokens=_MAX_TOKENS)
    except GraphQLError as e:
        raise GraphQLDocumentError(e.message) from e
    values = _effective_variables(document, dict(variables or {}))
    fragments = {
        d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)
    }
    has_mutation = False
    mutation_fields: list[MutationField] = []
    for definition in document.definitions:
        if isinstance(definition, OperationDefinitionNode) and (
            definition.operation == OperationType.MUTATION
        ):
            has_mutation = True
            mutation_fields.extend(
                _root_fields(definition.selection_set, fragments, values, set())
            )
    collector = _Collector(values)
    visit(document, collector)
    return GraphQLDocument(
        has_mutation=has_mutation,
        mutation_fields=tuple(mutation_fields),
        string_literals=tuple(collector.strings),
        argument_values=tuple(collector.arguments),
    )
