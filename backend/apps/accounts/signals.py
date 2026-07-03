from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model

from apps.accounts.models import Organization, Membership

User = get_user_model()


@receiver(post_save, sender=User)
def create_org_on_signup(sender, instance, created, **kwargs):
    if created:
        org = Organization.objects.create(name=f"{instance.username}'s Organization")
        Membership.objects.create(org=org, user=instance, role=Membership.Role.OWNER)
