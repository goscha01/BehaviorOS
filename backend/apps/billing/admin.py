from django.contrib import admin
from apps.billing.models import StripeCustomer, Subscription


@admin.register(StripeCustomer)
class StripeCustomerAdmin(admin.ModelAdmin):
    list_display = ('org', 'stripe_customer_id', 'created_at')
    search_fields = ('org__name', 'stripe_customer_id')


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ('org', 'plan', 'status', 'current_period_end', 'cancel_at_period_end')
    list_filter = ('plan', 'status')
    search_fields = ('org__name', 'stripe_subscription_id')
