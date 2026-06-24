from fastapi import FastAPI

from . import (
    agent_routes,
    auth_routes,
    commands_routes,
    file_routes,
    group_routes,
    im_routes,
    membership_routes,
    openai_compat,
    provider_routes,
    session_routes,
    settings_routes,
    skill_routes,
    user_routes,
)


def mount_routes(app: FastAPI) -> None:
    app.include_router(auth_routes.router, prefix="/api/auth", tags=["auth"])
    app.include_router(user_routes.router, prefix="/api/users", tags=["users"])
    app.include_router(group_routes.router, prefix="/api/groups", tags=["groups"])
    app.include_router(membership_routes.router, prefix="/api", tags=["membership"])
    app.include_router(agent_routes.router, prefix="/api/agents", tags=["agents"])
    app.include_router(session_routes.router, prefix="/api/sessions", tags=["sessions"])
    app.include_router(file_routes.router, prefix="/api/files", tags=["files"])
    app.include_router(skill_routes.router, prefix="/api/skills", tags=["skills"])
    app.include_router(provider_routes.router, prefix="/api/providers", tags=["providers"])
    app.include_router(settings_routes.router, prefix="/api/settings", tags=["settings"])
    app.include_router(commands_routes.router, prefix="/api/commands", tags=["commands"])
    app.include_router(im_routes.router, prefix="/api/im", tags=["im"])
    app.include_router(openai_compat.router, prefix="/v1", tags=["openai-compat"])
