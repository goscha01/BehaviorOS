import stripe
from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.billing.services import sync_subscription


@csrf_exempt
@require_POST
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE', '')

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError:
        return HttpResponse(status=400)

    event_type = event['type']
    data = event['data']['object']

    if event_type == 'checkout.session.completed':
        subscription_id = data.get('subscription')
        if subscription_id:
            sub = stripe.Subscription.retrieve(subscription_id)
            sync_subscription(sub)

    elif event_type in (
        'customer.subscription.created',
        'customer.subscription.updated',
        'customer.subscription.deleted',
    ):
        sync_subscription(data)

    elif event_type == 'invoice.paid':
        subscription_id = data.get('subscription')
        if subscription_id:
            sub = stripe.Subscription.retrieve(subscription_id)
            sync_subscription(sub)

    elif event_type == 'invoice.payment_failed':
        subscription_id = data.get('subscription')
        if subscription_id:
            sub = stripe.Subscription.retrieve(subscription_id)
            sync_subscription(sub)

    return HttpResponse(status=200)
