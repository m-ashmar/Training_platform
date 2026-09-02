from django.contrib import admin
import logging

logger = logging.getLogger(__name__)

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
)
from .models import (
    ChallengeProxy,
    AchievementProxy,
    ChallengeParticipationProxy,
    UserAchievementProxy,
    LeaderboardProxy,
)

# Ensure models show under the custom Challenges app group without duplicates
try:
    admin_site.unregister(SocialChallenge)
except Exception:
    # Optional side effect: swallowing this silently is what made the
    # surrounding failures invisible in logs. Control flow is unchanged.
    logger.debug('suppressed non-fatal error', exc_info=True)
try:
    admin_site.unregister(SocialAchievement)
except Exception:
    # Optional side effect: swallowing this silently is what made the
    # surrounding failures invisible in logs. Control flow is unchanged.
    logger.debug('suppressed non-fatal error', exc_info=True)
try:
    admin_site.unregister(SocialChallengeParticipation)
except Exception:
    # Optional side effect: swallowing this silently is what made the
    # surrounding failures invisible in logs. Control flow is unchanged.
    logger.debug('suppressed non-fatal error', exc_info=True)
try:
    admin_site.unregister(SocialUserAchievement)
except Exception:
    # Optional side effect: swallowing this silently is what made the
    # surrounding failures invisible in logs. Control flow is unchanged.
    logger.debug('suppressed non-fatal error', exc_info=True)
try:
    admin_site.unregister(SocialLeaderboard)
except Exception:
    # Optional side effect: swallowing this silently is what made the
    # surrounding failures invisible in logs. Control flow is unchanged.
    logger.debug('suppressed non-fatal error', exc_info=True)
# Register proxies under custom admin site (dj-admin/)
admin_site.register(ChallengeProxy, ChallengeAdmin)
admin_site.register(AchievementProxy, AchievementAdmin)
admin_site.register(ChallengeParticipationProxy)
admin_site.register(UserAchievementProxy)
admin_site.register(LeaderboardProxy)

# Optional: also expose under default Django admin (/admin/)
try:
    admin.site.register(ChallengeProxy, ChallengeAdmin)
    admin.site.register(AchievementProxy, AchievementAdmin)
except admin.sites.AlreadyRegistered:
    # Optional side effect: swallowing this silently is what made the
    # surrounding failures invisible in logs. Control flow is unchanged.
    logger.debug('suppressed non-fatal error', exc_info=True)
try:
    admin.site.register(ChallengeParticipationProxy)
    admin.site.register(UserAchievementProxy)
    admin.site.register(LeaderboardProxy)
except admin.sites.AlreadyRegistered:
    # Optional side effect: swallowing this silently is what made the
    # surrounding failures invisible in logs. Control flow is unchanged.
    logger.debug('suppressed non-fatal error', exc_info=True)
