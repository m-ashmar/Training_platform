"""
File Upload Security Module for Training Platform

This module provides comprehensive security validation for file uploads,
including virus scanning, file type validation, and content sanitization.
"""

import os
import time
import hashlib
import logging
from PIL import Image, ImageSequence
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
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
    
    def validate_file(self, file, file_type='image', scan_content=True,
                      quarantine=True) -> Dict[str, Any]:
        """
        Comprehensive file validation

        Args:
            file: Django UploadedFile object
            file_type: Type of file ('image', 'document')
            quarantine: keep a sample of a refused file (`_quarantine_file`). True
                everywhere except where the upload is a private message: a refused
                chat photo is a person's own picture — its first 64 KB carry the EXIF,
                the GPS tag and the embedded thumbnail — and a sample of it would sit
                under `quarantine/`, outside the conversation and outside every erasure
                (review 2026-09-24). There the refusal and its `code` are the whole
                answer, and nothing of the file is kept.

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
            
            # Check for malicious content. Skipped only by a caller that re-encodes the
            # file from decoded pixels (`process_uploaded_image`): nothing of the upload's
            # bytes survives that, and searching compressed image data for strings such
            # as `<%` or `MZ` refused most real photographs (BACKEND_BUGS.md B-002).
            if scan_content:
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
            if quarantine:
                self._quarantine_file(file)
            raise
        except Exception as e:
            logger.error(f"File validation error: {file.name} - {e}")
            if quarantine:
                self._quarantine_file(file)
            raise ValidationError(_("File validation failed."), code="file_validation_error")
    
    def _validate_file_existence(self, file):
        """Check if file exists and is readable"""
        if not file:
            raise ValidationError(_("No file provided."), code="no_file")
        
        if not hasattr(file, 'read'):
            raise ValidationError(_("Invalid file object"), code="invalid_file_object")
        
        if file.size == 0:
            raise ValidationError(_("Empty file not allowed."), code="empty_file")
    
    def _validate_file_size(self, file, file_type):
        """Validate file size based on type"""
        max_size = self.MAX_IMAGE_SIZE if file_type == 'image' else self.MAX_DOCUMENT_SIZE
        
        if file.size > max_size:
            raise ValidationError(
                _("File size exceeds maximum allowed size."),
                code="file_too_large",
            )
    
    def _validate_file_extension(self, file):
        """Check for dangerous file extensions"""
        if not hasattr(file, 'name') or not file.name:
            raise ValidationError(_("File must have a name"), code="no_filename")
        
        file_ext = os.path.splitext(file.name)[1].lower()
        
        if file_ext in self.DANGEROUS_EXTENSIONS:
            raise ValidationError(_("File extension is not allowed."), code="dangerous_extension")
    
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
                    mime_type, _encoding = mimetypes.guess_type(file.name)
                    return mime_type or 'application/octet-stream'
                return 'application/octet-stream'
            
        except Exception as e:
            logger.error(f"MIME type detection failed: {e}")
            raise ValidationError(_("Could not determine file type"), code="mime_detection_failed")
    
    def _validate_mime_type(self, mime_type, file_type):
        """Validate MIME type against allowed types"""
        if file_type == 'image':
            allowed_types = self.ALLOWED_IMAGE_TYPES
        elif file_type == 'document':
            allowed_types = self.ALLOWED_DOCUMENT_TYPES
        else:
            raise ValidationError(_("Unknown file type."), code="unknown_file_type")
        
        if mime_type not in allowed_types:
            raise ValidationError(
                _("File type not allowed."),
                code="invalid_mime_type",
            )
    
    def _validate_image_content(self, file):
        """Validate image content and structure.

        No pixel-size cap here. There was one — 4096 px per side, code
        `image_too_large` — and because it ran BEFORE `process_uploaded_image`, that
        function's own caps (`MAX_IMAGE_DIMENSION`, `MAX_IMAGE_PIXELS`) and the codes the
        upload views document for them (`image_dimensions_too_large`,
        `image_too_many_pixels`) could never fire, while an ordinary 24 MP phone photo
        (5712x4284) was refused (SIDE_BUGS S-140). The size of a decoded image is decided
        in ONE place, by the function that decodes it.
        """
        try:
            file.seek(0)
            with Image.open(file) as img:
                # Verify image can be loaded
                img.verify()

                # Check image dimensions
                file.seek(0)
                img = Image.open(file)
                width, height = img.size

                # Check for reasonable aspect ratio
                aspect_ratio = max(width, height) / min(width, height)
                if aspect_ratio > 10:  # Very unusual aspect ratio
                    raise ValidationError(_("Invalid image aspect ratio"), code="invalid_aspect_ratio")
            
            file.seek(0)  # Reset file pointer
            
        except ValidationError:
            raise
        except Exception as e:
            logger.warning(f"Image validation failed: {e}")
            raise ValidationError(_("Invalid or corrupted image file"), code="corrupt_image")
    
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
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)
            
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
                        _("Potentially malicious content detected."),
                        code="malicious_content",
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
                        raise ValidationError(_("Executable files are not allowed"), code="executable_file")
            
        except ValidationError:
            raise
        except Exception as e:
            logger.error(f"Malicious content scan failed: {e}")
            # Don't fail validation if scanning fails
            pass
    
    #: Total bytes the quarantine directory may hold, and the age past which an entry
    #: is dropped. Every rejected upload used to be copied here at full size and kept
    #: for ever — no TTL, no cap, no purge anywhere — so 30 deliberately malformed 2 MB
    #: "images" put 58 MB on the volume in two seconds, and roughly 500 rejected
    #: requests fill the 1 GB Fly volume that also serves all user media. A cheap,
    #: low-bandwidth way for any authenticated account to exhaust the disk.
    QUARANTINE_MAX_BYTES = 64 * 1024 * 1024
    QUARANTINE_MAX_AGE_SECONDS = 7 * 24 * 3600
    #: Only the first slice of a rejected file is kept. The point of quarantine is to
    #: let a person see WHAT was sent, and the first few kilobytes carry the magic
    #: bytes, the header and the payload marker that caused the rejection.
    QUARANTINE_SAMPLE_BYTES = 64 * 1024
    #: How many samples may be kept, whatever their size: as many as the byte cap holds
    #: at full size. An empty or one-byte upload barely counts toward the byte cap, so
    #: without a count a few thousand of them sat there for the full seven days and
    #: every eviction pass had to walk all of them (SIDE_BUGS S-141).
    QUARANTINE_MAX_ENTRIES = QUARANTINE_MAX_BYTES // QUARANTINE_SAMPLE_BYTES
    #: At most one eviction pass per this many seconds, across every process. The pass
    #: used to run on EVERY rejected upload — so once the prefix held thousands of
    #: samples, each later rejection, an honest user's included, paid for thousands of
    #: storage calls synchronously inside its request.
    QUARANTINE_EVICT_EVERY_SECONDS = 60
    _QUARANTINE_EVICT_LOCK = "file_security:quarantine:evict"

    @staticmethod
    def quarantine_location() -> str:
        """The prefix a sample is stored under, from `settings.QUARANTINE_LOCATION`."""
        return str(getattr(settings, 'QUARANTINE_LOCATION', 'quarantine')).strip('/')

    @staticmethod
    def quarantine_storage():
        """WHERE a rejected upload's sample is kept: the default media storage.

        It used to be `MEDIA_ROOT/quarantine`, opened with the built-in `open()`. That
        was the local disk and nothing else, which stopped being an answer the day
        `MEDIA_BACKEND=gcs` went live: the web container's filesystem is ephemeral, and
        the Celery worker — which runs the same validators on the same uploads — has no
        volume at all, so the evidence of every refused upload was gone at the next
        restart or simply written into a scratch layer nobody can reach.

        Going through the storage API rather than adding a second local path is the
        honest fix, because the storage already IS the durable, shared place this
        deployment writes bytes to, and it is the same object in the worker as in the
        web process. On the filesystem backend it resolves to `MEDIA_ROOT/<prefix>`, so
        development and the test suite behave exactly as before — and `save()` creates
        the directory itself, which is why there is no longer an `os.makedirs` running
        once per validator instance.

        Nothing hands out a URL to these objects, and the bucket grants no public read
        (`gcs_storage.SignedGoogleCloudStorage`: uniform access, `default_acl=None`), so
        a quarantined sample is not reachable by anyone who has not been given bucket
        credentials.
        """
        from django.core.files.storage import default_storage

        return default_storage

    def _quarantine_file(self, file):
        """Keep a bounded sample of a rejected file, for a bounded time."""
        try:
            if not (hasattr(file, 'name') and file.name):
                return

            if self._quarantine_eviction_due():
                self._evict_quarantine()

            # `sanitize_filename`, which this class ships and did not use here. Django's
            # multipart parser strips path separators before the file object arrives, so
            # this was not reachable over HTTP — but any non-HTTP caller of
            # `validate_file()` could set `.name` and write outside the directory.
            safe_name = self.sanitize_filename(file.name)
            name = (f"{self.quarantine_location()}/"
                    f"quarantine_{safe_name}_{int(time.time())}")

            file.seek(0)
            sample = file.read(self.QUARANTINE_SAMPLE_BYTES)
            file.seek(0)
            stored = self.quarantine_storage().save(name, ContentFile(sample))

            logger.warning("File quarantined: %s (%d bytes sampled)",
                           stored, len(sample))

        except Exception as e:
            logger.error(f"Failed to quarantine file: {e}")

    @classmethod
    def _quarantine_eviction_due(cls) -> bool:
        """True for ONE caller per `QUARANTINE_EVICT_EVERY_SECONDS`, across processes.

        `cache.add` is atomic on the shared cache: the first caller in a window sets the
        key and runs the pass, everyone else skips it. If the cache itself is down the
        pass is skipped rather than run on every request — the samples are bounded in
        time as well as in size, and the next caller after the cache returns evicts.
        """
        from django.core.cache import cache

        try:
            return bool(cache.add(cls._QUARANTINE_EVICT_LOCK, 1,
                                  timeout=cls.QUARANTINE_EVICT_EVERY_SECONDS))
        except Exception:
            logger.debug("quarantine eviction lock unavailable", exc_info=True)
            return False

    @staticmethod
    def _quarantine_listing(storage, prefix):
        """`[(path, modified, size)]` for every sample directly under `prefix`.

        On object storage this is ONE paginated listing: the bucket answers each
        object's size and time in the listing itself. It used to be `listdir` and then a
        `size()` and a `get_modified_time()` per sample, and on GCS each of those is its
        own HTTP request (`storages.backends.gcloud`: `_get_blob` per call). The
        filesystem backend has no listing with metadata, and a `stat` is a syscall, so it
        keeps the storage API.
        """
        bucket = getattr(storage, "bucket", None)
        list_blobs = getattr(bucket, "list_blobs", None) if bucket is not None else None
        if callable(list_blobs):
            full = storage._normalize_name(prefix).rstrip("/") + "/"
            out = []
            for blob in list_blobs(prefix=full, delimiter="/"):
                leaf = blob.name[len(full):] if blob.name.startswith(full) else ""
                if not leaf:
                    continue
                out.append((f"{prefix}/{leaf}", blob.updated, int(blob.size or 0)))
            return out

        _dirs, names = storage.listdir(prefix)
        out = []
        for name in names:
            path = f"{prefix}/{name}"
            try:
                out.append((path, storage.get_modified_time(path), storage.size(path)))
            except Exception:
                # A sample another process evicted between listdir and stat.
                continue
        return out

    def _evict_quarantine(self):
        """Drop samples that are too old, then the oldest until under BOTH caps.

        Throttled by `_quarantine_eviction_due`: callers ask that first.
        """
        storage = self.quarantine_storage()
        prefix = self.quarantine_location()
        try:
            listing = self._quarantine_listing(storage, prefix)
        except (FileNotFoundError, NotADirectoryError):
            # Nothing has been quarantined yet. Not an error, and not worth a log line.
            return
        except Exception:
            logger.debug("quarantine eviction failed", exc_info=True)
            return

        entries = []
        total = 0
        now = timezone.now()
        for path, modified, size in listing:
            if modified is None:
                modified = now
            elif timezone.is_naive(modified):
                # FileSystemStorage returns naive times when USE_TZ is off.
                modified = timezone.make_aware(modified, timezone.get_default_timezone())
            if (now - modified).total_seconds() > self.QUARANTINE_MAX_AGE_SECONDS:
                self._drop_quarantined(storage, path)
                continue
            entries.append((modified, size, path))
            total += size

        entries.sort(key=lambda entry: entry[0])
        while entries and (total > self.QUARANTINE_MAX_BYTES
                           or len(entries) > self.QUARANTINE_MAX_ENTRIES):
            _modified, size, path = entries.pop(0)
            self._drop_quarantined(storage, path)
            total -= size

    @staticmethod
    def _drop_quarantined(storage, path):
        try:
            storage.delete(path)
        except Exception:
            logger.debug("could not drop quarantined sample %s", path, exc_info=True)


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


# ============================================================================
# Single entry point used by every upload view
# ============================================================================

# A tiny 1x1 image is legitimate; a 12000x12000 PNG is 435KB on disk but ~432MB
# once PIL decodes it — a one-request OOM on a 512MB container.
MAX_IMAGE_PIXELS = 40_000_000        # ~40MP, well above any phone camera
MAX_IMAGE_DIMENSION = 10_000         # px per side

# Extension is derived from the DETECTED format, never from the client filename.
FORMAT_TO_EXTENSION = {
    'JPEG': 'jpg',
    'PNG': 'png',
    'GIF': 'gif',
    'WEBP': 'webp',
}

#: Formats Pillow names differently from what the file IS. A JPEG whose MPF segment
#: holds more than one image — a phone's depth map or HDR gain map, a camera's
#: preview — is opened as `MPO` (PIL/JpegImagePlugin), while libmagic rightly calls it
#: `image/jpeg`. Its first image is an ordinary JPEG, and that is what is stored: the
#: re-encode below writes one frame, so the secondary images go the way of the EXIF.
#: Refusing it as `unsupported_format` refused genuine phone photos on every image
#: route (SIDE_BUGS S-143, the same class of bug as B-002).
FORMAT_ALIASES = {'MPO': 'JPEG'}

#: How many frames an animated GIF may hold. Every frame is decoded to the FULL canvas
#: and kept until the animation is written back out, so the frame count multiplies the
#: memory a single request takes; the pixel budget below bounds that for large
#: canvases, and this bounds the per-frame overhead of a small canvas repeated tens of
#: thousands of times (SIDE_BUGS S-079). 300 frames is ten seconds at 30 fps — a
#: sticker or a short loop — and, at the pixel budget, a 480x270 animation.
MAX_ANIMATION_FRAMES = 300


def process_uploaded_image(file, max_bytes=None, quarantine=True):
    """
    Validate and normalise an uploaded image. This is the ONLY function upload
    views should use.

    It closes four holes that existed while `file_security` had no call sites:

      * content was never inspected — a PHP/HTML/SVG payload with a spoofed
        `Content-Type: image/jpeg` header was accepted and stored;
      * the stored extension came from the client filename, so `.php`/`.html`/
        `.svg` files were written to disk;
      * no pixel-dimension cap, so a decompression bomb could OOM the container;
      * files were stored byte-identical, retaining EXIF/GPS metadata.

    `max_bytes` is checked FIRST, before the validator runs, so an oversize file is
    refused (`file_too_large`) without a quarantine sample being taken. `quarantine`
    is passed to `FileSecurityValidator.validate_file`: False for a private upload
    whose refused bytes must not be kept anywhere (the chat's photos).

    A file that passes the validator and then cannot be decoded — a JPEG cut short
    (a half-downloaded gallery photo passes `verify()`, which reads no scan data),
    a malformed frame, a decompression bomb Pillow itself refuses — is refused as
    `unreadable_image`. It used to escape as Pillow's OSError: a 500 on every route.

    Returns (ContentFile, extension) — a re-encoded image safe to store.
    Raises ValidationError on anything suspicious.
    """
    from django.core.files.base import ContentFile

    if file is None:
        raise ValidationError(_("No file provided."), code="no_file")

    if max_bytes and file.size > max_bytes:
        raise ValidationError(
            _("File too large. Maximum size is %(mb)s MB.") % {"mb": round(max_bytes / (1024 * 1024), 1)},
            code="file_too_large",
        )

    # Magic-byte + MIME + extension checks. No byte-pattern scan: the file is decoded and
    # re-encoded below, so no payload in it can reach storage, and the scan matched
    # ordinary JPEG data — 36 of 40 genuine 512 px photos were refused (B-002).
    FileSecurityValidator().validate_file(file, 'image', scan_content=False,
                                          quarantine=quarantine)

    # Decode with a guard against decompression bombs.
    file.seek(0)
    try:
        probe = Image.open(file)
        width, height = probe.size
        fmt = (probe.format or '').upper()
        fmt = FORMAT_ALIASES.get(fmt, fmt)
        # Counting frames reads each frame's header and decodes no pixels.
        n_frames = int(getattr(probe, 'n_frames', 1) or 1) if fmt == 'GIF' else 1
    except Exception:
        raise ValidationError(_("File is not a readable image."), code="unreadable_image")

    if fmt not in FORMAT_TO_EXTENSION:
        raise ValidationError(
            _("Unsupported image format: %(fmt)s") % {"fmt": fmt or "unknown"},
            code="unsupported_format",
        )
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise ValidationError(
            _("Image dimensions too large (max %(d)spx per side).") % {"d": MAX_IMAGE_DIMENSION},
            code="image_dimensions_too_large",
        )
    if width * height > MAX_IMAGE_PIXELS:
        raise ValidationError(_("Image has too many pixels."), code="image_too_many_pixels")
    # An animation is decoded frame by frame onto the whole canvas, so what it costs is
    # frames x canvas, not one canvas. A few-KB GIF of 100 tiny frames on a 4096x4096
    # canvas held ~1.6 GB before this: one request, one OOM-killed container. It gets
    # the same pixel budget a still image does, and the same code.
    if n_frames > MAX_ANIMATION_FRAMES:
        raise ValidationError(_("This animation has too many frames."),
                              code="image_too_many_pixels")
    if n_frames * width * height > MAX_IMAGE_PIXELS:
        raise ValidationError(_("Image has too many pixels."), code="image_too_many_pixels")

    # Re-encode. This strips EXIF/GPS and any appended payload (polyglots), because
    # only decoded pixel data is written back out. The first full DECODE happens here,
    # so a file that is broken past its header fails here and not before.
    try:
        encoded = _reencode(file, fmt, n_frames)
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        # OSError: "image file is truncated", a broken scan or frame. SyntaxError and
        # ValueError: what Pillow's plugins raise for a malformed chunk or frame.
        # DecompressionBombError: Pillow's own limit (not an OSError subclass).
        logger.info("image refused at decode", exc_info=True)
        raise ValidationError(_("File is not a readable image."), code="unreadable_image")

    return ContentFile(encoded), FORMAT_TO_EXTENSION[fmt]


def _reencode(file, fmt, n_frames) -> bytes:
    """Decode `file` and write it back out as `fmt`, with no metadata. The bytes.

    Raises whatever Pillow raises on a file it cannot decode; `process_uploaded_image`
    is the only caller and turns that into `unreadable_image`.
    """
    import io

    file.seek(0)
    img = Image.open(file)
    img.load()
    if fmt == 'JPEG' and img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    elif fmt == 'PNG' and img.mode not in ('RGB', 'RGBA', 'L', 'P'):
        img = img.convert('RGBA')

    # Animation parameters live in `img.info`, which is cleared just below, so read
    # them first. Without save_all, Pillow writes only the first frame and a user's
    # animated GIF is silently replaced by a still image.
    is_animated = fmt == 'GIF' and n_frames > 1
    if is_animated:
        gif_duration = img.info.get('duration', 100)
        gif_loop = img.info.get('loop', 0)
        frames = [f.convert('RGBA').convert('P', palette=Image.ADAPTIVE)
                  for f in ImageSequence.Iterator(img)]

    # Pillow carries metadata forward through `img.info` (JPEG COM/EXIF markers are
    # re-emitted from it on save), so clearing it is required — simply omitting
    # exif=/comment= is NOT enough and left GPS data in the stored file.
    img.info = {}

    out = io.BytesIO()
    save_kwargs = {'format': fmt}
    if fmt == 'JPEG':
        save_kwargs.update(quality=88, optimize=True, exif=b'')
    if is_animated:
        for f in frames:
            f.info = {}
        img = frames[0]
        save_kwargs.update(save_all=True, append_images=frames[1:],
                           duration=gif_duration, loop=gif_loop, disposal=2)
    img.save(out, **save_kwargs)
    return out.getvalue()


class SecureImageField(serializers.ImageField):
    """
    Drop-in replacement for DRF's ImageField that also validates content, caps
    pixel dimensions and re-encodes (stripping EXIF/GPS and neutralising polyglots).

    DRF's own ImageField only asks Pillow "does this parse as an image?", so a real
    photo carrying GPS coordinates, or a 12000x12000 decompression bomb, sailed
    through every serializer-based upload path (social posts, profile updates).
    """

    def __init__(self, *args, max_bytes=5 * 1024 * 1024, **kwargs):
        self.max_bytes = max_bytes
        super().__init__(*args, **kwargs)

    def to_internal_value(self, data):
        data = super().to_internal_value(data)          # base image sanity check
        safe_file, ext = process_uploaded_image(data, max_bytes=self.max_bytes)
        base = os.path.splitext(getattr(data, 'name', 'upload'))[0][:60] or 'upload'
        safe_file.name = f"{base}.{ext}"
        return safe_file


# ============================================================================
# Video — the same rule as images: the BYTES decide, never the name or the header
# ============================================================================
# A coach's library is images AND video, and video arrives from a phone, where the
# filename and the Content-Type are whatever the client felt like sending. The image
# path has decided by content since B-002; this is that rule for video.
#
# Two containers, because two is what the app can play everywhere: ISO BMFF (MP4/MOV,
# the only thing an iPhone produces) and Matroska/WebM (what Android often produces).
# Everything else — an AVI, an executable renamed to .mp4, an HTML page with a video
# MIME type — is refused before a single byte is stored.

#: Magic at the head of the file. ISO BMFF carries `ftyp` at offset 4; Matroska starts
#: with the EBML signature.
_ISO_BMFF_MARKER = b'ftyp'
_MATROSKA_MAGIC = b'\x1a\x45\xdf\xa3'

#: Brands we accept inside an ISO BMFF `ftyp` box, and the extension each is stored as.
#: A brand outside this set is some other ISO container (a JPEG 2000 file is also BMFF).
_ISO_BRANDS = {
    b'isom': 'mp4', b'iso2': 'mp4', b'iso4': 'mp4', b'iso5': 'mp4', b'iso6': 'mp4',
    b'mp41': 'mp4', b'mp42': 'mp4', b'avc1': 'mp4', b'dash': 'mp4', b'mmp4': 'mp4',
    b'M4V ': 'mp4', b'M4VH': 'mp4', b'M4VP': 'mp4',
    b'qt  ': 'mov',
}

#: How much of the head is enough to identify a container.
VIDEO_SNIFF_BYTES = 32


class VideoFacts:
    """What the bytes themselves say: container, stored extension, duration if declared.

    `duration_seconds` is None when the container does not declare one where we can read
    it without decoding (WebM keeps it in the Segment Info element). The size cap still
    applies, and the metadata task fills the duration with ffprobe afterwards.
    """

    __slots__ = ('container', 'extension', 'duration_seconds', 'size')

    def __init__(self, container, extension, duration_seconds, size):
        self.container = container
        self.extension = extension
        self.duration_seconds = duration_seconds
        self.size = size

    def __repr__(self):
        return (f"VideoFacts(container={self.container!r}, extension={self.extension!r}, "
                f"duration_seconds={self.duration_seconds!r}, size={self.size!r})")


def sniff_video_container(head: bytes):
    """`('iso', 'mp4')`, `('iso', 'mov')`, `('matroska', 'webm')` — or None.

    `head` is the first `VIDEO_SNIFF_BYTES` bytes. Nothing else about the upload is
    consulted: not `file.name`, not `Content-Type`, not the extension the client chose.
    """
    if not head:
        return None
    if head.startswith(_MATROSKA_MAGIC):
        return ('matroska', 'webm')
    if len(head) >= 12 and head[4:8] == _ISO_BMFF_MARKER:
        brand = head[8:12]
        extension = _ISO_BRANDS.get(brand)
        if extension is None:
            return None
        return ('iso', extension)
    return None


#: How many boxes one level of an ISO BMFF file may hold before the walk gives up. A
#: real MP4 or MOV has a handful at the top (`ftyp`, `moov`, `mdat`, `free`, and a
#: `moof`/`mdat` pair per fragment of a fragmented file); 64 MB of eight-byte boxes is
#: eight million, and walking them one seek and one read at a time burned tens of
#: seconds of CPU inside the upload request (SIDE_BUGS S-144).
MAX_ISO_BOXES_PER_LEVEL = 10_000


class _TooManyBoxes(Exception):
    """The walk hit `MAX_ISO_BOXES_PER_LEVEL`: nothing a real video looks like."""


def _iso_boxes(fp, start, end, depth=0):
    """Yield `(type, payload_start, payload_end)` for the boxes between two offsets.

    Depth-limited and size-checked: a crafted file can declare a box that contains
    itself, or one of length 0, and walking that naively never terminates. Count-limited
    too: a file of millions of tiny boxes terminates, but not soon enough to matter
    (`MAX_ISO_BOXES_PER_LEVEL`, raises `_TooManyBoxes`).
    """
    offset = start
    walked = 0
    while offset + 8 <= end and depth < 4:
        walked += 1
        if walked > MAX_ISO_BOXES_PER_LEVEL:
            raise _TooManyBoxes()
        fp.seek(offset)
        header = fp.read(8)
        if len(header) < 8:
            return
        size = int.from_bytes(header[0:4], 'big')
        box_type = header[4:8]
        payload = offset + 8
        if size == 1:
            extra = fp.read(8)
            if len(extra) < 8:
                return
            size = int.from_bytes(extra, 'big')
            payload = offset + 16
        elif size == 0:
            size = end - offset            # "to the end of the file"
        if size < (payload - offset):      # nonsense length: stop, do not loop
            return
        box_end = min(offset + size, end)
        yield box_type, payload, box_end
        if box_end <= offset:
            return
        offset = box_end


def probe_declared_duration(file, container) -> float | None:
    """The duration the container declares, read without decoding anything.

    ISO BMFF keeps it in `moov/mvhd` as `duration / timescale`. We read that so an
    oversize-in-TIME upload is refused at the door rather than after the bytes are
    stored and a worker gets round to it. A file that declares nothing (WebM, or an MP4
    whose `moov` we cannot find) returns None and is capped on size alone — and so does
    one with more than `MAX_ISO_BOXES_PER_LEVEL` boxes before its `moov`: the walk stops
    there rather than spend the request's CPU on it.
    """
    if container != 'iso':
        return None
    try:
        file.seek(0, os.SEEK_END)
        end = file.tell()
        for box_type, payload, box_end in _iso_boxes(file, 0, end):
            if box_type != b'moov':
                continue
            for inner_type, inner_start, inner_end in _iso_boxes(file, payload, box_end, 1):
                if inner_type != b'mvhd':
                    continue
                file.seek(inner_start)
                body = file.read(min(32, inner_end - inner_start))
                if len(body) < 20:
                    return None
                version = body[0]
                if version == 1:
                    if len(body) < 32:
                        return None
                    timescale = int.from_bytes(body[20:24], 'big')
                    duration = int.from_bytes(body[24:32], 'big')
                else:
                    timescale = int.from_bytes(body[12:16], 'big')
                    duration = int.from_bytes(body[16:20], 'big')
                if not timescale:
                    return None
                # 0xFFFFFFFF is "unknown" in a version-0 header.
                if duration in (0, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
                    return None
                return round(duration / timescale, 3)
        return None
    except _TooManyBoxes:
        logger.info("video declares more than %d boxes on one level; duration not read",
                    MAX_ISO_BOXES_PER_LEVEL)
        return None
    except Exception:
        logger.debug("could not read a declared duration", exc_info=True)
        return None
    finally:
        try:
            file.seek(0)
        except Exception:
            logger.debug("could not rewind the upload", exc_info=True)


def process_uploaded_video(file, max_bytes=None, max_seconds=None) -> VideoFacts:
    """Validate an uploaded video by its CONTENT. The one function video views use.

    Unlike `process_uploaded_image` this does NOT re-encode: re-encoding video means a
    transcoder, and the upload must not depend on one being installed. The original is
    stored as it arrived, which is why the checks here are the whole guard:

      * the container is sniffed from the magic bytes — an `.mp4` that is really a PHP
        script, an HTML page or a JPEG is refused however it is labelled;
      * the size is capped (`MEDIA_VIDEO_MAX_BYTES`), measured on the file, not on the
        Content-Length a client can lie about;
      * the duration is capped (`MEDIA_VIDEO_MAX_SECONDS`) when the container declares
        one where it can be read without decoding.

    The extension the file is STORED under comes from the detected container, never from
    the client's filename — the same rule as images, and the reason `.php` uploads could
    not reach the disk there.

    Raises `ValidationError` with a stable `code`: `no_file`, `empty_file`,
    `media_video_too_large`, `media_not_a_video`, `media_video_too_long`.
    """
    if file is None:
        raise ValidationError(_("No file provided."), code="no_file")

    size = getattr(file, 'size', None)
    if not size:
        raise ValidationError(_("Empty file not allowed."), code="empty_file")

    if max_bytes is None:
        max_bytes = getattr(settings, 'MEDIA_VIDEO_MAX_BYTES', 64 * 1024 * 1024)
    if max_seconds is None:
        max_seconds = getattr(settings, 'MEDIA_VIDEO_MAX_SECONDS', 90)

    if size > max_bytes:
        raise ValidationError(
            _("This video is too large. The most that can be uploaded is %(mb)s MB.")
            % {"mb": round(max_bytes / (1024 * 1024))},
            code="media_video_too_large",
        )

    file.seek(0)
    sniffed = sniff_video_container(file.read(VIDEO_SNIFF_BYTES))
    file.seek(0)
    if sniffed is None:
        raise ValidationError(
            _("This file is not a video. Upload an MP4 or a WebM."),
            code="media_not_a_video",
        )
    container, extension = sniffed

    duration = probe_declared_duration(file, container)
    if duration is not None and max_seconds and duration > max_seconds:
        raise ValidationError(
            _("This video is too long. The most that can be uploaded is %(seconds)s "
              "seconds.") % {"seconds": max_seconds},
            code="media_video_too_long",
        )

    file.seek(0)
    return VideoFacts(container, extension, duration, size)


# ============================================================================
# Audio — a voice note in the chat. The video rule, for sound: the BYTES decide
# ============================================================================
# A voice note is what a phone's recorder writes: AAC in an MPEG-4 container (`.m4a`,
# iOS and most Android builds), Opus in Ogg, or Opus/Vorbis in WebM (Android, Chrome).
# Like video it is stored as it arrived — no transcoder is needed to accept one — so
# the container is sniffed from its magic bytes, the size is measured on the file and
# the duration is read from the container where it declares one, with the same box
# walk video uses (`probe_declared_duration`).

#: ISO BMFF brands a recorder writes for sound: the audio-only brands, the generic
#: MPEG-4 ones an Android recorder stamps on an AAC file, and 3GPP.
_ISO_AUDIO_BRANDS = frozenset({
    b'M4A ', b'M4B ', b'isom', b'iso2', b'iso4', b'iso5', b'iso6', b'mp41', b'mp42',
    b'dash', b'3gp4', b'3gp5', b'3gp6', b'3gp7', b'3gg6',
})
_OGG_MAGIC = b'OggS'


def sniff_audio_container(head: bytes):
    """`('iso', 'm4a')`, `('ogg', 'ogg')`, `('matroska', 'webm')` — or None.

    `head` is the first `VIDEO_SNIFF_BYTES` bytes; nothing else about the upload is
    consulted, exactly as for video.
    """
    if not head:
        return None
    if head.startswith(_OGG_MAGIC):
        return ('ogg', 'ogg')
    if head.startswith(_MATROSKA_MAGIC):
        return ('matroska', 'webm')
    if (len(head) >= 12 and head[4:8] == _ISO_BMFF_MARKER
            and head[8:12] in _ISO_AUDIO_BRANDS):
        return ('iso', 'm4a')
    return None


def process_uploaded_audio(file, max_bytes=None, max_seconds=None) -> VideoFacts:
    """Validate an uploaded voice note by its CONTENT. The audio twin of
    `process_uploaded_video`, answering the same `VideoFacts`.

    The caps default to the video ones (`MEDIA_VIDEO_MAX_BYTES`,
    `MEDIA_VIDEO_MAX_SECONDS`): a voice note is not a longer thing than a clip, and a
    second pair of numbers would be a second thing to keep in step.

    Raises `ValidationError` with a stable `code`: `no_file`, `empty_file`,
    `media_audio_too_large`, `media_not_audio`, `media_audio_too_long`.
    """
    if file is None:
        raise ValidationError(_("No file provided."), code="no_file")

    size = getattr(file, 'size', None)
    if not size:
        raise ValidationError(_("Empty file not allowed."), code="empty_file")

    if max_bytes is None:
        max_bytes = getattr(settings, 'MEDIA_VIDEO_MAX_BYTES', 64 * 1024 * 1024)
    if max_seconds is None:
        max_seconds = getattr(settings, 'MEDIA_VIDEO_MAX_SECONDS', 90)

    if size > max_bytes:
        raise ValidationError(
            _("This recording is too large. The most that can be sent is %(mb)s MB.")
            % {"mb": round(max_bytes / (1024 * 1024))},
            code="media_audio_too_large",
        )

    file.seek(0)
    sniffed = sniff_audio_container(file.read(VIDEO_SNIFF_BYTES))
    file.seek(0)
    if sniffed is None:
        raise ValidationError(
            _("This file is not a voice recording. Send an M4A, Ogg or WebM file."),
            code="media_not_audio",
        )
    container, extension = sniffed

    duration = probe_declared_duration(file, container)
    if duration is not None and max_seconds and duration > max_seconds:
        raise ValidationError(
            _("This recording is too long. The most that can be sent is %(seconds)s "
              "seconds.") % {"seconds": max_seconds},
            code="media_audio_too_long",
        )

    file.seek(0)
    return VideoFacts(container, extension, duration, size)
