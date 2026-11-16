---
name: ask-muse
description: Run a bounded Muse activity through the shared dispatcher or use the legacy Muse implementation profile.
---

Use `{{CODEX_ROOT}}/agent-framework/scripts/provider_runner.py status --activity ACTIVITY --config ROUTING` and `provider_runner.py dispatch --activity ACTIVITY --provider muse --workspace WORKSPACE --task-file TASK --config ROUTING` for the selected activity. The editable chain chooses regular Wonder or Wonder Full. A static activity disables shell and workspace writes at the CLI.

The default preserves Muse Code's stored authentication, with environment API-key overrides removed from the child. Confirm the intended stored account separately: the framework does not read stored credentials or know whether they use a subscription or an API key.

When the user explicitly asks to use their Meta Model API credits, use `--provider muse --muse-auth api` on both status and dispatch. This is authorization for the API billing route for that task, never an automatic fallback when stored auth reaches a limit. The runner reads `META_API_KEY` from the parent process, or Windows User environment when absent from the process, and injects it only into that Muse Code child. A missing or blank key prevents a real launch; status and dry-run remain local and do not require a key. Never print the credential, put it in command arguments, task files or routing JSON, or run `muse auth set` to change stored credentials. A custom configured Muse key name stays filtered and redacted; API selection always uses `META_API_KEY`.

Set up the selected key through your shell's secret store or environment manager before running the framework (on Windows, a persisted User `META_API_KEY` is also supported). See Meta's [authentication and billing](https://dev.meta.ai/docs/muse-code/auth) and [subscriptions](https://dev.meta.ai/docs/muse-code/subscriptions). Additional Model API keys use pay-as-you-go billing. Meta may charge PAYG after available credits are gone; the framework does not check balances or enforce a credit ceiling. Do not promise credit-only spending or model entitlement. Meta reserves Wonder Full for subscription use: when API mode selects `muse-wonder-full-1.0` or `cli-default`, the runner uses `muse-wonder-1.0` with the selected effort and reports that effective model. See [Meta's model availability](https://dev.meta.ai/docs/models).

Examples (paths are placeholders, never credentials):

```text
python {{CODEX_ROOT}}/agent-framework/scripts/provider_runner.py status --activity implementation --provider muse --muse-auth api --config ROUTING
python {{CODEX_ROOT}}/agent-framework/scripts/provider_runner.py dispatch --activity implementation --provider muse --muse-auth api --workspace WORKSPACE --task-file TASK --config ROUTING
python {{CODEX_ROOT}}/agent-framework/scripts/provider_runner.py implement --provider muse --muse-auth api --muse-profile complex --workspace WORKSPACE --task-file TASK --config ROUTING
python {{CODEX_ROOT}}/agent-framework/scripts/provider_runner.py quota-set --provider muse --muse-auth api --reason "User-reported API limit" --config ROUTING
```

`--muse-auth api` requires `--provider muse`, and cannot combine with `--profile` or legacy Claude review. Stored-auth quota stays in `muse-quota.json`; explicit API quota uses `muse-api-quota.json`. Profiles share quota within their selected auth mode, while all Muse modes share the pending writer inventory and cessation protections. A limit in either mode never automatically switches billing modes. Mode metadata describes selection, not the billing type of a stored credential.

The legacy `implement --provider muse --muse-profile routine|complex|escalation` command remains available with its historical native Luna fallback semantics. The canonical dispatcher handles cross-provider retries only after a known terminal failure and resolved ownership.
