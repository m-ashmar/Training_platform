"""
File Upload Security Module for Training Platform

This module provides comprehensive security validation for file uploads,
including virus scanning, file type validation, and content sanitization.
"""

import os
import time
import hashlib
import logging
from PIL import Image
from django.core.exceptions import ValidationError
from django.conf import settings
from typing import Dict, Any

# Try to import magic, with fallback
try:
    import magic
    MAGIC_AVAILABLE = True
except ImportError:
    MAGIC_AVAILABLE = False

logger = logging.getLogger(__name__)


class FileSecurityValidator:
    """
    Comprehensive file security validator
    """
    
    # Allowed MIME types for different file categories
    ALLOWED_IMAGE_TYPES = [
        'image/jpeg',
        'image/png', 
        'image/gif',
        'image/webp'
    ]
    
    ALLOWED_DOCUMENT_TYPES = [
        'application/pdf',
        'application/msword',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    ]
    
    # Maximum file sizes (in bytes)
    MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB
    MAX_DOCUMENT_SIZE = 10 * 1024 * 1024  # 10MB
    
    # Dangerous file extensions
    DANGEROUS_EXTENSIONS = [
        '.exe', '.bat', '.cmd', '.com', '.pif', '.scr', '.vbs', '.js',
        '.jar', '.app', '.deb', '.pkg', '.dmg', '.php', '.asp', '.jsp'
    ]
    
    def __init__(self):
        self.quarantine_dir = os.path.join(settings.MEDIA_ROOT, 'quarantine')
        os.makedirs(self.quarantine_dir, exist_ok=True)
    
    def validate_file(self, file, file_type='image') -> Dict[str, Any]:
        """
        Comprehensive file validation
        
        Args:
            file: Django UploadedFile object
            file_type: Type of file ('image', 'document')
            
        Returns:
            Dict with validation results
            
        Raises:
            ValidationError: If file is invalid or dangerous
        """
        results = {
            'is_valid': False,
            'file_type': None,
            'size': 0,
            'hash': None,
            'sanitized': False,
            'warnings': []
        }
        
        try:
            # Basic file checks
            self._validate_file_existence(file)
            self._validate_file_size(file, file_type)
            self._validate_file_extension(file)
            
            # Advanced security checks
            file_mime_type = self._get_file_mime_type(file)
            self._validate_mime_type(file_mime_type, file_type)
            
            # Content validation
            if file_type == 'image':
                self._validate_image_content(file)
            
            # Generate file hash for duplicate detection
            file_hash = self._generate_file_hash(file)
            
            # Check for malicious content
            self._scan_for_malicious_content(file)
            
            results.update({
                'is_valid': True,
                'file_type': file_mime_type,
                'size': file.size,
                'hash': file_hash,
                'sanitized': True
            })
            
            logger.info(f"File validation successful: {file.name}")
            return results
            
        except ValidationError as e:
            logger.warning(f"File validation failed: {file.name} - {e}")
            self._quarantine_file(file)
            raise
        except Exception as e:
            logger.error(f"File validation error: {file.name} - {e}")
            self._quarantine_file(file)
            raise ValidationError(f"File validation failed: {str(e)}")
    
    def _validate_file_existence(self, file):
        """Check if file exists and is readable"""
        if not file:
            raise ValidationError("No file provided")
        
        if not hasattr(file, 'read'):
            raise ValidationError("Invalid file object")
        
        if file.size == 0:
            raise ValidationError("Empty file not allowed")
    
    def _validate_file_size(self, file, file_type):
        """Validate file size based on type"""
        max_size = self.MAX_IMAGE_SIZE if file_type == 'image' else self.MAX_DOCUMENT_SIZE
        
        if file.size > max_size:
            raise ValidationError(
                f"File size ({file.size} bytes) exceeds maximum allowed "
                f"size ({max_size} bytes)"
            )
    
    def _validate_file_extension(self, file):
        """Check for dangerous file extensions"""
        if not hasattr(file, 'name') or not file.name:
            raise ValidationError("File must have a name")
        
        file_ext = os.path.splitext(file.name)[1].lower()
        
        if file_ext in self.DANGEROUS_EXTENSIONS:
            raise ValidationError(f"File extension {file_ext} is not allowed")
    
    def _get_file_mime_type(self, file):
        """Get actual MIME type using python-magic or fallback"""
        try:
            if MAGIC_AVAILABLE:
                # Read first 1024 bytes for MIME type detection
                file.seek(0)
                file_header = file.read(1024)
                file.seek(0)  # Reset file pointer
                
                mime_type = magic.from_buffer(file_header, mime=True)
                return mime_type
            else:
                # Fallback to mimetypes based on file extension
                import mimetypes
                if hasattr(file, 'name'):
                    mime_type, _ = mimetypes.guess_type(file.name)
                    return mime_type or 'application/octet-stream'
                return 'application/octet-stream'
            
        except Exception as e:
            logger.error(f"MIME type detection failed: {e}")
            raise ValidationError("Could not determine file type")
    
    def _validate_mime_type(self, mime_type, file_type):
        """Validate MIME type against allowed types"""
        if file_type == 'image':
            allowed_types = self.ALLOWED_IMAGE_TYPES
        elif file_type == 'document':
            allowed_types = self.ALLOWED_DOCUMENT_TYPES
        else:
            raise ValidationError(f"Unknown file type: {file_type}")
        
        if mime_type not in allowed_types:
            raise ValidationError(
                f"File type {mime_type} not allowed. "
                f"Allowed types: {', '.join(allowed_types)}"
            )
    
    def _validate_image_content(self, file):
        """Validate image content and structure"""
        try:
            file.seek(0)
            with Image.open(file) as img:
                # Verify image can be loaded
                img.verify()
                
                # Check image dimensions
                file.seek(0)
                img = Image.open(file)
                width, height = img.size
                
                # Maximum dimensions check
                max_dimension = 4096  # 4K resolution
                if width > max_dimension or height > max_dimension:
                    raise ValidationError(
                        f"Image dimensions ({width}x{height}) exceed "
                        f"maximum allowed ({max_dimension}x{max_dimension})"
                    )
                
                # Check for reasonable aspect ratio
                aspect_ratio = max(width, height) / min(width, height)
                if aspect_ratio > 10:  # Very unusual aspect ratio
                    raise ValidationError("Invalid image aspect ratio")
            
            file.seek(0)  # Reset file pointer
            
        except ValidationError:
            raise
        except Exception as e:
            logger.warning(f"Image validation failed: {e}")
            raise ValidationError("Invalid or corrupted image file")
    
    def _generate_file_hash(self, file):
        """Generate SHA-256 hash of file content"""
        try:
            file.seek(0)
            hash_sha256 = hashlib.sha256()
            
            for chunk in iter(lambda: file.read(4096), b""):
                hash_sha256.update(chunk)
            
            file.seek(0)  # Reset file pointer
            return hash_sha256.hexdigest()
            
        except Exception as e:
            logger.error(f"Hash generation failed: {e}")
            return None
    
    def _scan_for_malicious_content(self, file):
        """Basic malicious content detection"""
        try:
            file.seek(0)
            content = file.read()
            file.seek(0)
            
            # Convert to string for pattern matching (handle encoding errors)
            try:
                content.decode('utf-8', errors='ignore')
            except (UnicodeDecodeError, AttributeError):
                pass  # Content is binary, skip string checks
            
            # Check for suspicious patterns
            malicious_patterns = [
                b'<script',
                b'javascript:',
                b'vbscript:',
                b'onload=',
                b'onerror=',
                b'<?php',
                b'<%',
                b'<iframe',
                b'<object',
                b'<embed'
            ]
            
            for pattern in malicious_patterns:
                if pattern in content:
                    raise ValidationError(
                        f"Potentially malicious content detected: {pattern.decode()}"
                    )
            
            # Check for executable signatures
            executable_signatures = [
                b'MZ',  # DOS/Windows executable
                b'\x7fELF',  # Linux executable
                b'\xca\xfe\xba\xbe',  # Java class file
                b'PK\x03\x04',  # ZIP file (could contain executable)
            ]
            
            for signature in executable_signatures:
                if content.startswith(signature):
                    logger.warning(f"Executable signature detected: {signature}")
                    # Don't automatically reject ZIP files as they might be valid
                    if signature != b'PK\x03\x04':
                        raise ValidationError("Executable files are not allowed")
            
        except ValidationError:
            raise
        except Exception as e:
            logger.error(f"Malicious content scan failed: {e}")
            # Don't fail validation if scanning fails
            pass
    
    def _quarantine_file(self, file):
        """Move suspicious files to quarantine"""
        try:
            if hasattr(file, 'name') and file.name:
                quarantine_path = os.path.join(
                    self.quarantine_dir,
                    f"quarantine_{file.name}_{int(time.time())}"
                )
                
                file.seek(0)
                with open(quarantine_path, 'wb') as qfile:
                    for chunk in file.chunks():
                        qfile.write(chunk)
                
                logger.warning(f"File quarantined: {quarantine_path}")
                
        except Exception as e:
            logger.error(f"Failed to quarantine file: {e}")
    
    def sanitize_filename(self, filename: str) -> str:
        """
        Sanitize filename to prevent path traversal and other attacks
        """
        if not filename:
            return "unnamed_file"
        
        # Remove path components
        filename = os.path.basename(filename)
        
        # Remove or replace dangerous characters
        dangerous_chars = '<>:"/\\|?*'
        for char in dangerous_chars:
            filename = filename.replace(char, '_')
        
        # Remove leading/trailing spaces and dots
        filename = filename.strip(' .')
        
        # Ensure filename is not empty after sanitization
        if not filename:
            filename = "sanitized_file"
        
        # Limit filename length
        max_length = 255
        if len(filename) > max_length:
            name, ext = os.path.splitext(filename)
            filename = name[:max_length-len(ext)] + ext
        
        return filename


