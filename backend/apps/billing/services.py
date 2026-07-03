import stripe
from django.conf import settings

stripe.api_key = settings.STRIPE_SECRET_KEY


def get_or_create_stripe_customer(org):
    from apps.billing.models import StripeCustomer

    try:
        return org.stripe_customer
    except StripeCustomer.DoesNotExist:
        customer = stripe.Customer.create(
            name=org.name,
            metadata={'org_id': str(org.id)},
        )
        return StripeCustomer.objects.create(
            org=org, stripe_customer_id=customer.id
        )


def create_checkout_session(org, price_id, success_url, cancel_url):
    stripe_customer = get_or_create_stripe_customer(org)
    session = stripe.checkout.Session.create(
        customer=stripe_customer.stripe_customer_id,
        payment_method_types=['card'],
        line_items=[{'price': price_id, 'quantity': 1}],
        mode='subscription',
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={'org_id': str(org.id)},
    )
    return session


def create_portal_session(org, return_url):
    stripe_customer = get_or_create_stripe_customer(org)
    session = stripe.billing_portal.Session.create(
        customer=stripe_customer.stripe_customer_id,
        return_url=return_url,
    )
    return session


def sync_subscription(stripe_subscription):
    from apps.billing.models import Subscription, StripeCustomer
    from datetime import datetime, timezone

    customer_id = stripe_subscription.get('customer')
    try:
        stripe_customer = StripeCustomer.objects.get(stripe_customer_id=customer_id)
    except StripeCustomer.DoesNotExist:
        return None

    plan = 'starter'
    if stripe_subscription.get('items', {}).get('data'):
        price_id = stripe_subscription['items']['data'][0].get('price', {}).get('id', '')
        if price_id == settings.STRIPE_PRO_PRICE_ID:
            plan = 'pro'

    period_end = stripe_subscription.get('current_period_end')
    if period_end:
        period_end = datetime.fromtimestamp(period_end, tz=timezone.utc)

    subscription, _ = Subscription.objects.update_or_create(
        stripe_subscription_id=stripe_subscription['id'],
        defaults={
            'org': stripe_customer.org,
            'status': stripe_subscription.get('status', 'incomplete'),
            'plan': plan,
            'current_period_end': period_end,
            'cancel_at_period_end': stripe_subscription.get('cancel_at_period_end', False),
        },
    )
    return subscription
