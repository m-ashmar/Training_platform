"""
Management command to backfill missing RoutineProgress records for all users.

This command ensures that every user assigned to a routine has RoutineProgress
records for each day of the routine.

Usage:
    python manage.py backfill_routine_progress
    python manage.py backfill_routine_progress --dry-run
    python manage.py backfill_routine_progress --user-id=57
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from routine.models import Routine, RoutineProgress
from users.models import CustomUser


class Command(BaseCommand):
    help = 'Backfill missing RoutineProgress records for all users assigned to routines'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be created without actually creating records',
        )
        parser.add_argument(
            '--user-id',
            type=int,
            help='Only process a specific user by ID',
        )
        parser.add_argument(
            '--routine-id',
            type=int,
            help='Only process a specific routine by ID',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        user_id = options.get('user_id')
        routine_id = options.get('routine_id')

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No changes will be made'))

        # Build queryset
        routines = Routine.objects.filter(is_active=True).prefetch_related('assigned_to')
        
        if routine_id:
            routines = routines.filter(id=routine_id)
            self.stdout.write(f'Filtering to routine ID: {routine_id}')

        total_created = 0
        total_skipped = 0
        users_processed = set()

        with transaction.atomic():
            for routine in routines:
                assigned_users = routine.assigned_to.all()
                
                if user_id:
                    assigned_users = assigned_users.filter(id=user_id)

                for user in assigned_users:
                    users_processed.add(user.id)
                    
                    for day in range(1, routine.days + 1):
                        # Check if record exists
                        exists = RoutineProgress.objects.filter(
                            user=user,
                            routine=routine,
                            day=day
                        ).exists()

                        if exists:
                            total_skipped += 1
                            continue

                        if dry_run:
                            self.stdout.write(
                                f'  Would create: {user.username} - {routine.name} Day {day}'
                            )
                        else:
                            RoutineProgress.objects.create(
                                user=user,
                                routine=routine,
                                day=day,
                                status='Not Started',
                                exercises_completed=0,
                                total_exercises=routine.routine_exercises.filter(day=day).count()
                            )
                            self.stdout.write(
                                self.style.SUCCESS(
                                    f'  Created: {user.username} - {routine.name} Day {day}'
                                )
                            )
                        
                        total_created += 1

            if dry_run:
                # Rollback in dry run mode
                transaction.set_rollback(True)

        # Summary
        self.stdout.write('')
        self.stdout.write(self.style.HTTP_INFO('=' * 50))
        self.stdout.write(self.style.HTTP_INFO('SUMMARY'))
        self.stdout.write(self.style.HTTP_INFO('=' * 50))
        self.stdout.write(f'Users processed: {len(users_processed)}')
        self.stdout.write(f'Records created: {total_created}')
        self.stdout.write(f'Records skipped (already exist): {total_skipped}')
        
        if dry_run:
            self.stdout.write(self.style.WARNING('\nDRY RUN - No actual changes were made'))
        else:
            self.stdout.write(self.style.SUCCESS(f'\nSuccessfully created {total_created} RoutineProgress records'))
