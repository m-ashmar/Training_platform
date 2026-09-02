from django.test import TestCase
from django.contrib.auth import get_user_model
from wallet.models import Wallet, Transaction
from subscription.models import Subscription, SubscriptionPlan, Payment
from decimal import Decimal

User = get_user_model()

class SubscriptionWalletIntegrationTests(TestCase):
    def setUp(self):
        # Create Trainer
        self.trainer = User.objects.create_user(
            username="trainer",
            email="trainer@example.com",
            password="password",
            phone_number="1234567890",
            user_type="trainer"
        )
        self.trainer_wallet, _ = Wallet.objects.get_or_create(owner=self.trainer, owner_type="trainer")

        # Create Client
        self.client = User.objects.create_user(
            username="client",
            email="client@example.com",
            password="password",
            phone_number="0987654321",
            user_type="client"
        )
        # Assign trainer to client
        self.client.assigned_trainer = self.trainer
        self.client.save()
        
        self.client_wallet, _ = Wallet.objects.get_or_create(owner=self.client, owner_type="client")

        # Create Plan
        self.plan = SubscriptionPlan.objects.create(
            name="Test Plan",
            description="A test plan description",
            price=Decimal("100.00"),
            duration_days=30
        )

        # Create Subscription
        self.subscription = Subscription.objects.create(
            user=self.client,
            plan=self.plan,
            status="pending"
        )

    def test_external_payment_triggers_topup_and_transfer(self):
        """
        Verify that when an external payment (e.g., Stripe) completes:
        1. A 'topup' Transaction is created (System -> Client Wallet)
        2. A 'transfer' Transaction is created (Client Wallet -> Trainer Wallet)
        3. Trainer wallet balance increases
        4. Client wallet balance remains neutral (received topup, then sent transfer)
        """
        initial_trainer_balance = self.trainer_wallet.balance
        initial_client_balance = self.client_wallet.balance

        # Create a completed Payment via 'stripe'
        payment = Payment.objects.create(
            subscription=self.subscription,
            amount=Decimal("100.00"),
            currency="USD",
            payment_method="stripe",
            status="completed"
        )

        # Refresh objects
        self.client_wallet.refresh_from_db()
        self.trainer_wallet.refresh_from_db()

        # Check balances
        self.assertEqual(self.client_wallet.balance, initial_client_balance, 
                         "Client outcome should be neutral (TopUp +100, Transfer -100)")
        self.assertEqual(self.trainer_wallet.balance, initial_trainer_balance + Decimal("100.00"), 
                         "Trainer should receive the funds")

        # Check Transactions
        # Should have 2 transactions related to this flow
        # 1. TopUp
        topups = Transaction.objects.filter(destination_wallet=self.client_wallet, tx_type="topup", amount=Decimal("100.00"))
        self.assertTrue(topups.exists(), "Should have a TopUp transaction for the client")
        
        # 2. Transfer
        transfers = Transaction.objects.filter(source_wallet=self.client_wallet, destination_wallet=self.trainer_wallet, tx_type="transfer", amount=Decimal("100.00"))
        self.assertTrue(transfers.exists(), "Should have a Transfer transaction from client to trainer")
