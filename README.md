# Training Platform

A comprehensive Django-based training platform with advanced subscription management and Syrian bank payment gateway integration. This project is currently under active development.

## Features

### Core Platform
- **User Management**: Complete user registration, authentication, and profile management
- **Diet Planning**: AI-powered meal planning and nutrition tracking
- **Workout Routines**: Customizable exercise routines and progress tracking
- **Challenges**: Interactive fitness challenges and competitions

### Subscription System (In Development)
- **Multi-tier Subscriptions**: Basic, Premium, and Enterprise plans
- **Syrian Bank Integration**: Native support for Syrian payment gateways
  - Syriatel Cash
  - Al-Baraka Bank
  - BEMO Bank
- **Payment Processing**: Secure payment handling with webhook support
- **Subscription Management**: Automatic renewals, cancellations, and upgrades
- **Admin Interface**: Comprehensive admin panel for subscription management

## Architecture

### Backend (Django)
- **Django 4.2+**: Modern web framework
- **Django REST Framework**: RESTful API endpoints
- **Celery**: Asynchronous task processing
- **PostgreSQL/MySQL**: Database support
- **Redis**: Caching and session management

### Frontend (Flutter)
- **Cross-platform**: iOS and Android support
- **Modern UI**: Material Design 3 components
- **Payment Integration**: Native payment flow screens
- **Real-time Updates**: WebSocket integration

### Payment Gateways
- **Syriatel Cash**: Mobile money integration
- **Al-Baraka Bank**: Traditional banking support
- **BEMO Bank**: Digital banking solutions
- **Webhook System**: Real-time payment notifications
- **Error Handling**: Comprehensive error management

## Installation

### Prerequisites
- Python 3.8+
- Node.js 16+
- Flutter SDK
- PostgreSQL/MySQL
- Redis

### Backend Setup
```bash
# Clone the repository
git clone https://github.com/m-ashmar/Training_platform.git
cd Training_platform

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Environment configuration
cp .env.example .env
# Edit .env with your configuration

# Database setup
python manage.py migrate
python manage.py createsuperuser

# Run development server
python manage.py runserver
```

### Frontend Setup
```bash
cd flutter
flutter pub get
flutter run
```

## Configuration

### Environment Variables
```env
# Database
DATABASE_URL=postgresql://user:password@localhost:5432/training_platform

# Redis
REDIS_URL=redis://localhost:6379

# Payment Gateways
SYRIATEL_API_KEY=your_syriatel_api_key
SYRIATEL_SECRET_KEY=your_syriatel_secret_key
ALBARAKA_API_KEY=your_albaraka_api_key
ALBARAKA_SECRET_KEY=your_albaraka_secret_key
BEMO_API_KEY=your_bemo_api_key
BEMO_SECRET_KEY=your_bemo_secret_key

# Webhook URLs
WEBHOOK_BASE_URL=https://yourdomain.com/webhooks

# Security
SECRET_KEY=your_django_secret_key
DEBUG=True
```

### Payment Gateway Setup
Each payment gateway requires specific configuration:

#### Syriatel Cash
- API credentials from Syriatel developer portal
- Webhook endpoint configuration
- Test environment setup

#### Al-Baraka Bank
- Merchant account setup
- API integration credentials
- Transaction monitoring

#### BEMO Bank
- Digital banking integration
- API authentication
- Payment verification

## API Documentation

### Subscription Endpoints
```
POST /api/subscriptions/create/
GET /api/subscriptions/plans/
GET /api/subscriptions/user-subscription/
POST /api/subscriptions/cancel/
POST /api/subscriptions/upgrade/
```

### Payment Endpoints
```
POST /api/payments/initiate/
POST /api/payments/verify/
GET /api/payments/history/
POST /api/webhooks/syriatel/
POST /api/webhooks/albaraka/
POST /api/webhooks/bemo/
```

## Testing

### Run All Tests
```bash
python manage.py test
```

### Test Payment Gateways
```bash
python manage.py test_gateway --gateway=syriatel
python manage.py test_gateway --gateway=albaraka
python manage.py test_gateway --gateway=bemo
```

### Test Subscription Flow
```bash
python manage.py test subscription.tests
```

## Database Schema

### Subscription Models
- `SubscriptionPlan`: Plan definitions and pricing
- `UserSubscription`: User subscription relationships
- `Payment`: Payment transaction records
- `PaymentGateway`: Gateway configuration
- `PaymentGatewayResponse`: Gateway response logs
- `PaymentGatewayError`: Error tracking

### Core Models
- `CustomUser`: Extended user model
- `DietPlan`: User diet plans
- `Routine`: Workout routines
- `Challenge`: Fitness challenges

## Security Features

- **JWT Authentication**: Secure token-based authentication
- **Payment Encryption**: All payment data encrypted
- **Webhook Verification**: Signed webhook validation
- **Rate Limiting**: API rate limiting protection
- **CORS Configuration**: Cross-origin request security

## Development Status

### Current Development
- **Subscription System**: Core functionality implemented, testing in progress
- **Payment Gateways**: Integration completed, requires real API credentials
- **Frontend Integration**: Flutter screens created, needs backend integration
- **Testing**: Comprehensive test suite implemented
- **Documentation**: Complete documentation available

### Next Steps
- Complete frontend-backend integration
- Implement real payment gateway testing
- Performance optimization
- Security audit
- Production deployment preparation

## Contributing

This project is currently in active development. For collaboration:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests
5. Submit a pull request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Recent Development Updates

### Major Features Added
- **Complete Subscription System**: Multi-tier subscription management
- **Syrian Payment Integration**: Native support for local banks
- **Webhook Infrastructure**: Real-time payment notifications
- **Admin Interface**: Comprehensive subscription management
- **Flutter Frontend**: Modern mobile payment flow
- **Testing Suite**: Comprehensive test coverage

### Technical Improvements
- **Service Layer Architecture**: Clean separation of concerns
- **Dynamic Configuration**: Environment-based gateway selection
- **Error Handling**: Robust error management system
- **Documentation**: Complete API and setup documentation
- **Security**: Enhanced payment security measures

### Payment Gateway Features
- **Syriatel Cash**: Mobile money integration with webhook support
- **Al-Baraka Bank**: Traditional banking with secure API
- **BEMO Bank**: Digital banking with real-time verification
- **Unified Interface**: Consistent API across all gateways
- **Transaction Logging**: Complete payment audit trail

## Support

For development support and questions:
- Email: dev-support@trainingplatform.com
- Documentation: [docs.trainingplatform.com](https://docs.trainingplatform.com)
- Issues: [GitHub Issues](https://github.com/m-ashmar/Training_platform/issues)

## Acknowledgments

- Django community for the excellent framework
- Syrian banking partners for payment gateway integration
- Flutter team for cross-platform development tools
- All contributors and development team members 