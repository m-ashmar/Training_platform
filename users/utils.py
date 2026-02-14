import logging
import random
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from django.core.mail import send_mail
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)

def send_push_notification(user, title, message, data=None):
    """
    Send push notification to user's devices via FCM.
    
    Args:
        user: CustomUser instance
        title: Notification title
        message: Notification message
        data: Optional data payload
    
    Returns:
        bool: True if notification was sent successfully, False otherwise
    """
    from .models import DeviceToken
    from social.firebase_service import FirebaseNotificationService
    
    try:
        # Get all tokens for the user
        tokens = list(DeviceToken.objects.filter(user=user).values_list('token', flat=True))
        
        if not tokens:
            logger.info(f"No device tokens found for user {user.id}")
            return False
            
        # Use the singleton service
        service = FirebaseNotificationService()
        
        success_count = service.send_multicast(
            tokens=tokens,
            title=title,
            body=message,
            data=data
        )
        
        logger.info(f"Push notification sent to user {user.id}: {title} - {message} ({success_count}/{len(tokens)} successful)")
        return success_count > 0
        
    except Exception as e:
        logger.error(f"Error sending push notification to user {user.id}: {str(e)}")
        return False

def generate_otp():
    """
    Generate a random 6-digit OTP code.
    
    Returns:
        str: 6-digit OTP code
    """
    return str(random.randint(100000, 999999))

def send_otp_email(user, otp_code):
    """
    Send OTP code to user's email address.
    
    Args:
        user: CustomUser instance
        otp_code: 6-digit OTP code to send
    
    Returns:
        bool: True if email was sent successfully, False otherwise
    """
    try:
        subject = 'Verify Your Email - Training Platform'
        
        # Try to render HTML template, fallback to plain text
        try:
            html_message = render_to_string('emails/otp_verification.html', {
                'user': user,
                'otp_code': otp_code,
            })
            plain_message = f"Your OTP verification code is: {otp_code}\n\nThis code will expire in 10 minutes.\n\nIf you didn't request this code, please ignore this email."
        except Exception as e:
            logger.warning(f"Could not render email template: {e}. Using plain text.")
            html_message = None
            plain_message = f"Your OTP verification code is: {otp_code}\n\nThis code will expire in 10 minutes.\n\nIf you didn't request this code, please ignore this email."
        
        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@trainingplatform.com')
        
        send_mail(
            subject=subject,
            message=plain_message,
            from_email=from_email,
            recipient_list=[user.email],
            html_message=html_message,
            fail_silently=False,
        )
        
        logger.info(f"OTP email sent to {user.email} for user {user.id}")
        return True
        
    except Exception as e:
        logger.error(f"Error sending OTP email to {user.email}: {str(e)}")
        return False

def create_otp(user):
    """
    Create a new OTP verification record for a user.
    
    Args:
        user: CustomUser instance
    
    Returns:
        OTPVerification: Created OTP verification instance
    """
    from .models import OTPVerification
    
    # Invalidate any existing unverified OTPs for this user
    OTPVerification.objects.filter(
        user=user,
        is_verified=False,
        expires_at__gt=timezone.now()
    ).update(is_verified=True)  # Mark as used to invalidate
    
    # Generate new OTP
    otp_code = generate_otp()
    expires_at = timezone.now() + timedelta(minutes=10)
    
    otp = OTPVerification.objects.create(
        user=user,
        otp_code=otp_code,
        email=user.email,
        expires_at=expires_at
    )
    
    # Send OTP email
    send_otp_email(user, otp_code)
    
    logger.info(f"OTP created for user {user.id} ({user.email})")
    return otp

def verify_otp(email, otp_code):
    """
    Verify an OTP code for a user.
    
    Args:
        email: User's email address
        otp_code: OTP code to verify
    
    Returns:
        tuple: (success: bool, otp_instance: OTPVerification or None, error_message: str or None)
    """
    from .models import OTPVerification, CustomUser
    
    try:
        # Find user by email
        user = CustomUser.objects.get(email=email)
        
        # Find the most recent unverified OTP for this user
        # OTP codes are numeric, so we can compare directly
        otp = OTPVerification.objects.filter(
            user=user,
            email=email,
            otp_code=otp_code.strip(),  # Remove any whitespace
            is_verified=False
        ).order_by('-created_at').first()
        
        if not otp:
            # Try to find any unverified OTP for this user to provide better error message
            otp_exists = OTPVerification.objects.filter(
                user=user,
                email=email,
                is_verified=False
            ).exists()
            
            if not otp_exists:
                return False, None, "OTP code not found. Please request a new one."
            else:
                return False, None, "Invalid OTP code. Please check and try again."
        
        if not otp:
            return False, None, "OTP code not found. Please request a new one."
        
        # Check if OTP is still valid
        if not otp.is_valid():
            return False, None, "OTP code has expired. Please request a new one."
        
        # Mark OTP as verified
        otp.is_verified = True
        otp.verified_at = timezone.now()
        otp.save()
        
        logger.info(f"OTP verified for user {user.id} ({email})")
        return True, otp, None
        
    except CustomUser.DoesNotExist:
        return False, None, "User with this email does not exist."
    except Exception as e:
        logger.error(f"Error verifying OTP for {email}: {str(e)}")
        return False, None, "An error occurred while verifying OTP. Please try again." 