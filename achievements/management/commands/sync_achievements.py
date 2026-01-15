"""
Sync Achievements Management Command

Syncs achievement definitions from registry.py to the database.
Run: python manage.py sync_achievements
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from achievements.models import Achievement
from achievements.registry import ACHIEVEMENTS


class Command(BaseCommand):
    help = 'Sync achievement definitions from registry to database'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without making changes',
        )
        parser.add_argument(
            '--reset',
            action='store_true',
            help='Delete all achievements and recreate from registry',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        reset = options['reset']

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN - No changes will be made\n'))

        if reset and not dry_run:
            self.stdout.write('Deleting existing achievements...')
            Achievement.objects.all().delete()
            self.stdout.write(self.style.SUCCESS('Deleted all achievements\n'))

        created_count = 0
        updated_count = 0
        skipped_count = 0

        with transaction.atomic():
            for achievement_def in ACHIEVEMENTS:
                if dry_run:
                    # Just show what would be done
                    exists = Achievement.objects.filter(key=achievement_def.key).exists()
                    if exists:
                        self.stdout.write(f'  Would update: {achievement_def.name}')
                    else:
                        self.stdout.write(f'  Would create: {achievement_def.name}')
                    continue

                # Create or update achievement
                defaults = {
                    'name': achievement_def.name,
                    'description': achievement_def.description,
                    'category': achievement_def.category,
                    'criteria': achievement_def.to_criteria(),
                    'points': achievement_def.points,
                    'badge_color': achievement_def.badge_color,
                    'is_rare': achievement_def.is_rare,
                    'is_secret': achievement_def.is_secret,
                    'is_active': True,
                }

                achievement, created = Achievement.objects.update_or_create(
                    key=achievement_def.key,
                    defaults=defaults
                )

                if created:
                    created_count += 1
                    self.stdout.write(
                        self.style.SUCCESS(f'✅ Created: {achievement.name}')
                    )
                else:
                    updated_count += 1
                    self.stdout.write(
                        self.style.WARNING(f'🔄 Updated: {achievement.name}')
                    )

        # Summary
        self.stdout.write('\n' + '=' * 50)
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'DRY RUN complete. {len(ACHIEVEMENTS)} achievements would be synced.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'Sync complete!\n'
                f'  Created: {created_count}\n'
                f'  Updated: {updated_count}\n'
                f'  Total: {Achievement.objects.count()}'
            ))
