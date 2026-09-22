# Modular IDP Pipeline Notebooks

Please refer to [Using Notebooks with IDP Common Library](../../../docs/using-notebooks-with-idp-common.md).

## Notebooks

The numbered notebooks (`step1_…` through `step6_…`) walk through a modular run of the pipeline against `samples/bank-statement-multipage.pdf`.

In addition:

- [`step3_extraction_with_missing_pages.ipynb`](./step3_extraction_with_missing_pages.ipynb) — variant of `step3_extraction.ipynb` that demonstrates the optional **MISSING vs BLANK field handling** feature ([issue #317](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/317)). Runs extraction twice on the same section — once with the full document (control), once with a transactions-worksheet page dropped — and diffs the outputs to show how `extraction.missing_field_handling` distinguishes pages-not-submitted from fields-left-blank. Requires `step2_classification.ipynb` to have been run first. See [`docs/missing-page-handling.md`](../../../docs/missing-page-handling.md) for the full feature guide.