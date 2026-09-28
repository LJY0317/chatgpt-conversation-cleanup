from __future__ import annotations

from dataclasses import replace
import subprocess

from .appserver import AppServerError, AppServerSession
from .platforms import PlatformAdapter, PlatformError
from .profiles import Profile, ProfileIdentity


class IdentityError(RuntimeError):
    pass


def _identity_from_account_response(result: dict) -> ProfileIdentity | None:
    account = result.get("account")
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        return None
    routing = result.get("workspaceRouting")
    routing = routing if isinstance(routing, dict) else {}
    email = account.get("email")
    plan = account.get("planType")
    workspace_account_id = routing.get("chatgptAccountId")
    return ProfileIdentity(
        email=email if isinstance(email, str) and email else None,
        workspace_account_id=(
            workspace_account_id
            if isinstance(workspace_account_id, str) and workspace_account_id
            else None
        ),
        plan=plan if isinstance(plan, str) and plan else None,
    )


def resolve_identity(
    profile: Profile,
    platform: PlatformAdapter,
    *,
    timeout=5,
    popen=subprocess.Popen,
) -> ProfileIdentity | None:
    try:
        with AppServerSession(
            profile,
            platform,
            timeout=timeout,
            popen=popen,
        ) as session:
            return _identity_from_account_response(session.account())
    except (PlatformError, AppServerError) as error:
        raise IdentityError(str(error)) from error


def enrich_identities(profiles: list[Profile], platform: PlatformAdapter) -> list[Profile]:
    enriched = []
    for profile in profiles:
        try:
            identity = resolve_identity(profile, platform)
        except (IdentityError, OSError, subprocess.SubprocessError):
            identity = None
        enriched.append(replace(profile, identity=identity) if identity else profile)
    return enriched
