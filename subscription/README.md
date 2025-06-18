# Subscription App

A comprehensive subscription management system for the training platform, handling user subscriptions, plans, payments, and feature access control.

## Features

### Core Functionality
- **Subscription Plans**: Multiple tiers with different features and pricing
- **User Subscriptions**: Individual user subscription management
- **Payment Tracking**: Complete payment history and status tracking
- **Feature Access Control**: Granular permissions based on subscription level
- **Usage Tracking**: Monitor feature usage against limits
- **Trial Management**: Free trial subscriptions with automatic expiration
- **Auto-renewal**: Configurable automatic subscription renewal

### Subscription Plans
- **Basic Plan** ($9.99/month): Diet and routine access
- **Premium Plan** ($19.99/month): Full access to diet, routines, challenges, and AI advice
- **Professional Plan** ($39.99/month): All features with priority support
- **Enterprise Plan** ($99.99/month): Unlimited access for organizations

## Models

### SubscriptionPlan
- Defines available subscription tiers
- Includes feature flags and usage limits
- Configurable pricing and duration

### Subscription
- Links users to their subscription plan
- Tracks subscription status and dates
- Handles trial periods and auto-renewal

### Payment
- Records all payment transactions
- Supports multiple payment methods
- Tracks payment status and metadata

### SubscriptionFeature
- Individual features that can be enabled/disabled
- Used for granular access control

### SubscriptionUsage
- Tracks usage of subscription features
- Enforces usage limits per subscription plan

## API Endpoints

### Subscription Plans
```
GET /api/subscription/v1/plans/           # List all plans
GET /api/subscription/v1/plans/{id}/      # Get specific plan
GET /api/subscription/v1/plans/available/ # Get available plans
```

### User Subscriptions
```
GET    /api/subscription/v1/subscriptions/           # List user subscriptions
POST   /api/subscription/v1/subscriptions/           # Create subscription
GET    /api/subscription/v1/subscriptions/{id}/      # Get subscription details
PUT    /api/subscription/v1/subscriptions/{id}/      # Update subscription
DELETE /api/subscription/v1/subscriptions/{id}/      # Cancel subscription
POST   /api/subscription/v1/subscriptions/{id}/cancel/ # Cancel with reason
POST   /api/subscription/v1/subscriptions/{id}/renew/  # Renew subscription
GET    /api/subscription/v1/subscriptions/current/   # Get current subscription
GET    /api/subscription/v1/subscriptions/{id}/usage/ # Get usage stats
```

### Payments
```
GET    /api/subscription/v1/payments/           # List payments
POST   /api/subscription/v1/payments/           # Create payment
GET    /api/subscription/v1/payments/{id}/      # Get payment details
POST   /api/subscription/v1/payments/{id}/confirm/ # Confirm payment
```

### Access Control
```
POST /api/subscription/v1/access/check/        # Check feature access
```

### Admin Management
```
GET  /api/subscription/v1/admin/management/    # Get subscription statistics
POST /api/subscription/v1/admin/management/    # Create trial subscription
```

## Permissions

### Custom Permission Classes
- `HasActiveSubscription`: Requires active subscription
- `HasDietAccess`: Requires diet feature access
- `HasRoutineAccess`: Requires routine feature access
- `HasChallengesAccess`: Requires challenges feature access
- `HasAIAdviceAccess`: Requires AI advice feature access
- `HasPrioritySupportAccess`: Requires priority support access
- `MealUsageLimit`: Checks daily meal creation limits
- `RoutineUsageLimit`: Checks routine creation limits

## Usage Examples

### Creating a Subscription
```python
from subscription.utils import create_trial_subscription

# Create a 7-day trial subscription
subscription = create_trial_subscription(user, plan_id, trial_days=7)
```

### Checking Access
```python
from subscription.utils import check_subscription_access

# Check if user has access to diet and routine features
access = check_subscription_access(user, ['diet', 'routine'])
if access['has_access']:
    # User can access these features
    pass
```

### Tracking Usage
```python
from subscription.utils import track_feature_usage

# Track meal creation
track_feature_usage(user, 'daily_meals', increment=1)
```

### Using Permissions in Views
```python
from subscription.permissions import HasDietAccess

class DietViewSet(viewsets.ModelViewSet):
    permission_classes = [HasDietAccess]
    # ... rest of view
```

## Setup

### 1. Run Migrations
```bash
python manage.py makemigrations subscription
python manage.py migrate
```

### 2. Create Default Plans
```bash
python manage.py setup_subscription_plans
```

### 3. Add to Main URLs
Add to your main `urls.py`:
```python
path('api/subscription/', include('subscription.urls')),
```

### 4. Update Settings
Add to `INSTALLED_APPS`:
```python
INSTALLED_APPS = [
    # ...
    'subscription',
    # ...
]
```

## Admin Interface

The subscription app provides a comprehensive admin interface with:

- **Subscription Plans**: Manage plan features and pricing
- **Subscriptions**: View and manage user subscriptions
- **Payments**: Track payment history and status
- **Features**: Manage available subscription features
- **Usage**: Monitor feature usage across subscriptions

## Scheduled Tasks

### Recommended Cron Jobs
```bash
# Daily: Expire subscriptions
python manage.py shell -c "from subscription.utils import expire_subscriptions; expire_subscriptions()"

# Daily: Send notifications
python manage.py shell -c "from subscription.utils import send_subscription_notifications; send_subscription_notifications()"
```

## Integration with Other Apps

### Diet App Integration
```python
from subscription.permissions import HasDietAccess

class DietPlanViewSet(viewsets.ModelViewSet):
    permission_classes = [HasDietAccess]
    # ... rest of view
```

### Routine App Integration
```python
from subscription.permissions import HasRoutineAccess

class RoutineViewSet(viewsets.ModelViewSet):
    permission_classes = [HasRoutineAccess]
    # ... rest of view
```

## Testing

Run tests with:
```bash
python manage.py test subscription
```

## Contributing

When adding new features:
1. Update models with new fields
2. Create migrations
3. Update serializers
4. Add API endpoints
5. Update permissions if needed
6. Add admin interface
7. Update documentation

## Security Considerations

- All sensitive operations use database transactions
- Payment confirmations should be validated via webhooks
- Subscription status is checked on every access
- Usage limits are enforced at the permission level
- Admin actions are logged for audit purposes 