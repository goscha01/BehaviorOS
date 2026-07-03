from apps.accounts.models import Membership


class OrgContextMiddleware:
    """Sets request.org based on the authenticated user's primary membership."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.org = None
        if hasattr(request, 'user') and request.user.is_authenticated:
            membership = (
                Membership.objects.filter(user=request.user)
                .select_related('org')
                .first()
            )
            if membership:
                request.org = membership.org
        return self.get_response(request)