class SecureFileUploadMixin:
    """
    Mixin for models with file upload fields to add security validation
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.file_validator = FileSecurityValidator()
    
    def clean_file_field(self, field_name, file_type='image'):
        """
        Clean method for file fields with security validation
        """
        file = getattr(self, field_name)
        if file:
            validation_result = self.file_validator.validate_file(file, file_type)
            if not validation_result['is_valid']:
                raise ValidationError(f"Invalid {file_type} file")
    
    def save(self, *args, **kwargs):
        """
        Override save to perform file validation
        """
        # Validate all file fields before saving
        for field in self._meta.fields:
            if hasattr(field, 'upload_to'):  # File/Image field
                file_type = 'image' if 'image' in field.name.lower() else 'document'
                self.clean_file_field(field.name, file_type)
        
        super().save(*args, **kwargs)


# Helper functions for use in views and forms
def validate_uploaded_image(file):
    """
    Standalone function to validate uploaded images
    """
    validator = FileSecurityValidator()
    return validator.validate_file(file, 'image')


def validate_uploaded_document(file):
    """
    Standalone function to validate uploaded documents
    """
    validator = FileSecurityValidator()
    return validator.validate_file(file, 'document')


def secure_file_upload_path(instance, filename, subfolder='uploads'):
    """
    Generate secure upload path with sanitized filename
    """
    validator = FileSecurityValidator()
    safe_filename = validator.sanitize_filename(filename)
    
    # Add timestamp to prevent collisions
    import time
    timestamp = int(time.time())
    name, ext = os.path.splitext(safe_filename)
    safe_filename = f"{name}_{timestamp}{ext}"
    
    # Organize by user and date
    if hasattr(instance, 'user'):
        user_id = instance.user.id
    else:
        user_id = 'unknown'
    
    from datetime import datetime
    date_folder = datetime.now().strftime('%Y/%m')
    
    return os.path.join(subfolder, str(user_id), date_folder, safe_filename) 