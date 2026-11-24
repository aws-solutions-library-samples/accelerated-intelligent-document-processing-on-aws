Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Decision tables

`idp_common.decision_tables` evaluates decision tables: a subset of the decision
tables of [DMN 1.5](https://www.omg.org/spec/DMN/1.5/). It knows nothing about
documents or review queues. Review routing builds a table from a profile's routing
rules and evaluates it here (see
[`review_queues/README.md`](../review_queues/README.md)).

| Module | Holds |
|---|---|
| `models.py` | `DecisionTable`, `TableInput`, `TableOutput`, `TableRule`, `DecisionResult`, and the exceptions |
| `sfeel.py` | The unary tests a rule's input cells are written in: `parse`, `matches`, `parse_literal`, `format_literal`, `display_values`, `to_dmn13` |
| `engine.py` | `evaluate`, the `Engine` protocol, the `ENGINES` registry and `BuiltinEngine` |
| `dmn.py` | DMN XML: `read_decision_table`, `read_dmn`, `write_decision_table`, `DmnImportError` |

Importing the package loads nothing. `DecisionTable`, `evaluate` and `ENGINES` are
loaded on first use, and `models.py`, `sfeel.py` and `engine.py` import only the
standard library, pydantic and `idp_common.field_paths`. `dmn.py` is the one
module with a third-party dependency, `defusedxml`, which it imports when it
first parses a file. Install it with the `dmn` extra; evaluating tables needs
only the `decision_tables` extra, which has no dependency of its own.

## Evaluating a table

```python
from idp_common.decision_tables import evaluate
from idp_common.decision_tables.models import (
    DecisionTable, TableInput, TableOutput, TableRule,
)

table = DecisionTable(
    id="review-routing",
    hit_policy="FIRST",
    inputs=[TableInput(id="in1", expression="invoice_total")],
    outputs=[TableOutput(id="out1", name="queue", default='"default"')],
    rules=[
        TableRule(id="high", input_entries=["> 1000"], output_entries=['"approvals"']),
    ],
)
result = evaluate(table, {"invoice_total": 2500})
result.matched_rule_id   # "high"
result.outputs           # {"queue": "approvals"}
result.inputs_used       # {"invoice_total": 2500}
```

- `context` maps each input's `expression` text to its value. An expression the
  context lacks reads as null.
- The result is a `DecisionResult`: the matched rule's id (empty when none
  matched), `default_used`, the outputs by name, and the context values of the
  table's inputs.
- Evaluation is deterministic and does no I/O.

## What the builtin engine supports

- **Hit policy `FIRST` only.** The first rule in table order whose every input
  entry is true wins. Any other hit policy raises `UnsupportedHitPolicy`, which
  names it. A `DecisionTable` built without a hit policy has DMN's default,
  `UNIQUE`, so a table must say `FIRST` to be evaluated.
- **No match.** Each output takes its `default`. An output with no default is
  null, and `default_used` is true.
- **Allowed values.** When an input has `allowed_values` and its value does not
  satisfy them, the table does not apply: every output is null and no rule
  matches.
- **Output entries** are literals: a string, a number, `true`, `false` or `null`.
  A whole number is returned as an `int`, any other number as a `Decimal`.
- **Quantifiers.** `TableInput.quantifier` is `""`, `"any"` or `"all"`. When it is
  set and the value is a list, a rule's test is applied to each element: `any`
  needs one element to pass, `all` needs every element of a non-empty list to
  pass, and an empty list passes neither. The entry `-` matches whatever the list
  holds. This field is not part of DMN; a table read from DMN XML leaves it
  empty.

Not supported: hit policies other than `FIRST`, aggregation, chained decisions,
and FEEL expressions beyond the unary tests below.

## Unary tests

A rule's input entry is a unary test in this grammar:

