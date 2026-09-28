import mimetypes

from django.apps import AppConfig

#: Media types Python's `mimetypes` gets wrong or does not know, fixed once per process.
#: `.m4a` read from the host's tables as `audio/mp4a-latm` (macOS) or not at all (a slim
#: image with no /etc/mime.types), and `.ogg` was unknown: a voice note was served, and
#: uploaded to Cloud Storage, with a type no player accepts (BACKEND_BUGS B-183).
MEDIA_TYPES = {
    '.m4a': 'audio/mp4',
    '.aac': 'audio/aac',
    '.ogg': 'audio/ogg',
    '.oga': 'audio/ogg',
    '.opus': 'audio/ogg',
    '.mp3': 'audio/mpeg',
}


def register_media_types():
    for extension, content_type in MEDIA_TYPES.items():
        mimetypes.add_type(content_type, extension)


class MediakitConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "mediakit"
    verbose_name = "Media kit"

    def ready(self):
        register_media_types()
