# X5.0 standalone compatibility baseline

Baseline before federated work: `0f45639b0dcc68a86957188ea10836566cfdad53`.

The federated roadmap may extend Xerrameca, but must not silently change the existing standalone contract.

## Protected runtime behavior

The following behavior is regression-protected through X5-X8:

- `GET /health` remains available.
- REST command/inbox/claim/reply/list/get/messages flows remain compatible.
- MCP exposes exactly these seven tools:
  - `xerrameca_command`
  - `xerrameca_inbox`
  - `xerrameca_claim`
  - `xerrameca_reply`
  - `xerrameca_list`
  - `xerrameca_get`
  - `xerrameca_messages`
- current alternating two-agent dialogue semantics remain compatible.
- completion requires the existing consensus behavior.
- SQLite restart persistence remains valid.
- Pluribus-backed identity remains a supported explicit provider mode when configured.
- credentials are request-scoped and must never be persisted in Xerrameca conversation/event storage.
- direct `pluribus.*` imports remain forbidden in Xerrameca source.

## Development rule

New federated functionality must be additive behind ports/configuration. The current standalone deployment must remain runnable while node/federated mode is developed.

## Regression manifest

`tests/baseline_regressions.txt` is the explicit X5-X8 protected test manifest. CI runs it as a dedicated gate in addition to the complete test suite.

## X5.0 PASS

X5.0 passes only when:

1. the protected manifest is green;
2. the full test suite is green;
3. compile and standalone-boundary checks are green;
4. the standalone Docker image still builds;
5. no runtime behavior change is required to satisfy this phase.
