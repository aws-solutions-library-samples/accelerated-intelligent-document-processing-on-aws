# Evaluation Golden Fixtures

These fixtures gate every scoring-behavior change against the currently-pinned
`stickler-eval==1.0.0` (bumped from 0.5.0 in the Stickler 1.0 migration —
Hungarian FD/FN accounting, threshold-reach and object-level-threshold
changes upstream shift counts on structured-list evaluations, so goldens
were regenerated). Each `.input.json` describes one call to
`EvaluationService.evaluate_section` (config + section + expected/actual dicts).
The corresponding `.golden.json` under `section_goldens/` is the exact
JSON-serialized `SectionEvaluationResult` that call produces today, including
the raw `stickler_comparison_result` blob (the cross-Lambda contract consumed by
`patterns/unified/src/test_execution_aggregation_function/index.py`).

If a change is expected to shift metrics (R1, R2, R3, R4 per the recommendations
doc), regenerate the goldens with:

```bash
python scripts/regenerate_evaluation_goldens.py
```

and review the diff before committing. If a change is expected to leave metrics
unchanged (R5, R6, R8, R9, R7 replacement, §6 reorg), the goldens must be
byte-identical — regeneration must be a no-op.
