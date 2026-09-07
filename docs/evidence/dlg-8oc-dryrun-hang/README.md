# Resolved: workflow dry-run blocked on generated approval gates

The 2026-08-27 investigation found that child calls were stubbed during dry-run,
but the generated script could not tell it was a dry-run. Its approval loops
waited indefinitely; dependent threads and the main pipeline waited behind them.
There were no child processes responsible for the hang.

The fix exposed `dry_run` to scripts, made dry-run item threads disposable, and
added `workflows.dryRunTimeoutSeconds`. A stuck script now fails with
`dry_run_timeout` rather than hanging indefinitely. The issue was closed as
`dlg-8oc` with the fix in `c73edf5`.

The generated plan, workflow copy, process listing and stack dump were deleted
after the fix was established. Regression coverage remains in the workflow
tests; raw runtime artifacts are not project documentation.
