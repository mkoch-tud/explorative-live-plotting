"""Safe parsing of user-supplied Polars filter expressions."""

from __future__ import annotations

import ast
import inspect
import operator
from types import ModuleType
from typing import Any

import polars as pl

from .errors import ConfigurationError

_BINARY_OPERATORS = {
    ast.BitAnd: operator.and_,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS = {
    ast.Invert: operator.invert,
    ast.Not: operator.not_,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}
_COMPARISON_OPERATORS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda left, right: left in right,
    ast.NotIn: lambda left, right: left not in right,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}


def expression_names(expression: str) -> set[str]:
    """Return public root names referenced by an expression, excluding Polars."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise ConfigurationError(f"invalid Polars expression: {error.msg}") from error
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id != "pl" and not node.id.startswith("_")
    }


class _ExpressionEvaluator:
    """Evaluate expression syntax without exposing Python builtins or arbitrary calls."""

    def __init__(self, module: ModuleType | None, module_name: str | None) -> None:
        self.names: dict[str, Any] = {"pl": pl}
        if module is not None:
            self.names.update(
                (name, value)
                for name, value in vars(module).items()
                if not name.startswith("_")
            )
        self.module_name = module_name

    def evaluate(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return self.evaluate(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id.startswith("_") or node.id not in self.names:
                suffix = (
                    f" in config module {self.module_name}"
                    if self.module_name is not None
                    else "; configure a module to use its variables"
                )
                raise ConfigurationError(f"unknown expression name {node.id!r}{suffix}")
            return self.names[node.id]
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise ConfigurationError("private attributes are not allowed in expressions")
            owner = self.evaluate(node.value)
            try:
                return getattr(owner, node.attr)
            except AttributeError as error:
                raise ConfigurationError(
                    f"expression value has no attribute {node.attr!r}"
                ) from error
        if isinstance(node, ast.List):
            return [self.evaluate(item) for item in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(self.evaluate(item) for item in node.elts)
        if isinstance(node, ast.Set):
            return {self.evaluate(item) for item in node.elts}
        if isinstance(node, ast.Dict):
            return {
                self.evaluate(key): self.evaluate(value)
                for key, value in zip(node.keys, node.values, strict=True)
            }
        if isinstance(node, ast.Subscript):
            return self.evaluate(node.value)[self.evaluate(node.slice)]
        if isinstance(node, ast.Slice):
            return slice(
                self.evaluate(node.lower) if node.lower is not None else None,
                self.evaluate(node.upper) if node.upper is not None else None,
                self.evaluate(node.step) if node.step is not None else None,
            )
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
            return _BINARY_OPERATORS[type(node.op)](
                self.evaluate(node.left), self.evaluate(node.right)
            )
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
            return _UNARY_OPERATORS[type(node.op)](self.evaluate(node.operand))
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            values = [self.evaluate(value) for value in node.values]
            combiner = pl.all_horizontal if isinstance(node.op, ast.And) else pl.any_horizontal
            return combiner(values)
        if isinstance(node, ast.Compare):
            left = self.evaluate(node.left)
            results = []
            for operation, comparator in zip(node.ops, node.comparators, strict=True):
                right = self.evaluate(comparator)
                function = _COMPARISON_OPERATORS.get(type(operation))
                if function is None:
                    raise ConfigurationError("unsupported comparison in Polars expression")
                results.append(function(left, right))
                left = right
            return results[0] if len(results) == 1 else pl.all_horizontal(results)
        if isinstance(node, ast.IfExp):
            return pl.when(self.evaluate(node.test)).then(self.evaluate(node.body)).otherwise(
                self.evaluate(node.orelse)
            )
        if isinstance(node, ast.Call):
            function = self.evaluate(node.func)
            module_name = str(getattr(function, "__module__", ""))
            owner = function.__self__ if inspect.ismethod(function) else None
            owner_module = str(getattr(type(owner), "__module__", "")) if owner is not None else ""
            if not callable(function) or not (
                module_name.startswith("polars") or owner_module.startswith("polars")
            ):
                raise ConfigurationError("only Polars functions and methods may be called")
            if any(keyword.arg is None for keyword in node.keywords):
                raise ConfigurationError("expanded keyword arguments are not allowed")
            return function(
                *(self.evaluate(argument) for argument in node.args),
                **{keyword.arg: self.evaluate(keyword.value) for keyword in node.keywords},
            )
        raise ConfigurationError(
            f"unsupported syntax in Polars expression: {type(node).__name__}"
        )


def parse_filter_expression(
    expression: str,
    schema: pl.Schema,
    module: ModuleType | None = None,
    module_name: str | None = None,
) -> pl.Expr:
    """Resolve a configured or inline expression and verify it is a boolean filter."""
    text = str(expression or "").strip()
    if not text:
        raise ConfigurationError("Polars filter expression cannot be empty")
    try:
        tree = ast.parse(text, mode="eval")
        result = _ExpressionEvaluator(module, module_name).evaluate(tree)
    except ConfigurationError:
        raise
    except SyntaxError as error:
        raise ConfigurationError(f"invalid Polars expression: {error.msg}") from error
    except Exception as error:
        raise ConfigurationError(f"cannot evaluate Polars expression: {error}") from error
    if not isinstance(result, pl.Expr):
        raise ConfigurationError("Polars filter expression must produce a pl.Expr")
    try:
        result_schema = pl.LazyFrame(schema=schema).select(result.alias("_filter")).collect_schema()
    except Exception as error:
        raise ConfigurationError(f"invalid Polars filter expression: {error}") from error
    if result_schema["_filter"] != pl.Boolean:
        raise ConfigurationError(
            f"Polars filter expression must produce Boolean, got {result_schema['_filter']}"
        )
    return result
