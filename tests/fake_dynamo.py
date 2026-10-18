"""
A small in-memory stand-in for a DynamoDB table, for tests of code whose correctness is in
its conditional writes (claiming a delivery step, creating a row once, refusing an older
summary). It evaluates the subset of condition and update expressions SHIP actually uses, so
a test exercises the real expression strings rather than a hand-written imitation of them.

Not a DynamoDB emulator: anything outside that subset raises, so a new expression form fails
loudly here instead of silently passing.
"""

import copy
import re


class ConditionalCheckFailedException(Exception):
    """Named exactly like botocore's runtime class: the stores recognize DynamoDB's refusal by
    this class name."""


def _split_top(expr: str, word: str) -> list[str]:
    """Splits on ' AND ' / ' OR ' outside parentheses."""
    parts, depth, current, i = [], 0, "", 0
    token = f" {word} "
    while i < len(expr):
        if expr[i] == "(":
            depth += 1
        elif expr[i] == ")":
            depth -= 1
        if depth == 0 and expr.startswith(token, i):
            parts.append(current)
            current, i = "", i + len(token)
            continue
        current += expr[i]
        i += 1
    parts.append(current)
    return parts


def _strip(expr: str) -> str:
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        for i, ch in enumerate(expr):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(expr) - 1:
                return expr
        expr = expr[1:-1].strip()
    return expr


def evaluate(expr: str, item: dict | None, names: dict, values: dict) -> bool:
    item = item or {}
    expr = _strip(expr)
    ors = _split_top(expr, "OR")
    if len(ors) > 1:
        return any(evaluate(p, item, names, values) for p in ors)
    ands = _split_top(expr, "AND")
    if len(ands) > 1:
        return all(evaluate(p, item, names, values) for p in ands)

    def attr(token: str) -> str:
        return names.get(token, token)

    m = re.fullmatch(r"attribute_not_exists\((\S+)\)", expr)
    if m:
        return attr(m.group(1)) not in item
    m = re.fullmatch(r"attribute_exists\((\S+)\)", expr)
    if m:
        return attr(m.group(1)) in item
    m = re.fullmatch(r"(\S+) (=|<=|<|>=|>) (:\w+)", expr)
    if m:
        left, op, right = item.get(attr(m.group(1))), m.group(2), values[m.group(3)]
        if op == "=":
            return left == right
        if left is None:
            return False
        return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[op]
    raise NotImplementedError(f"fake table cannot evaluate: {expr!r}")


class FakeTable:
    def __init__(self, key: str, rows: dict | None = None):
        self.key = key
        self.rows: dict = rows if rows is not None else {}
        self.calls: list[tuple] = []

    # reads
    def get_item(self, Key, **kw):
        self.calls.append(("get_item", kw))
        row = self.rows.get(Key[self.key])
        return {"Item": copy.deepcopy(row)} if row is not None else {}

    def scan(self, **kw):
        self.calls.append(("scan", kw))
        flt = kw.get("FilterExpression")
        names, values = kw.get("ExpressionAttributeNames") or {}, kw.get("ExpressionAttributeValues") or {}
        return {"Items": [copy.deepcopy(r) for r in self.rows.values()
                          if not flt or evaluate(flt, r, names, values)]}

    # writes
    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeNames=None,
                 ExpressionAttributeValues=None, ReturnValues=None, **kw):
        existing = self.rows.get(Item[self.key])
        if ConditionExpression and not evaluate(ConditionExpression, existing, ExpressionAttributeNames or {},
                                                ExpressionAttributeValues or {}):
            raise ConditionalCheckFailedException()
        self.rows[Item[self.key]] = copy.deepcopy(Item)
        return {"Attributes": copy.deepcopy(existing)} if (ReturnValues == "ALL_OLD" and existing) else {}

    def update_item(self, Key, UpdateExpression, ConditionExpression=None, ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, ReturnValues=None, **kw):
        names, values = ExpressionAttributeNames or {}, ExpressionAttributeValues or {}
        existing = self.rows.get(Key[self.key])
        if ConditionExpression and not evaluate(ConditionExpression, existing, names, values):
            raise ConditionalCheckFailedException()
        row = existing if existing is not None else dict(Key)
        for clause, body in re.findall(r"\b(SET|REMOVE|ADD)\b\s+(.*?)(?=\s+\b(?:SET|REMOVE|ADD)\b\s|$)", UpdateExpression):
            for piece in [p.strip() for p in re.split(r",(?![^()]*\))", body)]:
                if clause == "SET":
                    target, source = [x.strip() for x in piece.split("=", 1)]
                    target = names.get(target, target)
                    m = re.fullmatch(r"if_not_exists\((\w+),\s*(:\w+)\)", source)
                    if m:
                        row.setdefault(target, values[m.group(2)])
                    else:
                        row[target] = values[source]
                elif clause == "REMOVE":
                    row.pop(names.get(piece, piece), None)
                else:
                    target, source = piece.split()
                    row[target] = set(row.get(target, set())) | set(values[source])
        self.rows[Key[self.key]] = row
        return {"Attributes": copy.deepcopy(row)} if ReturnValues == "ALL_NEW" else {}

    def delete_item(self, Key, **kw):
        self.rows.pop(Key[self.key], None)
