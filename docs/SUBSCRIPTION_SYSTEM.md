# Subscription System Documentation

## Overview

The Training Platform now includes a comprehensive subscription management system with native Syrian bank payment gateway integration. This system supports multiple subscription tiers, automatic renewals, and secure payment processing.

## Architecture

### Core Components

1. **Subscription Models**
   - `SubscriptionPlan`: Defines available subscription tiers
   - `UserSubscription`: Manages user subscription relationships
   - `Payment`: Tracks payment transactions
   - `PaymentGateway`: Stores gateway configurations

2. **Payment Gateways**
   - Syriatel Cash (Mobile Money)
   - Al-Baraka Bank (Traditional Banking)
   - BEMO Bank (Digital Banking)

3. **Service Layer**
   - `SubscriptionService`: Core subscription logic
   - `PaymentService`: Payment processing
   - `GatewayService`: Gateway management

## Subscription Plans

### Available Plans

1. **Basic Plan**
   - Price: $9.99/month
   - Features: Basic diet plans, limited routines
   - Duration: 30 days

2. **Premium Plan**
   - Price: $19.99/month
   - Features: Advanced diet plans, unlimited routines, AI recommendations
   - Duration: 30 days

3. **Enterprise Plan**
   - Price: $49.99/month
   - Features: All Premium features + custom plans, priority support
   - Duration: 30 days

## Payment Gateway Integration

### Syriatel Cash

```python
# Configuration
SYRIATEL_API_KEY = "your_api_key"
SYRIATEL_SECRET_KEY = "your_secret_key"
SYRIATEL_WEBHOOK_URL = "https://yourdomain.com/webhooks/syriatel/"

# Usage
from subscription.services.syriatel import SyriatelCashGateway

gateway = SyriatelCashGateway()
payment = gateway.initiate_payment(amount=1000, currency="SYP")
```

### Al-Baraka Bank

```python
# Configuration
ALBARAKA_API_KEY = "your_api_key"
ALBARAKA_SECRET_KEY = "your_secret_key"
ALBARAKA_WEBHOOK_URL = "https://yourdomain.com/webhooks/albaraka/"

# Usage
from subscription.services.albaraka import AlBarakaGateway

gateway = AlBarakaGateway()
payment = gateway.initiate_payment(amount=1000, currency="SYP")
```

### BEMO Bank

```python
# Configuration
BEMO_API_KEY = "your_api_key"
BEMO_SECRET_KEY = "your_secret_key"
BEMO_WEBHOOK_URL = "https://yourdomain.com/webhooks/bemo/"

# Usage
from subscription.services.bemo import BEMOGateway

gateway = BEMOGateway()
payment = gateway.initiate_payment(amount=1000, currency="SYP")
```

## API Endpoints

### Subscription Management

#### Create Subscription
```http
POST /api/subscriptions/create/
Content-Type: application/json

{
    "plan_id": 1,
    "payment_method": "syriatel",
    "amount": 1000
}
```

#### Get User Subscription
```http
GET /api/subscriptions/user-subscription/
Authorization: Bearer <token>
```

#### Cancel Subscription
```http
POST /api/subscriptions/cancel/
Authorization: Bearer <token>

{
    "reason": "Too expensive"
}
```

#### Upgrade Subscription
```http
POST /api/subscriptions/upgrade/
Authorization: Bearer <token>

{
    "new_plan_id": 2
}
```

### Payment Processing

#### Initiate Payment
```http
POST /api/payments/initiate/
Content-Type: application/json

{
    "amount": 1000,
    "currency": "SYP",
    "gateway": "syriatel",
    "description": "Premium subscription"
}
```

#### Verify Payment
```http
POST /api/payments/verify/
Content-Type: application/json

{
    "payment_id": "payment_123",
    "gateway": "syriatel"
}
```

#### Payment History
```http
GET /api/payments/history/
Authorization: Bearer <token>
```

### Webhook Endpoints

#### Syriatel Webhook
```http
POST /api/webhooks/syriatel/
Content-Type: application/json

{
    "payment_id": "payment_123",
    "status": "completed",
    "amount": 1000,
    "signature": "webhook_signature"
}
```

#### Al-Baraka Webhook
```http
POST /api/webhooks/albaraka/
Content-Type: application/json

{
    "transaction_id": "txn_456",
    "status": "success",
    "amount": 1000,
    "signature": "webhook_signature"
}
```

#### BEMO Webhook
```http
POST /api/webhooks/bemo/
Content-Type: application/json

{
    "payment_reference": "ref_789",
    "status": "completed",
    "amount": 1000,
    "signature": "webhook_signature"
}
```

## Database Schema

### SubscriptionPlan
```sql
CREATE TABLE subscription_plan (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    price DECIMAL(10,2) NOT NULL,
    duration_days INTEGER NOT NULL,
    features JSONB,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
```

### UserSubscription
```sql
CREATE TABLE user_subscription (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES auth_user(id),
    plan_id INTEGER REFERENCES subscription_plan(id),
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    status VARCHAR(20) DEFAULT 'active',
    auto_renew BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
```

### Payment
```sql
CREATE TABLE payment (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES auth_user(id),
    subscription_id INTEGER REFERENCES user_subscription(id),
    amount DECIMAL(10,2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'SYP',
    gateway VARCHAR(50) NOT NULL,
    gateway_payment_id VARCHAR(255),
    status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
```

## Configuration

### Environment Variables

