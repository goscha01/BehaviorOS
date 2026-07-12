class OrgQuerySetMixin:
    """Mixin for DRF views to scope querysets to the current user's organization.

    Falls back to permission-side org resolution when middleware missed it
    (see apps.accounts.permissions._ensure_org for why).
    """

    org_field = 'org'

    def get_queryset(self):
        qs = super().get_queryset()
        org = getattr(self.request, 'org', None)
        if org is None:
            # Late-resolve so JWT-authenticated requests aren't scoped to none().
            from apps.accounts.permissions import _ensure_org
            org = _ensure_org(self.request)
        if org is not None:
            return qs.filter(**{self.org_field: org})
        return qs.none()
