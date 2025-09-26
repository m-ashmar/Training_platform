from django.core.management.base import BaseCommand
from users.models import CustomUser, TrainerClientRelation
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Approve trainer-client relationship for testing purposes'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--trainer-id',
            type=int,
            help='Trainer user ID',
        )
        parser.add_argument(
            '--client-id', 
            type=int,
            help='Client user ID',
        )
        parser.add_argument(
            '--auto-latest',
            action='store_true',
            help='Automatically approve the latest trainer-client relationship',
        )
    
    def handle(self, *args, **options):
        if options['auto_latest']:
            # Get the latest trainer-client relationship
            try:
                latest_relation = TrainerClientRelation.objects.filter(
                    status='pending'
                ).order_by('-created_at').first()
                
                if latest_relation:
                    trainer_id = latest_relation.trainer.id
                    client_id = latest_relation.client.id
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'Found latest pending relationship: Trainer {trainer_id} -> Client {client_id}'
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.WARNING('No pending trainer-client relationships found')
                    )
                    return
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f'Error finding latest relationship: {str(e)}')
                )
                return
        else:
            trainer_id = options['trainer_id']
            client_id = options['client_id']
            
            if not trainer_id or not client_id:
                self.stdout.write(
                    self.style.ERROR('Both --trainer-id and --client-id are required')
                )
                return
        
        try:
            # Get the relationship and approve it
            relation = TrainerClientRelation.objects.get(
                trainer_id=trainer_id,
                client_id=client_id
            )
            
            if relation.status == 'approved':
                self.stdout.write(
                    self.style.WARNING(
                        f'Relationship between trainer {trainer_id} and client {client_id} is already approved'
                    )
                )
                return
            
            relation.status = 'approved'
            relation.save()
            
            self.stdout.write(
                self.style.SUCCESS(
                    f'✅ Successfully approved relationship between trainer {trainer_id} and client {client_id}'
                )
            )
            
            logger.info(f'Management command approved trainer {trainer_id} -> client {client_id} relationship')
            
        except TrainerClientRelation.DoesNotExist:
            self.stdout.write(
                self.style.ERROR(
                    f'No relationship found between trainer {trainer_id} and client {client_id}'
                )
            )
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'Error approving relationship: {str(e)}')
            ) 