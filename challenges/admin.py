from django.contrib import admin

from admin_dashboard.admin import (
    admin_site,
    ChallengeAdmin,
    AchievementAdmin,
)
from social.models import (
    Challenge as SocialChallenge,
    Achievement as SocialAchievement,
    ChallengeParticipation as SocialChallengeParticipation,
    UserAchievement as SocialUserAchievement,
    Leaderboard as SocialLeaderboard,
    Notification as SocialNotification,
)
from .models import (
    ChallengeProxy,
    AchievementProxy,
    ChallengeParticipationProxy,
    UserAchievementProxy,
    LeaderboardProxy,
    NotificationProxy,
)

# Ensure models show under the custom Challenges app group without duplicates
try:
    admin_site.unregister(SocialChallenge)
except Exception:
    pass
try:
    admin_site.unregister(SocialAchievement)
except Exception:
    pass
try:
    admin_site.unregister(SocialChallengeParticipation)
except Exception:
    pass
try:
    admin_site.unregister(SocialUserAchievement)
except Exception:
    pass
try:
    admin_site.unregister(SocialLeaderboard)
except Exception:
    pass
try:
    admin_site.unregister(SocialNotification)
except Exception:
    pass

# Register proxies under custom admin site (dj-admin/)
admin_site.register(ChallengeProxy, ChallengeAdmin)
admin_site.register(AchievementProxy, AchievementAdmin)
admin_site.register(ChallengeParticipationProxy)
admin_site.register(UserAchievementProxy)
admin_site.register(LeaderboardProxy)
admin_site.register(NotificationProxy)

# Optional: also expose under default Django admin (/admin/)
try:
    admin.site.register(ChallengeProxy, ChallengeAdmin)
    admin.site.register(AchievementProxy, AchievementAdmin)
except admin.sites.AlreadyRegistered:
    pass
try:
    admin.site.register(ChallengeParticipationProxy)
    admin.site.register(UserAchievementProxy)
    admin.site.register(LeaderboardProxy)
    admin.site.register(NotificationProxy)
except admin.sites.AlreadyRegistered:
    pass
