# Detection A/B on real labeled corpora (#753)

Raw evidence for [`docs/benchmarking/feature-multi-instance.md`](../../../docs/benchmarking/feature-multi-instance.md) §2.

Run via **Test Studio** (the TestRunner Lambda the UI calls), not the benchmark
harness — the harness silently skips reference corpora, see the study's honesty
notes. Two configuration profiles per corpus, differing in nothing but
`extraction.multi_instance_detection.enabled`; `numberOfFiles: 40` takes the same
deterministic first 40 documents on both sides, so the comparison is paired.

Driven by [`benchmarks/harness/detection_ab_teststudio.py`](../../harness/detection_ab_teststudio.py)
(`launch` / `analyse`) — a results directory holds data, not executable code.

| file | what |
|---|---|
| `runs.json` | the four run ids |
| `paired_summary.txt` | `analyse.py` output |
| `detection_vs_ground_truth.txt` | per-document detection verdict against each baseline's `checks` count |

Headline: **precision and recall 1.000** on 40 bank-check images (18 true
positives, 0 false positives, 0 misses, 22 correct silences) with the count
**exactly** right on all 18; **−1.3 accuracy points** on RealKIE-FCC-Verified
(p = 0.001), where there are no multi-record documents to find; tokens ±2%.