```env
# Payment Gateways
SYRIATEL_API_KEY=your_syriatel_api_key
SYRIATEL_SECRET_KEY=your_syriatel_secret_key
SYRIATEL_WEBHOOK_SECRET=your_webhook_secret

ALBARAKA_API_KEY=your_albaraka_api_key
ALBARAKA_SECRET_KEY=your_albaraka_secret_key
ALBARAKA_WEBHOOK_SECRET=your_webhook_secret

BEMO_API_KEY=your_bemo_api_key
BEMO_SECRET_KEY=your_bemo_secret_key
BEMO_WEBHOOK_SECRET=your_webhook_secret

# Webhook Configuration
WEBHOOK_BASE_URL=https://yourdomain.com/webhooks
WEBHOOK_TIMEOUT=30

# Payment Settings
PAYMENT_TIMEOUT=300
PAYMENT_RETRY_ATTEMPTS=3
PAYMENT_RETRY_DELAY=60

# Subscription Settings
SUBSCRIPTION_GRACE_PERIOD=7
SUBSCRIPTION_RENEWAL_REMINDER_DAYS=3
```

### Django Settings

```python
# settings.py

# Subscription Settings
SUBSCRIPTION_SETTINGS = {
    'GRACE_PERIOD_DAYS': 7,
    'RENEWAL_REMINDER_DAYS': 3,
    'AUTO_RENEWAL_ENABLED': True,
    'PAYMENT_TIMEOUT': 300,
    'RETRY_ATTEMPTS': 3,
    'RETRY_DELAY': 60,
}

# Payment Gateway Settings
PAYMENT_GATEWAYS = {
    'syriatel': {
        'enabled': True,
        'api_key': env('SYRIATEL_API_KEY'),
        'secret_key': env('SYRIATEL_SECRET_KEY'),
        'webhook_secret': env('SYRIATEL_WEBHOOK_SECRET'),
        'base_url': 'https://api.syriatel.com',
        'timeout': 30,
    },
    'albaraka': {
        'enabled': True,
        'api_key': env('ALBARAKA_API_KEY'),
        'secret_key': env('ALBARAKA_SECRET_KEY'),
        'webhook_secret': env('ALBARAKA_WEBHOOK_SECRET'),
        'base_url': 'https://api.albaraka.com',
        'timeout': 30,
    },
    'bemo': {
        'enabled': True,
        'api_key': env('BEMO_API_KEY'),
        'secret_key': env('BEMO_SECRET_KEY'),
        'webhook_secret': env('BEMO_WEBHOOK_SECRET'),
        'base_url': 'https://api.bemo.com',
        'timeout': 30,
    },
}
```

## Testing

### Unit Tests

```python
# test_subscription_models.py
from django.test import TestCase
from subscription.models import SubscriptionPlan, UserSubscription

class SubscriptionPlanTest(TestCase):
    def setUp(self):
        self.plan = SubscriptionPlan.objects.create(
            name="Basic Plan",
            price=9.99,
            duration_days=30
        )

    def test_plan_creation(self):
        self.assertEqual(self.plan.name, "Basic Plan")
        self.assertEqual(self.plan.price, 9.99)
```

### Integration Tests

```python
# test_payment_gateways.py
from django.test import TestCase
from subscription.services.syriatel import SyriatelCashGateway

class SyriatelGatewayTest(TestCase):
    def setUp(self):
        self.gateway = SyriatelCashGateway()

    def test_payment_initiation(self):
        payment = self.gateway.initiate_payment(amount=1000)
        self.assertIsNotNone(payment.gateway_payment_id)
```

### Management Commands

```bash
# Test all gateways
python manage.py test_gateway --all

# Test specific gateway
python manage.py test_gateway --gateway=syriatel

# Create test subscriptions
python manage.py create_test_subscriptions

# Sync subscription status
python manage.py sync_subscriptions
```

## Security

### Payment Data Protection

1. **Encryption**: All sensitive payment data is encrypted
2. **Webhook Verification**: All webhooks are verified using signatures
3. **API Key Management**: Secure storage and rotation of API keys
4. **Rate Limiting**: API endpoints are rate-limited to prevent abuse

### Webhook Security

```python
# Webhook verification
def verify_webhook_signature(payload, signature, secret):
    expected_signature = hmac.new(
        secret.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected_signature)
```

## Monitoring

### Logging

```python
import logging

logger = logging.getLogger('subscription')

# Payment logging
logger.info(f"Payment initiated: {payment_id}")

# Error logging
logger.error(f"Payment failed: {payment_id}, error: {error}")
```

### Health Checks

```python
# Gateway health check
def check_gateway_health(gateway_name):
    try:
        gateway = get_gateway(gateway_name)
        return gateway.health_check()
    except Exception as e:
        logger.error(f"Gateway {gateway_name} health check failed: {e}")
        return False
```

## Troubleshooting

### Common Issues

1. **Payment Timeout**
   - Check gateway connectivity
   - Verify API credentials
   - Check network configuration

2. **Webhook Failures**
   - Verify webhook URL accessibility
   - Check signature verification
   - Review webhook payload format

3. **Subscription Renewal Issues**
   - Check payment gateway status
   - Verify user payment method
   - Review subscription settings

### Debug Commands

```bash
# Check gateway status
python manage.py check_gateways

# View payment logs
python manage.py view_payment_logs --days=7

# Test webhook endpoints
python manage.py test_webhooks
```

## Support

For technical support:
- Email: tech-support@trainingplatform.com
- Documentation: [docs.trainingplatform.com](https://docs.trainingplatform.com)
- Issues: [GitHub Issues](https://github.com/m-ashmar/Training_platform/issues) 