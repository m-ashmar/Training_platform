import logging
import firebase_admin
from firebase_admin import credentials, messaging
from django.conf import settings
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class FirebaseNotificationService:
    """
    Service for sending push notifications via Firebase Cloud Messaging (FCM).
    Handles initialization and sending logic.
    """
    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(FirebaseNotificationService, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.initialize()
            self._initialized = True

    def initialize(self):
        """Initialize Firebase Admin SDK with credentials from settings."""
        try:
            if not firebase_admin._apps:
                cred_path = getattr(settings, 'FIREBASE_CREDENTIALS_PATH', None)
                if cred_path:
                    cred = credentials.Certificate(cred_path)
                    firebase_admin.initialize_app(cred)
                    logger.info("Firebase Admin SDK initialized successfully.")
                else:
                    # Try default credentials (e.g. environment variables)
                    # or check if already initialized externally
                    logger.warning("FIREBASE_CREDENTIALS_PATH not set. Attempting default initialization.")
                    try:
                        firebase_admin.initialize_app()
                        logger.info("Firebase Admin SDK initialized with default credentials.")
                    except Exception as e:
                        logger.error(f"Failed to initialize Firebase with default credentials: {e}")
        except Exception as e:
            logger.error(f"Error initializing Firebase Admin SDK: {e}")

    def send_to_token(self, token: str, title: str, body: str, data: Optional[Dict[str, str]] = None) -> bool:
        """
        Send a notification to a specific device token.
        
        Args:
            token: The FCM device token.
            title: Notification title.
            body: Notification body.
            data: Optional data payload (all values must be strings).
        
        Returns:
            bool: True if sent successfully, False otherwise.
        """
        try:
            message = messaging.Message(
                notification=messaging.Notification(
                    title=title,
                    body=body,
                ),
                data=data or {},
                token=token,
            )
            response = messaging.send(message)
            logger.info(f"Successfully sent message to token partial {token[:10]}...: {response}")
            return True
        except firebase_admin.exceptions.FirebaseError as e:
            logger.error(f"Error sending message to token partial {token[:10]}...: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error sending message: {e}")
            return False

    def send_multicast(self, tokens: List[str], title: str, body: str, data: Optional[Dict[str, str]] = None) -> int:
        """
        Send a notification to multiple device tokens.
        
        Args:
            tokens: List of FCM device tokens (max 500 per batch).
            title: Notification title.
            body: Notification body.
            data: Optional data payload.
            
        Returns:
            int: Number of successfully sent messages.
        """
        if not tokens:
            return 0
            
        # Firebase limits multicast/batch to 500
        success_count = 0
        batch_size = 500
        
        for i in range(0, len(tokens), batch_size):
            batch_tokens = tokens[i:i + batch_size]
            try:
                message = messaging.MulticastMessage(
                    notification=messaging.Notification(
                        title=title,
                        body=body,
                    ),
                    data=data or {},
                    tokens=batch_tokens,
                )
                response = messaging.send_each_for_multicast(message)
                success_count += response.success_count
                
                if response.failure_count > 0:
                    logger.warning(f"Batch {i//batch_size}: {response.failure_count} messages failed to send.")
                    for idx, resp in enumerate(response.responses):
                        if not resp.success:
                            # In a real app, you might handle invalid tokens here (e.g. remove from DB)
                            logger.debug(f"Failed token {batch_tokens[idx]}: {resp.exception}")
                            
            except Exception as e:
                logger.error(f"Error sending multicast batch {i}: {e}")
                
        logger.info(f"Multicast sent: {success_count}/{len(tokens)} successful.")
        return success_count

    def send_to_topic(self, topic: str, title: str, body: str, data: Optional[Dict[str, str]] = None) -> bool:
        """
        Send a notification to a topic.
        
        Args:
            topic: Topic name.
            title: Notification title.
            body: Notification body.
            data: Optional data payload.
            
        Returns:
            bool: True if sent successfully, False otherwise.
        """
        try:
            message = messaging.Message(
                notification=messaging.Notification(
                    title=title,
                    body=body,
                ),
                data=data or {},
                topic=topic,
            )
            response = messaging.send(message)
            logger.info(f"Successfully sent message to topic {topic}: {response}")
            return True
        except Exception as e:
            logger.error(f"Error sending message to topic {topic}: {e}")
            return False
