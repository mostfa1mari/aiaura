# /experiments

Every research experiment gets one directory here: `YYYY-MM-DD--short-slug/` containing
an `EXPERIMENT.md` with:

- hypothesis
- dataset_version / feature_version / model_version
- parameters
- training / validation / test periods (chronological, no overlap)
- results (with sample sizes and confidence intervals)
- conclusion (supported / refuted / inconclusive)

**Failed experiments are never deleted.** A refuted hypothesis is research output.

No experiments exist yet: the project is gated at Phase 0 pending the data-source
decision (see [../docs/PHASE0_REPORT.md](../docs/PHASE0_REPORT.md)).
