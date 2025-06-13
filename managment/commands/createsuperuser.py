from django.contrib.auth.management.commands.createsuperuser import Command as BaseCreateSuperuserCommand
from django.core.management import CommandError

class Command(BaseCreateSuperuserCommand):
    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument('--phone_number', required=False, help='Phone number for the superuser')

    def handle(self, *args, **options):
        email = options.get('email')
        username = options.get('username')
        phone_number = options.get('phone_number')
        password = options.get('password')

        if not email:
            raise CommandError("The Email field must be set")
        if not username:
            raise CommandError("The Username field must be set")
        if not phone_number:
            raise CommandError("The Phone Number field must be set")

        self.UserModel._default_manager.create_superuser(
            email=email, username=username, phone_number=phone_number, password=password
        )
        self.stdout.write(self.style.SUCCESS('Superuser created successfully'))