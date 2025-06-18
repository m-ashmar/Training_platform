# Payment Gateway Integration Documentation

## Overview

This document provides comprehensive information about the payment gateway integration system for the Training Platform. The system supports multiple Syrian payment gateways including Syriatel Cash, Al-Baraka Bank, and BEMO Bank.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Supported Payment Methods](#supported-payment-methods)
3. [Configuration](#configuration)
4. [API Endpoints](#api-endpoints)
5. [Webhook Integration](#webhook-integration)
6. [Testing](#testing)
7. [Production Deployment](#production-deployment)
8. [Troubleshooting](#troubleshooting)

## Architecture Overview

The payment system follows a modular, service-oriented architecture:

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Frontend      │    │   Backend API    │    │ Payment Gateway │
│   (Flutter)     │◄──►│   (Django)       │◄──►│   (Syrian       │
│                 │    │                  │    │   Banks)        │
└─────────────────┘    └──────────────────┘    └─────────────────┘
                              │
                              ▼
                       ┌──────────────────┐
                       │   Database       │
                       │   (SQLite/PostgreSQL) │
                       └──────────────────┘
```

### Key Components

- **PaymentGatewayService**: Main service orchestrating payment operations
- **Gateway Implementations**: Individual gateway classes (Syriatel, Baraka, BEMO)
- **Webhook Handler**: Processes payment notifications from gateways
- **Configuration System**: Environment-based gateway configuration
- **Frontend UI**: Flutter screens for payment flow

## Supported Payment Methods

### 1. Syriatel Cash
- **Type**: Mobile Wallet
- **Currencies**: SYP, USD
- **Limits**: 100 - 1,000,000 SYP
- **Features**: QR Code payments, mobile app integration

### 2. Al-Baraka Bank
- **Type**: Bank Transfer
- **Currencies**: SYP, USD
- **Limits**: 100 - 5,000,000 SYP
- **Features**: Bank account transfers, reference-based payments

### 3. BEMO Bank
- **Type**: Bank Transfer
- **Currencies**: SYP, USD
- **Limits**: 100 - 3,000,000 SYP
- **Features**: Bank account transfers, secure transactions

## Configuration

### Environment Variables

Set the following environment variables for production:

```bash
# Environment Mode
GATEWAY_MODE=production  # or 'sandbox'

# Syriatel Cash
SYRIATEL_PRODUCTION_API_KEY=your_api_key
SYRIATEL_PRODUCTION_API_SECRET=your_api_secret
SYRIATEL_PRODUCTION_WEBHOOK_SECRET=your_webhook_secret
SYRIATEL_PRODUCTION_MERCHANT_ID=your_merchant_id

# Al-Baraka Bank
BARAKA_PRODUCTION_API_KEY=your_api_key
BARAKA_PRODUCTION_API_SECRET=your_api_secret
BARAKA_PRODUCTION_WEBHOOK_SECRET=your_webhook_secret
BARAKA_PRODUCTION_MERCHANT_ID=your_merchant_id

# BEMO Bank
BEMO_PRODUCTION_API_KEY=your_api_key
BEMO_PRODUCTION_API_SECRET=your_api_secret
BEMO_PRODUCTION_WEBHOOK_SECRET=your_webhook_secret
BEMO_PRODUCTION_MERCHANT_ID=your_merchant_id
```

### Configuration File

The main configuration is in `subscription/settings/gateway_config.py`:

```python
# Gateway Registry
GATEWAY_REGISTRY = {
    'syriatel_cash': {
        'name': 'Syriatel Cash',
        'config': SYRIATEL_CONFIG,
        'class_name': 'SyriatelCashGateway',
        'supported_currencies': ['SYP', 'USD'],
        'min_amount': 100,
        'max_amount': 1000000,
    },
    # ... other gateways
}
```

## API Endpoints

### 1. Get Available Gateways

```http
GET /api/subscription/v1/gateways/
```

**Response:**
```json
{
  "success": true,
  "gateways": [
    {
      "name": "syriatel_cash",
      "display_name": "Syriatel Cash",
      "supported_currencies": ["SYP", "USD"],
      "min_amount": 100,
      "max_amount": 1000000,
      "enabled": true
    }
  ]
}
```

### 2. Initiate Payment

```http
POST /api/subscription/v1/gateways/
Content-Type: application/json
Authorization: Bearer <token>

{
  "gateway": "syriatel_cash",
  "amount": "1000.00",
  "currency": "SYP",
  "subscription_id": "uuid"
}
```

**Response:**
```json
{
  "success": true,
  "payment_id": "uuid",
  "reference": "SYRIATEL_CASH_1234567890_1234",
  "payment_url": "https://syriatel.sy/pay/transaction_id",
  "expires_at": 1234567890
}
```

### 3. Check Payment Status

```http
GET /api/subscription/v1/payments/{payment_id}/status/
Authorization: Bearer <token>
```

**Response:**
```json
{
  "success": true,
  "payment_id": "uuid",
  "status": "completed",
  "amount": "1000.00",
  "currency": "SYP",
  "payment_method": "syriatel_cash",
  "created_at": "2024-01-01T12:00:00Z",
  "updated_at": "2024-01-01T12:05:00Z"
}
```

### 4. Webhook Endpoint

```http
POST /api/subscription/webhook/{gateway_name}/
Content-Type: application/json
X-Gateway-Signature: <signature>
X-Gateway-Timestamp: <timestamp>
```

**Expected Webhook Payload (Syriatel):**
```json
{
  "transaction_id": "SYRIATEL_123456789",
  "reference": "SYRIATEL_CASH_1234567890_1234",
  "status": "completed",
  "amount": 1000.00,
  "currency": "SYP",
  "timestamp": 1234567890,
  "signature": "abc123..."
}
```

## Webhook Integration

### Webhook Security

All webhooks are verified using HMAC-SHA256 signatures:

```python
# Signature verification
expected_signature = hmac.new(
    webhook_secret.encode(),
    payload,
    hashlib.sha256
).hexdigest()

is_valid = hmac.compare_digest(signature, expected_signature)
```

### Webhook Processing

1. **Signature Verification**: Validate webhook authenticity
2. **Payload Parsing**: Extract payment data
3. **Status Update**: Update payment and subscription status
4. **Logging**: Record all webhook activities

### Webhook Headers

| Header | Description | Required |
|--------|-------------|----------|
| `X-Gateway-Signature` | HMAC signature | Yes |
| `X-Gateway-Timestamp` | Unix timestamp | Yes |
| `Content-Type` | application/json | Yes |

## Testing

### Management Command

Use the Django management command to test gateways:

```bash
# List available gateways
python manage.py test_gateway --list-gateways

# Test specific gateway
python manage.py test_gateway --gateway syriatel_cash --amount 1000

# Test with webhook simulation
python manage.py test_gateway --gateway syriatel_cash --test-webhook

# Test all gateways
python manage.py test_gateway --amount 1000 --currency SYP
```

### Manual Testing

1. **Payment Initiation Test:**
   ```bash
   curl -X POST http://localhost:8000/api/subscription/v1/gateways/ \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{
       "gateway": "syriatel_cash",
       "amount": "1000.00",
       "currency": "SYP",
       "subscription_id": "uuid"
     }'
   ```

2. **Webhook Test:**
   ```bash
   curl -X POST http://localhost:8000/api/subscription/webhook/syriatel_cash/ \
     -H "Content-Type: application/json" \
     -H "X-Gateway-Signature: test_signature" \
     -H "X-Gateway-Timestamp: 1234567890" \
     -d '{
       "transaction_id": "SYRIATEL_123456789",
       "reference": "SYRIATEL_CASH_1234567890_1234",
       "status": "completed",
       "amount": 1000.00,
       "currency": "SYP",
       "timestamp": 1234567890
     }'
   ```

## Production Deployment

### 1. Environment Setup

```bash
# Set production environment
export GATEWAY_MODE=production

# Configure real API credentials
export SYRIATEL_PRODUCTION_API_KEY=real_api_key
export SYRIATEL_PRODUCTION_API_SECRET=real_api_secret
export SYRIATEL_PRODUCTION_WEBHOOK_SECRET=real_webhook_secret
export SYRIATEL_PRODUCTION_MERCHANT_ID=real_merchant_id
# ... repeat for other gateways
```

### 2. Database Migration

```bash
# Apply payment model changes
python manage.py makemigrations subscription
python manage.py migrate
```

### 3. Gateway Configuration

Update gateway implementations with real API endpoints:

```python
# In subscription/gateways/syriatel.py
def _make_api_call(self, endpoint, method='GET', data=None):
    # Replace placeholder with real HTTP client
    import requests
    
    headers = {
        'Authorization': f'Bearer {self.api_key}',
        'Content-Type': 'application/json',
        'X-Merchant-ID': self.merchant_id
    }
    
    url = f"{self.api_url}{endpoint}"
    
    if method == 'GET':
        response = requests.get(url, headers=headers, timeout=self.timeout)
    else:
        response = requests.post(url, headers=headers, json=data, timeout=self.timeout)
    
    response.raise_for_status()
    return response.json()
```

### 4. Webhook URL Configuration

Configure webhook URLs in your payment gateway dashboards:

```
https://yourdomain.com/api/subscription/webhook/syriatel_cash/
https://yourdomain.com/api/subscription/webhook/baraka_bank/
https://yourdomain.com/api/subscription/webhook/bemo_bank/
```

### 5. SSL Certificate

Ensure HTTPS is enabled for webhook security:

```python
# In settings.py
SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
```

## Frontend Integration

### Flutter Implementation

The Flutter app includes three main payment screens:

1. **PaymentMethodSelector**: Choose payment method
2. **PaymentSummary**: Review and confirm payment
3. **PaymentStatus**: Real-time payment status tracking

### Usage Example

```dart
// Navigate to payment method selector
Navigator.push(
  context,
  MaterialPageRoute(
    builder: (context) => PaymentMethodSelector(
      amount: 1000.0,
      currency: 'SYP',
      availableMethods: getMockPaymentMethods(),
      onMethodSelected: (method) {
        // Handle method selection
      },
    ),
  ),
);
```

## Troubleshooting

### Common Issues

1. **Webhook Not Received**
   - Check webhook URL configuration
   - Verify signature validation
   - Check server logs for errors

2. **Payment Initiation Fails**
   - Verify API credentials
   - Check amount limits
   - Validate currency support

3. **Status Not Updated**
   - Check webhook processing
   - Verify database transactions
   - Review payment reference matching

### Debug Mode

Enable debug mode for detailed logging:

```bash
export PAYMENT_DEBUG=True
```

### Log Files

Check payment logs in:
```
logs/subscription.log
```

### Gateway-Specific Issues

#### Syriatel Cash
- Verify QR code generation
- Check mobile app integration
- Validate transaction references

#### Al-Baraka Bank
- Confirm bank account details
- Verify transfer references
- Check bank API connectivity

#### BEMO Bank
- Validate account credentials
- Check transfer limits
- Verify API endpoints

## Security Considerations

1. **API Key Management**: Store credentials securely
2. **Webhook Verification**: Always verify signatures
3. **HTTPS Only**: Use SSL for all communications
4. **Rate Limiting**: Implement request throttling
5. **Input Validation**: Validate all payment data
6. **Audit Logging**: Log all payment activities

## Support

For technical support or questions about payment integration:

1. Check the logs for error details
2. Use the test command to verify gateway connectivity
3. Review the webhook processing logs
4. Contact the development team with specific error messages

## Future Enhancements

1. **Additional Gateways**: Support for more Syrian banks
2. **Recurring Payments**: Automatic subscription renewals
3. **Refund Processing**: Handle payment refunds
4. **Analytics Dashboard**: Payment analytics and reporting
5. **Multi-Currency**: Enhanced currency support
6. **Mobile SDK**: Native mobile payment SDKs 