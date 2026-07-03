from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model

from apps.accounts.models import Organization, Membership
from apps.training.models import BusinessProfile, ScenarioTemplate, Script

User = get_user_model()


class Command(BaseCommand):
    help = 'Seed the database with demo data'

    def handle(self, *args, **options):
        # Create demo user (signal auto-creates org)
        user, created = User.objects.get_or_create(
            username='demo',
            defaults={'email': 'demo@behavioros.com'},
        )
        if created:
            user.set_password('demo1234')
            user.save()
            self.stdout.write(self.style.SUCCESS('Created demo user (demo / demo1234)'))
        else:
            self.stdout.write('Demo user already exists')

        org = user.memberships.first().org
        org.name = 'Demo Dispatch Co.'
        org.save()

        # Business Profile
        profile, _ = BusinessProfile.objects.get_or_create(
            org=org,
            name='Demo HVAC Company',
            defaults={
                'service_desc': 'Full-service HVAC repair, installation, and maintenance for residential and commercial properties.',
                'coverage_area': 'Greater Metro Area (50 mile radius)',
                'hours': 'Mon-Fri 7am-7pm, Sat 8am-2pm, Emergency 24/7',
                'pricing_notes': 'Service call: $89. Diagnostic: free with repair. After-hours emergency: $149 dispatch fee.',
                'policies': {
                    'cancellation': '24-hour notice required for free cancellation',
                    'warranty': '1-year warranty on all repairs',
                    'payment': 'Due at time of service. We accept all major cards.',
                },
            },
        )
        self.stdout.write(self.style.SUCCESS(f'Business profile: {profile.name}'))

        # Scenario Template
        scenario, _ = ScenarioTemplate.objects.get_or_create(
            org=org,
            name='Inbound Service Call - AC Not Working',
            defaults={
                'system_prompt': (
                    "You are a homeowner calling about your air conditioning unit that stopped working. "
                    "You are frustrated because it's been hot for two days. You want someone to come today "
                    "if possible. You will ask about pricing, availability, and warranty. If the dispatcher "
                    "is professional and helpful, you'll agree to schedule. If they seem unsure or give "
                    "vague answers, push back and ask more questions."
                ),
                'difficulty': 'medium',
                'intent': 'Schedule AC repair service call',
                'rubric': {
                    'must_confirm': ['address', 'phone_number', 'preferred_time'],
                    'must_mention': ['pricing', 'warranty', 'what_to_expect'],
                    'fail_if': ['overpromise_timeline', 'wrong_pricing', 'rude_behavior'],
                },
                'is_default': True,
            },
        )
        self.stdout.write(self.style.SUCCESS(f'Scenario: {scenario.name}'))

        # Script
        script, _ = Script.objects.get_or_create(
            org=org,
            name='Standard Inbound Call Script',
            defaults={
                'content': (
                    "Greeting: 'Thank you for calling [Company Name], this is [Your Name]. "
                    "How can I help you today?'\n\n"
                    "Identify Issue: Ask what's happening, when it started, and the type/age of equipment.\n\n"
                    "Empathize: Acknowledge their frustration. 'I understand how uncomfortable that must be.'\n\n"
                    "Offer Solution: 'We can get a technician out to diagnose the issue. "
                    "Our service call fee is $89, and the diagnostic is free if you proceed with the repair.'\n\n"
                    "Collect Info: Full name, address, phone number, preferred appointment time.\n\n"
                    "Confirm: Repeat back all details. Mention the 1-year warranty on repairs.\n\n"
                    "Close: 'You'll receive a confirmation text. Is there anything else I can help with?'"
                ),
                'version': 1,
            },
        )
        self.stdout.write(self.style.SUCCESS(f'Script: {script.name}'))

        self.stdout.write(self.style.SUCCESS('\nSeed complete! Login: demo / demo1234'))
