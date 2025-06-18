"""
Payment Gateway Service
=======================

This module provides a unified interface for all payment gateways.
It handles payment initiation, webhook verification, and status updates.
"""

import logging
import time
import hashlib
import hmac
from typing import Dict, Any, Optional, Tuple
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from ..settings.gateway_config import (
    get_gateway_config, get_gateway_info, is_gateway_enabled,
    GATEWAY_MODE, DEBUG_MODE, SECURITY_CONFIG
)

logger = logging.getLogger(__name__)

class PaymentGatewayError(Exception):
    """Base exception for payment gateway errors."""
    pass

class PaymentGatewayService:
    """
    Main service class for handling payment gateway operations.
    
    This class provides a unified interface for all payment gateways,
    handling payment initiation, webhook verification, and status updates.
    """
    
    def __init__(self, gateway_name: str):
        """
        Initialize the payment gateway service.
        
        Args:
            gateway_name: Name of the gateway (e.g., 'syriatel_cash')
        
        Raises:
            PaymentGatewayError: If gateway is not available or configured
        """
        self.gateway_name = gateway_name
        self.config = self._load_config()
        self.gateway = self._initialize_gateway()
        
        logger.info(f"Initialized {gateway_name} gateway in {GATEWAY_MODE} mode")
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration for the gateway."""
        try:
            return get_gateway_config(self.gateway_name)
        except ValueError as e:
            raise PaymentGatewayError(f"Configuration error: {str(e)}")
    
    def _initialize_gateway(self):
        """Initialize the specific gateway implementation."""
        try:
            # Dynamic import based on gateway name
            # Use only the prefix before the first underscore for the module name
            module_base = self.gateway_name.split('_')[0]
            module_name = f"subscription.gateways.{module_base}"
            class_name = get_gateway_info(self.gateway_name)['class_name']
            
            module = __import__(module_name, fromlist=[class_name])
            gateway_class = getattr(module, class_name)
            
            return gateway_class(self.config)
            
        except (ImportError, AttributeError) as e:
            logger.error(f"Failed to initialize gateway {self.gateway_name}: {str(e)}")
            raise PaymentGatewayError(f"Gateway initialization failed: {str(e)}")
    
    def initiate_payment(
        self, 
        amount: Decimal, 
        currency: str, 
        user_data: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Initiate a payment through the gateway.
        
        Args:
            amount: Payment amount
            currency: Payment currency (SYP, USD)
            user_data: User information (email, phone, name)
            metadata: Additional payment metadata
        
        Returns:
            Dictionary containing payment response
        
        Raises:
            PaymentGatewayError: If payment initiation fails
        """
        try:
            # Validate inputs
            self._validate_payment_request(amount, currency, user_data)
            
            # Prepare payment data
            payment_data = {
                'amount': float(amount),
                'currency': currency,
                'merchant_id': self.config['merchant_id'],
                'user_data': user_data,
                'metadata': metadata or {},
                'timestamp': int(time.time()),
                'reference': self._generate_reference(),
            }
            
            # Call gateway implementation
            response = self.gateway.initiate_payment(payment_data)
            
            # Log the request
            logger.info(
                f"Payment initiated: {self.gateway_name}, "
                f"Amount: {amount} {currency}, "
                f"Reference: {payment_data['reference']}"
            )
            
            return {
                'success': True,
                'gateway': self.gateway_name,
                'reference': payment_data['reference'],
                'response': response,
                'timestamp': timezone.now(),
            }
            
        except Exception as e:
            logger.error(f"Payment initiation failed: {str(e)}")
            raise PaymentGatewayError(f"Payment initiation failed: {str(e)}")
    
    def verify_webhook(
        self, 
        payload: bytes, 
        headers: Dict[str, str]
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Verify webhook signature and extract payment data.
        
        Args:
            payload: Raw webhook payload
            headers: Webhook headers
        
        Returns:
            Tuple of (is_valid, payment_data)
        
        Raises:
            PaymentGatewayError: If verification fails
        """
        try:
            # Verify signature
            if not self._verify_signature(payload, headers):
                logger.warning(f"Invalid webhook signature for {self.gateway_name}")
                return False, {}
            
            # Parse payload
            payment_data = self.gateway.parse_webhook_payload(payload, headers)
            
            # Validate payment data
            if not self._validate_webhook_data(payment_data):
                logger.warning(f"Invalid webhook data for {self.gateway_name}")
                return False, {}
            
            logger.info(f"Webhook verified successfully for {self.gateway_name}")
            return True, payment_data
            
        except Exception as e:
            logger.error(f"Webhook verification failed: {str(e)}")
            raise PaymentGatewayError(f"Webhook verification failed: {str(e)}")
    
    def get_payment_status(self, reference: str) -> Dict[str, Any]:
        """
        Get payment status from gateway.
        
        Args:
            reference: Payment reference ID
        
        Returns:
            Dictionary containing payment status
        """
        try:
            return self.gateway.get_payment_status(reference)
        except Exception as e:
            logger.error(f"Failed to get payment status: {str(e)}")
            raise PaymentGatewayError(f"Status check failed: {str(e)}")
    
    def _validate_payment_request(
        self, 
        amount: Decimal, 
        currency: str, 
        user_data: Dict[str, Any]
    ):
        """Validate payment request parameters."""
        # Check amount limits
        gateway_info = get_gateway_info(self.gateway_name)
        min_amount = Decimal(gateway_info['min_amount'])
        max_amount = Decimal(gateway_info['max_amount'])
        
        if amount < min_amount:
            raise ValidationError(f"Amount {amount} is below minimum {min_amount}")
        if amount > max_amount:
            raise ValidationError(f"Amount {amount} exceeds maximum {max_amount}")
        
        # Check currency support
        if currency not in gateway_info['supported_currencies']:
            raise ValidationError(f"Currency {currency} not supported by {self.gateway_name}")
        
        # Validate user data
        required_fields = ['email', 'phone']
        for field in required_fields:
            if field not in user_data or not user_data[field]:
                raise ValidationError(f"Missing required field: {field}")
    
    def _validate_webhook_data(self, payment_data: Dict[str, Any]) -> bool:
        """Validate webhook payment data."""
        required_fields = ['reference', 'status', 'amount']
        return all(field in payment_data for field in required_fields)
    
    def _verify_signature(self, payload: bytes, headers: Dict[str, str]) -> bool:
        """Verify webhook signature."""
        try:
            signature_header = SECURITY_CONFIG['webhook_signature_header']
            signature = headers.get(signature_header)
            
            if not signature:
                logger.warning(f"Missing signature header: {signature_header}")
                return False
            
            # Generate expected signature
            expected_signature = hmac.new(
                self.config['webhook_secret'].encode(),
                payload,
                hashlib.sha256
            ).hexdigest()
            
            return hmac.compare_digest(signature, expected_signature)
            
        except Exception as e:
            logger.error(f"Signature verification failed: {str(e)}")
            return False
    
    def _generate_reference(self) -> str:
        """Generate unique payment reference."""
        timestamp = int(time.time() * 1000)
        random_suffix = str(hash(time.time()) % 10000).zfill(4)
        return f"{self.gateway_name.upper()}_{timestamp}_{random_suffix}"


class PaymentGatewayManager:
    """
    Manager class for handling multiple payment gateways.
    
    This class provides high-level operations across all available gateways.
    """
    
    @staticmethod
    def get_available_gateways() -> Dict[str, Dict[str, Any]]:
        """
        Get all available and configured gateways.
        
        Returns:
            Dictionary of gateway information
        """
        from ..settings.gateway_config import get_available_gateways, get_gateway_info
        
        available = {}
        for gateway_name in get_available_gateways():
            info = get_gateway_info(gateway_name)
            available[gateway_name] = {
                'name': info['name'],
                'supported_currencies': info['supported_currencies'],
                'min_amount': info['min_amount'],
                'max_amount': info['max_amount'],
                'enabled': True,
            }
        
        return available
    
    @staticmethod
    def get_gateway_service(gateway_name: str) -> PaymentGatewayService:
        """
        Get a payment gateway service instance.
        
        Args:
            gateway_name: Name of the gateway
        
        Returns:
            PaymentGatewayService instance
        
        Raises:
            PaymentGatewayError: If gateway is not available
        """
        if not is_gateway_enabled(gateway_name):
            raise PaymentGatewayError(f"Gateway {gateway_name} is not enabled")
        
        return PaymentGatewayService(gateway_name)
    
    @staticmethod
    def test_gateway_connection(gateway_name: str) -> Dict[str, Any]:
        """
        Test connection to a payment gateway.
        
        Args:
            gateway_name: Name of the gateway to test
        
        Returns:
            Dictionary containing test results
        """
        try:
            service = PaymentGatewayManager.get_gateway_service(gateway_name)
            
            # Test with minimal amount
            test_data = {
                'amount': Decimal('100'),
                'currency': 'SYP',
                'user_data': {
                    'email': 'test@example.com',
                    'phone': '+963123456789',
                    'name': 'Test User'
                }
            }
            
            result = service.initiate_payment(**test_data)
            
            return {
                'success': True,
                'gateway': gateway_name,
                'message': 'Connection test successful',
                'reference': result['reference']
            }
            
        except Exception as e:
            return {
                'success': False,
                'gateway': gateway_name,
                'message': f'Connection test failed: {str(e)}',
                'error': str(e)
            } 