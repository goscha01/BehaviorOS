class OrgQuerySetMixin:
    """Mixin for DRF views to scope querysets to the current user's organization."""

    org_field = 'org'

    def get_queryset(self):
        qs = super().get_queryset()
        if hasattr(self.request, 'org') and self.request.org:
            return qs.filter(**{self.org_field: self.request.org})
        return qs.none()
