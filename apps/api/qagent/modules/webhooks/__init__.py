"""QAgent webhooks: inbound events from a connected source provider.

Distinct from `modules/notify/` (QAgent's own *outbound* Slack/webhook
alerts) and from ADR-0009's cloning concerns. This is GitHub calling QAgent,
not QAgent calling anywhere - so the only identity a caller can prove is
"knows the per-repository secret", verified by an HMAC signature, never a
bearer token (CLAUDE.md section 19's "git push -> ... -> Run QA -> ...")."""
