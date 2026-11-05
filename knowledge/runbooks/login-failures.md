---
id: rb-login-failures
title: Users cannot sign in (401, 429 too many attempts, everyone logged out)
type: runbook
tags: [login, auth, jwt, 401, 429, rate-limit, proxy]
---

# Users cannot sign in

GarageFlow issues JWT access tokens from `POST /api/auth/login`. Tokens last `Jwt:ExpiryMinutes`
(default 60) and are signed with `Jwt:SigningKey`. There are no refresh tokens: when a token expires,
the user signs in again.

## 401 "Sign-in failed" for one user

- Wrong password, or the email has a typo. Emails are stored in lower case.
- The user was deactivated by an Admin (`POST /api/users/{id}/deactivate`). Check `GET /api/users`
  (Admin only): `isActive: false`.
- In Production there are no demo users. `advisor@garageflow.local` only exists in Development.

## 429 "Too many requests" on the login screen

Login is rate limited per caller IP address: `RateLimiting:LoginPermitLimit` attempts (default 10) per
`RateLimiting:LoginWindowSeconds` (default 60).

- A whole shop behind one office router shares one public IP, so 10 staff signing in at opening time
  can hit the limit. Wait one minute, or raise the limit for that site.
- **Behind a reverse proxy or load balancer** (Azure App Service, nginx), every request appears to come
  from the proxy's IP unless `Hosting:BehindProxy=true`. Then the whole company shares one limit.
  Turn it on only when a trusted proxy sets `X-Forwarded-For`.

## Everyone was logged out at once

- The API was redeployed with a **different `Jwt:SigningKey`**. Every existing token fails validation
  (401) and everyone must sign in again. Keep the key stable across deployments (store it in the
  secret store, not in the release pipeline).
- The server clock jumped. Tokens are validated with a clock skew of 1 minute.

## Integrations (like FlowPilot) get 401 after a restart

Expected after a signing-key change: the integration should sign in again automatically. FlowPilot
does this once per request; repeated 401s mean its credentials are wrong.

## Verify

Sign in through the UI or `POST /api/auth/login`, then call `GET /api/auth/me` with the token.
