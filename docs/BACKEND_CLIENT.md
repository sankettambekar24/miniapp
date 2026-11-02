# Backend client (`hss/backend/`)

Stdlib-only (urllib + json) client for the Django backend `/api/v1`. Built from `docs/BACKEND_ANALYSIS.md` and the
recorded probes. **Status: MOCK-TESTED** (contract tests, fake transport, recorded fixtures). Source-derived write
endpoints are **IMPLEMENTED-UNTESTED**. Nothing here is VERIFIED-LIVE-API: it was never run against the live server.

## Modules
- `client.py` `BackendClient(base_url, session, transport=None, clock, sleep, on_wait, ...)`, `Page`, `validate_base_url`
- `errors.py` typed exceptions + `error_from_response`; `transport.py` `Transport` protocol, `UrllibTransport` (no redirects)
- `redact.py` `redact_text`, `redact_obj`, `RedactingFilter`; `testing.py` `FakeTransport`, `probe_response`

## Public API (return types)
Health: `health_live()->dict`, `health_ready()->dict`.
Auth: `login(u,p)->{id,username,role}`, `refresh()->None`, `logout(revoke_all=False)->None`, `change_password(cur,new)->None`,
`register(u,p)->dict`, `me()->dict`, properties `is_authenticated`, `current_user`.
Users (admin): `list_users(page)->Page`, `get_user(id)`, `create_user(u,p,role=None)`, `set_user_active(id,bool)`, `disable_user(id)`, `set_user_role(id,role)`.
Devices: `list_devices(page)->Page`, `get_device(id)`, `create_device(label,public_id,protocol_version,owner_user=None)`, `set_device_status(id,status)`, `assign_device(id,owner_user|None)`.
Files: `list_files(page)->Page`, `get_file(id)`, `create_file_metadata(owner_user_id,display_name)`, `get_file_content(id)` (always raises), `list_grants(file_id)->list`, `create_grant(file_id,subject_user_id,permission,expires_at=None)`, `revoke_grant(file_id,grant_id)`.
Audit (admin): `list_audit_events(page, event_type)->Page`, `get_audit_event(id)`.
Offline policies: `list_offline_policies(page)->Page`, `get_offline_policy(id)`, `revoke_offline_policy(id)`, `create_offline_policy()` (always raises, sends nothing).
Hardware auth: `hw_preauth`, `hw_create_attempt`, `hw_get_attempt`, `hw_report_event`, `hw_cancel_attempt`, `hw_complete_attempt` (always NotImplementedByBackend). `capability=` selects the `X-Auth-Attempt-Capability` header instead of Bearer.
`Page(results, count, next, previous)`; `check_same_origin(next_url)` validates pagination links. Everything else returns the backend JSON dict.

## Error taxonomy (all subclass `BackendError`: `.status .code .message .request_id .retry_after .fields`)
| Class | Trigger |
|---|---|
| NetworkError / Timeout | no connection / timeout |
| ColdStart | 502/503/504 without an app error code (Render waking, readiness degraded) |
| Unauthorized | 401 (also failed refresh; `TOKEN_REVOKED`/`ROLE_CHANGED` force re-login) |
| Forbidden / NotFound / MethodNotAllowed | 403 / 404 / 405 |
| ValidationError | 400; `.fields` parsed from `field: msg; ...` |
| Conflict / Gone | 409 / 410 |
| Throttled | 429, `.retry_after` seconds from Retry-After |
| ServerError | other 5xx, or 5xx with an app code |
| NotImplementedByBackend | codes `FILE_CONTENT_UNAVAILABLE`, `OFFLINE_POLICY_ISSUANCE_DISABLED`, `FACTOR_VERIFICATION_UNAVAILABLE`; not an outage, never retried |
Envelope parsed: `{"error":{"code","message","request_id"}}` (`backend/common/exceptions.py`). Messages are redacted.
`InvalidBaseUrl` (ValueError) for bad URLs: https required, http only for localhost/127.0.0.1, no credentials/query.

## Retry and timeouts
First request 90 s (Render cold start), later 15 s. Only GET/HEAD/OPTIONS retry, on NetworkError/Timeout/ColdStart,
max 4 attempts, backoff 2,4,8.. capped 20 s, calling `on_wait(attempt, seconds, reason)` (reason: cold-start/network/timeout).
POST/PUT/PATCH/DELETE are never retried. 4xx never retried. On 401 (not login/refresh/capability): refresh once; GET is
replayed, other methods raise Unauthorized (refresh already done, user may repeat). A second 401 clears the session.

## Security properties
- Access token only in `SessionStore.backend_token`; refresh token only in the client object (memory). Nothing on disk.
  Note: `SessionStore` has no refresh slot (core is read-only for this task), so the client drops its refresh token/user
  whenever it observes `backend_token is None`; `SessionStore.clear()` (lock/disconnect) therefore leaves it unauthenticated
  and a stale refresh token cannot revive the session.
- `logout()` always clears local tokens even if the call fails. Refresh failure (400/401/403) clears the session.
- Logs: method, path (no query), status, duration only. All text passes `redact` (JWT-shaped strings, Bearer, password,
  token, refresh, access, cookie, secret, capability); `RedactingFilter` available for handlers. Exceptions/repr never
  carry tokens (tested). Redirects are never followed (would leak Authorization). Path ids are URL-quoted.
- Server role is advisory in `current_user`; use `me()` as authority.

## Backend limitations (UI features BLOCKED)
Hardware-verified login/session (complete never succeeds, factors stored verified=false), file content download/upload
(503 by design), offline policy issuance (503), hardware-only login (JWT only via password).
