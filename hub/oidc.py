"""Authenticate scheduled jobs with GitHub Actions OIDC tokens, so no shared secret has to be copied anywhere.

A workflow with `permissions: id-token: write` asks GitHub for a short-lived JWT signed by GitHub
(issuer https://token.actions.githubusercontent.com). We accept it only if the signature verifies against
GitHub's published keys, the audience is ours, and the claims show it comes from OUR repository's
refresh workflow on the main branch. A fork or another repository gets a token with its own name in
`repository`, so it is refused.
"""
from __future__ import annotations

import jwt

ISSUER = "https://token.actions.githubusercontent.com"
JWKS_URL = ISSUER + "/.well-known/jwks"
WORKFLOW_FILE = ".github/workflows/refresh.yml"
ALLOWED_EVENTS = {"schedule", "workflow_dispatch"}

_jwks_client: jwt.PyJWKClient | None = None


def _github_key(token: str):
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(JWKS_URL, cache_keys=True, lifespan=3600, timeout=10)
    return _jwks_client.get_signing_key_from_jwt(token).key


class OIDCRejected(Exception):
    pass


def verify_github_token(token: str, repository: str, audience: str, key_resolver=None) -> dict:
    """Return the verified claims or raise OIDCRejected. `key_resolver(token) -> key` is a test hook."""
    if not token or token.count(".") != 2:
        raise OIDCRejected("not a JWT")
    try:
        key = (key_resolver or _github_key)(token)
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=audience, issuer=ISSUER,
                            options={"require": ["exp", "iat", "iss", "aud", "repository", "ref", "workflow_ref"]},
                            leeway=30)
    except Exception as e:  # signature, expiry, audience, issuer, network to the key set
        raise OIDCRejected(type(e).__name__) from None
    repo = repository.lower()
    if str(claims.get("repository", "")).lower() != repo:
        raise OIDCRejected("repository")
    if claims.get("ref") != "refs/heads/main":
        raise OIDCRejected("ref")
    if not str(claims.get("workflow_ref", "")).lower().startswith(f"{repo}/{WORKFLOW_FILE}@"):
        raise OIDCRejected("workflow")
    if claims.get("event_name") not in ALLOWED_EVENTS:
        raise OIDCRejected("event")
    return claims
