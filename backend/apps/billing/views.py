from django.conf import settings
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsOrgMember
from apps.billing.models import Subscription
from apps.billing.serializers import (
    SubscriptionSerializer,
    CreateCheckoutSerializer,
    CreatePortalSerializer,
)
from apps.billing.services import create_checkout_session, create_portal_session


class SubscriptionStatusView(APIView):
    permission_classes = [IsAuthenticated, IsOrgMember]

    def get(self, request):
        try:
            subscription = request.org.subscription
            return Response(SubscriptionSerializer(subscription).data)
        except Subscription.DoesNotExist:
            return Response({'plan': None, 'status': None})


class CreateCheckoutView(APIView):
    permission_classes = [IsAuthenticated, IsOrgMember]

    def post(self, request):
        serializer = CreateCheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        plan = serializer.validated_data['plan']
        price_id = (
            settings.STRIPE_PRO_PRICE_ID
            if plan == 'pro'
            else settings.STRIPE_STARTER_PRICE_ID
        )

        session = create_checkout_session(
            org=request.org,
            price_id=price_id,
            success_url=serializer.validated_data['success_url'],
            cancel_url=serializer.validated_data['cancel_url'],
        )
        return Response({'checkout_url': session.url})


class CreatePortalView(APIView):
    permission_classes = [IsAuthenticated, IsOrgMember]

    def post(self, request):
        serializer = CreatePortalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        session = create_portal_session(
            org=request.org,
            return_url=serializer.validated_data['return_url'],
        )
        return Response({'portal_url': session.url})