```
unary_tests    ::= "-" | "not" "(" positive_tests ")" | positive_tests
positive_tests ::= positive_test ( "," positive_test )*
positive_test  ::= "null" | endpoint | op endpoint | interval
op             ::= "<" | "<=" | ">" | ">=" | "=" | "!="
interval       ::= ( "[" | "(" | "]" ) endpoint ".." endpoint ( "]" | ")" | "[" )
endpoint       ::= number | string | "true" | "false" | 'date("' YYYY-MM-DD '")'
number         ::= "-"? DIGIT+ ( "." DIGIT+ )?
string         ::= '"' ( [^"\\] | '\"' | '\\' )* '"'
```

| Entry | True when the value |
|---|---|
| `-` | is anything, null included |
| `> 1000` | is a number above 1000 |
| `[100..500]` | is from 100 to 500, both included |
| `(100..500)` or `]100..500[` | is between 100 and 500, both excluded |
| `"Invoice"` or `= "Invoice"` | is the string `Invoice` |
| `"Low", "Medium"` | is one of the two strings |
| `!= "pass"` or `not("pass")` | is not the string `pass` |
| `not("a", "b")` | is neither string |
| `true` | is the boolean true |
| `null` | is null |
| `not(null)` | is not null |
| `< date("2026-07-01")` | is a date before 1 July 2026 |

### Three-valued results

`matches` returns `True`, `False` or `None` (unknown). A rule's entry counts as
matched only when the result is `True`.

- `=` and `!=` are always boolean. Null equals only null, and a value of another
  type than the literal is not equal to it.
- An ordering test (`<`, `<=`, `>`, `>=`) or an interval is unknown on null, and on
  a value of another type than its literals.
- A list of tests is true when any test is true, otherwise unknown when any test
  is unknown, otherwise false.
- `not(...)` of unknown is unknown.

So `> 1000` does not match a missing value, and neither does `not(> 1000)`.

### Types

- **Numbers** are compared exactly, through
  `idp_common.field_paths.exact_numeric_reading`. An `int`, a `float`, a
  `Decimal` and a numeric string are numbers. A boolean is never a number. A
  string such as `1,000` is not a number.
- **Strings** are case-sensitive. The string literal `"5"` is not equal to the
  number 5.
- **Booleans** are `true` and `false`. The string `"true"` is a string.
- **Dates.** A date literal compares with a `date`, with an ISO `YYYY-MM-DD`
  string, and with the date part of an ISO date-time string or a `datetime`.

### Errors

A test the grammar does not accept raises `UnaryTestSyntaxError`, a `ValueError`,
whose `column` is the 1-based position where parsing stopped. Parsed tests are
cached per text.

### Showing a test's values

`display_values(text)` returns the values the tests in a cell compare with, for
showing to a reader: one entry per test, in order. A comparison gives its literal (a
string without its quotes, any other literal as written), a range gives `low..high`
and `null` gives `null`. The operator and a surrounding `not(...)` are left out, so
`>= 0.8` gives `["0.8"]`, `not("a", "b")` gives `["a", "b"]`, `[1..10]` gives
`["1..10"]` and `-` gives an empty list. It raises `UnaryTestSyntaxError` for text
that does not parse. Review routing uses it for the `{condition_N.value}`
placeholder of a routing rule's reason. `format_literal(value)` is the other
direction: the literal text of a string, a number, a boolean or `None`.

### Equivalent spellings

`5` and `= 5` parse to the same tests, as do `not(5)` and `!= 5`. `to_dmn13(text)`
returns a test in the form DMN 1.3 modelers accept, where equality is the bare
literal and inequality is `not(literal)`. It raises `DecisionTableError` for a
test that has `!=` among several tests, which DMN 1.3 cannot express.

## DMN XML

`dmn.py` reads and writes one decision that holds one decision table.

```python
from idp_common.decision_tables.dmn import read_decision_table, write_decision_table

xml = write_decision_table(table)                      # DMN 1.5
xml = write_decision_table(table, dmn_version="1.3")   # for Camunda Modeler and dmn-js
table = read_decision_table(xml)
```

**Reading.** The reader accepts the DMN 1.3, 1.4 and 1.5 model namespaces.
`read_dmn` returns a `DmnDocument`: the table, the version, the table's
`aggregation` attribute, and the attributes from other namespaces on
`decisionTable` and on each `rule`, so a caller can keep its own data in a file.
`read_decision_table` returns the table alone.

