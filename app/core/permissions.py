"""RBAC vocabulary: admin roles, permissions, and API-key scopes.

Role→permission mapping is seeded into the `roles`/`permissions`/`role_permissions`
tables by the initial migration; the constants here are the canonical list.
"""
from typing import Dict, FrozenSet

# --- admin permissions ---
USERS_READ = "users.read"
USERS_WRITE = "users.write"
USERS_DELETE = "users.delete"
FACES_READ = "faces.read"
FACES_WRITE = "faces.write"
FACES_DELETE = "faces.delete"
PACKAGES_READ = "packages.read"
PACKAGES_WRITE = "packages.write"
PACKAGES_DELETE = "packages.delete"
USAGE_READ = "usage.read"
AUDIT_READ = "audit.read"
ADMIN_MANAGE = "admin.manage"

ALL_PERMISSIONS: FrozenSet[str] = frozenset({
    USERS_READ, USERS_WRITE, USERS_DELETE,
    FACES_READ, FACES_WRITE, FACES_DELETE,
    PACKAGES_READ, PACKAGES_WRITE, PACKAGES_DELETE,
    USAGE_READ, AUDIT_READ, ADMIN_MANAGE,
})

# --- admin roles ---
SUPER_ADMIN = "SUPER_ADMIN"
ADMIN = "ADMIN"
MANAGER = "MANAGER"
SUPPORT = "SUPPORT"
VIEWER = "VIEWER"

ROLE_PERMISSIONS: Dict[str, FrozenSet[str]] = {
    SUPER_ADMIN: ALL_PERMISSIONS,
    ADMIN: ALL_PERMISSIONS - {ADMIN_MANAGE},
    MANAGER: frozenset({USERS_READ, USERS_WRITE, FACES_READ, FACES_DELETE,
                        PACKAGES_READ, PACKAGES_WRITE, USAGE_READ, AUDIT_READ}),
    SUPPORT: frozenset({USERS_READ, USERS_WRITE, FACES_READ, PACKAGES_READ, USAGE_READ}),
    VIEWER: frozenset({USERS_READ, FACES_READ, PACKAGES_READ, USAGE_READ, AUDIT_READ}),
}
ALL_ROLES = tuple(ROLE_PERMISSIONS.keys())

# --- API key scopes ---
SCOPE_FACES_UPLOAD = "faces:upload"
SCOPE_FACES_SEARCH = "faces:search"
SCOPE_FACES_READ = "faces:read"
SCOPE_PERSONS_READ = "persons:read"
ALL_SCOPES: FrozenSet[str] = frozenset({SCOPE_FACES_UPLOAD, SCOPE_FACES_SEARCH, SCOPE_FACES_READ, SCOPE_PERSONS_READ})
