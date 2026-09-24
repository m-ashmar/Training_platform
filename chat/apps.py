from django.apps import AppConfig


class ChatConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'chat'

    def ready(self):
        # A conversation's files go with it, read before its messages cascade as a
        # plain DELETE. On `Conversation`, never on `Message`: a delete receiver on
        # Message would switch off Django's fast delete for the whole table
        # (`chat.media.delete_conversation_files`, `training_platform/signals.py`).
        from django.db.models.signals import pre_delete

        from .media import delete_conversation_files
        from .models import Conversation

        pre_delete.connect(delete_conversation_files, sender=Conversation,
                           dispatch_uid='chat-conversation-files')