| Element | Becomes |
|---|---|
| `decision` `id`, `name` | `DecisionTable.id`, `name` |
| `decisionTable` `hitPolicy` | `hit_policy` (`UNIQUE` when absent, DMN's default) |
| `input` `id`, `label`; `inputExpression` `typeRef` and text; `inputValues` text | `TableInput.id`, `label`, `type_ref`, `expression`, `allowed_values` |
| `output` `id`, `name`, `typeRef`; `defaultOutputEntry` text | `TableOutput.id`, `name`, `type_ref`, `default` |
| `rule` `id`; `description`; `inputEntry` and `outputEntry` texts | `TableRule.id`, `description`, `input_entries`, `output_entries` |

- An id a modeler left out is generated (`input_1`, `output_1`, `rule_1`).
- An empty input cell reads as `-`, which is how modelers write "any value".
- A table with one unnamed output takes the decision's name for it.
- A table has as many outputs as it declares, and a rule has one output entry for
  each. The reader returns them all, in order; which outputs a table must have is
  the caller's rule. Review routing's table has three, `outcome`, `queue` and
  `reason` (a string, the text for the reviewer): its export writes all three, and
  its import accepts a file with the first two alone, whose routing rules then have
  no reason. That mapping is in `idp_common.review_queues.routing_dmn`, described in
  the
  [review queues README](../review_queues/README.md#the-routing-table-and-its-inputs).
- `itemDefinition`, `inputData`, `outputValues`, `variable`, `extensionElements`,
  the diagram and attributes in other namespaces do not affect the table.

The reader does not judge the table's content. The hit policy, the cells and the
outputs are returned as written, for the caller to accept or refuse.

**Refused**, with a `DmnImportError` whose `construct` names the cause:

| `construct` | Cause |
|---|---|
| `size` | More than `MAX_DMN_BYTES` (512 KB). Checked before parsing |
| `DTD` | A DTD or an entity declaration |
| `XML` | Not well-formed |
| `namespace` | Not a DMN 1.3, 1.4 or 1.5 model namespace |
| `decision` | No decision, or more than one |
| `businessKnowledgeModel`, `knowledgeRequirement` | Business knowledge models |
| `requiredDecision` | A decision that requires another decision |
| `literalExpression`, `context`, `invocation`, `relation`, `list`, `functionDefinition` and the other expression kinds | Decision logic that is not a decision table |

**Safety.** Input is untrusted. It is parsed with
`defusedxml.ElementTree.fromstring(..., forbid_dtd=True)`, so entity expansion and
external entities are refused, and any DTD is refused whether or not it declares
an entity.

**Writing.** `write_decision_table` writes DMN 1.5 by default and DMN 1.3 on
request. The 1.3 form uses the 1.3 namespaces and writes `= x` as `x` and `!= x`
as `not(x)`, because DMN 1.3 unary tests have no `=` or `!=` operator. A table
written as 1.5 reads back equal. A table written as 1.3 reads back with those two
spellings changed and the same meaning. The file includes a minimal diagram so
graphical modelers open it. Attributes from another namespace are passed as
`{uri}name` keys with a prefix for each namespace in `namespace_prefixes`.

**Conformance.** `tests/unit/decision_tables/test_tck_first_hit_policy.py` runs the
DMN TCK's two FIRST hit-policy cases through the reader and the engine. The
fixtures and their provenance are in `tests/unit/decision_tables/tck/`.

## Engines

`ENGINES` maps an engine name to an implementation, and `evaluate(table, context,
engine="builtin")` looks the name up. An unregistered name raises
`UnknownEngine`.

To add an engine:

1. Write a class with `evaluate(self, table, context) -> DecisionResult` (the
   `Engine` protocol in `engine.py`).
2. Register an instance in `ENGINES` under a new name.
3. Keep its imports out of `engine.py`'s module level if they are heavy, so the
   routing Lambda does not load them.

## Exceptions

All derive from `DecisionTableError`, which is a `ValueError`:
`UnaryTestSyntaxError`, `UnsupportedHitPolicy`, `UnknownEngine` and
`DmnImportError`.
