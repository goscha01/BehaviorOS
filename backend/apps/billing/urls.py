from django.urls import path
from apps.billing.views import SubscriptionStatusView, CreateCheckoutView, CreatePortalView
from apps.billing.webhooks import stripe_webhook

urlpatterns = [
    path('subscription/', SubscriptionStatusView.as_view(), name='subscription-status'),
    path('checkout/', CreateCheckoutView.as_view(), name='create-checkout'),
    path('portal/', CreatePortalView.as_view(), name='create-portal'),
    path('webhook/', stripe_webhook, name='stripe-webhook'),
]
